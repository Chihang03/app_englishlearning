#!/bin/sh
# Execute as root on the existing host after isolated compatibility/load acceptance.
# Rollback keeps the current database, including learning committed after cutover.
set -eu
APP=/opt/englishlearning/app
DATA=/var/lib/englishlearning/vocabulary.db
UNIT=/etc/systemd/system/englishlearning.service
REMINDER=/etc/systemd/system/learning-reminders.service
SNIPPET=/etc/nginx/snippets/englishlearning-rust-locations.conf
restore() {
    saved=$1
    test -f "$saved/old-release"
    systemctl stop learning-reminders.timer learning-reminders.service || true
    systemctl stop englishlearning.service || true
    old=$(cat "$saved/old-release")
    ln -s "$old" "$APP.rollback"
    mv -Tf "$APP.rollback" "$APP"
    cp "$saved/englishlearning.service" "$UNIT"
    rm -rf /etc/systemd/system/englishlearning.service.d
    if [ -d "$saved/service.d" ]; then cp -a "$saved/service.d" /etc/systemd/system/englishlearning.service.d; fi
    for site in englishlearning englishlearning-passkeys; do cp "$saved/nginx-$site" "/etc/nginx/sites-available/$site"; done
    if [ -f "$saved/reminder.service" ]; then cp "$saved/reminder.service" "$REMINDER"; fi
    if [ -f "$saved/reminder.timer" ]; then cp "$saved/reminder.timer" /etc/systemd/system/learning-reminders.timer; fi
    if [ -f "$saved/snippet" ]; then cp "$saved/snippet" "$SNIPPET"; else rm -f "$SNIPPET"; fi
    if [ -f "$saved/app-parent-mode" ]; then chmod "$(cat "$saved/app-parent-mode")" /opt/englishlearning; fi
    systemctl daemon-reload
    nginx -t
    systemctl start englishlearning.service
    systemctl reload nginx
    if [ "$(cat "$saved/timer-active")" = active ]; then systemctl start learning-reminders.timer; fi
}
if [ "${1:-}" = --rollback ]; then restore "$2"; exit; fi
: "${1:?Usage: switch-rust-release.sh /absolute/release OR --rollback /backup-directory}"
RELEASE=$1
test "$(id -u)" = 0
test -L "$APP"
test -f "$RELEASE/SHA256SUMS"
(cd "$RELEASE" && sha256sum -c SHA256SUMS >/dev/null)
"$RELEASE/bin/englishctl" inspect --database "$DATA" >/dev/null
SAVED=/var/backups/englishlearning-rust-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -m 700 "$SAVED"
readlink -f "$APP" > "$SAVED/old-release"
stat -c %a /opt/englishlearning > "$SAVED/app-parent-mode"
cp "$UNIT" "$SAVED/englishlearning.service"
if [ -d /etc/systemd/system/englishlearning.service.d ]; then cp -a /etc/systemd/system/englishlearning.service.d "$SAVED/service.d"; fi
for site in englishlearning englishlearning-passkeys; do cp "/etc/nginx/sites-available/$site" "$SAVED/nginx-$site"; done
if [ -f "$REMINDER" ]; then cp "$REMINDER" "$SAVED/reminder.service"; fi
if [ -f /etc/systemd/system/learning-reminders.timer ]; then cp /etc/systemd/system/learning-reminders.timer "$SAVED/reminder.timer"; fi
if [ -f "$SNIPPET" ]; then cp "$SNIPPET" "$SAVED/snippet"; fi
systemctl is-active learning-reminders.timer > "$SAVED/timer-active" || true
SUCCESS=no
trap 'if [ "$SUCCESS" != yes ]; then restore "$SAVED"; fi' EXIT
systemctl stop learning-reminders.timer learning-reminders.service || true
systemctl stop englishlearning.service
"$RELEASE/bin/englishctl" backup --database "$DATA" --output "$SAVED/vocabulary.db" >/dev/null
chown root:root "$SAVED/vocabulary.db"
chmod 600 "$SAVED/vocabulary.db"
"$RELEASE/bin/englishctl" assessment-install --database "$DATA" --backup "$SAVED/before-assessment.db" > "$SAVED/assessment-install.json"
chown root:root "$SAVED/before-assessment.db"
chmod 600 "$SAVED/before-assessment.db"
"$RELEASE/bin/englishctl" fix-study-examples --database "$DATA" > "$SAVED/fixed-example-repair.json"
cp "$RELEASE/deploy/englishlearning-rust.service" "$UNIT"
cp "$RELEASE/deploy/learning-reminders-rust.service" "$REMINDER"
cp "$RELEASE/deploy/learning-reminders.timer" /etc/systemd/system/learning-reminders.timer
rm -f /etc/systemd/system/englishlearning.service.d/runtime.conf /etc/systemd/system/englishlearning.service.d/native-catalog.conf
# Nginx needs traversal of this parent, without directory listing permission.
chmod o+x /opt/englishlearning
cp "$RELEASE/deploy/nginx-rust-locations.conf" "$SNIPPET"
for site in englishlearning englishlearning-passkeys; do
    awk '
    function braces(line, a,b) {a=gsub(/{/,"{",line);b=gsub(/}/,"}",line);return a-b}
    !holding && /location \/ \{/ {holding=1;block=$0 "\n";depth=braces($0);next}
    holding {block=block $0 "\n";depth+=braces($0);if(depth==0){if(block~/proxy_pass http:\/\/127.0.0.1:8000/)print "    include /etc/nginx/snippets/englishlearning-rust-locations.conf;";else printf "%s",block;holding=0;block=""}next}
    {print}
    END {if(holding)exit 1}
    ' "$SAVED/nginx-$site" > "/etc/nginx/sites-available/$site"
done
ln -s "$RELEASE" "$APP.next"
mv -Tf "$APP.next" "$APP"
systemctl daemon-reload
nginx -t
systemctl start englishlearning.service
ready=no
for attempt in $(seq 1 30); do
    if curl -fsS --max-time 3 http://127.0.0.1:8000/internal/ready >/dev/null; then ready=yes; break; fi
    sleep 1
done
test "$ready" = yes
systemctl reload nginx
# Reload returns before all new workers serve requests; wait for the exact release.
public_ready=no
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 5 https://english.16-76-139-180.sslip.io:8443/api/health >/dev/null 2>&1 &&
       curl -fsS --max-time 5 https://english.16-76-139-180.sslip.io:8443/version.json > "$SAVED/public-version.json" 2>/dev/null &&
       cmp -s "$SAVED/public-version.json" "$RELEASE/static/version.json"; then
        public_ready=yes
        break
    fi
    sleep 1
done
test "$public_ready" = yes
systemctl enable --now learning-reminders.timer
SUCCESS=yes
printf '%s\n' "$SAVED"
