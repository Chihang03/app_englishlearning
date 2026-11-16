#!/bin/sh
# Execute as root on the server: unpack an uploaded release archive, verify it and switch to it.
set -eu
: "${1:?Usage: install-uploaded-release.sh /absolute/release.tar.gz release-name}"
: "${2:?Usage: install-uploaded-release.sh /absolute/release.tar.gz release-name}"
ARCHIVE=$1
NAME=$2
case "$NAME" in *[!A-Za-z0-9._-]* | .*) printf '%s\n' 'Invalid release name' >&2; exit 1 ;; esac
test "$(id -u)" = 0
test -f "$ARCHIVE"
RELEASES=/opt/englishlearning/releases
TARGET=$RELEASES/$NAME
test ! -e "$TARGET"
mkdir -p "$RELEASES"
STAGE=$(mktemp -d "$RELEASES/.incoming-XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$ARCHIVE" -C "$STAGE" --no-same-owner
chown -R root:root "$STAGE"
chmod -R u=rwX,go=rX "$STAGE"
(cd "$STAGE" && sha256sum -c --quiet SHA256SUMS)
mv -T "$STAGE" "$TARGET"
trap - EXIT
rm -f "$ARCHIVE"
"$TARGET/deploy/switch-rust-release.sh" "$TARGET"
