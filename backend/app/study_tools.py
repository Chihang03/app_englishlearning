from __future__ import annotations

import json

from fastapi import HTTPException

from .security import utc_now_iso
from .sense_learning import record_related_exposure
from .senses import senses_for_word


def attempt_content(conn, user_id: int, attempt_id: str):
    row = conn.execute("""SELECT a.id AS attempt_id,a.completed_at,a.sense_id,a.example_id,
        w.id AS word_id,w.word,w.pronunciation,s.part_of_speech,s.definition_cn,s.definition_en,
        e.sentence,e.translation_cn,e.target_form
        FROM study_attempts a JOIN word_senses s ON s.id=a.sense_id
        JOIN words w ON w.id=s.word_id JOIN sense_examples e ON e.id=a.example_id AND e.sense_id=s.id
        WHERE a.id=? AND a.user_id=? AND (w.owner_id IS NULL OR w.owner_id=?)""",
        (attempt_id, user_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="找不到这道题目，请重新进入学习。")
    return row


def word_meanings(conn, user_id: int, attempt_id: str):
    # Record exposure before returning any answer-bearing definitions/examples.
    record_related_exposure(conn, user_id, attempt_id)
    content = attempt_content(conn, user_id, attempt_id)
    if content["completed_at"] is None:
        conn.execute("UPDATE study_attempts SET answer_exposed=1 WHERE id=?", (attempt_id,))
    return {"word": content["word"], "senses": senses_for_word(conn, content["word_id"], user_id)}


def report_content(conn, user_id: int, attempt_id: str, category: str, details: str):
    conn.execute("BEGIN IMMEDIATE")
    content = attempt_content(conn, user_id, attempt_id)
    conn.execute("""INSERT INTO content_reports(user_id,attempt_id,word_id,sense_id,example_id,
        category,details,content_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,attempt_id,category) DO NOTHING""",
        (user_id, attempt_id, content["word_id"], content["sense_id"], content["example_id"],
         category, details.strip(), json.dumps(dict(content), ensure_ascii=False), utc_now_iso()))
    report = conn.execute("SELECT id FROM content_reports WHERE user_id=? AND attempt_id=? AND category=?",
                          (user_id, attempt_id, category)).fetchone()
    return {"id": report["id"], "status": "received"}
