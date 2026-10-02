"""xuiagent - what xui-mtu needs to do on one server: this hub, or a tunnel's far end over SSH.

Self-contained (standard library only): the hub imports it, and sends this very file to a far
server as `ssh root@HOST python3 - OP ARGS < xuiagent.py`, so nothing is installed there except
what an operation writes on purpose (an MTU pin, the kernel settings file).

    links                       interfaces: kind, MTU, IPv4 address, far end, and which addresses
                                route through which interface
    probe IFACE PEER HI         the biggest packet (bytes, IP header included) that crosses IFACE
                                to PEER unfragmented, at most HI; 0 when even small ones do not
    setmtu IFACE N              set it now and keep it: the tunnel's own config where one names it,
                                and a pin re-applied every 5 minutes (a tunnel that is recreated
                                comes back with the old value otherwise)
    kernel [--apply]            the TCP settings for proxied traffic: which are off; --apply sets
                                them and keeps them in /etc/sysctl.d
    hello                       hostname and machine id (to tell servers apart)

Another MTU guard already looking after an interface (mtunnel's mtu-guard for AmneziaWG,
gre-mtu-guard for the GRE tunnels of gre-up.sh, mh-mtu-guard for multi-hop GRE) owns it: links
says so, and xui-mtu leaves it alone - two guards on one tunnel would push its MTU back and forth.
"""
import glob, json, os, re, subprocess, sys, time

PINS = "/etc/xui-mtu.pins"
CRON = "/etc/cron.d/xui-mtu"
CRON_LINE = ("*/5 * * * * root [ -f %s ] && while read i m; do [ -e /sys/class/net/$i ] && "
             "[ \"$(cat /sys/class/net/$i/mtu)\" != \"$m\" ] && ip link set dev $i mtu $m; done < %s\n"
             "@reboot root sleep 60; [ -f %s ] && while read i m; do [ -e /sys/class/net/$i ] && "
             "ip link set dev $i mtu $m; done < %s\n") % (PINS, PINS, PINS, PINS)
# TCP for proxied connections. tcp_notsent_lowat counts as set at any small value: the point is
# not the Linux default (unlimited); a tunnel like Backhaul sets 32768 itself when it starts.
KERNEL = {"net.ipv4.tcp_slow_start_after_idle": ("0", None), "net.ipv4.tcp_notsent_lowat": ("16384", (1, 65536)),
          "net.ipv4.tcp_mtu_probing": ("1", None)}
KERNEL_FILE = "/etc/sysctl.d/99-proxy-tcp.conf"
TUNNEL_KINDS = ("gre", "gretap", "ipip", "sit", "geneve", "vxlan", "wireguard", "amneziawg", "tun", "ip6gre", "ip6tnl")


def run(*cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _active(timer, cache={}):
    if timer not in cache:
        cache[timer] = run("systemctl", "is-active", "--quiet", timer, timeout=5)[0] == 0
    return cache[timer]


def _read(path):
    try:
        return open(path).read()
    except (OSError, UnicodeDecodeError):
        return ""


def owner(iface):
    """The other MTU guard that looks after iface, or ''."""
    if _active("mtu-guard.timer") and iface.startswith("awg") and \
            os.path.exists("/etc/amnezia/amneziawg/%s.conf" % iface):
        return "mtu-guard"
    if _active("gre-mtu-guard.timer") and re.search(r"ip link set %s mtu \d+" % re.escape(iface),
                                                    _read("/usr/local/sbin/gre-up.sh")):
        return "gre-mtu-guard"
    if _active("mh-mtu-guard.timer") and iface in [l.split()[0] for l in _read("/etc/mh-mtu-guard.conf").splitlines()
                                                   if l.split() and not l.startswith("#")]:
        return "mh-mtu-guard"
    return ""


def links():
    rc, raw = run("ip", "-j", "-d", "addr", "show", timeout=10)
    out = []
    for l in (json.loads(raw) if rc == 0 else []):
        info = l.get("linkinfo") or {}
        kind = info.get("info_kind") or ""
        v4 = [a for a in l.get("addr_info", []) if a.get("family") == "inet"]
        out.append({"iface": l["ifname"], "kind": kind, "mtu": l.get("mtu"), "tunnel": kind in TUNNEL_KINDS,
                    "local": v4[0]["local"] if v4 else "", "prefix": v4[0]["prefixlen"] if v4 else 0,
                    "remote": (info.get("info_data") or {}).get("remote") or "",
                    "up": "UP" in l.get("flags", []), "owner": owner(l["ifname"]) if kind in TUNNEL_KINDS else ""})
    return out


def ping(iface, peer, size, n=2):
    """Replies to n pings of `size` bytes of payload, Don't Fragment, through iface."""
    rc, out = run("ping", "-n", "-q", "-M", "do", "-c", str(n), "-i", "0.2", "-W", "1", "-s", str(size),
                  "-I", iface, peer, timeout=n + 4)
    m = re.search(r"(\d+) (?:packets )?received", out)
    return int(m.group(1)) if m else 0


def probe(iface, peer, hi):
    """Largest packet (IP header included) that gets through, at most hi. Each size is given
    four pings, so a lossy tunnel is not mistaken for a small one; 0 when small pings fail too."""
    if not ping(iface, peer, 200, 4):
        return 0
    ok = lambda size: ping(iface, peer, size - 28, 4) > 0
    if ok(hi):
        return hi
    lo, top = 576, hi - 1
    if not ok(lo):
        return 0
    while lo < top:
        mid = (lo + top + 1) // 2
        lo, top = (mid, top) if ok(mid) else (lo, mid - 1)
    return lo if ok(lo) else 0                       # once more: the answer must hold twice


def persist(iface, mtu):
    """Write the new MTU where the tunnel is made: systemd units and scripts that name the
    interface on the same line as an mtu, and WireGuard/AmneziaWG configs."""
    done = []
    pat = re.compile(r"(\bmtu[\s=]+)(\d{3,5})\b", re.I)
    files = glob.glob("/etc/systemd/system/*.service") + glob.glob("/etc/network/interfaces") + \
        glob.glob("/etc/network/interfaces.d/*") + glob.glob("/usr/local/bin/*.sh") + glob.glob("/etc/rc.local")
    for f in files:
        try:
            text = open(f).read()
        except (OSError, UnicodeDecodeError):
            continue
        name = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(iface))
        lines, changed = text.split("\n"), False
        for i, line in enumerate(lines):
            if name.search(line) and pat.search(line):
                new = pat.sub(lambda m: m.group(1) + str(mtu), line)
                if new != line:
                    lines[i], changed = new, True
        if changed:
            if not os.path.exists(f + ".bak-mtu"):
                with open(f + ".bak-mtu", "w") as b:
                    b.write(text)
            with open(f, "w") as h:
                h.write("\n".join(lines))
            done.append(f)
    for f in ("/etc/amnezia/amneziawg/%s.conf" % iface, "/etc/wireguard/%s.conf" % iface):
        try:
            text = open(f).read()
        except OSError:
            continue
        if re.search(r"(?im)^\s*MTU\s*=", text):
            new = re.sub(r"(?im)^(\s*MTU\s*=\s*)\d+", lambda m: m.group(1) + str(mtu), text)
        else:
            new = re.sub(r"(?im)^(\[Interface\][^\n]*\n)", lambda m: m.group(1) + "MTU = %d\n" % mtu, text, count=1)
        if new != text:
            if not os.path.exists(f + ".bak-mtu"):
                with open(f + ".bak-mtu", "w") as b:
                    b.write(text)
            with open(f, "w") as h:
                h.write(new)
            done.append(f)
    if any(f.endswith(".service") for f in done):
        run("systemctl", "daemon-reload", timeout=30)
    return done


def pin(iface, mtu):
    pins = {}
    try:
        for line in open(PINS):
            p = line.split()
            if len(p) == 2:
                pins[p[0]] = p[1]
    except OSError:
        pass
    if mtu:
        pins[iface] = str(mtu)
    else:
        pins.pop(iface, None)
    with open(PINS, "w") as f:
        f.write("".join("%s %s\n" % kv for kv in sorted(pins.items())))
    if not os.path.exists(CRON) or open(CRON).read() != CRON_LINE:
        with open(CRON, "w") as f:
            f.write(CRON_LINE)
    return pins


def setmtu(iface, mtu):
    if not re.match(r"^[A-Za-z0-9_.-]{1,15}$", iface) or not 576 <= int(mtu) <= 9000:
        return {"ok": False, "msg": "bad interface or mtu"}
    if not os.path.exists("/sys/class/net/" + iface):
        return {"ok": False, "msg": "no interface " + iface}
    rc, out = run("ip", "link", "set", "dev", iface, "mtu", str(mtu), timeout=10)
    if rc:
        return {"ok": False, "msg": out}
    files = persist(iface, int(mtu))
    pin(iface, int(mtu))
    return {"ok": True, "files": files}


def kernel_off():
    off = []
    for k, (want, rng) in KERNEL.items():
        got = run("sysctl", "-n", k, timeout=5)[1].strip()
        ok = got == want if not rng else got.isdigit() and rng[0] <= int(got) <= rng[1]
        if not ok:
            off.append(k)
    return off


def kernel_fix():
    """Apply them now and keep them for the next boot."""
    lines = ["# xui-optimizer: TCP for proxied connections (users <-> x-ui <-> tunnel <-> abroad)"]
    lines += ["%s = %s" % (k, v[0]) for k, v in KERNEL.items()]
    with open(KERNEL_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    rc, out = run("sysctl", "-p", KERNEL_FILE, timeout=15)
    off = kernel_off()
    return {"ok": rc == 0 and not off, "off": off, "msg": "" if not off else out[-200:]}


def hello():
    try:
        mid = open("/etc/machine-id").read().strip()
    except OSError:
        mid = ""
    return {"host": run("hostname", timeout=5)[1], "id": mid, "at": int(time.time())}


def main(args):
    op = args[0] if args else ""
    if op == "links":
        return links()
    if op == "probe" and len(args) == 4:
        return {"size": probe(args[1], args[2], int(args[3]))}
    if op == "setmtu" and len(args) == 3:
        return setmtu(args[1], int(args[2]))
    if op == "unpin" and len(args) == 2:
        return {"ok": True, "pins": pin(args[1], 0)}
    if op == "kernel":
        return kernel_fix() if "--apply" in args else {"off": kernel_off()}
    if op == "hello":
        return hello()
    return {"ok": False, "msg": "unknown op"}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:])))
