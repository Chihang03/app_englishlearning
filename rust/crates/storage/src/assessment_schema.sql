-- Additive extension: main user_version remains 22 for exact legacy rollback.
CREATE TABLE IF NOT EXISTS assessment_schema(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT OR IGNORE INTO assessment_schema VALUES(1);
CREATE TABLE IF NOT EXISTS assessment_items(
 id TEXT PRIMARY KEY, version TEXT NOT NULL, word TEXT NOT NULL, family TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('recognition','recall')), band INTEGER NOT NULL CHECK(band BETWEEN 0 AND 5),
 difficulty REAL NOT NULL, payload TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 calibration_status TEXT NOT NULL DEFAULT 'prior', responses INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS assessment_sessions(
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 state TEXT NOT NULL CHECK(state IN ('active','completed','abandoned')), bank_version TEXT NOT NULL,
 model_version TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT, result_json TEXT, applied_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS assessment_one_active ON assessment_sessions(user_id) WHERE state='active';
CREATE TABLE IF NOT EXISTS assessment_responses(
 session_id TEXT NOT NULL REFERENCES assessment_sessions(id) ON DELETE CASCADE,
 sequence INTEGER NOT NULL, item_id TEXT NOT NULL REFERENCES assessment_items(id),
 issued_at TEXT NOT NULL, answered_at TEXT, answer TEXT, correct INTEGER CHECK(correct IN (0,1)),
 option_order TEXT NOT NULL, PRIMARY KEY(session_id,sequence), UNIQUE(session_id,item_id));
CREATE TABLE IF NOT EXISTS user_proficiency_profiles(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 session_id TEXT NOT NULL REFERENCES assessment_sessions(id), mode TEXT NOT NULL DEFAULT 'frequency' CHECK(mode IN ('frequency','adaptive')),
 mean REAL NOT NULL, sd REAL NOT NULL, initial_mean REAL NOT NULL, model_version TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_unit_difficulty(
 learning_unit_id INTEGER PRIMARY KEY REFERENCES learning_units(id) ON DELETE CASCADE,
 difficulty REAL NOT NULL, source TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
 calibration_version TEXT, retired INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_unit_readiness(
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 learning_unit_id INTEGER NOT NULL REFERENCES learning_units(id) ON DELETE CASCADE,
 state TEXT NOT NULL CHECK(state IN ('deferred','reinforce')), evidence TEXT NOT NULL,
 profile_revision INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(user_id,learning_unit_id));
CREATE TABLE IF NOT EXISTS proficiency_events(
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 event_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, word TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS proficiency_events_user ON proficiency_events(user_id,kind,id);
CREATE TABLE IF NOT EXISTS assessment_calibrations(
 version TEXT PRIMARY KEY, report_json TEXT NOT NULL, created_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)));
