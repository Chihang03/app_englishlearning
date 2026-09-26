from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from .security import utc_iso_from, utc_now_iso
from .senses import contains_target, example_for_sense, senses_for_word, write_state
from .srs import next_state


def selected_filter(user_id: int) -> tuple[str, tuple[int, int]]:
    return """(w.owner_id=? OR (w.owner_id IS NULL AND EXISTS (
        SELECT 1 FROM word_list_memberships m JOIN user_vocabulary_lists u ON u.list_id=m.list_id
        WHERE m.word_id=w.id AND u.user_id=? AND u.selected=1)))""", (user_id,user_id)


def learning_metrics(conn, user_id: int, today: date) -> dict[str, int]:
    visible, args = selected_filter(user_id)
    new = conn.execute(f"""SELECT COUNT(DISTINCT w.id) AS words,COUNT(*) AS senses
        FROM word_senses s JOIN words w ON w.id=s.word_id
        LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=?
        WHERE s.active=1 AND p.sense_id IS NULL AND {visible}
          AND EXISTS(SELECT 1 FROM sense_examples e WHERE e.sense_id=s.id AND e.active=1)
    """, (user_id,*args)).fetchone()
    new_words = conn.execute(f"""SELECT COUNT(*) FROM words w WHERE {visible}
        AND EXISTS(SELECT 1 FROM word_senses s WHERE s.word_id=w.id AND s.active=1)
        AND NOT EXISTS(SELECT 1 FROM srs_state l WHERE l.word_id=w.id AND l.user_id=?)
        AND NOT EXISTS(SELECT 1 FROM word_senses s JOIN sense_srs_state p ON p.sense_id=s.id
            WHERE s.word_id=w.id AND p.user_id=?)""", (*args,user_id,user_id)).fetchone()[0]
    learned = conn.execute("""SELECT COUNT(*) FROM (
        SELECT word_id FROM srs_state WHERE user_id=? UNION
        SELECT s.word_id FROM word_senses s JOIN sense_srs_state p ON p.sense_id=s.id WHERE p.user_id=?
    )""", (user_id,user_id)).fetchone()[0]
    progress = conn.execute("""SELECT COUNT(*) AS learned_senses,
        COUNT(DISTINCT CASE WHEN p.status='Learning' THEN s.word_id END) AS learning,
        COUNT(DISTINCT CASE WHEN p.status='Learning' AND p.next_review_date<=? THEN s.word_id END) AS learning_due,
        COUNT(CASE WHEN p.status!='Mature' AND p.next_review_date<=? THEN 1 END) AS due_senses,
        COUNT(CASE WHEN p.status IN ('Reviewing','Mature') THEN 1 END) AS mastered_senses,
        COUNT(DISTINCT CASE WHEN p.wrong_count>0 AND p.status!='Mature' THEN s.word_id END) AS lapse_words,
        COUNT(DISTINCT CASE WHEN p.wrong_count>0 AND p.status!='Mature' AND p.next_review_date<=? THEN s.word_id END) AS due_lapses
        FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id
        WHERE p.user_id=? AND s.active=1""", (today.isoformat(),today.isoformat(),today.isoformat(),user_id)).fetchone()
    complete = conn.execute("""SELECT
        COUNT(CASE WHEN learned=total AND established=total THEN 1 END) AS mastered,
        COUNT(CASE WHEN learned=total AND mature=total THEN 1 END) AS mature
        FROM (SELECT s.word_id,COUNT(*) AS total,COUNT(p.sense_id) AS learned,
            SUM(CASE WHEN p.status IN ('Reviewing','Mature') THEN 1 ELSE 0 END) AS established,
            SUM(CASE WHEN p.status='Mature' THEN 1 ELSE 0 END) AS mature
            FROM word_senses s LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=?
            WHERE s.active=1 GROUP BY s.word_id)""", (user_id,)).fetchone()
    legacy = conn.execute("SELECT COUNT(*) FROM srs_state WHERE user_id=? AND sense_migrated=0", (user_id,)).fetchone()[0]
    return {"total_learned": learned, "new_words": new_words, "new_senses": new["senses"],
            "words_with_new_senses": new["words"], "due_review": progress["due_senses"],
            **dict(progress), **dict(complete), "legacy_unmapped_words": legacy}


def make_card(conn, sense, user_id: int, remaining_today: int) -> dict[str, Any]:
    example = example_for_sense(conn, sense["id"], user_id)
    if example is None:
        raise HTTPException(status_code=409, detail="This sense has no usable example")
    import re
    cloze = re.sub(rf"(?<![A-Za-z'-]){re.escape(example['target_form'])}(?![A-Za-z'-])", "_______",example["sentence"],flags=re.I)
    started = conn.execute("""SELECT 1 FROM srs_state WHERE user_id=? AND word_id=? UNION ALL
        SELECT 1 FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id WHERE p.user_id=? AND s.word_id=? LIMIT 1""",
        (user_id,sense["word_id"],user_id,sense["word_id"])).fetchone()
    return {"id": sense["word_id"], "word": sense["word"], "sense_id": sense["id"],
            "example_id": example["id"], "part_of_speech": sense["part_of_speech"],
            "definition_cn": sense["definition_cn"], "definition_en": sense["definition_en"],
            "cloze_sentence": cloze, "example_sentence": example["sentence"],
            "example_translation_cn": example["translation_cn"], "answer_form": example["target_form"],
            "status": sense["status"], "is_new_word": started is None, "remaining_today": remaining_today}


def next_sense_card(conn, user_id: int, today: date) -> dict[str, Any]:
    remaining = conn.execute("""SELECT COUNT(*) FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id
        WHERE p.user_id=? AND s.active=1 AND p.status!='Mature' AND p.next_review_date<=?""",
        (user_id,today.isoformat())).fetchone()[0]
    row = conn.execute("""SELECT s.*,w.word,p.status FROM sense_srs_state p
        JOIN word_senses s ON s.id=p.sense_id JOIN words w ON w.id=s.word_id
        WHERE p.user_id=? AND s.active=1 AND (w.owner_id IS NULL OR w.owner_id=?)
          AND p.status!='Mature' AND p.next_review_date<=?
          AND EXISTS(SELECT 1 FROM sense_examples e WHERE e.sense_id=s.id AND e.active=1)
        ORDER BY CASE WHEN p.status='Learning' THEN 0 ELSE 1 END,p.next_review_date,p.lapse_count DESC,s.id LIMIT 1
    """, (user_id,user_id,today.isoformat())).fetchone()
    if row is None:
        visible, args = selected_filter(user_id)
        row = conn.execute(f"""SELECT s.*,w.word,'New' AS status FROM word_senses s
            JOIN words w ON w.id=s.word_id LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=?
            WHERE s.active=1 AND p.sense_id IS NULL AND {visible}
              AND EXISTS(SELECT 1 FROM sense_examples e WHERE e.sense_id=s.id AND e.active=1)
            ORDER BY CASE WHEN EXISTS(SELECT 1 FROM srs_state l WHERE l.word_id=w.id AND l.user_id=?)
              OR EXISTS(SELECT 1 FROM sense_srs_state other JOIN word_senses os ON os.id=other.sense_id
                WHERE os.word_id=w.id AND other.user_id=?) THEN 1 ELSE 0 END,s.position,w.id,s.id LIMIT 1
        """, (user_id,*args,user_id,user_id)).fetchone()
    if row is None:
        return {"card": None, "message": "今天没有到期复习或未学习义项。"}
    return {"card": make_card(conn,row,user_id,remaining)}


def record_sense_review(conn, user_id: int, today: date, word_id: int, sense_id: int | None,
                        example_id: int | None, answer: str) -> dict[str, Any]:
    if (sense_id is None) != (example_id is None):
        raise HTTPException(status_code=422,detail="Submit both sense_id and example_id")
    if sense_id is None:
        pairs = conn.execute("""SELECT s.id AS sense_id,e.id AS example_id FROM word_senses s
            JOIN sense_examples e ON e.sense_id=s.id JOIN words w ON w.id=s.word_id
            WHERE w.id=? AND s.active=1 AND e.active=1 AND (w.owner_id IS NULL OR w.owner_id=?)""", (word_id,user_id)).fetchall()
        if len(pairs) != 1:
            raise HTTPException(status_code=422,detail="Select a specific sense_id and example_id")
        sense_id,example_id = pairs[0]["sense_id"],pairs[0]["example_id"]
    row = conn.execute("""SELECT e.*,w.word,s.definition_cn,s.definition_en FROM sense_examples e
        JOIN word_senses s ON s.id=e.sense_id JOIN words w ON w.id=s.word_id
        WHERE e.id=? AND s.id=? AND w.id=? AND s.active=1 AND e.active=1
            AND (w.owner_id IS NULL OR w.owner_id=?)""", (example_id,sense_id,word_id,user_id)).fetchone()
    if row is None or not contains_target(row["sentence"],row["target_form"]):
        raise HTTPException(status_code=404,detail="Sense/example pair not found")
    answer = answer.strip()
    correct = " ".join(answer.casefold().split()) == " ".join(row["target_form"].casefold().split())
    conn.execute("""INSERT INTO review_history(user_id,word_id,sense_id,example_id,review_time,user_answer,is_correct)
        VALUES(?,?,?,?,?,?,?)""", (user_id,word_id,sense_id,example_id,utc_now_iso(),answer,int(correct)))
    current = conn.execute("SELECT * FROM sense_srs_state WHERE user_id=? AND sense_id=?", (user_id,sense_id)).fetchone()
    cutoff = utc_iso_from(datetime.now(timezone.utc)-timedelta(days=180))
    had_wrong = conn.execute("SELECT 1 FROM review_history WHERE user_id=? AND sense_id=? AND is_correct=0 AND review_time>=? LIMIT 1", (user_id,sense_id,cutoff)).fetchone() is not None
    state = next_state(dict(current) if current else None,correct,had_wrong,today)
    # The scheduler can return identity fields from the input; expose only state.
    state = {k:v for k,v in state.items() if k not in ("user_id","sense_id","last_example_id")}
    write_state(conn,user_id,sense_id,state,example_id)
    return {"is_correct": correct,"is_blank": not answer,"correct_answer": row["target_form"],
            "word":row["word"],"sense_id":sense_id,"example_id":example_id,
            "example_sentence":row["sentence"],"definition_cn":row["definition_cn"],
            "definition_en":row["definition_en"],"srs_state":state,
            "other_senses":[s for s in senses_for_word(conn,word_id,user_id) if s["id"]!=sense_id]}
