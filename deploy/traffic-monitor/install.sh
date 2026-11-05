#!/bin/bash
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run as root'; exit 1; }
SOURCE=$(cd "$(dirname "$0")" && pwd)
SNIPPET=/etc/nginx/snippets/englishlearning-rust-locations.conf
EXTRA=/etc/nginx/snippets/englishlearning-extra-traffic.conf
[ -f "$SNIPPET" ] || { echo 'Existing HTTPS snippet missing'; exit 1; }
BACKUP=$(mktemp -d /var/backups/server-traffic-XXXXXXXX)
chmod 700 "$BACKUP"
cp -a "$SNIPPET" "$BACKUP/locations.conf"
if [ -e "$EXTRA" ]; then cp -a "$EXTRA" "$BACKUP/traffic.conf"; fi
install -d -m 755 /opt/server-traffic /var/lib/server-traffic
install -m 644 "$SOURCE/monitor.py" "$SOURCE/index.html" /opt/server-traffic/
install -m 644 "$SOURCE/"*.service "$SOURCE/"*.timer /etc/systemd/system/
install -m 644 "$SOURCE/nginx.conf" "$EXTRA"
if ! grep -qF 'include /etc/nginx/snippets/englishlearning-extra-*.conf;' "$SNIPPET"; then
    printf '\ninclude /etc/nginx/snippets/englishlearning-extra-*.conf;\n' >> "$SNIPPET"
fi
if ! nginx -t; then
    cp -a "$BACKUP/locations.conf" "$SNIPPET"
    if [ -e "$BACKUP/traffic.conf" ]; then cp -a "$BACKUP/traffic.conf" "$EXTRA"; else rm -f "$EXTRA"; fi
    exit 1
fi
for unit in xray.service hysteria-server.service tuic.service nginx.service; do
    systemctl set-property "$unit" IPAccounting=yes
done
systemctl daemon-reload
systemctl start server-traffic-collect.service
systemctl enable --now server-traffic-collect.timer server-traffic-web.service
systemctl restart server-traffic-web.service
for attempt in {1..20}; do
    status=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9108/server-traffic/api || true)
    [ "$status" = 401 ] && break
    sleep 0.5
done
[ "$status" = 401 ] || { echo "Dashboard readiness failed"; exit 1; }
systemctl reload nginx
systemctl is-active server-traffic-web.service server-traffic-collect.timer
printf 'Installed. Nginx configuration backup: %s\n' "$BACKUP"
