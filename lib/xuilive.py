"""xuilive - put a changed panel template into the running Xray without restarting it.

Why this matters: 3x-ui bills every user from Xray's cumulative traffic counters, polled every
5 seconds. A restart of Xray loses what the old process counted since the last poll, and the
panel then starts the new process from an empty baseline, so what users move before its first
poll is never billed either: up to about 10 seconds of every user's traffic, per restart. (It
also drops every user's connections.) So the tools never restart for something Xray can take
live:

    outbounds            rmo / ado, only the ones that differ (the first one is Xray's default
                         route and is not swapped live)
    guard-* inbounds     rmi / adi (local probe ports, no users)
    rules and balancers  one adrules call without -append: the whole routing at once, atomically

What Xray cannot take live (the dns block, observatory, policy, stats, api, log, routing
domainStrategy, a new default outbound, users' inbounds) needs a restart; then it is Xray
alone (systemctl reload x-ui: SIGUSR1 to the panel, which rebuilds the config from its database
and restarts only the core), not the whole panel.

    import xuilive
    how = xuilive.apply(new_template, old_template)   # "live", "restart", or raises xuilive.Failed

config.json is rewritten to match, as the tools read it as "what runs". tunnel-guard re-asserts a
failover or a mux bypass on its next run when the rules were replaced (as after a restart);
it is started at once.
"""
import glob, json, os, subprocess, tempfile, time

BIN = "/usr/local/x-ui/bin"
CFG = BIN + "/config.json"
XRAY = (glob.glob(BIN + "/xray-linux-*") or [None])[0]
NOT_LIVE = ("dns", "burstObservatory", "observatory", "policy", "stats", "api", "log", "fakedns", "reverse", "transport")


class Failed(Exception):
    pass


def _run(*cmd, timeout=20, data=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=data)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def load_cfg():
    with open(CFG) as f:
        return json.load(f)


def api_addr(cfg):
    tag = (cfg.get("api") or {}).get("tag", "api")
    for i in cfg.get("inbounds", []):
        if i.get("tag") == tag:
            return "%s:%s" % (i.get("listen") or "127.0.0.1", i.get("port"))
    return "127.0.0.1:62789"


def panel_xray_pid():
    """The panel's own Xray (a child of x-ui), not any other xray on the box."""
    rc, main = _run("systemctl", "show", "-p", "MainPID", "--value", "x-ui", timeout=5)
    main = main.strip()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open("/proc/%s/stat" % pid) as f:
                ppid = f.read().rsplit(")", 1)[1].split()[1]
            if ppid == main and os.path.basename(os.readlink("/proc/%s/exe" % pid)).startswith("xray"):
                return pid
        except (OSError, IndexError):
            continue
    return ""


def _api(cfg, verb, *args, payload=None):
    path = None
    if payload is not None:
        fd, path = tempfile.mkstemp(suffix=".json", prefix="xuilive-")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.chmod(path, 0o600)
        args = args + (path,)
    try:
        return _run(XRAY, "api", verb, "--server=" + api_addr(cfg), *args, timeout=20)
    finally:
        if path:
            os.remove(path)


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def as_panel_writes(rules):
    """The template's rules the way 3x-ui writes them into config.json (stripDisabledRules):
    a rule switched off is left out (never the api rule), "enabled" and "comment" are dropped."""
    out = []
    for r in rules:
        if r.get("enabled") is False and r.get("outboundTag") != "api":
            continue
        out.append({k: v for k, v in r.items() if k not in ("enabled", "comment")})
    return out


def injected(cfg, old):
    """Rules the panel adds of its own when it writes config.json (its egress rule, for one):
    live rules the old template does not account for."""
    mine = {json.dumps(r, sort_keys=True) for r in as_panel_writes((old.get("routing") or {}).get("rules", []))}
    return [r for r in (cfg.get("routing") or {}).get("rules", []) if json.dumps(r, sort_keys=True) not in mine]


def plan(tpl, old, cfg=None):
    """What applying the change old -> tpl (both the panel's template) to the running Xray takes:
    {"restart": [reasons], "outbounds": (add, drop), "inbounds": (add, drop), "rules": [...] or
    None}. The panel rewrites some parts on their way to config.json (log paths, online stats,
    its own egress rule), so what is live-able is judged on the template's own change."""
    cfg = cfg or load_cfg()
    why = [k for k in NOT_LIVE if not _same(tpl.get(k), old.get(k))]
    old_r, new_r = old.get("routing") or {}, tpl.get("routing") or {}
    why += ["routing." + k for k in set(old_r) | set(new_r)
            if k not in ("rules", "balancers") and not _same(old_r.get(k), new_r.get(k))]
    if not _same((old.get("outbounds") or [{}])[0], (tpl.get("outbounds") or [{}])[0]):
        why.append("default outbound")
    live_o = {o.get("tag"): o for o in cfg.get("outbounds", [])}          # x-ui copies these verbatim
    new_o = {o.get("tag"): o for o in tpl.get("outbounds", [])}
    add_o = [t for t, o in new_o.items() if t not in live_o or not _same(o, live_o[t])]
    drop_o = [t for t in live_o if t not in new_o]
    guard = lambda c: {i.get("tag"): i for i in c.get("inbounds", []) if (i.get("tag") or "").startswith("guard-")}
    live_i, new_i = guard(cfg), guard(tpl)
    add_i = [t for t, i in new_i.items() if t not in live_i or not _same(i, live_i[t])]
    drop_i = [t for t in live_i if t not in new_i]
    rules = injected(cfg, old) + as_panel_writes(new_r.get("rules", []))
    same_rules = _same(rules, (cfg.get("routing") or {}).get("rules", [])) and         _same(new_r.get("balancers") or [], (cfg.get("routing") or {}).get("balancers") or [])
    return {"restart": why, "outbounds": (add_o, drop_o), "inbounds": (add_i, drop_i),
            "rules": None if same_rules else rules,
            "nothing": not (why or add_o or drop_o or add_i or drop_i or not same_rules)}


def restart_xray(wait=20):
    """Xray alone, rebuilt by the panel from its database (SIGUSR1), not the panel."""
    before = panel_xray_pid()
    rc, _ = _run("systemctl", "reload", "x-ui", timeout=30)
    if rc:
        _run("systemctl", "restart", "x-ui", timeout=60)
    for _ in range(wait * 2):
        time.sleep(0.5)
        now = panel_xray_pid()
        if now and now != before:
            time.sleep(2)
            return True
    return bool(panel_xray_pid())


def apply(tpl, old, allow_restart=True):
    """Bring the running Xray from template `old` to template `tpl` (already saved in the
    panel's database)."""
    if not XRAY:
        raise Failed("no xray binary in " + BIN)
    cfg = load_cfg()
    p = plan(tpl, old, cfg)
    if p["nothing"]:
        return "live"
    if p["restart"]:
        if not allow_restart:
            raise Failed("needs an Xray restart: " + ", ".join(p["restart"]))
        restart_xray()
        return "restart"
    new = json.loads(json.dumps(cfg))
    new["outbounds"] = tpl.get("outbounds", [])
    new["routing"] = dict(cfg.get("routing") or {}, rules=p["rules"] if p["rules"] is not None
                          else (cfg.get("routing") or {}).get("rules", []))
    bal = (tpl.get("routing") or {}).get("balancers")
    if bal:
        new["routing"]["balancers"] = bal
    else:
        new["routing"].pop("balancers", None)
    by_tag = {o.get("tag"): o for o in tpl.get("outbounds", [])}
    ok = True
    # outbounds first (rules may name new ones), old ones after the rules stop naming them
    for t in p["outbounds"][0]:
        _api(cfg, "rmo", t)
        rc, out = _api(cfg, "ado", payload={"outbounds": [by_tag[t]]})
        ok = ok and rc == 0
    guard_new = {i.get("tag"): i for i in tpl.get("inbounds", []) if (i.get("tag") or "").startswith("guard-")}
    for t in p["inbounds"][0]:
        _api(cfg, "rmi", t)
        rc, out = _api(cfg, "adi", payload={"inbounds": [guard_new[t]]})
        ok = ok and rc == 0
    if ok and p["rules"] is not None:
        rc, out = _api(cfg, "adrules", payload={"routing": {"rules": new["routing"]["rules"],
                                                            "balancers": new["routing"].get("balancers", [])}})
        ok = rc == 0
    if ok:
        for t in p["outbounds"][1]:
            _api(cfg, "rmo", t)
        for t in p["inbounds"][1]:
            _api(cfg, "rmi", t)
    if not ok:                                   # half applied is worse than one restart
        if not allow_restart:
            raise Failed("Xray's API refused part of the change")
        restart_xray()
        return "restart"
    keep = [i for i in cfg.get("inbounds", []) if not (i.get("tag") or "").startswith("guard-")]
    new["inbounds"] = keep + list(guard_new.values())
    with open(CFG + ".tmp", "w") as f:
        json.dump(new, f, indent=2)
    os.chmod(CFG + ".tmp", 0o600)
    os.replace(CFG + ".tmp", CFG)
    if p["rules"] is not None:                   # a failover or mux bypass lives in the rules: re-assert now
        _run("systemctl", "start", "--no-block", "tunnel-guard.service", timeout=10)
    return "live"
