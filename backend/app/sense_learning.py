from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import secrets
from typing import Any

from fastapi import HTTPException

from .security import local_day_bounds, utc_iso_from, utc_now_iso
from .senses import contains_target, example_for_sense, senses_for_word, write_state
from .srs import RELEARNING_DELAY_SECONDS, next_state
from .adaptive_memory import advance_memory, maybe_calibrate, memory_summary
from .learning_filters import learnable_word_sql
from .word_forms import answer_form_label


# Both counters and card selection use the same due-time and visibility rules.
# A queued correction's timestamp replaces its old calendar-day due date.
_DUE_SENSES_SQL = f"""SELECT p.*,s.word_id,q.ready_at,q.queue_order,
    q.sense_id IS NOT NULL AS is_relearning
    FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id
    JOIN words w ON w.id=s.word_id
    LEFT JOIN relearning_queue q ON q.user_id=p.user_id AND q.sense_id=p.sense_id
    LEFT JOIN adaptive_memory m ON m.user_id=p.user_id AND m.sense_id=p.sense_id
    WHERE p.user_id=? AND s.active=1 AND s.learning_enabled=1 AND (w.owner_id IS NULL OR w.owner_id=?)
      AND {learnable_word_sql('p.user_id')}
      AND (p.status!='Mature' OR m.sense_id IS NOT NULL)
      AND ((q.sense_id IS NULL AND p.next_review_date<=?) OR q.ready_at<=?)
      AND EXISTS(SELECT 1 FROM sense_examples e WHERE e.sense_id=s.id AND e.active=1)"""


def selected_filter(user_id: int) -> tuple[str, tuple[int, int, int, int]]:
    return f"""(w.owner_id=? OR (w.owner_id IS NULL AND EXISTS (
        SELECT 1 FROM word_list_memberships m JOIN user_vocabulary_lists u ON u.list_id=m.list_id
        WHERE m.word_id=w.id AND u.user_id=? AND u.selected=1)))
        AND {learnable_word_sql('?')}""", (user_id,user_id,user_id,user_id)


def learning_metrics(conn, user_id: int, today: date) -> dict[str, Any]:
    now = utc_now_iso()
    visible, args = selected_filter(user_id)
    new = conn.execute(f"""SELECT COUNT(DISTINCT w.id) AS words,COUNT(*) AS senses
        FROM word_senses s JOIN words w ON w.id=s.word_id
        LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=?
        WHERE s.active=1 AND s.learning_enabled=1 AND p.sense_id IS NULL AND {visible}
          AND EXISTS(SELECT 1 FROM sense_examples e WHERE e.sense_id=s.id AND e.active=1)
    """, (user_id,*args)).fetchone()
    new_words = conn.execute(f"""SELECT COUNT(*) FROM words w WHERE {visible}
        AND EXISTS(SELECT 1 FROM word_senses s WHERE s.word_id=w.id AND s.active=1 AND s.learning_enabled=1)
        AND NOT EXISTS(SELECT 1 FROM srs_state l WHERE l.word_id=w.id AND l.user_id=?)
        AND NOT EXISTS(SELECT 1 FROM word_senses s JOIN sense_srs_state p ON p.sense_id=s.id
            WHERE s.word_id=w.id AND s.learning_enabled=1 AND p.user_id=?)""", (*args,user_id,user_id)).fetchone()[0]
    learned = conn.execute("""SELECT COUNT(*) FROM (
        SELECT word_id FROM srs_state WHERE user_id=? UNION
        SELECT s.word_id FROM word_senses s JOIN sense_srs_state p ON p.sense_id=s.id
            WHERE p.user_id=? AND s.learning_enabled=1
    )""", (user_id,user_id)).fetchone()[0]
    progress = conn.execute(f"""SELECT COUNT(*) AS learned_senses,
        COUNT(DISTINCT CASE WHEN p.status='Learning' AND {learnable_word_sql('p.user_id')} THEN s.word_id END) AS learning,
        COUNT(CASE WHEN p.status IN ('Reviewing','Mature') THEN 1 END) AS mastered_senses,
        COUNT(DISTINCT CASE WHEN p.wrong_count>0 AND p.status!='Mature' AND {learnable_word_sql('p.user_id')} THEN s.word_id END) AS lapse_words
        FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id
        JOIN words w ON w.id=s.word_id
        WHERE p.user_id=? AND s.active=1 AND s.learning_enabled=1""", (user_id,)).fetchone()
    due = conn.execute(f"""SELECT COUNT(*) AS due_senses,
        COUNT(DISTINCT CASE WHEN status='Learning' THEN word_id END) AS learning_due,
        COUNT(DISTINCT CASE WHEN wrong_count>0 THEN word_id END) AS due_lapses,
        COUNT(DISTINCT CASE WHEN is_relearning THEN word_id END) AS pending_relearning,
        COUNT(CASE WHEN is_relearning THEN 1 END) AS pending_relearning_senses
        FROM ({_DUE_SENSES_SQL})""", (user_id,user_id,today.isoformat(),now)).fetchone()
    complete = conn.execute("""SELECT
        COUNT(CASE WHEN learned=total AND established=total THEN 1 END) AS mastered,
        COUNT(CASE WHEN learned=total AND mature=total THEN 1 END) AS mature
        FROM (SELECT s.word_id,COUNT(*) AS total,COUNT(p.sense_id) AS learned,
            SUM(CASE WHEN p.status IN ('Reviewing','Mature') THEN 1 ELSE 0 END) AS established,
            SUM(CASE WHEN p.status='Mature' THEN 1 ELSE 0 END) AS mature
            FROM word_senses s LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=?
            WHERE s.active=1 AND s.learning_enabled=1 GROUP BY s.word_id)""", (user_id,)).fetchone()
    legacy = conn.execute("SELECT COUNT(*) FROM srs_state WHERE user_id=? AND sense_migrated=0", (user_id,)).fetchone()[0]
    upcoming = conn.execute(f"""SELECT MIN(q.ready_at) AS next_relearning_at
        FROM relearning_queue q JOIN word_senses s ON s.id=q.sense_id
        JOIN words w ON w.id=s.word_id
        JOIN sense_srs_state p ON p.user_id=q.user_id AND p.sense_id=q.sense_id
        WHERE q.user_id=? AND s.active=1 AND s.learning_enabled=1 AND p.status!='Mature' AND q.ready_at>?
        AND (w.owner_id IS NULL OR w.owner_id=?)
        AND {learnable_word_sql('q.user_id')}
        AND EXISTS(SELECT 1 FROM sense_examples e WHERE e.sense_id=s.id AND e.active=1)""",
        (user_id,now,user_id)).fetchone()
    return {"total_learned": learned, "new_words": new_words, "new_senses": new["senses"],
            "words_with_new_senses": new["words"], "due_review": due["due_senses"],
            **dict(progress), **dict(due), **dict(complete), "legacy_unmapped_words": legacy,
            "next_relearning_at": upcoming["next_relearning_at"],
            "memory_model": memory_summary(conn,user_id)}


def make_card(conn, sense, user_id: int, remaining_today: int, attempt) -> dict[str, Any]:
    example = conn.execute("SELECT * FROM sense_examples WHERE id=?", (attempt["example_id"],)).fetchone()
    if example is None:
        raise HTTPException(status_code=409, detail="This sense has no usable example")
    import re
    cloze = re.sub(rf"(?<![A-Za-z'-]){re.escape(example['target_form'])}(?![A-Za-z'-])", "_______",example["sentence"],flags=re.I)
    started = conn.execute("""SELECT 1 FROM srs_state WHERE user_id=? AND word_id=? UNION ALL
        SELECT 1 FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id WHERE p.user_id=? AND s.word_id=? LIMIT 1""",
        (user_id,sense["word_id"],user_id,sense["word_id"])).fetchone()
    memory = conn.execute("SELECT known_candidate,confirmations FROM adaptive_memory WHERE user_id=? AND sense_id=?",
                          (user_id,sense["id"])).fetchone()
    return {"id": sense["word_id"], "word": sense["word"], "sense_id": sense["id"],
            "example_id": example["id"], "part_of_speech": sense["part_of_speech"],
            "definition_cn": sense["definition_cn"], "definition_en": sense["definition_en"],
            "cloze_sentence": cloze, "example_sentence": example["sentence"],
            "example_translation_cn": example["translation_cn"], "answer_form": example["target_form"],
            "answer_form_label": answer_form_label(sense["word"],example["target_form"],sense["part_of_speech"],example["sentence"]),
            "status": sense["status"], "is_new_word": started is None, "remaining_today": remaining_today,
            "attempt_id": attempt["id"], "needs_correction": bool(attempt["hint_used"]),
            "pronunciation_used": bool(attempt["pronunciation_used"]),
            "answer_exposed": bool(attempt["answer_exposed"]),
            "known_candidate": bool(memory["known_candidate"]) if memory else False,
            "confirmations": memory["confirmations"] if memory else 0,
            "is_relearning": conn.execute("SELECT 1 FROM relearning_queue WHERE user_id=? AND sense_id=?",
                (user_id,sense["id"])).fetchone() is not None}


def start_attempt(conn, user_id: int, sense_id: int, example_id: int):
    attempt_id = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO study_attempts(id,user_id,sense_id,example_id,created_at,tracking_version) VALUES(?,?,?,?,?,1)",
                 (attempt_id,user_id,sense_id,example_id,utc_now_iso()))
    cutoff = utc_iso_from(datetime.now(timezone.utc)-timedelta(seconds=RELEARNING_DELAY_SECONDS))
    if conn.execute("SELECT 1 FROM sense_exposures WHERE user_id=? AND sense_id=? AND exposed_at>=?",
                    (user_id,sense_id,cutoff)).fetchone():
        conn.execute("UPDATE study_attempts SET answer_exposed=1 WHERE id=?",(attempt_id,))
    return conn.execute("SELECT * FROM study_attempts WHERE id=?", (attempt_id,)).fetchone()


def next_sense_card(conn, user_id: int, today: date) -> dict[str, Any]:
    # Issuing a round and recording its answer must serialize across devices.
    conn.execute("BEGIN IMMEDIATE")
    now = utc_now_iso()
    due_args = (user_id,user_id,today.isoformat(),now)
    # A refresh resumes the same round, including its revealed-answer flag.
    active = conn.execute(f"""SELECT a.* FROM study_attempts a
        JOIN word_senses s ON s.id=a.sense_id JOIN words w ON w.id=s.word_id
        JOIN sense_examples e ON e.id=a.example_id
        WHERE a.user_id=? AND a.completed_at IS NULL AND s.active=1 AND s.learning_enabled=1 AND e.active=1
        AND (w.owner_id IS NULL OR w.owner_id=?) AND {learnable_word_sql('a.user_id')}
        ORDER BY a.created_at,a.id LIMIT 1""",
        (user_id,user_id)).fetchone()
    # Catalog updates can archive a round; retire it so it cannot block a replacement.
    conn.execute("""UPDATE study_attempts SET completed_at=? WHERE user_id=? AND completed_at IS NULL
        AND (sense_id IN (SELECT id FROM word_senses WHERE active=0 OR learning_enabled=0)
          OR example_id IN (SELECT id FROM sense_examples WHERE active=0))""", (utc_now_iso(),user_id))
    remaining = conn.execute(f"SELECT COUNT(*) FROM ({_DUE_SENSES_SQL})", due_args).fetchone()[0]
    if active is not None:
        row = conn.execute("""SELECT s.*,w.word,COALESCE(p.status,'New') AS status
            FROM word_senses s JOIN words w ON w.id=s.word_id
            LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=? WHERE s.id=?""",
            (user_id,active["sense_id"])).fetchone()
        return {"card": make_card(conn,row,user_id,remaining,active)}
    row = conn.execute(f"""SELECT s.*,w.word,p.status FROM ({_DUE_SENSES_SQL}) p
        JOIN word_senses s ON s.id=p.sense_id JOIN words w ON w.id=s.word_id
        ORDER BY p.is_relearning,
          CASE WHEN p.is_relearning=0 THEN p.next_review_date END,
          CASE WHEN p.is_relearning=0 THEN p.lapse_count END DESC,
          p.queue_order,p.ready_at,s.id LIMIT 1
    """, due_args).fetchone()
    retry_after = 0
    if row is None:
        pending = conn.execute(f"""SELECT MIN(q.ready_at) AS ready_at FROM relearning_queue q
            JOIN word_senses s ON s.id=q.sense_id JOIN words w ON w.id=s.word_id
            JOIN sense_srs_state p ON p.sense_id=q.sense_id AND p.user_id=q.user_id
            WHERE q.user_id=? AND s.active=1 AND s.learning_enabled=1 AND p.status!='Mature' AND q.ready_at>?
            AND (w.owner_id IS NULL OR w.owner_id=?)
            AND {learnable_word_sql('q.user_id')}
            AND EXISTS(SELECT 1 FROM sense_examples e WHERE e.sense_id=s.id AND e.active=1)
            """, (user_id,now,user_id)).fetchone()
        if pending["ready_at"] is not None:
            retry_after = max(1,math.ceil((datetime.fromisoformat(pending["ready_at"]) - datetime.now(timezone.utc)).total_seconds()))
    if row is None:
        visible, args = selected_filter(user_id)
        row = conn.execute(f"""SELECT s.*,w.word,'New' AS status FROM word_senses s
            JOIN words w ON w.id=s.word_id LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=?
            WHERE s.active=1 AND s.learning_enabled=1 AND p.sense_id IS NULL AND {visible}
              AND EXISTS(SELECT 1 FROM sense_examples e WHERE e.sense_id=s.id AND e.active=1)
            ORDER BY CASE WHEN EXISTS(SELECT 1 FROM srs_state l WHERE l.word_id=w.id AND l.user_id=?)
              OR EXISTS(SELECT 1 FROM sense_srs_state other JOIN word_senses os ON os.id=other.sense_id
                WHERE os.word_id=w.id AND other.user_id=?) THEN 1 ELSE 0 END,s.position,w.id,s.id LIMIT 1
        """, (user_id,*args,user_id,user_id)).fetchone()
    if row is None:
        if retry_after > 0:
            return {"card": None, "retry_after_seconds": retry_after,
                    "message": "所选词库暂无新词或到期复习，可选择其他词库继续学习。"}
        return {"card": None, "message": "今天没有到期复习或未学习义项。"}
    example = example_for_sense(conn,row["id"],user_id)
    attempt = start_attempt(conn,user_id,row["id"],example["id"])
    return {"card": make_card(conn,row,user_id,remaining,attempt)}


def record_hint(conn, user_id: int, attempt_id: str, kind: str) -> dict[str, bool]:
    conn.execute("BEGIN IMMEDIATE")
    attempt = conn.execute("SELECT * FROM study_attempts WHERE id=? AND user_id=? AND completed_at IS NULL",
                           (attempt_id,user_id)).fetchone()
    if attempt is None:
        raise HTTPException(status_code=409,detail="本轮题目已结束，请加载下一题。")
    column = "pronunciation_used" if kind == "pronunciation" else "answer_exposed"
    conn.execute(f"UPDATE study_attempts SET {column}=1 WHERE id=?",(attempt_id,))
    return {"recorded": True}


def record_related_exposure(conn, user_id: int, attempt_id: str) -> dict[str, bool]:
    conn.execute("BEGIN IMMEDIATE")
    source = conn.execute("""SELECT s.word_id,s.id FROM study_attempts a JOIN word_senses s ON s.id=a.sense_id
        WHERE a.id=? AND a.user_id=?""",(attempt_id,user_id)).fetchone()
    if source is None:
        raise HTTPException(status_code=409,detail="题目不匹配，请加载下一题。")
    now = utc_now_iso()
    conn.execute("""INSERT INTO sense_exposures(user_id,sense_id,exposed_at)
        SELECT ?,id,? FROM word_senses WHERE word_id=? AND id!=? AND active=1
        ON CONFLICT(user_id,sense_id) DO UPDATE SET exposed_at=excluded.exposed_at""",
        (user_id,now,source["word_id"],source["id"]))
    conn.execute("""UPDATE study_attempts SET answer_exposed=1 WHERE user_id=? AND completed_at IS NULL
        AND sense_id IN (SELECT id FROM word_senses WHERE word_id=? AND id!=?)""",
        (user_id,source["word_id"],source["id"]))
    return {"recorded": True}


def record_sense_review(conn, user_id: int, today: date, word_id: int, sense_id: int | None,
                        example_id: int | None, answer: str, attempt_id: str | None = None,
                        learner_timezone=timezone.utc, active_response_ms: int | None = None) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    if (sense_id is None) != (example_id is None):
        raise HTTPException(status_code=422,detail="Submit both sense_id and example_id")
    if sense_id is None:
        pairs = conn.execute("""SELECT s.id AS sense_id,e.id AS example_id FROM word_senses s
            JOIN sense_examples e ON e.sense_id=s.id JOIN words w ON w.id=s.word_id
            WHERE w.id=? AND s.active=1 AND s.learning_enabled=1 AND e.active=1 AND (w.owner_id IS NULL OR w.owner_id=?)""", (word_id,user_id)).fetchall()
        if len(pairs) != 1:
            raise HTTPException(status_code=422,detail="Select a specific sense_id and example_id")
        sense_id,example_id = pairs[0]["sense_id"],pairs[0]["example_id"]
    row = conn.execute("""SELECT e.*,w.word,s.definition_cn,s.definition_en FROM sense_examples e
        JOIN word_senses s ON s.id=e.sense_id JOIN words w ON w.id=s.word_id
        WHERE e.id=? AND s.id=? AND w.id=? AND s.active=1 AND s.learning_enabled=1 AND e.active=1
            AND (w.owner_id IS NULL OR w.owner_id=?)""", (example_id,sense_id,word_id,user_id)).fetchone()
    if row is None or not contains_target(row["sentence"],row["target_form"]):
        raise HTTPException(status_code=404,detail="Sense/example pair not found")
    if conn.execute("SELECT 1 FROM user_muted_words WHERE user_id=? AND word=lower(trim(?))",
                    (user_id,row["word"])).fetchone():
        raise HTTPException(status_code=409,detail="此单词已设为不再学习，请加载下一题。")
    if not conn.execute(f"SELECT 1 FROM words w WHERE w.id=? AND {learnable_word_sql('?')}",
                        (word_id,user_id,user_id)).fetchone():
        raise HTTPException(status_code=409,detail="此单词已跳过，请加载下一题。")
    day_start,day_end = local_day_bounds(today,learner_timezone)
    if conn.execute("""SELECT 1 FROM review_history WHERE user_id=? AND sense_id=?
        AND is_independent=1 AND review_time>=? AND review_time<? LIMIT 1""",
        (user_id,sense_id,day_start,day_end)).fetchone():
        raise HTTPException(status_code=409,detail="这个义项今天已独立通过，请加载下一题。")
    if attempt_id is not None:
        attempt = conn.execute("""SELECT * FROM study_attempts WHERE id=? AND user_id=?
            AND sense_id=? AND example_id=? AND completed_at IS NULL""",
            (attempt_id,user_id,sense_id,example_id)).fetchone()
        if attempt is None:
            raise HTTPException(status_code=409,detail="本轮题目已结束或不匹配，请加载下一题。")
    else:
        # Compatibility clients still use the server's persisted round state.
        attempt = conn.execute("""SELECT * FROM study_attempts WHERE user_id=? AND sense_id=?
            AND completed_at IS NULL""", (user_id,sense_id)).fetchone()
        if attempt is not None and attempt["example_id"] != example_id:
            raise HTTPException(status_code=409,detail="请先完成当前例句的纠正。")
        if attempt is None:
            pending = conn.execute("SELECT ready_at FROM relearning_queue WHERE user_id=? AND sense_id=?",
                                   (user_id,sense_id)).fetchone()
            if pending and pending["ready_at"] > utc_now_iso():
                raise HTTPException(status_code=409,detail="待巩固词需要间隔后再独立作答。")
            scheduled = conn.execute("SELECT next_review_date,status FROM sense_srs_state WHERE user_id=? AND sense_id=?",
                                     (user_id,sense_id)).fetchone()
            if not pending and scheduled and (scheduled["next_review_date"] > today.isoformat() or scheduled["status"] == "Mature"):
                raise HTTPException(status_code=409,detail="这个义项尚未到复习时间，请加载下一题。")
            attempt = start_attempt(conn,user_id,sense_id,example_id)
    answer = answer.strip()
    correct = " ".join(answer.casefold().split()) == " ".join(row["target_form"].casefold().split())
    assisted = bool(attempt["pronunciation_used"] or attempt["answer_exposed"])
    independent = correct and not attempt["hint_used"] and not assisted
    now = utc_now_iso()
    current = conn.execute("SELECT * FROM sense_srs_state WHERE user_id=? AND sense_id=?", (user_id,sense_id)).fetchone()
    cutoff = utc_iso_from(datetime.now(timezone.utc)-timedelta(days=180))
    had_wrong = conn.execute("SELECT 1 FROM review_history WHERE user_id=? AND sense_id=? AND is_correct=0 AND review_time>=? LIMIT 1", (user_id,sense_id,cutoff)).fetchone() is not None
    memory = advance_memory(conn,user_id,sense_id,current,correct=correct,independent=independent,
                            practice=bool(attempt["hint_used"]) or (correct and not independent),
                            assisted=assisted,today=today,now=now,active_response_ms=active_response_ms,
                            allow_known_prior=attempt["tracking_version"] == 1)
    conn.execute("""INSERT INTO review_history(user_id,word_id,sense_id,example_id,review_time,user_answer,
        is_correct,attempt_id,is_independent,is_first_attempt,pronunciation_used,active_response_ms,
        base_recall_probability,predicted_recall_probability,memory_model_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id,word_id,sense_id,example_id,now,answer,int(correct),attempt["id"],int(independent),
         int(not attempt["hint_used"] and not assisted),int(attempt["pronunciation_used"]),active_response_ms,
         memory["base_recall_probability"],memory["predicted_recall_probability"],memory["model_version"]))
    if correct and not independent:
        # Copying a revealed answer is practice, not another successful recall.
        state = dict(current) if current else next_state(None,True,False,today)
        if current is None:
            state["review_count"] = 0
        state["review_count"] += 1
        if assisted:
            state.update(correct_count=0,interval_days=0,next_review_date=today.isoformat(),status="Learning")
    else:
        state = next_state(dict(current) if current else None,correct,had_wrong,today)
    if independent:
        state.update(interval_days=memory["interval_days"],next_review_date=memory["next_review_date"],
                     status="Mature" if memory["mature"] and not had_wrong else "Reviewing")
    # The scheduler can return identity fields from the input; expose only state.
    state = {k:v for k,v in state.items() if k not in ("user_id","sense_id","last_example_id")}
    write_state(conn,user_id,sense_id,state,example_id)
    if independent:
        conn.execute("DELETE FROM relearning_queue WHERE user_id=? AND sense_id=?", (user_id,sense_id))
    else:
        # Timestamps use whole seconds; round upward to guarantee the full delay.
        ready = utc_iso_from(datetime.now(timezone.utc)+timedelta(seconds=RELEARNING_DELAY_SECONDS+1))
        order = conn.execute("SELECT COALESCE(MAX(queue_order),0)+1 FROM relearning_queue WHERE user_id=?", (user_id,)).fetchone()[0]
        conn.execute("""INSERT INTO relearning_queue(user_id,sense_id,queued_at,ready_at,queue_order) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id,sense_id) DO UPDATE SET queued_at=excluded.queued_at,
                ready_at=excluded.ready_at,queue_order=excluded.queue_order""",
            (user_id,sense_id,now,ready,order))
    if correct:
        conn.execute("UPDATE study_attempts SET completed_at=? WHERE id=?", (now,attempt["id"]))
    else:
        conn.execute("UPDATE study_attempts SET hint_used=1 WHERE id=?", (attempt["id"],))
    maybe_calibrate(conn,user_id,now)
    return {"is_correct": correct,"is_independent": independent,"is_blank": not answer,
            "outcome": "independent" if independent else "assisted" if correct and assisted and not attempt["hint_used"] else "corrected" if correct else "incorrect",
            "memory": {k:v for k,v in memory.items() if k not in ("base_recall_probability","mature")},
            "correct_answer": row["target_form"],
            "word":row["word"],"sense_id":sense_id,"example_id":example_id,
            "example_sentence":row["sentence"],"definition_cn":row["definition_cn"],
            "definition_en":row["definition_en"],"srs_state":state,
            "other_senses":[s for s in senses_for_word(conn,word_id,user_id) if s["id"]!=sense_id]}
