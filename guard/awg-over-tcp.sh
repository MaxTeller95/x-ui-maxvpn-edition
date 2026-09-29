#!/usr/bin/env bash
# awg-over-tcp.sh - carry an AWG tunnel inside TCP/TLS (wstunnel) when UDP is filtered.
#
#   bash awg-over-tcp.sh awg0 root@203.0.113.10                 # far end calls it awg0 too
#   bash awg-over-tcp.sh awg1 root@203.0.113.20 awg0          # far end's own name differs
#   SNI=hub.example.com bash awg-over-tcp.sh awg0 root@...    # TLS to a bare IP is dropped
#
# Run it on the hub. What it is for: some Iranian networks let only the first six
# packets of any UDP flow in from abroad, whatever the port, the rate or the
# obfuscation - so an AWG handshake may complete and the tunnel still dies a second
# later, and awg-hop cannot help because every port behaves the same. TCP is not cut.
#
# Layout (one tunnel):
#   hub: awg0 -> 127.0.0.1:LPORT (wstunnel client) ==TLS/WebSocket over TCP==>
#   far: wstunnel server :PORT -> 127.0.0.1:<its AWG ListenPort> -> awg0
#
# The far server only forwards to its own AWG port (--restrict-to), and only for a
# client that knows a random path prefix, kept in /etc/wstunnel-*.env (root only).
# Nothing else about the tunnel changes: keys, addresses and AWG settings stay.
set -euo pipefail

IFACE=${1:?usage: awg-over-tcp.sh <iface> <user@server-abroad> [far-iface]}
DEST=${2:?usage: awg-over-tcp.sh <iface> <user@server-abroad> [far-iface]}
RIFACE=${3:-$IFACE}
PORT=${PORT:-28443}                       # TCP port on the far end
SNI=${SNI:-}                              # a name that resolves to the far end, if TLS to its IP is blocked
VER=${WSTUNNEL_VERSION:-11.0.0}
CONF=/etc/amnezia/amneziawg/$IFACE.conf
FAR=${DEST#*@}

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -f "$CONF" ] || { echo "no $CONF"; exit 1; }

# a stable local port per interface: awg0 -> 51010, awg1 -> 51020, ...
n=$(printf '%s' "$IFACE" | tr -dc 0-9); LPORT=${LPORT:-$((51010 + 10 * ${n:-0}))}

echo "==> wstunnel $VER on $DEST (downloaded there from GitHub, checksum verified)"
ssh "$DEST" "set -e; if ! wstunnel --version 2>/dev/null | grep -q ' $VER\$'; then
  d=\$(mktemp -d); cd \$d
  u=https://github.com/erebe/wstunnel/releases/download/v$VER
  curl -fsSLO \$u/wstunnel_${VER}_linux_amd64.tar.gz; curl -fsSL \$u/checksums.txt | grep linux_amd64.tar.gz | sha256sum -c -
  tar xzf wstunnel_${VER}_linux_amd64.tar.gz wstunnel && install -m 755 wstunnel /usr/local/bin/wstunnel; rm -rf \$d
fi; wstunnel --version"
if ! wstunnel --version 2>/dev/null | grep -q " $VER\$"; then
  echo "==> copying wstunnel to the hub from $DEST (GitHub may not be reachable from here)"
  scp -q "$DEST:/usr/local/bin/wstunnel" /usr/local/bin/wstunnel && chmod 755 /usr/local/bin/wstunnel
fi

FPORT=$(ssh "$DEST" "awg show $RIFACE listen-port")
[ -n "$FPORT" ] || { echo "no AWG interface $RIFACE on $DEST"; exit 1; }
SECRET=$(head -c 18 /dev/urandom | base64 | tr '+/' 'ab')

echo "==> far end: wstunnel server on tcp/$PORT -> 127.0.0.1:$FPORT ($RIFACE)"
ssh "$DEST" "set -e; umask 077
printf 'WSTUNNEL_RESTRICT_HTTP_UPGRADE_PATH_PREFIX=%s\n' '$SECRET' > /etc/wstunnel-awg.env
cat > /etc/systemd/system/wstunnel-awg.service <<EOF
[Unit]
Description=wstunnel server: AWG tunnel to the hub carried over TCP/TLS (UDP is filtered into Iran)
After=network-online.target
Wants=network-online.target
[Service]
EnvironmentFile=/etc/wstunnel-awg.env
ExecStart=/usr/local/bin/wstunnel server --restrict-to 127.0.0.1:$FPORT wss://0.0.0.0:$PORT
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload; systemctl enable -q wstunnel-awg.service; systemctl restart wstunnel-awg.service"

echo "==> hub: wstunnel client 127.0.0.1:$LPORT -> wss://$FAR:$PORT${SNI:+ (SNI $SNI)}"
umask 077
printf 'WSTUNNEL_HTTP_UPGRADE_PATH_PREFIX=%s\n' "$SECRET" > "/etc/wstunnel-$IFACE.env"
cat > "/etc/systemd/system/wstunnel-$IFACE.service" <<EOF
[Unit]
Description=wstunnel client: $IFACE (AWG) carried over TCP/TLS to $FAR (UDP is filtered into Iran)
After=network-online.target
Wants=network-online.target
Before=awg-quick@$IFACE.service
[Service]
EnvironmentFile=/etc/wstunnel-$IFACE.env
ExecStart=/usr/local/bin/wstunnel client ${SNI:+--tls-sni-override $SNI }-L udp://127.0.0.1:$LPORT:127.0.0.1:$FPORT?timeout_sec=0 wss://$FAR:$PORT
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload; systemctl enable -q "wstunnel-$IFACE.service"; systemctl restart "wstunnel-$IFACE.service"

echo "==> pointing $IFACE at the local end"
cp -a "$CONF" "$CONF.bak-$(date +%Y%m%d-%H%M%S)"
sed -i -E "s/^(Endpoint\s*=\s*).*/\1127.0.0.1:$LPORT/" "$CONF"
systemctl restart "awg-quick@$IFACE"

for _ in $(seq 25); do
  sleep 1
  t=$(awg show "$IFACE" latest-handshakes | awk '{print $2}' | sort -n | tail -1)
  if [ -n "$t" ] && [ "$t" -gt 0 ] && [ $(( $(date +%s) - t )) -lt 60 ]; then
    echo "done: $IFACE handshakes over TCP. Undo: restore $CONF from its .bak and disable wstunnel-$IFACE."
    exit 0
  fi
done
echo "no handshake yet. Check: journalctl -u wstunnel-$IFACE -n 20"
echo "(a TLS handshake that times out usually means TLS to the bare IP is filtered - try SNI=<domain of $FAR>)"
exit 1
