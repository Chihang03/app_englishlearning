"""Read-only account and service overview, accessible only to administrators."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from .auth import get_admin_user
from .database import connect
from .security import local_day_bounds, resolve_timezone, today_in

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/overview")
def overview(user: dict[str, Any] = Depends(get_admin_user)) -> dict[str, int]:
    tz = resolve_timezone(user["timezone"])
    start, end = local_day_bounds(today_in(tz), tz)
    with connect() as conn:
        activity = conn.execute("""SELECT COUNT(*) AS reviews,
            COUNT(CASE WHEN r.review_time >= ? AND r.review_time < ? THEN 1 END) AS today_reviews,
            COUNT(DISTINCT CASE WHEN r.review_time >= ? AND r.review_time < ? THEN r.user_id END) AS today_active_users
            FROM review_history r JOIN users u ON u.id=r.user_id WHERE u.role='learner'""",
            (start, end, start, end)).fetchone()
        return {
            "users": conn.execute("SELECT COUNT(*) FROM users WHERE role='learner'").fetchone()[0],
            **dict(activity),
            "public_words": conn.execute("""SELECT COUNT(*) FROM words w WHERE w.owner_id IS NULL
                AND EXISTS(SELECT 1 FROM word_senses s JOIN sense_examples e ON e.sense_id=s.id
                    WHERE s.word_id=w.id AND s.active=1 AND e.active=1)""").fetchone()[0],
            "pending_reports": conn.execute("SELECT COUNT(*) FROM content_reports WHERE status='pending'").fetchone()[0],
        }


@router.get("/users")
def users(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: dict[str, Any] = Depends(get_admin_user),
) -> dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users WHERE role='learner'").fetchone()[0]
        rows = conn.execute("""SELECT u.id, u.username, u.timezone, u.created_at,
            COUNT(r.id) AS reviews, MAX(r.review_time) AS last_review_at
            FROM users u LEFT JOIN review_history r ON r.user_id=u.id
            WHERE u.role='learner' GROUP BY u.id ORDER BY u.id DESC LIMIT ? OFFSET ?""",
            (limit, offset)).fetchall()
    return {"users": [dict(row) for row in rows], "total": total, "offset": offset, "limit": limit}
