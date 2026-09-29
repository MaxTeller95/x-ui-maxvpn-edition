#!/usr/bin/env bash
# awg-hop-setup.sh - let the hub move a tunnel's UDP port on the server abroad.
#
#   bash awg-hop-setup.sh MAIN root@203.0.113.10 [awg0]
#
# Run it on the hub. It makes a key that exists only for this, installs awg-port
# on the server abroad, pins the key to that one command, and writes the `hop`
# block into /etc/tunnel-guard.json.
#
# The key is deliberately narrow. Its authorized_keys line is
#
#   command="/usr/local/sbin/awg-port",restrict ssh-ed25519 ...
#
# so whoever holds it can set the tunnel's port and read it back - no shell, no
# port forwarding, no file access, nothing else. That matters here: the hub sits
# in Iran and the servers abroad should not become reachable just because it is.
set -euo pipefail

NAME=${1:?usage: awg-hop-setup.sh <path-name> <user@server-abroad> [iface]}
DEST=${2:?usage: awg-hop-setup.sh <path-name> <user@server-abroad> [iface]}
IFACE=${3:-awg0}
KEY=/root/.ssh/awg-hop
HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

if [ ! -f "$KEY" ]; then
  echo "==> making the key (it exists only to change the tunnel port)"
  ssh-keygen -t ed25519 -N "" -C "hub-awg-hop" -f "$KEY" >/dev/null
fi
PUB=$(cat "$KEY.pub")

echo "==> installing awg-port on $DEST"
scp -q "$HERE/awg-port" "$DEST:/usr/local/sbin/awg-port"
ssh "$DEST" "chmod 755 /usr/local/sbin/awg-port; printf '%s\n' '$IFACE' > /etc/awg-port.conf"

echo "==> pinning the key to that one command"
LINE="command=\"/usr/local/sbin/awg-port\",restrict $PUB"
ssh "$DEST" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
  grep -q 'hub-awg-hop' ~/.ssh/authorized_keys && sed -i '/hub-awg-hop/d' ~/.ssh/authorized_keys
  printf '%s\n' '$LINE' >> ~/.ssh/authorized_keys"

echo "==> checking the key can do the one thing and nothing more"
GOT=$(ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$DEST" "get $IFACE" 2>&1 || true)
echo "    port now: $GOT"
SHELL_TRY=$(ssh -i "$KEY" -o BatchMode=yes "$DEST" "id" 2>&1 || true)
case "$SHELL_TRY" in
  *uid=*) echo "    WARNING: the key got a shell - the forced command is not in place"; exit 1;;
  *)      echo "    a shell is refused, as it should be";;
esac

echo "==> writing the hop block into /etc/tunnel-guard.json"
python3 - "$NAME" "$DEST" "$KEY" <<'PY'
import json, sys
name, dest, key = sys.argv[1:4]
p = "/etc/tunnel-guard.json"
c = json.load(open(p))
if name not in c.get("paths", {}):
    sys.exit("no path %r in %s" % (name, p))
c["paths"][name]["hop"] = {"ssh": dest, "key": key}
json.dump(c, open(p, "w"), indent=2, ensure_ascii=False)
print("    %s -> hop via %s" % (name, dest))
PY

install -m 755 "$HERE/awg-hop" /usr/local/sbin/awg-hop
echo
echo "done. try it:"
echo "  awg-hop --status"
echo "  awg-hop $NAME --dry-run"
echo
echo "tunnel-guard will now hop this path by itself when a restart does not bring"
echo "the handshake back. Nothing else changed."
