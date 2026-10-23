#!/usr/bin/env bash
set -euo pipefail
V=1.15.4
SHA=d8e3ee9a9d48636249484d91af37566dfc0a9d7e71edcbe7acd49cf18bc3c87c
HERE=$(cd "$(dirname "$0")" && pwd)

if ! /opt/spire/bin/spire-server --version 2>&1 | grep -qx "$V"; then
  curl -fsSL -o /tmp/spire.tgz "https://github.com/spiffe/spire/releases/download/v$V/spire-$V-linux-amd64-musl.tar.gz"
  echo "$SHA  /tmp/spire.tgz" | sha256sum -c --quiet -
  rm -rf /opt/spire && mkdir -p /opt/spire && tar -xzf /tmp/spire.tgz -C /opt/spire --strip-components 1
fi
install -D -m 644 "$HERE/server.conf" /etc/avos/spire-server.conf
mkdir -p /var/lib/avos/spire

cat > /etc/systemd/system/avos-spire-server.service <<EOF
[Unit]
Description=Avos SPIRE Server (POC 3)
After=network.target

[Service]
ExecStart=/opt/spire/bin/spire-server run -config /etc/avos/spire-server.conf
RuntimeDirectory=spire-server
Restart=always

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/avos-registrar.service <<EOF
[Unit]
Description=Avos identity registrar (POC 3)
After=avos-spire-server.service

[Service]
ExecStart=/usr/bin/python3 $HERE/registrar.py serve
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q --now avos-spire-server avos-registrar
systemctl restart avos-spire-server avos-registrar
until /opt/spire/bin/spire-server healthcheck -socketPath /run/spire-server/api.sock > /dev/null 2>&1; do sleep 0.2; done
echo "SPIRE $V server healthy; registrar on :8082"
