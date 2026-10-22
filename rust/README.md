# Rust migration: first working slice

This workspace starts the migration described in [the full plan](../docs/rust-migration-plan.md).
It does not replace the learner/auth API yet.

## Implemented

- Frozen OpenAPI, 45 route definitions (including GET `/api/next` as a write), and 180 synthetic py-fsrs 6.3.0 reference cases.
- Five crates with a pinned toolchain and Cargo.lock.
- f64 FSRS scheduler math and legacy Card JSON reader, with Python due-date/state and numeric parity tests. The database-backed study transaction and personal calibration workflow are still Python.
- Read-only SQLite worker with a fixed thread, bounded queue, statement progress deadline, cancellation and transaction cleanup. It rejects missing/wrong-version databases and never migrates one.
- `englishctl catalog`: fixed-revision, account-scoped, one-word-at-a-time catalogue export with a 30-second execution budget and atomic output. It opens SQLite read-only, validates the learner and revision, bounds individual words and cleans failed output.
- Optional Python integration through `ENGLISH_NATIVE_CATALOG=/absolute/path/to/englishctl`. The existing cache lock coalesces builders; validated native output uses the existing immutable files and ETag. Missing/failed native tools fall back to the existing Python exporter. Warm 304 requests run neither exporter.
- A loopback-only `english-server` readiness foundation. It serves `/api/health` and `/internal/ready`; all other routes return a JSON 404. It is not a public API replacement.

Old Passkey import/verification, all learning transactions, content package import, native reminders and final API cutover remain pending. This stage enables production use only of the catalogue subprocess after parity verification.

## Verify locally

```bash
cd rust
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo build --workspace --release --locked
cd ..
backend/.venv/bin/python backend/scripts/export_rust_reference.py --check
cd backend
PYTHONPATH=.:tests ENGLISH_TEST_NATIVE_CATALOG=../rust/target/release/englishctl \
  .venv/bin/python -m unittest test_rust_catalog test_memory_optimization test_http_cache
```

Tests always create temporary databases. Never point a test suite at a production DATA_DIR.
The optional real-binary tests skip when no binary exists; the Rust CI job builds it first.

Reference fixture regeneration intentionally requires fsrs 6.3.0; schema generation uses
[pinned reference dependencies](../backend/requirements-rust-reference.txt).
Review fixture changes instead of treating regeneration as proof that semantics match.

## Native tool

```text
englishctl catalog --database <v22-db> --user-id <learner> \
  --database-id <database-identity> --revision <content-revision> \
  --output <new-file>
englishctl fsrs-probe --input <synthetic-case.json>
english-server --database <v22-db> --listen 127.0.0.1:8001
```

The catalogue command prints only bounded digest/version metadata to stdout; the JSON body
is written to the new file. It refuses to overwrite an existing output. It does not create
a missing source database, migrate a schema or sync content on startup.

## Linux release

Build on matching Linux or cross-compile with cargo-zigbuild. The initial target is
`x86_64-unknown-linux-gnu.2.35`, matching the verified Ubuntu host; bundled SQLite is compiled
into the binary. Verify `file`, `ldd`, CLI execution and parity on the target before enabling.
Do not copy a macOS binary or compile on the 1 GiB production instance.

The native catalogue binary is versioned separately from the Python app, so reverting the
integration is a drop-in removal plus service restart, retaining the current database/cache.
See [native deployment](../deploy/RUST.md) for activation and recovery.

## R2–R4 checkpoint (2026-10-22)

The workspace now also implements streaming content fingerprint/diff/build/validate/apply,
transactional and idempotent imports, the account/dictionary/settings/vocabulary APIs,
legacy password/session/Passkey compatibility, and the learning/review/exposure/FSRS/
statistics/calendar transaction. `english-server --enable-api` enables these native APIs;
it does not yet enable production cutover. The earlier first-slice scope above is historical.

35 integration checks passed against isolated v22 databases, including exact catalogue
bytes and Python response/database comparisons. Real software-authenticator signatures,
credential import and replay rejection are covered; these do not constitute iPhone testing.
R5 management/reminders, full runtime packaging and production cutover are still pending.
