from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .migrations import run_migrations
from .content_overrides import apply_word_overrides
from .senses import authored_sense, migrate_legacy_progress, save_senses, validate_senses
from .learning_filters import basic_word_sql
from .security import utc_now_iso
from .morphology import seed_morphology
from .word_forms import WORD_FORMS_PATH
from .chinese_sentences import sync_database as sync_sentence_translations
from .chinese_glosses import sync_database as sync_gloss_translations


BASE_DIR = Path(__file__).resolve().parents[1]

# Runtime data (the SQLite file) lives wherever DATA_DIR points, so a deployment
# can keep it on a persistent volume outside the source tree. The seed word list
# ships with the code and is always read from the repository.
DATA_DIR = Path(os.environ["DATA_DIR"]).expanduser().resolve() if os.environ.get("DATA_DIR") else BASE_DIR / "data"
DB_PATH = DATA_DIR / "vocabulary.db"
SEED_PATH = BASE_DIR / "data" / "seed_words.json"
VOCABULARY_CATALOG_PATH = BASE_DIR / "data" / "vocabulary_catalog.json"
STARTER_LIST = {
    "list_id": "starter_examples",
    "title": "内置例词",
    "description": "应用自带的示例词，可随时取消选择。",
    "source_url": "",
    "source_word_count": 0,
    "sort_order": 0,
    "default_selected": 1,
}

# With several people reviewing at once, a writer can find the database briefly
# locked. Waiting beats failing the request.
BUSY_TIMEOUT_MS = 5000


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def init_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _enable_wal()
    run_migrations(DB_PATH)
    seed_words()
    seed_vocabulary_catalog()
    sync_gloss_translations(DB_PATH, strict=False)
    with connect() as conn:
        catalog_bytes = VOCABULARY_CATALOG_PATH.read_bytes() if VOCABULARY_CATALOG_PATH.exists() else b'{"lists":[],"words":[]}'
        seed_morphology(conn, WORD_FORMS_PATH.read_bytes() if WORD_FORMS_PATH.exists() else b'', catalog_bytes)
    sync_sentence_translations(DB_PATH, strict=False)


def _enable_wal() -> None:
    """Let readers and a writer work concurrently instead of blocking each other.

    The journal mode is stored in the database file, so this only has to be set
    once, but running it on every start keeps a restored or copied file correct.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()


def seed_words() -> None:
    """Load the bundled starter words into the shared library (owner_id NULL)."""
    if not SEED_PATH.exists():
        return
    words = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    with connect() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO words(
                owner_id, word, part_of_speech, definition_cn, definition_en,
                example_sentence, example_translation_cn, pronunciation
            )
            VALUES(
                NULL, :word, :part_of_speech, :definition_cn, :definition_en,
                :example_sentence, :example_translation_cn, :pronunciation
            )
            """,
            words,
        )
        conn.executemany(
            """
            UPDATE words
            SET example_translation_cn = COALESCE(NULLIF(example_translation_cn, ''), :example_translation_cn)
            WHERE word = :word AND owner_id IS NULL
            """,
            words,
        )


def seed_vocabulary_catalog() -> None:
    """Copy the bundled, example-complete catalog into SQLite for fast queries."""
    catalog_bytes = VOCABULARY_CATALOG_PATH.read_bytes() if VOCABULARY_CATALOG_PATH.exists() else b'{"lists":[],"words":[]}'
    seed_bytes = SEED_PATH.read_bytes() if SEED_PATH.exists() else b'[]'
    fingerprint = hashlib.sha256(catalog_bytes + b'\0' + seed_bytes).hexdigest()
    with connect() as conn:
        stored = conn.execute("SELECT value FROM vocabulary_catalog_state WHERE key='fingerprint'").fetchone()
        if stored and stored[0] == fingerprint:
            migrate_legacy_progress(conn)
            return
    catalog = json.loads(catalog_bytes)
    seed_entries = json.loads(seed_bytes)

    lists = [STARTER_LIST, *catalog.get("lists", [])]
    words = catalog.get("words", [])
    list_ids = {item.get("id", item.get("list_id")) for item in lists}
    if words and catalog.get("format_version") != 2:
        raise ValueError("Re-export vocabulary_catalog.json with paired sense format version 2")
    for item in words:
        validate_senses(item.get("senses", []))
        for membership in item.get("memberships", []):
            if membership.get("list_id") not in list_ids:
                raise ValueError(f"Unknown vocabulary list {membership.get('list_id')!r}")

    with connect() as conn:
        for item in lists:
            conn.execute(
                """
                INSERT INTO vocabulary_lists(
                    list_id, title, description, source_url, source_word_count,
                    word_count, sort_order, default_selected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(list_id) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    source_url = excluded.source_url,
                    source_word_count = excluded.source_word_count,
                    sort_order = excluded.sort_order,
                    default_selected = excluded.default_selected
                """,
                (
                    item["id"] if "id" in item else item["list_id"],
                    item["title"],
                    item.get("description", ""),
                    item.get("source_url", ""),
                    int(item.get("source_word_count", 0)),
                    int(item.get("word_count", 0)),
                    int(item.get("sort_order", 1000)),
                    int(bool(item.get("default_selected", False))),
                ),
            )

        # Memberships are a materialized view of the shipped catalog. Refresh
        # them without touching learners' SRS history or selected-list choices.
        catalog_list_ids = [item["id"] for item in catalog.get("lists", [])]
        if catalog_list_ids:
            placeholders = ",".join("?" for _ in catalog_list_ids)
            conn.execute(
                f"DELETE FROM word_list_memberships WHERE list_id IN ({placeholders})",
                catalog_list_ids,
            )
            conn.execute("UPDATE word_senses SET active=0 WHERE source='macOS Dictionary'")
            conn.execute("UPDATE sense_examples SET active=0 WHERE source='macOS Dictionary'")

        starter_by_word = {s["word"]: s for s in seed_entries}

        for item in words:
            word = str(item["word"]).strip()
            example = str(item["example_sentence"]).strip()
            existing = conn.execute(
                "SELECT id FROM words WHERE owner_id IS NULL AND word = ?", (word,)
            ).fetchone()
            values = (
                str(item.get("part_of_speech") or "词汇").strip(),
                str(item.get("definition_cn") or "词义待补充").strip(),
                str(item.get("definition_en") or "").strip() or None,
                example,
                str(item.get("example_translation_cn") or "").strip() or None,
                str(item.get("pronunciation") or word).strip(),
            )
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO words(
                        owner_id, word, part_of_speech, definition_cn, definition_en,
                        example_sentence, example_translation_cn, pronunciation
                    ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (word, *values),
                )
                word_id = int(cursor.lastrowid)
            else:
                word_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE words SET part_of_speech = ?,
                        definition_cn = CASE WHEN trim(?) IN ('', '词义待补充')
                            AND COALESCE(definition_en,'')=COALESCE(?,'') AND part_of_speech=?
                            THEN definition_cn ELSE ? END, definition_en = ?,
                        example_sentence = ?, example_translation_cn = ?, pronunciation = ?
                    WHERE id = ?
                    """,
                    (values[0],values[1],values[2],values[0],*values[1:],word_id),
                )
            conn.executemany(
                """
                INSERT OR REPLACE INTO word_list_memberships(list_id, word_id, position)
                VALUES (?, ?, ?)
                """,
                [
                    (membership["list_id"], word_id, int(membership["position"]))
                    for membership in item.get("memberships", [])
                ],
            )
            senses = list(item["senses"])
            if word in starter_by_word:
                starter = authored_sense(starter_by_word[word], "内置例词")[0]
                # Merge a curated example into an exactly matching meaning.
                # A separate legacy gloss is never attached to another sense.
                matching = [s for s in senses if s["definition_cn"] == starter["definition_cn"]
                            and s["part_of_speech"] == starter["part_of_speech"]]
                if len(matching) == 1:
                    for ex in starter["examples"]:
                        if not any(e["sentence"] == ex["sentence"] for e in matching[0]["examples"]):
                            matching[0]["examples"].append(ex)
                else:
                    senses += [starter]
            save_senses(conn, word_id, senses)

        # Curated starter/private pairs can be preserved without guessing which
        # meaning an imported, flattened dictionary record was exercising.
        for seed in seed_entries:
            row = conn.execute("SELECT id FROM words WHERE owner_id IS NULL AND word=?", (seed["word"],)).fetchone()
            if row and not conn.execute("SELECT 1 FROM word_senses WHERE word_id=? AND active=1", (row[0],)).fetchone():
                save_senses(conn, row[0], authored_sense(seed, "内置例词"))
        for row in conn.execute("SELECT * FROM words WHERE owner_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM word_senses WHERE word_id=words.id)").fetchall():
            if row["example_sentence"].strip():
                save_senses(conn, row["id"], authored_sense(dict(row)))
        migrate_legacy_progress(conn)

        apply_word_overrides(conn)

        # The original sample entries remain selectable even before a generated
        # catalog is present, and stay available as a small fallback collection.
        if SEED_PATH.exists():
            seed_words = json.loads(SEED_PATH.read_text(encoding="utf-8"))
            for position, seed in enumerate(seed_words, start=1):
                row = conn.execute(
                    "SELECT id FROM words WHERE owner_id IS NULL AND word = ?",
                    (seed["word"],),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO word_list_memberships(list_id, word_id, position)
                        VALUES (?, ?, ?)
                        """,
                        (STARTER_LIST["list_id"], int(row["id"]), position),
                    )

        conn.execute(
            """
            UPDATE vocabulary_lists
            SET word_count = (
                SELECT COUNT(*) FROM word_list_memberships m
                WHERE m.list_id = vocabulary_lists.list_id
                  AND EXISTS(SELECT 1 FROM word_senses s WHERE s.word_id=m.word_id AND s.active=1 AND s.learning_enabled=1)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_vocabulary_lists(user_id, list_id, selected)
            SELECT users.id, vocabulary_lists.list_id, vocabulary_lists.default_selected
            FROM users CROSS JOIN vocabulary_lists
            WHERE users.role = 'learner'
            """
        )
        conn.execute("INSERT OR REPLACE INTO vocabulary_catalog_state(key,value) VALUES('fingerprint',?)", (fingerprint,))


def selected_vocabulary_list_ids(conn: sqlite3.Connection, user_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT list_id FROM user_vocabulary_lists WHERE user_id = ? AND selected = 1 ORDER BY list_id",
        (user_id,),
    ).fetchall()
    if rows:
        return [row["list_id"] for row in rows]
    has_preferences = conn.execute(
        "SELECT 1 FROM user_vocabulary_lists WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    if has_preferences:
        return []
    defaults = conn.execute(
        "SELECT list_id FROM vocabulary_lists WHERE default_selected = 1 ORDER BY list_id"
    ).fetchall()
    return [row["list_id"] for row in defaults]


def get_vocabulary_lists(user_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        selected = set(selected_vocabulary_list_ids(conn, user_id))
        rows = conn.execute(
            """
            WITH touched_words AS (
                SELECT word_id FROM review_history WHERE user_id=?
                UNION SELECT word_id FROM srs_state WHERE user_id=?
                UNION SELECT s.word_id FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id WHERE p.user_id=?
                UNION SELECT s.word_id FROM study_attempts a JOIN word_senses s ON s.id=a.sense_id WHERE a.user_id=?
            ), word_progress AS (
                SELECT s.word_id,
                    MIN(CASE WHEN p.status='Mature' THEN 1 ELSE 0 END) AS mastered
                FROM word_senses s
                LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=?
                WHERE s.active=1 AND s.learning_enabled=1 GROUP BY s.word_id
            )
            SELECT v.list_id,v.title,v.description,v.source_url,v.source_word_count,
                COUNT(p.word_id) AS word_count,
                COUNT(CASE WHEN p.word_id IS NOT NULL AND t.word_id IS NOT NULL THEN 1 END) AS learned_word_count,
                COALESCE(SUM(p.mastered),0) AS mastered_word_count
            FROM vocabulary_lists v
            LEFT JOIN word_list_memberships m ON m.list_id=v.list_id
            LEFT JOIN word_progress p ON p.word_id=m.word_id
            LEFT JOIN touched_words t ON t.word_id=m.word_id
            GROUP BY v.list_id ORDER BY v.sort_order,v.title
            """, (user_id,user_id,user_id,user_id,user_id)
        ).fetchall()
    return [{**row_to_dict(row), "selected": row["list_id"] in selected} for row in rows]
def get_settings(user_id: int) -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def update_settings(user_id: int, values: dict[str, str]) -> dict[str, str]:
    with connect() as conn:
        # Serialize with card issuance/reviews so enabling the filter retires
        # affected rounds on every device in the same transaction as the setting.
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            """
            INSERT INTO settings(user_id, key, value)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            [(user_id, key, value) for key, value in values.items()],
        )
        if values.get("skip_basic_600") == "true":
            conn.execute(f"""UPDATE study_attempts SET completed_at=?
                WHERE user_id=? AND completed_at IS NULL AND sense_id IN (
                    SELECT s.id FROM word_senses s JOIN words w ON w.id=s.word_id
                    WHERE {basic_word_sql()})""", (utc_now_iso(), user_id))
    return get_settings(user_id)
