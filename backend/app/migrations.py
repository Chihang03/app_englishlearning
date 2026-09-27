from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path

from .security import DEFAULT_TIMEZONE, hash_password, resolve_timezone, utc_iso_from, utc_now_iso
from .srs import RELEARNING_DELAY_SECONDS


logger = logging.getLogger(__name__)

SCHEMA_VERSION = 7


def run_migrations(db_path: Path) -> None:
    """Bring the database at db_path up to SCHEMA_VERSION.

    Versions are tracked with SQLite's own `user_version`, so no bookkeeping
    table is needed and a fresh file starts at 0 like a pre-versioning one.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Explicit transaction control: table rebuilds must be all-or-nothing.
    conn.isolation_level = None
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return

        _backup(db_path, version)

        # Rebuilding tables re-points foreign keys mid-flight, so enforcement is
        # off for the duration. This pragma is a no-op inside a transaction.
        conn.execute("PRAGMA foreign_keys = OFF")

        if version < 1:
            conn.execute("BEGIN")
            _migrate_to_v1(conn)
            conn.execute("PRAGMA user_version = 1")
            conn.execute("COMMIT")
            logger.info("Database migrated to schema version 1")

        if version < 2:
            conn.execute("BEGIN")
            _migrate_to_v2(conn)
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
            logger.info("Database migrated to schema version 2 (multi-user)")

        if version < 3:
            conn.execute("BEGIN")
            _migrate_to_v3(conn)
            conn.execute("PRAGMA user_version = 3")
            conn.execute("COMMIT")
            logger.info("Database migrated to schema version 3 (passkeys)")

        if version < 4:
            conn.execute("BEGIN")
            _migrate_to_v4(conn)
            conn.execute("PRAGMA user_version = 4")
            conn.execute("COMMIT")
            logger.info("Database migrated to schema version 4 (local vocabulary lists)")

        if version < 5:
            conn.execute("BEGIN")
            _migrate_to_v5(conn)
            conn.execute("PRAGMA user_version = 5")
            conn.execute("COMMIT")
            logger.info("Database migrated to schema version 5 (sense-level learning)")

        if version < 6:
            conn.execute("BEGIN")
            _migrate_to_v6(conn)
            conn.execute("PRAGMA user_version = 6")
            conn.execute("COMMIT")
            logger.info("Database migrated to schema version 6 (independent recall)")

        if version < 7:
            conn.execute("BEGIN")
            _migrate_to_v7(conn)
            conn.execute("PRAGMA user_version = 7")
            conn.execute("COMMIT")
            logger.info("Database migrated to schema version 7 (adaptive memory)")

        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Migration left {len(violations)} foreign key violations")
    finally:
        conn.close()


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    # Preserve all existing schedules. Memory states are initialized lazily at
    # the next actual review; old, untracked hints cannot establish prior knowledge.
    _run(conn, [
        "ALTER TABLE study_attempts ADD COLUMN pronunciation_used INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE study_attempts ADD COLUMN answer_exposed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE study_attempts ADD COLUMN tracking_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE review_history ADD COLUMN pronunciation_used INTEGER",
        "ALTER TABLE review_history ADD COLUMN active_response_ms INTEGER",
        "ALTER TABLE review_history ADD COLUMN base_recall_probability REAL",
        "ALTER TABLE review_history ADD COLUMN predicted_recall_probability REAL",
        "ALTER TABLE review_history ADD COLUMN memory_model_version TEXT",
        """CREATE TABLE adaptive_memory (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sense_id INTEGER NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
            card_json TEXT NOT NULL, known_candidate INTEGER NOT NULL DEFAULT 0,
            confirmations INTEGER NOT NULL DEFAULT 0, first_independent_at TEXT,
            last_independent_at TEXT, PRIMARY KEY(user_id,sense_id)
        )""",
        """CREATE TABLE memory_profiles (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            forgetting_multiplier REAL NOT NULL DEFAULT 1,
            evaluated_count INTEGER NOT NULL DEFAULT 0, training_count INTEGER NOT NULL DEFAULT 0,
            validation_count INTEGER NOT NULL DEFAULT 0, baseline_log_loss REAL,
            candidate_log_loss REAL, adopted INTEGER NOT NULL DEFAULT 0, evaluated_at TEXT
        )""",
        "CREATE INDEX idx_memory_samples ON review_history(user_id,id) WHERE base_recall_probability IS NOT NULL",
        """CREATE TABLE sense_exposures (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sense_id INTEGER NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
            exposed_at TEXT NOT NULL, PRIMARY KEY(user_id,sense_id)
        )""",
    ])


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    _run(conn, [
        """CREATE TABLE study_attempts (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sense_id INTEGER NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
            example_id INTEGER NOT NULL REFERENCES sense_examples(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL, hint_used INTEGER NOT NULL DEFAULT 0 CHECK(hint_used IN (0,1)),
            completed_at TEXT
        )""",
        "CREATE UNIQUE INDEX idx_open_study_attempt ON study_attempts(user_id,sense_id) WHERE completed_at IS NULL",
        """CREATE TABLE relearning_queue (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sense_id INTEGER NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
            queued_at TEXT NOT NULL, ready_at TEXT NOT NULL, queue_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,sense_id)
        )""",
        "CREATE INDEX idx_relearning_order ON relearning_queue(user_id,queued_at)",
        "ALTER TABLE review_history ADD COLUMN attempt_id TEXT REFERENCES study_attempts(id)",
        # NULL means old history whose hint usage cannot be reconstructed.
        "ALTER TABLE review_history ADD COLUMN is_independent INTEGER CHECK(is_independent IN (0,1))",
        "ALTER TABLE review_history ADD COLUMN is_first_attempt INTEGER CHECK(is_first_attempt IN (0,1))",
    ])
    now = utc_now_iso()
    ready = utc_iso_from(datetime.now(timezone.utc)+timedelta(seconds=RELEARNING_DELAY_SECONDS+1))
    conn.execute("""INSERT INTO relearning_queue(user_id,sense_id,queued_at,ready_at,queue_order)
        SELECT user_id,sense_id,?,?,sense_id FROM sense_srs_state WHERE status='Learning'""", (now,ready))


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    _run(conn, [
        """CREATE TABLE word_senses (
            id INTEGER PRIMARY KEY, word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
            sense_key TEXT NOT NULL, part_of_speech TEXT NOT NULL,
            definition_cn TEXT NOT NULL DEFAULT '', definition_en TEXT,
            source TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            CHECK(length(trim(definition_cn)) > 0 OR length(trim(COALESCE(definition_en,''))) > 0),
            UNIQUE(word_id, sense_key)
        )""",
        "CREATE INDEX idx_senses_word_active ON word_senses(word_id, active, position)",
        """CREATE TABLE sense_examples (
            id INTEGER PRIMARY KEY, sense_id INTEGER NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
            sentence TEXT NOT NULL CHECK(length(trim(sentence)) > 0), translation_cn TEXT,
            target_form TEXT NOT NULL CHECK(length(trim(target_form)) > 0), source TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), UNIQUE(sense_id, sentence)
        )""",
        "CREATE INDEX idx_examples_sense_active ON sense_examples(sense_id, active)",
        """CREATE TABLE sense_srs_state (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sense_id INTEGER NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
            review_count INTEGER NOT NULL DEFAULT 0, correct_count INTEGER NOT NULL DEFAULT 0,
            wrong_count INTEGER NOT NULL DEFAULT 0, lapse_count INTEGER NOT NULL DEFAULT 0,
            easiness_factor REAL NOT NULL DEFAULT 2.5, interval_days INTEGER NOT NULL DEFAULT 0,
            next_review_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('New','Learning','Reviewing','Mature')),
            last_example_id INTEGER REFERENCES sense_examples(id) ON DELETE SET NULL,
            PRIMARY KEY(user_id,sense_id)
        )""",
        "CREATE INDEX idx_sense_srs_due ON sense_srs_state(user_id, status, next_review_date)",
        "ALTER TABLE review_history ADD COLUMN sense_id INTEGER REFERENCES word_senses(id)",
        "ALTER TABLE review_history ADD COLUMN example_id INTEGER REFERENCES sense_examples(id)",
        "CREATE INDEX idx_reviews_sense ON review_history(user_id, sense_id, id)",
        "ALTER TABLE srs_state ADD COLUMN legacy_example_sentence TEXT",
        "ALTER TABLE srs_state ADD COLUMN sense_migrated INTEGER NOT NULL DEFAULT 0",
        """UPDATE srs_state SET legacy_example_sentence =
            (SELECT example_sentence FROM words WHERE words.id = srs_state.word_id)""",
        "CREATE TABLE vocabulary_catalog_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ])


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    _run(conn, [
        """
        CREATE TABLE webauthn_users (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            user_handle BLOB NOT NULL UNIQUE
        )
        """,
        """
        CREATE TABLE passkeys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            credential_id BLOB NOT NULL UNIQUE,
            public_key BLOB NOT NULL,
            sign_count INTEGER NOT NULL,
            device_type TEXT NOT NULL,
            backed_up INTEGER NOT NULL,
            transports TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        )
        """,
        "CREATE INDEX idx_passkeys_user ON passkeys(user_id)",
        """
        CREATE TABLE webauthn_challenges (
            token_hash TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            challenge BLOB NOT NULL,
            origin TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            session_hash TEXT,
            name TEXT,
            expires_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_webauthn_challenges_expiry ON webauthn_challenges(expires_at)"
    ])


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    _run(conn, [
        """
        CREATE TABLE vocabulary_lists (
            list_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_word_count INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 1000,
            default_selected INTEGER NOT NULL DEFAULT 0 CHECK (default_selected IN (0, 1))
        )
        """,
        """
        CREATE TABLE word_list_memberships (
            list_id TEXT NOT NULL REFERENCES vocabulary_lists(list_id) ON DELETE CASCADE,
            word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            PRIMARY KEY (list_id, word_id)
        )
        """,
        "CREATE INDEX idx_word_list_memberships_word ON word_list_memberships(word_id, list_id)",
        """
        CREATE TABLE user_vocabulary_lists (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            list_id TEXT NOT NULL REFERENCES vocabulary_lists(list_id) ON DELETE CASCADE,
            selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
            PRIMARY KEY (user_id, list_id)
        )
        """,
        """
        CREATE TRIGGER words_example_required_insert
        BEFORE INSERT ON words
        WHEN length(trim(NEW.example_sentence)) = 0
        BEGIN
            SELECT RAISE(ABORT, 'example sentence is required');
        END
        """,
        """
        CREATE TRIGGER words_example_required_update
        BEFORE UPDATE OF example_sentence ON words
        WHEN length(trim(NEW.example_sentence)) = 0
        BEGIN
            SELECT RAISE(ABORT, 'example sentence is required');
        END
        """,
    ])


def _run(conn: sqlite3.Connection, statements: list[str]) -> None:
    """Execute DDL one statement at a time.

    sqlite3.executescript() implicitly commits any open transaction, which would
    defeat the all-or-nothing guarantee these migrations depend on.
    """
    for statement in statements:
        conn.execute(statement)


def _backup(db_path: Path, version: int) -> None:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.name}.bak-v{version}-{stamp}")
    # A file copy misses committed data still in SQLite's WAL. Take a consistent
    # SQLite snapshot so the migration backup includes recent learning records.
    with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(target)) as backup:
        source.backup(backup)
    logger.warning("Backed up database before migration: %s", target)


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """The original single-user schema.

    Creating it with IF NOT EXISTS leaves an existing database untouched, so a
    pre-versioning file and a brand new one both arrive at the same shape.
    """
    _run(
        conn,
        [
            """
            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL UNIQUE,
                part_of_speech TEXT NOT NULL,
                definition_cn TEXT NOT NULL,
                definition_en TEXT,
                example_sentence TEXT NOT NULL,
                example_translation_cn TEXT,
                pronunciation TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id INTEGER NOT NULL,
                review_time TEXT NOT NULL,
                user_answer TEXT NOT NULL,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS srs_state (
                word_id INTEGER PRIMARY KEY,
                review_count INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                wrong_count INTEGER NOT NULL DEFAULT 0,
                lapse_count INTEGER NOT NULL DEFAULT 0,
                easiness_factor REAL NOT NULL DEFAULT 2.5,
                interval_days INTEGER NOT NULL DEFAULT 0,
                next_review_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('New', 'Learning', 'Reviewing', 'Mature')),
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ],
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(words)").fetchall()}
    if "example_translation_cn" not in columns:
        conn.execute("ALTER TABLE words ADD COLUMN example_translation_cn TEXT")


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Give every learner their own progress.

    Words stay a shared library (owner_id NULL means public); progress, history
    and settings become per-user. Existing data belongs to a single learner, so
    it is all assigned to one account created here.
    """
    _run(
        conn,
        [
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                timezone TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_sessions_user ON sessions(user_id)",
        ],
    )

    has_legacy_data = (
        int(conn.execute("SELECT COUNT(*) FROM review_history").fetchone()[0]) > 0
        or int(conn.execute("SELECT COUNT(*) FROM srs_state").fetchone()[0]) > 0
    )
    legacy_user_id = _create_initial_user(conn) if has_legacy_data else None

    # words: shared library, plus an optional private owner.
    # NULLs compare as distinct in a plain unique index, which would let the
    # shared library hold duplicates. COALESCE folds them onto one value.
    _run(
        conn,
        [
            """
            CREATE TABLE words_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                word TEXT NOT NULL,
                part_of_speech TEXT NOT NULL,
                definition_cn TEXT NOT NULL,
                definition_en TEXT,
                example_sentence TEXT NOT NULL,
                example_translation_cn TEXT,
                pronunciation TEXT
            )
            """,
            """
            INSERT INTO words_v2(
                id, owner_id, word, part_of_speech, definition_cn, definition_en,
                example_sentence, example_translation_cn, pronunciation
            )
            SELECT
                id, NULL, word, part_of_speech, definition_cn, definition_en,
                example_sentence, example_translation_cn, pronunciation
            FROM words
            """,
            "DROP TABLE words",
            "ALTER TABLE words_v2 RENAME TO words",
            "CREATE UNIQUE INDEX idx_words_owner_word ON words(COALESCE(owner_id, 0), word)",
            "CREATE INDEX idx_words_owner ON words(owner_id)",
        ],
    )

    _run(
        conn,
        [
            """
            CREATE TABLE srs_state_v2 (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
            review_count INTEGER NOT NULL DEFAULT 0,
            correct_count INTEGER NOT NULL DEFAULT 0,
            wrong_count INTEGER NOT NULL DEFAULT 0,
            lapse_count INTEGER NOT NULL DEFAULT 0,
            easiness_factor REAL NOT NULL DEFAULT 2.5,
            interval_days INTEGER NOT NULL DEFAULT 0,
            next_review_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('New', 'Learning', 'Reviewing', 'Mature')),
                PRIMARY KEY (user_id, word_id)
            )
            """,
            """
            CREATE TABLE review_history_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
                review_time TEXT NOT NULL,
                user_answer TEXT NOT NULL,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1))
            )
            """,
            """
            CREATE TABLE settings_v2 (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
            """,
        ],
    )

    if legacy_user_id is not None:
        conn.execute(
            """
            INSERT INTO srs_state_v2(
                user_id, word_id, review_count, correct_count, wrong_count, lapse_count,
                easiness_factor, interval_days, next_review_date, status
            )
            SELECT ?, word_id, review_count, correct_count, wrong_count, lapse_count,
                   easiness_factor, interval_days, next_review_date, status
            FROM srs_state
            """,
            (legacy_user_id,),
        )
        conn.execute(
            """
            INSERT INTO review_history_v2(id, user_id, word_id, review_time, user_answer, is_correct)
            SELECT id, ?, word_id, review_time, user_answer, is_correct FROM review_history
            """,
            (legacy_user_id,),
        )
        # The obsolete macOS voice/rate rows are dropped here; pronunciation is a
        # browser-side preference now.
        conn.execute(
            """
            INSERT INTO settings_v2(user_id, key, value)
            SELECT ?, key, value FROM settings WHERE key = 'show_sentence_translation'
            """,
            (legacy_user_id,),
        )

    _run(
        conn,
        [
            "DROP TABLE srs_state",
            "DROP TABLE review_history",
            "DROP TABLE settings",
            "ALTER TABLE srs_state_v2 RENAME TO srs_state",
            "ALTER TABLE review_history_v2 RENAME TO review_history",
            "ALTER TABLE settings_v2 RENAME TO settings",
            "CREATE INDEX idx_review_history_user_time ON review_history(user_id, review_time)",
            "CREATE INDEX idx_srs_due ON srs_state(user_id, next_review_date, status)",
            "CREATE INDEX idx_srs_status ON srs_state(user_id, status)",
        ],
    )

    _convert_review_times_to_utc(conn)


def _convert_review_times_to_utc(conn: sqlite3.Connection) -> None:
    """Rewrite legacy naive timestamps as UTC.

    Before multi-user support these were written in the server's local time,
    which is ambiguous once accounts can sit in different timezones. They were
    all produced by the one existing learner, so they are interpreted in the
    default timezone and stored as UTC from here on.
    """
    tz = resolve_timezone(DEFAULT_TIMEZONE)
    updates: list[tuple[str, int]] = []
    for row in conn.execute("SELECT id, review_time FROM review_history").fetchall():
        try:
            moment = datetime.fromisoformat(row["review_time"])
        except (TypeError, ValueError):
            logger.warning("Skipping unparseable review_time on row %s", row["id"])
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        updates.append((utc_iso_from(moment), int(row["id"])))

    if updates:
        conn.executemany("UPDATE review_history SET review_time = ? WHERE id = ?", updates)
        logger.info("Converted %d review timestamps to UTC", len(updates))


def _create_initial_user(conn: sqlite3.Connection) -> int:
    """Create the account that inherits all pre-multi-user data."""
    username = (os.environ.get("INITIAL_USERNAME") or "admin").strip() or "admin"
    password = os.environ.get("INITIAL_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(12)

    cursor = conn.execute(
        "INSERT INTO users(username, password_hash, timezone, created_at) VALUES(?, ?, ?, ?)",
        (username, hash_password(password), DEFAULT_TIMEZONE, utc_now_iso()),
    )

    if generated:
        logger.warning(
            "\n%s\nExisting learning data was migrated to a new account.\n"
            "  username: %s\n  password: %s\nThis password is shown once. "
            "Set INITIAL_PASSWORD before migrating to choose your own.\n%s",
            "=" * 64,
            username,
            password,
            "=" * 64,
        )
    else:
        logger.warning("Existing learning data was migrated to account %r", username)

    return int(cursor.lastrowid)
