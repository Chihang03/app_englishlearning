from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, conlist, root_validator, validator

from .auth import get_current_user
from .auth import router as auth_router
from .database import (
    connect,
    get_settings,
    get_vocabulary_lists,
    init_database,
    row_to_dict,
    selected_vocabulary_list_ids,
    update_settings,
)
from .passkeys import router as passkeys_router
from .security import local_day_bounds, resolve_timezone, today_in, utc_iso_from, utc_now_iso
from .srs import next_state


# A word counts as freshly forgotten if it was missed within this window.
LAPSE_LOOKBACK_DAYS = 180

# The streak walks back day by day from today, so history older than this cannot
# extend it. Bounding the scan keeps the query flat as history grows.
STREAK_LOOKBACK_DAYS = 400

# Import is a single request, so it needs a ceiling.
MAX_IMPORT_WORDS = 500


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_database()
    yield


app = FastAPI(title="Context Vocabulary Trainer", lifespan=lifespan)

# Cross-origin requests are only needed when the frontend is served from a
# different origin than the API. The default deployment serves both from the
# same origin (see the StaticFiles mount at the bottom of this file), and the
# Vite dev server proxies /api, so CORS stays off unless CORS_ORIGINS is set.
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "").split(",") if origin.strip()]

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth_router)
app.include_router(passkeys_router)


class WordInput(BaseModel):
    word: str = Field(min_length=1, max_length=100)
    part_of_speech: str = Field(min_length=1, max_length=50)
    definition_cn: str = Field(min_length=1, max_length=500)
    definition_en: str | None = Field(default=None, max_length=1000)
    example_sentence: str = Field(min_length=1, max_length=1000)
    example_translation_cn: str | None = Field(default=None, max_length=1000)
    pronunciation: str | None = Field(default=None, max_length=200)

    @validator("example_sentence")
    def example_sentence_must_not_be_blank(cls, value: str) -> str:
        sentence = value.strip()
        if not sentence:
            raise ValueError("example sentence is required")
        return sentence

    @root_validator(skip_on_failure=True)
    def example_sentence_must_contain_word(cls, values: dict[str, Any]) -> dict[str, Any]:
        word = str(values.get("word") or "").strip()
        sentence = str(values.get("example_sentence") or "")
        if word and not re.search(rf"(?<![A-Za-z'-]){re.escape(word)}(?![A-Za-z'-])", sentence, re.I):
            raise ValueError("example sentence must contain the target word")
        return values


class ReviewInput(BaseModel):
    word_id: int
    user_answer: str = Field(default="", max_length=200)


class SettingsInput(BaseModel):
    show_sentence_translation: bool | None = None
    selected_word_list_ids: list[str] | None = Field(default=None, max_length=50)


def normalize_answer(value: str) -> str:
    return " ".join(value.strip().lower().split())


def mask_sentence(sentence: str, word: str) -> str:
    escaped = re.escape(word)
    pattern = re.compile(rf"(?<![A-Za-z'-]){escaped}(?![A-Za-z'-])", re.IGNORECASE)
    return pattern.sub("_______", sentence)


def user_timezone(user: dict[str, Any]):
    return resolve_timezone(user["timezone"])


def user_today(user: dict[str, Any]) -> date:
    """Today in the learner's own timezone, not the server's."""
    return today_in(user_timezone(user))


def due_lapse_count(conn, user_id: int, today: date) -> int:
    """Words the learner got wrong the first time and that are due again.

    These drive both the review queue and the lapse counter on the dashboard.
    """
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM srs_state s
            JOIN words w ON w.id = s.word_id
            WHERE s.user_id = ?
              AND s.status != 'Mature'
              AND length(trim(w.example_sentence)) > 0
              AND s.next_review_date <= ?
              AND EXISTS (
                  SELECT 1
                  FROM review_history first_review
                  WHERE first_review.user_id = s.user_id
                    AND first_review.word_id = s.word_id
                    AND first_review.id = (
                        SELECT MIN(rh.id)
                        FROM review_history rh
                        WHERE rh.user_id = s.user_id AND rh.word_id = s.word_id
                    )
                    AND first_review.is_correct = 0
              )
            """,
            (user_id, today.isoformat()),
        ).fetchone()["total"]
    )


def lapse_word_count(conn, user_id: int) -> int:
    """Every word the learner got wrong the first time, due or not."""
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM srs_state s
            JOIN words w ON w.id = s.word_id
            WHERE s.user_id = ?
              AND s.status != 'Mature'
              AND length(trim(w.example_sentence)) > 0
              AND EXISTS (
                  SELECT 1
                  FROM review_history first_review
                  WHERE first_review.user_id = s.user_id
                    AND first_review.word_id = s.word_id
                    AND first_review.id = (
                        SELECT MIN(rh.id)
                        FROM review_history rh
                        WHERE rh.user_id = s.user_id AND rh.word_id = s.word_id
                    )
                    AND first_review.is_correct = 0
              )
            """,
            (user_id,),
        ).fetchone()["total"]
    )


def new_word_count(conn, user_id: int) -> int:
    """Visible words this learner has never reviewed."""
    selected_lists = selected_vocabulary_list_ids(conn, user_id)
    list_filter = ""
    list_params: tuple[str, ...] = ()
    if selected_lists:
        placeholders = ",".join("?" for _ in selected_lists)
        list_filter = f"""
            OR (w.owner_id IS NULL AND EXISTS (
                SELECT 1 FROM word_list_memberships m
                WHERE m.word_id = w.id AND m.list_id IN ({placeholders})
            ))
        """
        list_params = tuple(selected_lists)
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM words w
            LEFT JOIN srs_state s ON s.word_id = w.id AND s.user_id = ?
            WHERE s.word_id IS NULL
              AND length(trim(w.example_sentence)) > 0
              AND (w.owner_id = ? {list_filter})
            """,
            (user_id, user_id, *list_params),
        ).fetchone()["total"]
    )


def learning_due_count(conn, user_id: int, today: date) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM srs_state s JOIN words w ON w.id = s.word_id
            WHERE s.user_id = ? AND s.status = 'Learning' AND s.next_review_date <= ?
              AND length(trim(w.example_sentence)) > 0
            """,
            (user_id, today.isoformat()),
        ).fetchone()["total"]
    )


def card_from_word(word: dict[str, Any], remaining_today: int) -> dict[str, Any]:
    return {
        "id": word["id"],
        "word": word["word"],
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
def stats(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    user_id = int(user["id"])
    tz = user_timezone(user)
    today = today_in(tz)
    day_start, day_end = local_day_bounds(today, tz)

    with connect() as conn:
        today_row = conn.execute(
            """
            SELECT COUNT(*) AS total, COALESCE(SUM(is_correct), 0) AS correct
            FROM review_history
            WHERE user_id = ? AND review_time >= ? AND review_time < ?
            """,
            (user_id, day_start, day_end),
        ).fetchone()
        learned = conn.execute(
            """
            SELECT COUNT(*) AS total FROM srs_state s JOIN words w ON w.id = s.word_id
            WHERE s.user_id = ? AND length(trim(w.example_sentence)) > 0
            """,
            (user_id,),
        ).fetchone()["total"]
        learning = conn.execute(
            """
            SELECT COUNT(*) AS total FROM srs_state s JOIN words w ON w.id = s.word_id
            WHERE s.user_id = ? AND s.status = 'Learning' AND length(trim(w.example_sentence)) > 0
            """,
            (user_id,),
        ).fetchone()["total"]
        reviewing = conn.execute(
            """
            SELECT COUNT(*) AS total FROM srs_state s JOIN words w ON w.id = s.word_id
            WHERE s.user_id = ? AND s.status IN ('Reviewing', 'Mature')
              AND length(trim(w.example_sentence)) > 0
            """,
            (user_id,),
        ).fetchone()["total"]
        mature = conn.execute(
            """
            SELECT COUNT(*) AS total FROM srs_state s JOIN words w ON w.id = s.word_id
            WHERE s.user_id = ? AND s.status = 'Mature' AND length(trim(w.example_sentence)) > 0
            """,
            (user_id,),
        ).fetchone()["total"]

        due = due_lapse_count(conn, user_id, today)
        new_words = new_word_count(conn, user_id)
        learning_due = learning_due_count(conn, user_id, today)
        lapse_words = lapse_word_count(conn, user_id)

        streak_cutoff = utc_iso_from(datetime.now(timezone.utc) - timedelta(days=STREAK_LOOKBACK_DAYS))
        history_rows = conn.execute(
            "SELECT review_time FROM review_history WHERE user_id = ? AND review_time >= ?",
            (user_id, streak_cutoff),
        ).fetchall()

    # Timestamps are stored in UTC, so which local day each one belongs to is
    # decided here, in the learner's timezone.
    history_days: set[date] = set()
    for row in history_rows:
        try:
            history_days.add(datetime.fromisoformat(row["review_time"]).astimezone(tz).date())
        except (TypeError, ValueError):
            continue

    streak = 0
    cursor = today
    while cursor in history_days:
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
        "due_lapses": due,
        "mastered": int(reviewing),
        "mature": int(mature),
        "streak_days": streak,
    }


@app.get("/api/next")
def next_card(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    user_id = int(user["id"])
    today = user_today(user).isoformat()

    with connect() as conn:
        remaining = due_lapse_count(conn, user_id, user_today(user))
        row = conn.execute(
            """
            SELECT w.*, s.status
            FROM words w
            JOIN srs_state s ON s.word_id = w.id AND s.user_id = ?
            WHERE s.status = 'Learning' AND s.next_review_date <= ?
              AND length(trim(w.example_sentence)) > 0
            ORDER BY s.next_review_date ASC, s.lapse_count DESC, w.id ASC
            LIMIT 1
            """,
            (user_id, today),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT w.*, s.status
                FROM words w
                JOIN srs_state s ON s.word_id = w.id AND s.user_id = ?
                WHERE s.status = 'Reviewing' AND s.next_review_date <= ?
                  AND length(trim(w.example_sentence)) > 0
                ORDER BY s.next_review_date ASC, s.interval_days ASC, w.id ASC
                LIMIT 1
                """,
                (user_id, today),
            ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT w.*, 'New' AS status
                FROM words w
                LEFT JOIN srs_state s ON s.word_id = w.id AND s.user_id = ?
                WHERE s.word_id IS NULL
                  AND length(trim(w.example_sentence)) > 0
                  AND (w.owner_id = ? OR (w.owner_id IS NULL AND EXISTS (
                    SELECT 1 FROM word_list_memberships m
                    JOIN user_vocabulary_lists uvl ON uvl.list_id = m.list_id
                    WHERE m.word_id = w.id AND uvl.user_id = ? AND uvl.selected = 1
                  )))
                ORDER BY w.id ASC
                LIMIT 1
                """,
                (user_id, user_id, user_id),
            ).fetchone()

    if row is None:
        return {"card": None, "message": "No due cards for today."}
    return {"card": card_from_word(row_to_dict(row), remaining)}


@app.post("/api/review")
def review(
    payload: ReviewInput,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    user_id = int(user["id"])
    today = user_today(user)
    now = utc_now_iso()

    with connect() as conn:
        word = row_to_dict(
            conn.execute(
                "SELECT * FROM words WHERE id = ? AND (owner_id IS NULL OR owner_id = ?)",
                (payload.word_id, user_id),
            ).fetchone()
        )
        if word is None or not str(word.get("example_sentence") or "").strip():
            raise HTTPException(status_code=404, detail="Word not found")

        user_answer = payload.user_answer.strip()
        is_correct = normalize_answer(user_answer) == normalize_answer(word["word"])
        conn.execute(
            """
            INSERT INTO review_history(user_id, word_id, review_time, user_answer, is_correct)
            VALUES(?, ?, ?, ?, ?)
            """,
            (user_id, payload.word_id, now, user_answer, int(is_correct)),
        )

        current = row_to_dict(
            conn.execute(
                "SELECT * FROM srs_state WHERE user_id = ? AND word_id = ?",
                (user_id, payload.word_id),
            ).fetchone()
        )
        if current is not None:
            # The scheduler only wants the counters, not the row's identity.
            current.pop("user_id", None)
            current.pop("word_id", None)

        cutoff = utc_iso_from(datetime.now(timezone.utc) - timedelta(days=LAPSE_LOOKBACK_DAYS))
        had_wrong_recently = (
            int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM review_history
                    WHERE user_id = ? AND word_id = ? AND is_correct = 0 AND review_time >= ?
                    """,
                    (user_id, payload.word_id, cutoff),
                ).fetchone()["total"]
            )
            > 0
        )
        updated = next_state(current, is_correct, had_wrong_recently, today)
        conn.execute(
            """
            INSERT INTO srs_state(
                user_id, word_id, review_count, correct_count, wrong_count, lapse_count,
                easiness_factor, interval_days, next_review_date, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, word_id) DO UPDATE SET
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
                user_id,
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


@app.get("/api/settings")
def settings(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    user_id = int(user["id"])
    values = get_settings(user_id)
    lists = get_vocabulary_lists(user_id)
    return {
        "show_sentence_translation": values.get("show_sentence_translation", "false") == "true",
        "selected_word_list_ids": [item["list_id"] for item in lists if item["selected"]],
    }


@app.patch("/api/settings")
def patch_settings(
    payload: SettingsInput,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    values: dict[str, str] = {}
    if payload.show_sentence_translation is not None:
        values["show_sentence_translation"] = "true" if payload.show_sentence_translation else "false"
    if values:
        update_settings(int(user["id"]), values)
    if payload.selected_word_list_ids is not None:
        user_id = int(user["id"])
        requested = set(payload.selected_word_list_ids)
        with connect() as conn:
            available = {
                row["list_id"]
                for row in conn.execute("SELECT list_id FROM vocabulary_lists").fetchall()
            }
            unknown = requested - available
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown word lists: {', '.join(sorted(unknown))}",
                )
            conn.execute("DELETE FROM user_vocabulary_lists WHERE user_id = ?", (user_id,))
            conn.executemany(
                "INSERT INTO user_vocabulary_lists(user_id, list_id, selected) VALUES (?, ?, ?)",
                [(user_id, list_id, int(list_id in requested)) for list_id in sorted(available)],
            )
    return settings(user)


@app.get("/api/word-lists")
def word_lists(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return {"lists": get_vocabulary_lists(int(user["id"]))}


@app.get("/api/dictionary/{word}")
def dictionary_lookup(
    word: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    with connect() as conn:
        entry = row_to_dict(
            conn.execute(
                """
                SELECT word, part_of_speech, definition_cn, definition_en,
                       example_sentence, pronunciation
                FROM words
                WHERE lower(word) = lower(?) AND length(trim(example_sentence)) > 0
                  AND (owner_id IS NULL OR owner_id = ?)
                ORDER BY CASE WHEN owner_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (word.strip(), int(user["id"]), int(user["id"])),
            ).fetchone()
        )
    return {
        "word": word,
        "source": "本地词库",
        "available": entry is not None,
        "definition": entry,
    }


@app.post("/api/words")
def add_word(
    payload: WordInput,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    user_id = int(user["id"])
    with connect() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO words(
                    owner_id, word, part_of_speech, definition_cn, definition_en,
                    example_sentence, example_translation_cn, pronunciation
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, *_word_values(payload)),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Word already exists or is invalid") from exc
        word = row_to_dict(conn.execute("SELECT * FROM words WHERE id = ?", (cursor.lastrowid,)).fetchone())
    return {"word": word}


@app.post("/api/words/import")
def import_words(
    payload: conlist(WordInput, max_length=MAX_IMPORT_WORDS),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, int]:
    user_id = int(user["id"])
    inserted = 0
    with connect() as conn:
        for item in payload:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO words(
                    owner_id, word, part_of_speech, definition_cn, definition_en,
                    example_sentence, example_translation_cn, pronunciation
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, *_word_values(item)),
            )
            inserted += cursor.rowcount
    return {"inserted": inserted}


def _word_values(item: WordInput) -> tuple[Any, ...]:
    return (
        item.word.strip(),
        item.part_of_speech.strip(),
        item.definition_cn.strip(),
        item.definition_en.strip() if item.definition_en else None,
        item.example_sentence.strip(),
        item.example_translation_cn.strip() if item.example_translation_cn else None,
        item.pronunciation.strip() if item.pronunciation else item.word.strip(),
    )


# The built frontend is served from the same origin as the API so that the
# browser never makes a cross-origin request. During development this mount is
# skipped: dist/ does not exist yet and Vite serves the app instead.
STATIC_DIR = (
    Path(os.environ["STATIC_DIR"]).expanduser().resolve()
    if os.environ.get("STATIC_DIR")
    else Path(__file__).resolve().parents[2] / "frontend" / "dist"
)

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
