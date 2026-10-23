#!/usr/bin/env bash
set -euo pipefail
CEDARPY=4.12.2
HERE=$(cd "$(dirname "$0")" && pwd)
VENV=/opt/avos/gateway
UV=$(command -v uv || echo "/home/${SUDO_USER:-ubuntu}/.local/bin/uv")

[ -f /var/lib/avos/gateway.env ] || { echo "missing /var/lib/avos/gateway.env; run just push" >&2; exit 1; }
[ -x $VENV/bin/python ] || "$UV" venv -q --python /usr/bin/python3.12 $VENV
"$UV" pip install -q --python $VENV/bin/python "cedarpy==$CEDARPY"
$VENV/bin/python "$HERE/gateway.py" check

cat > /etc/systemd/system/avos-gateway.service <<EOF
[Unit]
Description=Avos authorization gateway (POC 4)
After=avos-spire-server.service

[Service]
ExecStart=$VENV/bin/python $HERE/gateway.py serve
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q --now avos-gateway
systemctl restart avos-gateway
until (exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null; do sleep 0.2; done
echo "gateway on :8443"
