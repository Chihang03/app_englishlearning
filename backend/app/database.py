from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "vocabulary.db"
SEED_PATH = DATA_DIR / "seed_words.json"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
    with connect() as conn:
        conn.executescript(
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
            );

            CREATE TABLE IF NOT EXISTS review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word_id INTEGER NOT NULL,
                review_time TEXT NOT NULL,
                user_answer TEXT NOT NULL,
                is_correct INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
                FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
            );

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
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_review_history_time ON review_history(review_time);
            CREATE INDEX IF NOT EXISTS idx_srs_due ON srs_state(next_review_date, status);
            CREATE INDEX IF NOT EXISTS idx_srs_status ON srs_state(status);
            """
        )
        conn.executemany(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            [
                ("voice", "Samantha"),
                ("rate", "175"),
                ("show_sentence_translation", "false"),
            ],
        )
        ensure_column(conn, "words", "example_translation_cn", "TEXT")
    seed_words()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def seed_words() -> None:
    with connect() as conn:
        if not SEED_PATH.exists():
            return
        words = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        conn.executemany(
            """
            INSERT OR IGNORE INTO words(
                word, part_of_speech, definition_cn, definition_en,
                example_sentence, example_translation_cn, pronunciation
            )
            VALUES(
                :word, :part_of_speech, :definition_cn, :definition_en,
                :example_sentence, :example_translation_cn, :pronunciation
            )
            """,
            words,
        )
        conn.executemany(
            """
            UPDATE words
            SET example_translation_cn = COALESCE(NULLIF(example_translation_cn, ''), :example_translation_cn)
            WHERE word = :word
            """,
            words,
        )


def get_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def update_settings(values: dict[str, str]) -> dict[str, str]:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, value) for key, value in values.items()],
        )
    return get_settings()


def today_iso() -> str:
    return date.today().isoformat()
