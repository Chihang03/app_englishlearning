from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

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
    update_settings,
)
from .passkeys import router as passkeys_router
from .security import local_day_bounds, resolve_timezone, today_in, utc_iso_from, utc_now_iso
from .sense_learning import learning_metrics, next_sense_card, record_sense_review, record_hint, record_related_exposure
from .senses import authored_sense, save_senses, senses_for_word
from .study_tools import report_content, word_meanings
from .muted_words import mute_word, restore_word


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
    sense_id: int | None = None
    example_id: int | None = None
    user_answer: str = Field(default="", max_length=200)
    attempt_id: str | None = Field(default=None, max_length=100)
    active_response_ms: int | None = Field(default=None, ge=0, le=300000)


class HintInput(BaseModel):
    attempt_id: str = Field(min_length=1,max_length=100)
    kind: Literal["pronunciation", "answer"]


class ExposureInput(BaseModel):
    attempt_id: str = Field(min_length=1,max_length=100)


class ContentReportInput(ExposureInput):
    category: Literal["definition", "sentence", "translation", "pronunciation", "other"]
    details: str = Field(default="", max_length=2000)


class SettingsInput(BaseModel):
    show_sentence_translation: bool | None = None
    selected_word_list_ids: list[str] | None = Field(default=None, max_length=50)




def user_timezone(user: dict[str, Any]):
    return resolve_timezone(user["timezone"])


def user_today(user: dict[str, Any]) -> date:
    """Today in the learner's own timezone, not the server's."""
    return today_in(user_timezone(user))




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
            SELECT COUNT(*) AS total, COALESCE(SUM(is_correct), 0) AS correct,
                COUNT(DISTINCT CASE WHEN is_independent=1 THEN word_id END) AS success_words,
                COUNT(DISTINCT CASE WHEN is_independent=1 THEN sense_id END) AS success_senses,
                COALESCE(SUM(is_first_attempt),0) AS first_total,
                COALESCE(SUM(CASE WHEN is_first_attempt=1 AND is_correct=1 THEN 1 ELSE 0 END),0) AS first_correct
            FROM review_history
            WHERE user_id = ? AND review_time >= ? AND review_time < ?
            """,
            (user_id, day_start, day_end),
        ).fetchone()
        metrics = learning_metrics(conn,user_id,today)

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
        "today_success": int(today_row["success_words"]),
        "today_success_senses": int(today_row["success_senses"]),
        "today_independent_accuracy": round(today_row["first_correct"] / today_row["first_total"] * 100) if today_row["first_total"] else None,
        **metrics,
        "streak_days": streak,
    }


@app.get("/api/next")
def next_card(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with connect() as conn:
        return next_sense_card(conn,int(user["id"]),user_today(user))


@app.post("/api/review")
def review(payload: ReviewInput, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with connect() as conn:
        return record_sense_review(conn,int(user["id"]),user_today(user),payload.word_id,
                                   payload.sense_id,payload.example_id,payload.user_answer,
                                   payload.attempt_id,user_timezone(user),payload.active_response_ms)


@app.post("/api/study/hint")
def study_hint(payload: HintInput, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, bool]:
    with connect() as conn:
        return record_hint(conn,int(user["id"]),payload.attempt_id,payload.kind)


@app.post("/api/study/related-exposure")
def related_exposure(payload: ExposureInput, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, bool]:
    with connect() as conn:
        return record_related_exposure(conn,int(user["id"]),payload.attempt_id)


@app.post("/api/study/meanings")
def study_meanings(payload: ExposureInput, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with connect() as conn:
        return word_meanings(conn, int(user["id"]), payload.attempt_id)


@app.post("/api/content-reports")
def content_report(payload: ContentReportInput, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with connect() as conn:
        return report_content(conn, int(user["id"]), payload.attempt_id, payload.category, payload.details)


@app.get("/api/settings")
def settings(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    user_id = int(user["id"])
    values = get_settings(user_id)
    lists = get_vocabulary_lists(user_id)
    return {
        "show_sentence_translation": values.get("show_sentence_translation", "false") == "true",
        "selected_word_list_ids": [item["list_id"] for item in lists if item["selected"]],
    }


@app.get("/api/muted-words")
def muted_words(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT word,muted_at FROM user_muted_words WHERE user_id=? ORDER BY word COLLATE NOCASE",
                            (int(user["id"]),)).fetchall()
        return {"words": [dict(row) for row in rows]}


@app.post("/api/words/{word_id}/mute")
def mute(word_id: int, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with connect() as conn:
        return mute_word(conn, int(user["id"]), word_id)


@app.delete("/api/muted-words/{word}")
def restore(word: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with connect() as conn:
        return restore_word(conn, int(user["id"]), word)


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
                       example_sentence, pronunciation, id
                FROM words
                WHERE lower(word) = lower(?) AND length(trim(example_sentence)) > 0
                  AND (owner_id IS NULL OR owner_id = ?)
                ORDER BY CASE WHEN owner_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (word.strip(), int(user["id"]), int(user["id"])),
            ).fetchone()
        )
        if entry is not None:
            entry["senses"] = senses_for_word(conn,entry["id"],int(user["id"]))
            if not entry["senses"]:
                entry = None
            else:
                primary = entry["senses"][0]
                entry.update({key:primary[key] for key in ("part_of_speech","definition_cn","definition_en")})
                entry["example_sentence"] = primary["examples"][0]["sentence"]
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
        save_senses(conn, word["id"], authored_sense(word))
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
            if cursor.rowcount:
                word = row_to_dict(conn.execute("SELECT * FROM words WHERE id=?", (cursor.lastrowid,)).fetchone())
                save_senses(conn, word["id"], authored_sense(word))
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
