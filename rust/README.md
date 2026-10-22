# Native backend

Rust implements the production HTTP API, content pipeline and reminder task. The frontend
remains the existing React PWA. Python application sources are retained as offline
compatibility references and content-authoring tools; none is shipped in the native release.
The temporary Python-to-Rust catalogue bridge has been removed.

## Components

- `english-core`: f64 py-fsrs 6.3.0-compatible scheduling, with 180 frozen cases.
- `english-storage`: existing SQLite v22 schema, account/session/Passkey compatibility,
  dictionary/settings/study/calendar/statistics, administrator review/correction and reminders.
- `english-content`: streamed canonical fingerprints, optimistic content diff/staging packages,
  atomic validation/import receipts, public-only seed export, and streamed meaning catalogues.
- `english-server`: loopback HTTP server, one writer and two readers, bounded queues,
  16 admitted requests, query deadlines/cancellation, per-request data budgets and streamed files.
- `englishctl`: explicit initialization, inspection, backup, administrator provisioning,
  report exchange, content operations and one-shot reminders.

HTTP startup opens an existing v22 database. It never imports source JSON or runs a schema
migration. `englishctl init` creates a new v22 database and refuses an existing path; older
schemas are rejected so operators must perform an explicit, separately reviewed migration.
The current production schema is v22. Existing passwords, session hashes, COSE credentials,
FSRS Card JSON and content revisions remain readable by the old backend for rollback.

## Verification

```sh
cd rust
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo build --workspace --release --locked
cd ..
backend/.venv/bin/python backend/scripts/export_rust_reference.py --check
cd backend
PYTHONPATH=.:tests ENGLISH_TEST_NATIVE_CATALOG=../rust/target/release/englishctl \
ENGLISH_TEST_RUST_SERVER=../rust/target/release/english-server \
.venv/bin/python -m unittest test_rust_api test_rust_management test_rust_operations \
  test_rust_content test_rust_catalog test_memory_optimization test_http_cache
```

All integration tests create temporary databases. Never use a production DATA_DIR for tests.
Frozen OpenAPI/SQL/model order/report exchange/schema fixtures are generated from the Python
reference with pinned dependencies, without opening user databases. Review fixture changes;
regeneration alone does not prove behavioral compatibility.

## Operations

```sh
englishctl init --database /data/vocabulary.db
englishctl inspect --database /data/vocabulary.db
englishctl backup --database /data/vocabulary.db --output /backups/new.db
# Read the initial password from stdin, never from a command argument.
englishctl create-admin --database /data/vocabulary.db --password-stdin
english-server --database /data/vocabulary.db --listen 127.0.0.1:8000 \
  --enable-api --static-dir /release/static
englishctl reminders --database /data/vocabulary.db --dry-run
englishctl content export-seed --database /data/vocabulary.db --output /new/public-seed.db
englishctl content diff --before /snapshots/before.db --after /snapshots/after.db --output /new/patch.json
englishctl content build --source /new/patch.json --output /new/package.sqlite
englishctl content validate --package /new/package.sqlite
englishctl content apply --package /new/package.sqlite --database /data/vocabulary.db --dry-run
englishctl report-review --database /data/vocabulary.db --admin-id 1 export --output /new/review.json
```

`report-review validate/import --input` consumes `{manifest,result}` matching the HTTP
exchange. Imports require an existing administrator ID, validate the entire batch, back up
before changes and recheck under `BEGIN IMMEDIATE`. Manual items remain pending; matching
repeated results are no-ops. Public content packages use revision and before-value guards,
never replace rows, and reject private/account/learning table changes.

Linux releases target x86_64/glibc 2.35. SQLite and OpenSSL are compiled into the binaries.
Web Push uses RFC8188 encryption and the existing P-256 VAPID key; HTTPS requests have a
10-second timeout and never follow redirects. A persistent claim prevents ambiguous sends
from being repeated. Explicit HTTP failures retry at most three times, at least 60 seconds
apart, within the existing 20:00–20:09 local window.

See [deployment and recovery](../deploy/RUST.md) and the
[migration performance report](../docs/rust-migration-performance-report.md).
