#!/bin/sh
# Target-host clean-room smoke: chroot contains only release files, glibc and CA certificates.
set -eu
: "${1:?Usage: rust-clean-runtime.sh /absolute/release /new/clean-root}"
RELEASE=$1
CLEAN=$2
test "$(id -u)" = 0
test ! -e "$CLEAN"
mkdir -p "$CLEAN/release" "$CLEAN/data" "$CLEAN/tmp" "$CLEAN/etc/ssl/certs" "$CLEAN/dev"
cp -R "$RELEASE/bin" "$RELEASE/static" "$CLEAN/release/"
for binary in "$RELEASE/bin/english-server" "$RELEASE/bin/englishctl"; do
    ldd "$binary" | awk '/=> \// {print $3} /^[[:space:]]*\// {print $1}' | while IFS= read -r library; do
        test -f "$library"
        mkdir -p "$CLEAN$(dirname "$library")"
        cp -L "$library" "$CLEAN$library"
    done
done
# The ELF interpreter may not match awk's POSIX whitespace shorthand on all hosts.
for loader in /lib64/ld-linux-x86-64.so.2 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2; do
    if [ -f "$loader" ]; then mkdir -p "$CLEAN$(dirname "$loader")";cp -L "$loader" "$CLEAN$loader";fi
done
cp /etc/resolv.conf /etc/nsswitch.conf /etc/hosts "$CLEAN/etc/"
cp /etc/ssl/certs/ca-certificates.crt "$CLEAN/etc/ssl/certs/"
mknod -m 666 "$CLEAN/dev/urandom" c 1 9
mknod -m 666 "$CLEAN/dev/null" c 1 3
if [ -f "$RELEASE/content/public-seed.db.gz" ]; then
    gzip -dc "$RELEASE/content/public-seed.db.gz" > "$CLEAN/data/vocabulary.db"
else
    chroot "$CLEAN" /release/bin/englishctl init --database /data/vocabulary.db
fi
chroot "$CLEAN" /release/bin/englishctl inspect --database /data/vocabulary.db
printf 'temporary-clean-runtime-password\n' | chroot "$CLEAN" /release/bin/englishctl create-admin --database /data/vocabulary.db --password-stdin
chroot "$CLEAN" /release/bin/englishctl reminders --database /data/vocabulary.db --dry-run
chroot "$CLEAN" /release/bin/english-server --database /data/vocabulary.db --listen 127.0.0.1:18087 --enable-api --static-dir /release/static > "$CLEAN/server.log" 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true;wait "$PID" 2>/dev/null || true' EXIT
READY=no
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS http://127.0.0.1:18087/internal/ready >/dev/null; then READY=yes;break;fi
    sleep 1
done
test "$READY" = yes
curl -fsS http://127.0.0.1:18087/api/version
# No interpreters or language build tools were copied into the root.
test ! -e "$CLEAN/usr/bin/python3"
test ! -e "$CLEAN/usr/bin/node"
test ! -e "$CLEAN/usr/bin/cargo"
printf '\nclean_runtime=passed\n'
