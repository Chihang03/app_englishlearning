from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .migrations import run_migrations


BASE_DIR = Path(__file__).resolve().parents[1]

# Runtime data (the SQLite file) lives wherever DATA_DIR points, so a deployment
# can keep it on a persistent volume outside the source tree. The seed word list
# ships with the code and is always read from the repository.
DATA_DIR = Path(os.environ["DATA_DIR"]).expanduser().resolve() if os.environ.get("DATA_DIR") else BASE_DIR / "data"
DB_PATH = DATA_DIR / "vocabulary.db"
SEED_PATH = BASE_DIR / "data" / "seed_words.json"

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


def get_settings(user_id: int) -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def update_settings(user_id: int, values: dict[str, str]) -> dict[str, str]:
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO settings(user_id, key, value)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            [(user_id, key, value) for key, value in values.items()],
        )
    return get_settings(user_id)
