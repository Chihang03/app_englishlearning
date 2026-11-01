CREATE TABLE IF NOT EXISTS readiness_schema(version INTEGER PRIMARY KEY CHECK(version=2));
INSERT OR IGNORE INTO readiness_schema VALUES(2);
CREATE TABLE IF NOT EXISTS readiness_details(
 user_id INTEGER NOT NULL, learning_unit_id INTEGER NOT NULL,
 source TEXT NOT NULL, source_id TEXT NOT NULL, valid_until TEXT NOT NULL,
 definition_cn TEXT NOT NULL, lexical_unit_id INTEGER NOT NULL,
 PRIMARY KEY(user_id,learning_unit_id),
 FOREIGN KEY(user_id,learning_unit_id) REFERENCES user_unit_readiness(user_id,learning_unit_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS readiness_overrides(
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 learning_unit_id INTEGER NOT NULL REFERENCES learning_units(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL, PRIMARY KEY(user_id,learning_unit_id));
CREATE INDEX IF NOT EXISTS readiness_user_expiry ON readiness_details(user_id,valid_until);
CREATE INDEX IF NOT EXISTS assessment_sessions_user_time ON assessment_sessions(user_id,created_at);
CREATE INDEX IF NOT EXISTS idx_words_readiness_normalized ON words(lower(trim(word)));
CREATE INDEX IF NOT EXISTS idx_assessment_items_readiness ON assessment_items(word,kind,band);
