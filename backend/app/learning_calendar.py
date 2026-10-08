"""Historical learning totals, grouped in the learner's local calendar days."""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .auth import get_learner_user
from .database import connect
from .security import local_day_bounds, resolve_timezone, today_in

router = APIRouter()


@router.get("/api/learning-calendar")
def learning_calendar(month: str | None = None,
                      user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    tz = resolve_timezone(user["timezone"])
    today = today_in(tz)
    month = month if month is not None else today.strftime("%Y-%m")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise ValueError
        year, number = map(int, month.split("-"))
        first = date(year, number, 1)
        last = date(year, number, calendar.monthrange(year, number)[1])
        after = last + timedelta(days=1)
        start, _ = local_day_bounds(first, tz)
        end, _ = local_day_bounds(after, tz)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=422, detail="月份格式应为 YYYY-MM。") from None

    days = {}
    completions: dict[str, set[str]] = {}
    for number in range(1, last.day + 1):
        key = date(year, first.month, number).isoformat()
        days[key] = {"date": key, "reviews": 0, "completed_cards": 0,
                     "study_time_ms": 0, "first_total": 0, "first_correct": 0,
                     "timed_reviews": 0}
        completions[key] = set()

    with connect() as conn:
        # The existing (user_id, review_time) index bounds this read to one month.
        # Keep history independent of course selection and muted words.
        rows = conn.execute("""SELECT id, review_time, attempt_id, is_correct,
            is_first_attempt, active_response_ms FROM review_history
            WHERE user_id=? AND review_time>=? AND review_time<?""",
            (int(user["id"]), start, end))
        for row in rows:
            key = datetime.fromisoformat(row["review_time"]).astimezone(tz).date().isoformat()
            day = days[key]
            day["reviews"] += 1
            if row["is_correct"] == 1:
                attempt = row["attempt_id"]
                completions[key].add(attempt if attempt is not None else f"legacy:{row['id']}")
            if row["active_response_ms"] is not None:
                day["timed_reviews"] += 1
                day["study_time_ms"] += row["active_response_ms"]
            if row["is_first_attempt"] == 1:
                day["first_total"] += 1
                day["first_correct"] += int(row["is_correct"] == 1)

    result = []
    for key, day in days.items():
        result.append({
            "date": key, "reviews": day["reviews"],
            "completed_cards": len(completions[key]),
            "study_time_ms": day["study_time_ms"] if day["timed_reviews"] or not day["reviews"] else None,
            "first_attempt_accuracy": round(day["first_correct"] / day["first_total"] * 100)
            if day["first_total"] else None,
        })
    return {"month": month, "today": today.isoformat(), "days": result,
            "summary": {"learning_days": sum(day["reviews"] > 0 for day in result),
                        "completed_cards": sum(day["completed_cards"] for day in result)}}
