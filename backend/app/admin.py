"""Account overview and content maintenance, accessible only to administrators."""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import get_admin_user
from .content_overrides import store_override
from .database import connect
from .senses import contains_target
from .security import local_day_bounds, resolve_timezone, today_in, utc_now_iso

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


@router.get("/users/{user_id}")
def user_detail(user_id: int, user: dict[str, Any] = Depends(get_admin_user)) -> dict[str, Any]:
    with connect() as conn:
        learner = conn.execute("""SELECT id,username,timezone,created_at FROM users
            WHERE id=? AND role='learner'""", (user_id,)).fetchone()
        if learner is None:
            raise HTTPException(404, "用户不存在")
        tz = resolve_timezone(learner["timezone"])
        start, end = local_day_bounds(today_in(tz), tz)
        activity = conn.execute("""SELECT COUNT(*) AS reviews, COALESCE(SUM(is_correct),0) AS correct_reviews,
            COUNT(CASE WHEN review_time >= ? AND review_time < ? THEN 1 END) AS today_reviews,
            MAX(review_time) AS last_review_at FROM review_history WHERE user_id=?""", (start, end, user_id)).fetchone()
        states = {row["status"]: row["total"] for row in conn.execute("""SELECT status,COUNT(*) AS total
            FROM sense_srs_state WHERE user_id=? AND retired_at IS NULL GROUP BY status""", (user_id,))}
        learned_words = conn.execute("""SELECT COUNT(DISTINCT word_id) FROM (
            SELECT word_id FROM review_history WHERE user_id=?
            UNION SELECT s.word_id FROM study_attempts a JOIN word_senses s ON s.id=a.sense_id WHERE a.user_id=?
            UNION SELECT s.word_id FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id WHERE p.user_id=?)""",
            (user_id, user_id, user_id)).fetchone()[0]
        lists = conn.execute("""SELECT l.list_id,l.title FROM user_vocabulary_lists u
            JOIN vocabulary_lists l ON l.list_id=u.list_id WHERE u.user_id=? AND u.selected=1
            ORDER BY l.sort_order,l.list_id""", (user_id,)).fetchall()
        return {"user": dict(learner), "activity": {**dict(activity), "learned_words": learned_words,
            "learning_senses": states.get("Learning", 0), "reviewing_senses": states.get("Reviewing", 0),
            "mature_senses": states.get("Mature", 0),
            "muted_words": conn.execute("SELECT COUNT(*) FROM user_muted_words WHERE user_id=?", (user_id,)).fetchone()[0],
            "passkeys": conn.execute("SELECT COUNT(*) FROM passkeys WHERE user_id=?", (user_id,)).fetchone()[0]},
            "word_lists": [dict(row) for row in lists]}


@router.get("/reports")
def reports(
    status: Literal["pending", "resolved"] = "pending",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: dict[str, Any] = Depends(get_admin_user),
) -> dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM content_reports WHERE status=?", (status,)).fetchone()[0]
        rows = conn.execute("""SELECT r.id,r.category,r.details,r.created_at,r.status,u.username,
            w.word FROM content_reports r JOIN users u ON u.id=r.user_id JOIN words w ON w.id=r.word_id
            WHERE r.status=? ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?""", (status, limit, offset)).fetchall()
    return {"reports": [dict(row) for row in rows], "total": total, "offset": offset, "limit": limit}


def report_detail_from(conn, report_id: int) -> dict[str, Any]:
    row = conn.execute("""SELECT r.id,r.user_id,r.category,r.details,r.created_at,r.status,
        r.content_snapshot,r.resolution_notes,r.resolved_at,u.username,handler.username AS resolved_by
        FROM content_reports r JOIN users u ON u.id=r.user_id
        LEFT JOIN users handler ON handler.id=r.resolved_by WHERE r.id=?""", (report_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "反馈不存在")
    result = dict(row)
    content = json.loads(result.pop("content_snapshot"))
    result["content"] = {key: content.get(key) for key in (
        "word", "part_of_speech", "definition_cn", "definition_en", "sentence", "translation_cn", "target_form", "pronunciation"
    )}
    current = conn.execute("""SELECT w.word,w.pronunciation,s.part_of_speech,s.definition_cn,s.definition_en,
        e.sentence,e.translation_cn,e.target_form,s.active AS sense_active,e.active AS example_active
        FROM content_reports r JOIN words w ON w.id=r.word_id JOIN word_senses s ON s.id=r.sense_id
        JOIN sense_examples e ON e.id=r.example_id AND e.sense_id=s.id WHERE r.id=?""", (report_id,)).fetchone()
    result['editable'] = bool(current and current['sense_active'] and current['example_active'])
    result['current_content'] = {key: current[key] for key in result['content']} if current else None
    result['edits'] = [{**dict(row), 'before': json.loads(row['before_json']), 'after': json.loads(row['after_json'])}
        for row in conn.execute("""SELECT e.id,e.created_at,e.before_json,e.after_json,u.username
            FROM admin_content_edits e JOIN users u ON u.id=e.admin_id WHERE e.report_id=? ORDER BY e.id DESC LIMIT 10""", (report_id,))]
    for edit in result['edits']:
        edit.pop('before_json')
        edit.pop('after_json')
    return result


@router.get("/reports/{report_id}")
def report_detail(report_id: int, user: dict[str, Any] = Depends(get_admin_user)) -> dict[str, Any]:
    with connect() as conn:
        return report_detail_from(conn, report_id)


class ReportUpdate(BaseModel):
    status: Literal["pending", "resolved"]
    resolution_notes: str | None = Field(default=None, max_length=2000)


@router.patch("/reports/{report_id}")
def update_report(report_id: int, payload: ReportUpdate, user: dict[str, Any] = Depends(get_admin_user)) -> dict[str, Any]:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = report_detail_from(conn, report_id)
        notes = current["resolution_notes"] if payload.resolution_notes is None else payload.resolution_notes.strip()
        if payload.status == "resolved":
            # Repeated submissions preserve the original resolution time.
            resolved_at = current["resolved_at"] if current["status"] == "resolved" else utc_now_iso()
            conn.execute("""UPDATE content_reports SET status='resolved',resolution_notes=?,resolved_at=?,resolved_by=? WHERE id=?""",
                (notes, resolved_at, user["id"], report_id))
        else:
            conn.execute("""UPDATE content_reports SET status='pending',resolution_notes=?,resolved_at=NULL,resolved_by=NULL WHERE id=?""",
                (notes, report_id))
        return report_detail_from(conn, report_id)


class ContentChanges(BaseModel):
    definition_cn: str | None = Field(default=None, max_length=1000)
    definition_en: str | None = Field(default=None, max_length=2000)
    sentence: str | None = Field(default=None, max_length=2000)
    translation_cn: str | None = Field(default=None, max_length=2000)
    target_form: str | None = Field(default=None, max_length=200)
    pronunciation: str | None = Field(default=None, max_length=200)


class ContentUpdate(BaseModel):
    expected_content: dict[str, str | None]
    changes: ContentChanges
    resolution_notes: str = Field(default='', max_length=2000)


@router.patch('/reports/{report_id}/content')
def correct_report_content(report_id: int, payload: ContentUpdate, user: dict[str, Any] = Depends(get_admin_user)) -> dict[str, Any]:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        report = report_detail_from(conn, report_id)
        if not report['editable']:
            raise HTTPException(409, '题目已归档，无法修改')
        before = report['current_content']
        if before != payload.expected_content:
            raise HTTPException(409, '内容已被更新，请刷新后再修改')
        changes = {key: (value or '').strip() for key, value in payload.changes.model_dump(exclude_unset=True).items()}
        for key in ('definition_en', 'translation_cn', 'pronunciation'):
            if key in changes and not changes[key]:
                changes[key] = None
        changes = {key: value for key, value in changes.items() if before[key] != value}
        if not changes:
            raise HTTPException(400, '没有修改内容')
        after = {**before, **changes}
        if not (after['definition_cn'] or after['definition_en']):
            raise HTTPException(400, '至少保留一项释义')
        if not after['sentence'] or not after['target_form'] or not contains_target(after['sentence'], after['target_form']):
            raise HTTPException(400, '例句必须包含正确的答案词形')
        pair = conn.execute('SELECT word_id,sense_id,example_id FROM content_reports WHERE id=?', (report_id,)).fetchone()
        if conn.execute('SELECT 1 FROM sense_examples WHERE sense_id=? AND sentence=? AND id!=?',
                (pair['sense_id'], after['sentence'], pair['example_id'])).fetchone():
            raise HTTPException(409, '此义项已存在相同例句')
        sense_changes = {key: changes[key] for key in ('definition_cn','definition_en') if key in changes}
        example_changes = {key: changes[key] for key in ('sentence','translation_cn','target_form') if key in changes}
        if sense_changes:
            store_override(conn, 'sense', pair['sense_id'], {key: before[key] for key in ('definition_cn','definition_en')}, sense_changes, user['id'])
            conn.execute('UPDATE word_senses SET definition_cn=?,definition_en=? WHERE id=?', (after['definition_cn'], after['definition_en'], pair['sense_id']))
        if example_changes:
            fields = ('sentence','translation_cn','target_form')
            store_override(conn, 'example', pair['example_id'], {key: before[key] for key in fields}, {key: after[key] for key in fields}, user['id'])
            conn.execute('UPDATE sense_examples SET sentence=?,translation_cn=?,target_form=? WHERE id=?',
                (after['sentence'], after['translation_cn'], after['target_form'], pair['example_id']))
        word_changes = {key: changes[key] for key in ('pronunciation',) if key in changes}
        word = dict(conn.execute('SELECT * FROM words WHERE id=?', (pair['word_id'],)).fetchone())
        for key in sense_changes:
            if word[key] == before[key]:
                word_changes[key] = after[key]
        store_override(conn, 'word', pair['word_id'], {key: word[key] for key in ('pronunciation','definition_cn','definition_en')}, word_changes, user['id'])
        for key, value in word_changes.items():
            conn.execute(f'UPDATE words SET {key}=? WHERE id=?', (value, pair['word_id']))
        now = utc_now_iso()
        if sense_changes:
            conn.execute('UPDATE study_attempts SET completed_at=? WHERE sense_id=? AND completed_at IS NULL', (now, pair['sense_id']))
        elif example_changes:
            conn.execute('UPDATE study_attempts SET completed_at=? WHERE example_id=? AND completed_at IS NULL', (now, pair['example_id']))
        conn.execute('INSERT INTO admin_content_edits(report_id,admin_id,before_json,after_json,created_at) VALUES(?,?,?,?,?)',
            (report_id, user['id'], json.dumps(before,ensure_ascii=False), json.dumps(after,ensure_ascii=False), now))
        conn.execute("UPDATE content_reports SET status='resolved',resolution_notes=?,resolved_at=?,resolved_by=? WHERE id=?",
            (payload.resolution_notes.strip(), now, user['id'], report_id))
        return report_detail_from(conn, report_id)
