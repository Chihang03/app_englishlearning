from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import get_learner_user
from .admin import router as admin_router
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
from .senses import senses_for_word
from .study_tools import report_content, word_meanings
from .muted_words import mute_word, restore_word
from .http_cache import CachePolicyMiddleware
from .morphology import resolve_lexical_entries


# The streak walks back day by day from today, so history older than this cannot
# extend it. Bounding the scan keeps the query flat as history grows.
STREAK_LOOKBACK_DAYS = 400


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_database()
    yield


app = FastAPI(title="Context Vocabulary Trainer", lifespan=lifespan)
app.add_middleware(CachePolicyMiddleware)

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
app.include_router(admin_router)
app.include_router(passkeys_router)


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


class StudyDetailsInput(ExposureInput):
    target_lexical_unit_id: int | None = Field(default=None, gt=0)


class ContentReportInput(ExposureInput):
    category: Literal["definition", "sentence", "translation", "pronunciation", "other"]
    details: str = Field(default="", max_length=2000)


class SettingsInput(BaseModel):
    show_sentence_translation: bool | None = None
    skip_basic_600: bool | None = None
    selected_word_list_ids: list[str] | None = Field(default=None, max_length=50)
    speech_rate: Literal[90, 120, 175] | None = None




def user_timezone(user: dict[str, Any]):
    return resolve_timezone(user["timezone"])


def user_today(user: dict[str, Any]) -> date:
    """Today in the learner's own timezone, not the server's."""
    return today_in(user_timezone(user))




@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/stats")
def stats(user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
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
def next_card(user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    with connect() as conn:
        return next_sense_card(conn,int(user["id"]),user_today(user))


@app.post("/api/review")
def review(payload: ReviewInput, user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    with connect() as conn:
        return record_sense_review(conn,int(user["id"]),user_today(user),payload.word_id,
                                   payload.sense_id,payload.example_id,payload.user_answer,
                                   payload.attempt_id,user_timezone(user),payload.active_response_ms)


@app.post("/api/study/hint")
def study_hint(payload: HintInput, user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, bool]:
    with connect() as conn:
        return record_hint(conn,int(user["id"]),payload.attempt_id,payload.kind)


@app.post("/api/study/related-exposure")
def related_exposure(payload: ExposureInput, user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, bool]:
    with connect() as conn:
        return record_related_exposure(conn,int(user["id"]),payload.attempt_id)


@app.post("/api/study/meanings")
def study_meanings(payload: StudyDetailsInput, user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    with connect() as conn:
        return word_meanings(conn, int(user["id"]), payload.attempt_id, payload.target_lexical_unit_id)


@app.post("/api/content-reports")
def content_report(payload: ContentReportInput, user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    with connect() as conn:
        return report_content(conn, int(user["id"]), payload.attempt_id, payload.category, payload.details)


@app.get("/api/settings")
def settings(user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    user_id = int(user["id"])
    values = get_settings(user_id)
    lists = get_vocabulary_lists(user_id)
    return {
        "show_sentence_translation": values.get("show_sentence_translation", "false") == "true",
        "skip_basic_600": values.get("skip_basic_600", "false") == "true",
        "selected_word_list_ids": [item["list_id"] for item in lists if item["selected"]],
        "speech_rate": int(values["speech_rate"]) if values.get("speech_rate") in {"90", "120", "175"} else None,
    }


@app.get("/api/muted-words")
def muted_words(user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT word,muted_at FROM user_muted_words WHERE user_id=? ORDER BY word COLLATE NOCASE",
                            (int(user["id"]),)).fetchall()
        return {"words": [dict(row) for row in rows]}


@app.post("/api/words/{word_id}/mute")
def mute(word_id: int, user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    with connect() as conn:
        return mute_word(conn, int(user["id"]), word_id)


@app.delete("/api/muted-words/{word}")
def restore(word: str, user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    with connect() as conn:
        return restore_word(conn, int(user["id"]), word)


@app.patch("/api/settings")
def patch_settings(
    payload: SettingsInput,
    user: dict[str, Any] = Depends(get_learner_user),
) -> dict[str, Any]:
    values: dict[str, str] = {}
    if payload.show_sentence_translation is not None:
        values["show_sentence_translation"] = "true" if payload.show_sentence_translation else "false"
    if payload.skip_basic_600 is not None:
        values["skip_basic_600"] = "true" if payload.skip_basic_600 else "false"
    if payload.speech_rate is not None:
        values["speech_rate"] = str(payload.speech_rate)
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
def word_lists(user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    return {"lists": get_vocabulary_lists(int(user["id"]))}


@app.get("/api/dictionary/{word}")
def dictionary_lookup(
    word: str,
    user: dict[str, Any] = Depends(get_learner_user),
) -> dict[str, Any]:
    with connect() as conn:
        entries = resolve_lexical_entries(conn, word, int(user['id']))
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
        # Keep the old direct-headword result. A flat fallback is safe only
        # when exactly one learnable inflection target exists.
        targets = [e for e in entries if e['classification'] == 'INFLECTION' and e['learnable']]
        if entry is None and len(targets) == 1:
            target = targets[0]
            primary = target['senses'][0]
            entry = {'id': target['word_id'], 'word': target['headword'],
                     'part_of_speech': primary['part_of_speech'], 'definition_cn': primary['definition_cn'],
                     'definition_en': primary['definition_en'], 'example_sentence': primary['examples'][0]['sentence'],
                     'senses': target['senses'], 'lexical_unit_id': target['lexical_unit_id'],
                     'pronunciation': conn.execute('SELECT pronunciation FROM words WHERE id=?', (target['word_id'],)).fetchone()[0]}
    return {
        "word": word,
        "source": "本地词库",
        "available": entry is not None,
        "definition": entry,
        "entries": entries,
        "classification": 'UNKNOWN' if not entries else None,
    }


@app.post("/api/words", include_in_schema=False)
@app.post("/api/words/import", include_in_schema=False)
def retired_word_import() -> None:
    # Keep a deterministic response for old clients, including with SPA hosting.
    # No payload parsing, authentication side effects or database writes remain.
    raise HTTPException(status_code=410, detail="用户自导入功能已停用")


# The built frontend is served from the same origin as the API so that the
# browser never makes a cross-origin request. During development this mount is
# skipped: dist/ does not exist yet and Vite serves the app instead.
STATIC_DIR = (
    Path(os.environ["STATIC_DIR"]).expanduser().resolve()
    if os.environ.get("STATIC_DIR")
    else Path(__file__).resolve().parents[2] / "frontend" / "dist"
)


@app.get("/api/version")
def frontend_version() -> dict[str, str | None]:
    # Read the active build, so the marker always describes the served assets.
    path = STATIC_DIR / "version.json"
    if not STATIC_DIR.is_dir():
        path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "version.json"
    try:
        value = json.loads(path.read_text())["version"]
        return {"version": value if isinstance(value, str) else None}
    except (OSError, ValueError, KeyError, TypeError):
        return {"version": None}


if STATIC_DIR.is_dir():
    # Compress public files only; auth and personalized APIs are never compressed.
    app.mount("/", GZipMiddleware(StaticFiles(directory=STATIC_DIR, html=True),
                                  minimum_size=1024, compresslevel=6), name="frontend")
