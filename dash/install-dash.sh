#!/usr/bin/env bash
# Installs the dashboard and puts nginx in front of the x-ui panel on PUBLIC_PORT.
#   bash install-dash.sh 59085        # side by side: panel stays on its own port too (test)
#   bash install-dash.sh <panel port> move   # take over the panel's port; x-ui moves to 127.0.0.1
# The panel's secret base path is read from its database here and never printed.
set -euo pipefail
PUBLIC_PORT=${1:?public port}; MODE=${2:-side}
HERE=$(cd "$(dirname "$0")" && pwd)
DB=/etc/x-ui/x-ui.db
get() { python3 -c "import sqlite3,sys;r=sqlite3.connect('$DB').execute('select value from settings where key=?',(sys.argv[1],)).fetchone();print(r[0] if r else '')" "$1"; }
BASE=$(get webBasePath); PANEL_PORT=$(get webPort); CERT=$(get webCertFile); KEY=$(get webKeyFile)
PUB_IP=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
[ -n "$BASE" ] && [ -n "$PANEL_PORT" ] && [ -f "$CERT" ] || { echo "panel settings incomplete" >&2; exit 1; }
if [ "$MODE" = move ] && [ "$PUBLIC_PORT" != "$PANEL_PORT" ]; then echo "move needs the panel's own port ($PANEL_PORT)" >&2; exit 1; fi

install -d /opt/xo-dash
install -m 755 "$HERE/xo-dash" /opt/xo-dash/xo-dash
install -m 644 "$HERE/index.html" "$HERE/inject.js" /opt/xo-dash/
install -m 644 "$HERE/xo-dash.service" /etc/systemd/system/xo-dash.service
systemctl daemon-reload && systemctl enable --now xo-dash.service && systemctl restart xo-dash.service

umask 077
cat > /etc/nginx/conf.d/xo-dash.conf <<CONF
# x-ui panel behind nginx, with the Optimizer item added to its sidebar (xui-optimizer/dash)
map \$http_upgrade \$xo_conn { default upgrade; '' close; }
server {
    listen $PUB_IP:$PUBLIC_PORT ssl http2;
    ssl_certificate $CERT;
    ssl_certificate_key $KEY;
    client_max_body_size 50m;

    location ^~ ${BASE}xo-dash/ {
        proxy_pass http://127.0.0.1:9811;
        proxy_set_header Host \$host;
        proxy_read_timeout 180s;
    }
    location / {
        proxy_pass https://127.0.0.1:$PANEL_PORT;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$xo_conn;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Accept-Encoding "";
        proxy_redirect https://127.0.0.1:$PANEL_PORT/ /;
        proxy_read_timeout 300s;
        sub_filter_once on;
        sub_filter '</body>' '<script src="${BASE}xo-dash/inject.js"></script></body>';
    }
}
CONF
if [ "$MODE" = move ]; then
  python3 -c "import sqlite3;c=sqlite3.connect('$DB');c.execute(\"update settings set value='127.0.0.1' where key='webListen'\");c.commit()"
  systemctl restart x-ui; sleep 5
fi
nginx -t && systemctl reload nginx
echo "dashboard: https://<panel domain>:$PUBLIC_PORT<base path>xo-dash/  (log in to the panel first)"
