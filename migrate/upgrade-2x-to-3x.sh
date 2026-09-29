#!/usr/bin/env bash
# Upgrade a 3x-ui 2.x panel to 3.x, where clients become records of their own, so one
# client can sit on several inbounds with a single quota, expiry and usage counter.
#
# Download and verify the release first (through a working path if GitHub is blocked):
#   mkdir -p /tmp/xui-v3 && cd /tmp/xui-v3
#   curl -fsSL -o x-ui.tar.gz    https://github.com/MHSanaei/3x-ui/releases/download/v3.8.5/x-ui-linux-amd64.tar.gz
#   curl -fsSL -o sum.txt        https://github.com/MHSanaei/3x-ui/releases/download/v3.8.5/x-ui-linux-amd64.tar.gz.sha256
#   sha256sum -c sum.txt --ignore-missing && tar xzf x-ui.tar.gz
#
# Rehearse it first: copy the database to a scratch folder and start the new binary with
# XUI_DB_FOLDER / XUI_BIN_FOLDER pointing there, then inspect the result. That is how the
# tag and client changes below were checked before touching a live panel.
# Keeps: database, panel settings, custom geo .dat files, nginx/Optimizer setup.
set -euo pipefail
SRC=/tmp/xui-v3/x-ui
TS=$(date +%Y%m%d-%H%M%S)
BK=/root/xui-pre-upgrade-$TS

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -x "$SRC/x-ui" ] || { echo "$SRC/x-ui missing - download the release first" >&2; exit 1; }
"$SRC/x-ui" -v | tail -1 | grep -q '^3\.' || { echo "release in $SRC is not 3.x" >&2; exit 1; }

echo "== stopping the old twin sync (it does not understand the new client model)"
systemctl disable --now xui-twins.timer 2>/dev/null || true
mv /etc/xui-twins.json /etc/xui-twins.json.pre-v3 2>/dev/null || true

echo "== backup to $BK"
mkdir -p "$BK"
cp -a /usr/local/x-ui "$BK/x-ui-dir"
cp -a /etc/x-ui/x-ui.db "$BK/x-ui.db"
cp -a /usr/local/x-ui/bin/config.json "$BK/config.json"

echo "== stopping x-ui (users are disconnected from here until it is back)"
systemctl stop x-ui

echo "== installing the new release"
install -m 755 "$SRC/x-ui" /usr/local/x-ui/x-ui
install -m 755 "$SRC/x-ui.sh" /usr/local/x-ui/x-ui.sh
for f in "$SRC"/x-ui.service.*; do install -m 644 "$f" "/usr/local/x-ui/$(basename "$f")"; done
for f in "$SRC"/bin/*; do                                            # new cores (xray, mtg, tuic)
  b=$(basename "$f")
  case "$b" in *.dat|LICENSE|README.md) continue;; esac
  install -m 755 "$f" "/usr/local/x-ui/bin/$b"
done
for f in "$SRC"/bin/*.dat; do                                        # keep our own geo files
  [ -f "/usr/local/x-ui/bin/$(basename "$f")" ] || install -m 644 "$f" /usr/local/x-ui/bin/
done
install -m 644 "$SRC/x-ui.service.debian" /etc/systemd/system/x-ui.service 2>/dev/null || true
systemctl daemon-reload

echo "== starting x-ui"
systemctl start x-ui
sleep 12
systemctl is-active x-ui
/usr/local/x-ui/x-ui -v | tail -1

python3 - <<'EOF'
import json, sqlite3
db = sqlite3.connect("/etc/x-ui/x-ui.db")
t = [r[0] for r in db.execute("select name from sqlite_master where type='table'")]
print("clients table:", "clients" in t,
      "| clients:", db.execute("select count(*) from clients").fetchone()[0] if "clients" in t else "-",
      "| links:", db.execute("select count(*) from client_inbounds").fetchone()[0] if "client_inbounds" in t else "-")
cfg = json.load(open("/usr/local/x-ui/bin/config.json"))
tags = {i.get("tag") for i in cfg["inbounds"]}
print("inbounds live:", len(cfg["inbounds"]), "| routing rules:", len(cfg["routing"]["rules"]))
stale = {t2 for r in cfg["routing"]["rules"] for t2 in r.get("inboundTag", [])
         if t2 not in tags and t2 not in ("api", "dns-internal") and not t2.startswith("guard-")}
print("rule inboundTags with no inbound:", sorted(stale) or "none")
EOF

echo
echo "if anything is wrong, roll back with:"
echo "  systemctl stop x-ui && rm -rf /usr/local/x-ui && cp -a $BK/x-ui-dir /usr/local/x-ui \\"
echo "    && cp -a $BK/x-ui.db /etc/x-ui/x-ui.db && systemctl daemon-reload && systemctl start x-ui"
