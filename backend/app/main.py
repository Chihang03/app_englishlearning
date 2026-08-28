from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .database import connect, get_settings, init_database, row_to_dict, today_iso, update_settings
from .dictionary import lookup_system_definition
from .srs import next_state
from .tts import DEFAULT_RATE, DEFAULT_VOICE, list_english_voices, speak


app = FastAPI(title="Context Vocabulary Trainer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class WordInput(BaseModel):
    word: str = Field(min_length=1)
    part_of_speech: str = Field(min_length=1)
    definition_cn: str = Field(min_length=1)
    definition_en: str | None = None
    example_sentence: str = Field(min_length=1)
    example_translation_cn: str | None = None
    pronunciation: str | None = None


class ReviewInput(BaseModel):
    word_id: int
    user_answer: str = ""


class TTSInput(BaseModel):
    text: str = Field(min_length=1)


class SettingsInput(BaseModel):
    voice: str | None = None
    rate: int | None = Field(default=None, ge=80, le=320)
    show_sentence_translation: bool | None = None


@app.on_event("startup")
def startup() -> None:
    init_database()


def normalize_answer(value: str) -> str:
    return " ".join(value.strip().lower().split())


def mask_sentence(sentence: str, word: str) -> str:
    escaped = re.escape(word)
    if re.fullmatch(r"[A-Za-z]+", word):
        pattern = re.compile(rf"\b{escaped}\b", re.IGNORECASE)
    else:
        pattern = re.compile(escaped, re.IGNORECASE)
    return pattern.sub("_______", sentence)


def due_review_count(conn) -> int:
    today = today_iso()
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM srs_state s
            WHERE s.status != 'Mature'
              AND s.next_review_date <= ?
              AND EXISTS (
                  SELECT 1
                  FROM review_history first_review
                  WHERE first_review.word_id = s.word_id
                    AND first_review.id = (
                        SELECT MIN(rh.id)
                        FROM review_history rh
                        WHERE rh.word_id = s.word_id
                    )
                    AND first_review.is_correct = 0
              )
            """,
            (today,),
        ).fetchone()["total"]
    )


def new_word_count(conn) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM words w
            LEFT JOIN srs_state s ON s.word_id = w.id
            WHERE s.word_id IS NULL
            """
        ).fetchone()["total"]
    )


def learning_due_count(conn) -> int:
    today = today_iso()
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM srs_state
            WHERE status = 'Learning'
              AND next_review_date <= ?
            """,
            (today,),
        ).fetchone()["total"]
    )


def lapse_word_count(conn) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM srs_state s
            WHERE s.status != 'Mature'
              AND EXISTS (
                  SELECT 1
                  FROM review_history first_review
                  WHERE first_review.word_id = s.word_id
                    AND first_review.id = (
                        SELECT MIN(rh.id)
                        FROM review_history rh
                        WHERE rh.word_id = s.word_id
                    )
                    AND first_review.is_correct = 0
              )
            """
        ).fetchone()["total"]
    )


def due_lapse_count(conn) -> int:
    today = today_iso()
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM srs_state s
            WHERE s.status != 'Mature'
              AND s.next_review_date <= ?
              AND EXISTS (
                  SELECT 1
                  FROM review_history first_review
                  WHERE first_review.word_id = s.word_id
                    AND first_review.id = (
                        SELECT MIN(rh.id)
                        FROM review_history rh
                        WHERE rh.word_id = s.word_id
                    )
                    AND first_review.is_correct = 0
              )
            """,
            (today,),
        ).fetchone()["total"]
    )


def card_from_word(word: dict[str, Any], remaining_today: int) -> dict[str, Any]:
    return {
        "id": word["id"],
        "part_of_speech": word["part_of_speech"],
        "definition_cn": word["definition_cn"],
        "definition_en": word["definition_en"],
        "cloze_sentence": mask_sentence(word["example_sentence"], word["word"]),
        "example_sentence": word["example_sentence"],
        "example_translation_cn": word["example_translation_cn"],
        "status": word.get("status") or "New",
        "remaining_today": remaining_today,
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    today = today_iso()
    with connect() as conn:
        today_row = conn.execute(
            """
            SELECT COUNT(*) AS total, COALESCE(SUM(is_correct), 0) AS correct
            FROM review_history
            WHERE date(review_time) = ?
            """,
            (today,),
        ).fetchone()
        learned = conn.execute("SELECT COUNT(*) AS total FROM srs_state").fetchone()["total"]
        due = due_review_count(conn)
        learning = conn.execute(
            "SELECT COUNT(*) AS total FROM srs_state WHERE status = 'Learning'"
        ).fetchone()["total"]
        reviewing = conn.execute(
            "SELECT COUNT(*) AS total FROM srs_state WHERE status IN ('Reviewing', 'Mature')"
        ).fetchone()["total"]
        mature = conn.execute(
            "SELECT COUNT(*) AS total FROM srs_state WHERE status = 'Mature'"
        ).fetchone()["total"]
        new_words = new_word_count(conn)
        learning_due = learning_due_count(conn)
        lapse_words = lapse_word_count(conn)
        due_lapses = due_lapse_count(conn)
        history_days = {
            row["day"]
            for row in conn.execute(
                "SELECT DISTINCT date(review_time) AS day FROM review_history ORDER BY day DESC"
            ).fetchall()
        }

    streak = 0
    cursor = date.today()
    while cursor.isoformat() in history_days:
        streak += 1
        cursor -= timedelta(days=1)

    total_today = int(today_row["total"])
    correct_today = int(today_row["correct"])
    return {
        "today_learning": total_today,
        "today_accuracy": round((correct_today / total_today) * 100) if total_today else 0,
        "total_learned": int(learned),
        "due_review": due,
        "new_words": new_words,
        "learning": int(learning),
        "learning_due": learning_due,
        "lapse_words": lapse_words,
        "due_lapses": due_lapses,
        "mastered": int(reviewing),
        "mature": int(mature),
        "streak_days": streak,
    }


@app.get("/api/next")
def next_card() -> dict[str, Any]:
    today = today_iso()
    with connect() as conn:
        remaining = due_review_count(conn)
        row = conn.execute(
            """
            SELECT w.*, s.status
            FROM words w
            JOIN srs_state s ON s.word_id = w.id
            WHERE s.status = 'Learning' AND s.next_review_date <= ?
            ORDER BY s.next_review_date ASC, s.lapse_count DESC, w.id ASC
            LIMIT 1
            """,
            (today,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT w.*, s.status
                FROM words w
                JOIN srs_state s ON s.word_id = w.id
                WHERE s.status = 'Reviewing' AND s.next_review_date <= ?
                ORDER BY s.next_review_date ASC, s.interval_days ASC, w.id ASC
                LIMIT 1
                """,
                (today,),
            ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT w.*, 'New' AS status
                FROM words w
                LEFT JOIN srs_state s ON s.word_id = w.id
                WHERE s.word_id IS NULL
                ORDER BY w.id ASC
                LIMIT 1
                """
            ).fetchone()

    if row is None:
        return {"card": None, "message": "No due cards for today."}
    return {"card": card_from_word(row_to_dict(row), remaining)}


@app.post("/api/review")
def review(payload: ReviewInput) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        word = row_to_dict(conn.execute("SELECT * FROM words WHERE id = ?", (payload.word_id,)).fetchone())
        if word is None:
            raise HTTPException(status_code=404, detail="Word not found")

        user_answer = payload.user_answer.strip()
        is_correct = normalize_answer(user_answer) == normalize_answer(word["word"])
        conn.execute(
            """
            INSERT INTO review_history(word_id, review_time, user_answer, is_correct)
            VALUES(?, ?, ?, ?)
            """,
            (payload.word_id, now, user_answer, int(is_correct)),
        )

        current = row_to_dict(
            conn.execute("SELECT * FROM srs_state WHERE word_id = ?", (payload.word_id,)).fetchone()
        )
        cutoff = (date.today() - timedelta(days=180)).isoformat()
        had_wrong_recently = (
            conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM review_history
                WHERE word_id = ? AND is_correct = 0 AND date(review_time) >= ?
                """,
                (payload.word_id, cutoff),
            ).fetchone()["total"]
            > 0
        )
        updated = next_state(current, is_correct, had_wrong_recently)
        conn.execute(
            """
            INSERT INTO srs_state(
                word_id, review_count, correct_count, wrong_count, lapse_count,
                easiness_factor, interval_days, next_review_date, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(word_id) DO UPDATE SET
                review_count = excluded.review_count,
                correct_count = excluded.correct_count,
                wrong_count = excluded.wrong_count,
                lapse_count = excluded.lapse_count,
                easiness_factor = excluded.easiness_factor,
                interval_days = excluded.interval_days,
                next_review_date = excluded.next_review_date,
                status = excluded.status
            """,
            (
                payload.word_id,
                updated["review_count"],
                updated["correct_count"],
                updated["wrong_count"],
                updated["lapse_count"],
                updated["easiness_factor"],
                updated["interval_days"],
                updated["next_review_date"],
                updated["status"],
            ),
        )

    return {
        "is_correct": is_correct,
        "is_blank": user_answer == "",
        "correct_answer": word["word"],
        "example_sentence": word["example_sentence"],
        "srs_state": updated,
    }


@app.post("/api/tts")
def tts(payload: TTSInput) -> dict[str, str]:
    settings = get_settings()
    speak(
        payload.text,
        voice=settings.get("voice", DEFAULT_VOICE),
        rate=int(settings.get("rate", DEFAULT_RATE)),
    )
    return {"status": "played"}


@app.get("/api/settings")
def settings() -> dict[str, Any]:
    values = get_settings()
    return {
        "voice": values.get("voice", DEFAULT_VOICE),
        "rate": int(values.get("rate", DEFAULT_RATE)),
        "show_sentence_translation": values.get("show_sentence_translation", "false") == "true",
        "voices": [voice.__dict__ for voice in list_english_voices()],
    }


@app.get("/api/dictionary/{word}")
def dictionary_lookup(word: str) -> dict[str, Any]:
    definition = lookup_system_definition(word)
    return {
        "word": word,
        "source": "macOS DictionaryServices",
        "available": definition is not None,
        "definition": definition,
    }


@app.patch("/api/settings")
def patch_settings(payload: SettingsInput) -> dict[str, Any]:
    values: dict[str, str] = {}
    if payload.voice is not None:
        values["voice"] = payload.voice
    if payload.rate is not None:
        values["rate"] = str(payload.rate)
    if payload.show_sentence_translation is not None:
        values["show_sentence_translation"] = "true" if payload.show_sentence_translation else "false"
    update_settings(values)
    return settings()


@app.post("/api/words")
def add_word(payload: WordInput) -> dict[str, Any]:
    with connect() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO words(
                    word, part_of_speech, definition_cn, definition_en,
                    example_sentence, example_translation_cn, pronunciation
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.word.strip(),
                    payload.part_of_speech.strip(),
                    payload.definition_cn.strip(),
                    payload.definition_en.strip() if payload.definition_en else None,
                    payload.example_sentence.strip(),
                    payload.example_translation_cn.strip() if payload.example_translation_cn else None,
                    payload.pronunciation.strip() if payload.pronunciation else payload.word.strip(),
                ),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Word already exists or is invalid") from exc
        word = row_to_dict(conn.execute("SELECT * FROM words WHERE id = ?", (cursor.lastrowid,)).fetchone())
    return {"word": word}


@app.post("/api/words/import")
def import_words(payload: list[WordInput]) -> dict[str, int]:
    inserted = 0
    with connect() as conn:
        for item in payload:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO words(
                    word, part_of_speech, definition_cn, definition_en,
                    example_sentence, example_translation_cn, pronunciation
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.word.strip(),
                    item.part_of_speech.strip(),
                    item.definition_cn.strip(),
                    item.definition_en.strip() if item.definition_en else None,
                    item.example_sentence.strip(),
                    item.example_translation_cn.strip() if item.example_translation_cn else None,
                    item.pronunciation.strip() if item.pronunciation else item.word.strip(),
                ),
            )
            inserted += cursor.rowcount
    return {"inserted": inserted}
