#!/bin/sh
# Build tools stay on the build host. No Python, Node or Cargo enters the release.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
: "${RELEASE_OUTPUT:?Set RELEASE_OUTPUT to a new absolute directory}"
: "${ENGLISH_BINARY_DIR:?Set ENGLISH_BINARY_DIR to compiled Linux release binaries}"
test ! -e "$RELEASE_OUTPUT"
test -x "$ENGLISH_BINARY_DIR/english-server"
test -x "$ENGLISH_BINARY_DIR/englishctl"
(cd "$ROOT/frontend" && npm run build)
mkdir -p "$RELEASE_OUTPUT/bin" "$RELEASE_OUTPUT/static" "$RELEASE_OUTPUT/deploy"
cp "$ENGLISH_BINARY_DIR/english-server" "$ENGLISH_BINARY_DIR/englishctl" "$RELEASE_OUTPUT/bin/"
cp -R "$ROOT/frontend/dist/." "$RELEASE_OUTPUT/static/"
cp "$ROOT/deploy/englishlearning-rust.service" "$ROOT/deploy/learning-reminders-rust.service" "$ROOT/deploy/learning-reminders.timer" "$ROOT/deploy/nginx-rust-locations.conf" "$ROOT/deploy/switch-rust-release.sh" "$ROOT/deploy/rust-clean-runtime.sh" "$RELEASE_OUTPUT/deploy/"
cp -R "$ROOT/rust/licenses" "$RELEASE_OUTPUT/"
cp "$ROOT/rust/THIRD_PARTY_NOTICES.md" "$ROOT/deploy/RUST.md" "$RELEASE_OUTPUT/"
if [ -n "${ENGLISH_PUBLIC_SEED:-}" ]; then
    mkdir -p "$RELEASE_OUTPUT/content"
    cp "$ENGLISH_PUBLIC_SEED" "$RELEASE_OUTPUT/content/public-seed.db.gz"
fi
git -C "$ROOT" rev-parse HEAD > "$RELEASE_OUTPUT/SOURCE_COMMIT"
(cd "$RELEASE_OUTPUT" && find . -type f ! -name SHA256SUMS -exec shasum -a 256 {} \; > SHA256SUMS)
printf '%s\n' "$RELEASE_OUTPUT"
