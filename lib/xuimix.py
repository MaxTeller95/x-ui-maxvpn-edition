"""xuimix - one path over two or more tunnels at once, shared by every tool that writes routing.

A path NAME is normally one outbound aimed at one tunnel address. Mixed, it becomes:

    mix~NAME~1, mix~NAME~2b ...   copies of NAME, one per tunnel (weight 2 = a second copy, so
                                  that tunnel gets twice the share of new connections)
    balancer mix-NAME             picks a copy for every new connection
    every rule that went to NAME  goes to the balancer instead (users, DNS, the guard's probe)
    burstObservatory              pings each copy, so a tunnel that stops answering is skipped;
                                  the first tunnel is the fallback when none answers. Its one
                                  selector "mix~" covers every path's copies and stays even
                                  when nothing is mixed: the observatory is the only part Xray
                                  cannot change while running, so this way only the very first
                                  mix ever needs a restart and every later change is live

NAME itself stays as it is, unused: it is the model the copies are made from, the tools that
read it keep working, and turning mixing off only points the rules back at it. NAME-mux
(xui-split's mux copy for light traffic) is mixed the same way, over the same tunnels.

Why not a loopback outbound named NAME in front of the balancer, which would leave the rules
alone: Xray re-dispatches loopback traffic with the original user attached and counts that
user's bytes a second time - quotas would run out twice as fast.

Settings: "mix" under tunnels.NAME in /etc/tunnel-guard.json:
    {"on": true, "addrs": ["10.0.1.2", "10.0.10.1"], "how": "smart", "weights": [1, 1]}
how: smart      every healthy tunnel takes new connections; one that drops pings (over a third)
                or answers slower than 1.5 s leaves the mix until it recovers (Xray leastLoad)
     random     each new connection picks a tunnel at random, by weight
     roundrobin one tunnel after the other
"""
import copy, json, re, subprocess

MIX, SEP = "mix-", "~"             # balancer tag prefix; separator in copies' tags
COPY = "mix~"                      # every per-tunnel copy starts so: one observatory selector for all
HOW = ("smart", "random", "roundrobin")
GUARD_CONF = "/etc/tunnel-guard.json"
PING = {"destination": "https://www.google.com/generate_204", "connectivity": "",
        "interval": "1m", "sampling": 3, "timeout": "6s"}
MAX_WEIGHT = 3


# ------------------------------------------------------------------ reading routing
def target(rule):
    """The path a rule sends traffic to, whether straight or through its mix balancer."""
    if rule.get("outboundTag"):
        return rule["outboundTag"]
    b = rule.get("balancerTag") or ""
    return b[len(MIX):] if b.startswith(MIX) else None


def is_clone(tag):
    return (tag or "").startswith(COPY)


def base_of(tag):
    """mix~NAME~2b -> NAME; anything else is its own base."""
    return tag[len(COPY):].rsplit(SEP, 1)[0] if is_clone(tag) else (tag or "")


def copy_index(tag):
    """mix~NAME~2b -> 2 (the tunnel's place in the mix, from 1)."""
    return int(re.sub(r"\D", "", tag.rsplit(SEP, 1)[-1]) or 1)


def copy_tag(f, i, k=0):
    return "%s%s%s%d%s" % (COPY, f, SEP, i, "" if k == 0 else "bcd"[k - 1])


def mixed(t):
    """Tags (NAME and NAME-mux) whose traffic goes through a mix balancer in this config."""
    return {b["tag"][len(MIX):] for b in (t.get("routing") or {}).get("balancers") or []
            if (b.get("tag") or "").startswith(MIX)}


def route(t, tag):
    """What a new rule should say to reach `tag`: its balancer when it is mixed."""
    return {"balancerTag": MIX + tag} if tag in mixed(t) else {"outboundTag": tag}


def point(rules, t):
    """Aim rules that name a mixed outbound at its balancer, and rules that still name a
    balancer whose mix is gone back at the outbound."""
    m = mixed(t)
    for r in rules:
        ob, b = r.get("outboundTag"), r.get("balancerTag") or ""
        if ob in m:
            del r["outboundTag"]
            r["balancerTag"] = MIX + ob
        elif b.startswith(MIX) and b[len(MIX):] not in m:
            del r["balancerTag"]
            r["outboundTag"] = b[len(MIX):]
    return rules


# ------------------------------------------------------------------ where an outbound points
def _server(o):
    s = o.get("settings") or {}
    if "address" in s:
        return s
    for k in ("vnext", "servers"):
        if s.get(k):
            return s[k][0]
    return {}


def iface_addrs():
    try:
        raw = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        return {l["ifname"]: a["local"] for l in json.loads(raw or "[]") for a in l.get("addr_info", [])[:1]}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def set_addr(o, entry, ifs=None):
    """Aim an outbound at ADDR, or ADDR@IFACE (sent through that tun: sendThrough = its address)."""
    addr, _, via = (entry or "").partition("@")
    _server(o)["address"] = addr
    if via:
        local = (ifs if ifs is not None else iface_addrs()).get(via)
        if not local:
            raise ValueError("تانل %s روی این هاب نیست" % via)
        o["sendThrough"] = local
    else:
        o.pop("sendThrough", None)


# ------------------------------------------------------------------ settings
def load_conf():
    try:
        with open(GUARD_CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def spec_of(conf, name):
    m = ((conf.get("tunnels") or {}).get(name) or {}).get("mix") or {}
    return m if m.get("on") and len(m.get("addrs") or []) >= 2 else None


def clean_spec(addrs, how="smart", weights=None):
    addrs = list(dict.fromkeys(a for a in addrs if a))
    if len(addrs) < 2:
        raise ValueError("برای استفاده‌ی همزمان دست‌کم دو تانل لازم است")
    if how not in HOW:
        raise ValueError("روش نامعتبر: %s (smart، random یا roundrobin)" % how)
    w = [int(x) for x in (weights or [])] + [1] * len(addrs)
    w = [max(1, min(MAX_WEIGHT, x)) for x in w[:len(addrs)]]
    return {"on": True, "addrs": addrs, "how": how, "weights": w}


# ------------------------------------------------------------------ building
def strategy(how, copies):
    if how == "random":
        return {"type": "random"}
    if how == "roundrobin":
        return {"type": "roundRobin"}
    # every qualified copy stays selectable; the observatory decides who qualifies
    return {"type": "leastLoad", "settings": {"expected": copies, "maxRTT": "1500ms", "tolerance": 0.34}}


def family(t, name):
    tags = {o.get("tag") for o in t.get("outbounds", [])}
    return [x for x in (name, name + "-mux") if x in tags]


def remove(t, name):
    """Take NAME's mix out of a template: copies, balancers, observatory subjects; rules back
    to the outbounds."""
    fam = {name, name + "-mux"}
    old_style = lambda tag: not is_clone(tag) and SEP in (tag or "") and tag.split(SEP)[0] in fam
    t["outbounds"] = [o for o in t.get("outbounds", []) if not ((is_clone(o.get("tag")) and base_of(o.get("tag")) in fam)
                                                               or old_style(o.get("tag")))]
    routing = t.setdefault("routing", {})
    bal = [b for b in routing.get("balancers") or [] if b.get("tag") not in {MIX + f for f in fam}]
    if bal:
        routing["balancers"] = bal
    else:
        routing.pop("balancers", None)
    # the observatory stays (selector "mix~"): taking it away would need a restart, and with
    # no copies left it watches nothing
    point(routing.get("rules") or [], t)
    return t


def build(t, name, spec, ifs=None):
    """Put NAME's mix into a template (idempotent: an earlier mix of NAME is replaced)."""
    if t.get("observatory"):
        raise ValueError("این کانفیگ observatory معمولی دارد؛ Xray همزمان دو observatory قبول نمی‌کند")
    remove(t, name)
    fam = family(t, name)
    if name not in fam:
        raise ValueError("اوت‌باند %s پیدا نشد" % name)
    ifs = ifs if ifs is not None else iface_addrs()
    outs = t["outbounds"]
    routing = t.setdefault("routing", {})
    for f in fam:
        base = next(o for o in outs if o.get("tag") == f)
        copies = []
        for i, (addr, w) in enumerate(zip(spec["addrs"], spec["weights"]), 1):
            for k in range(w):
                c = copy.deepcopy(base)
                c["tag"] = copy_tag(f, i, k)
                set_addr(c, addr, ifs)
                copies.append(c)
        at = outs.index(base) + 1
        outs[at:at] = copies
        routing.setdefault("balancers", []).append({
            "tag": MIX + f, "selector": [COPY + f + SEP], "fallbackTag": copies[0]["tag"],
            "strategy": strategy(spec["how"], len(copies))})
        observatory(t)
    point(routing.setdefault("rules", []), t)
    return t


def observatory(t):
    """One selector for every mix, present for good (see the top)."""
    obs = t.setdefault("burstObservatory", {"subjectSelector": [], "pingConfig": dict(PING)})
    obs.setdefault("pingConfig", dict(PING))
    # earlier versions named copies NAME~1 and listed "NAME~" per path: those go
    obs["subjectSelector"] = [s for s in obs.get("subjectSelector") or [] if not s.endswith(SEP)] + [COPY]
    obs["subjectSelector"] = list(dict.fromkeys(obs["subjectSelector"]))


def reapply(t, conf=None):
    """Bring every configured mix into a template another tool has just rebuilt (xui-split
    replaces rules and mux copies wholesale). Paths that are not mixed are pointed back."""
    conf = conf if conf is not None else load_conf()
    done, ifs = [], None
    for name in list((conf.get("tunnels") or {})):
        spec = spec_of(conf, name)
        if spec and any(o.get("tag") == name for o in t.get("outbounds", [])):
            ifs = ifs if ifs is not None else iface_addrs()
            try:
                build(t, name, spec, ifs)
                done.append(name)
            except ValueError:
                remove(t, name)
    for tag in list(mixed(t)):                                # a mix nobody asked for any more
        if base_of(tag[:-4] if tag.endswith("-mux") else tag) not in done:
            remove(t, tag[:-4] if tag.endswith("-mux") else tag)
    point((t.get("routing") or {}).get("rules") or [], t)
    return done


def describe(t, name):
    """What the template says about NAME's mix: the copies and which tunnel each uses."""
    copies = [o for o in t.get("outbounds", []) if is_clone(o.get("tag")) and base_of(o["tag"]) == name]
    bal = next((b for b in (t.get("routing") or {}).get("balancers") or [] if b.get("tag") == MIX + name), None)
    out = []
    for o in copies:
        s = _server(o)
        out.append({"tag": o["tag"], "address": s.get("address"), "via": o.get("sendThrough", "")})
    return {"on": bool(bal), "copies": out, "strategy": (bal or {}).get("strategy", {}).get("type"),
            "fallback": (bal or {}).get("fallbackTag")}


def drift(t, name):
    """Copies that no longer match NAME (credentials, transport or mux changed since): the mix
    should be rebuilt from NAME."""
    outs = {o.get("tag"): o for o in t.get("outbounds", [])}
    strip = lambda o: {k: v for k, v in o.items() if k not in ("tag", "sendThrough")}
    bad = []
    for f in (name, name + "-mux"):
        if f not in outs:
            continue
        model = copy.deepcopy(outs[f])
        _server(model).pop("address", None)
        model = strip(model)
        for tag, o in outs.items():
            if is_clone(tag) and base_of(tag) == f:
                c = copy.deepcopy(o)
                _server(c).pop("address", None)
                if strip(c) != model:
                    bad.append(tag)
    return bad
