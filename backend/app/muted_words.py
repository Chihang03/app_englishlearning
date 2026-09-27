from __future__ import annotations

from fastapi import HTTPException

from .security import utc_now_iso


def unmuted_sql(user_expression: str) -> str:
    """Use only internal SQL expressions; callers provide the words alias w."""
    return f"""NOT EXISTS(SELECT 1 FROM user_muted_words muted
        WHERE muted.user_id={user_expression} AND muted.word=lower(trim(w.word)))"""


def mute_word(conn, user_id: int, word_id: int) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("""SELECT lower(trim(word)) AS word FROM words
        WHERE id=? AND (owner_id IS NULL OR owner_id=?)""", (word_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="单词不存在")
    word = row["word"]
    conn.execute("""INSERT INTO user_muted_words(user_id,word,muted_at) VALUES(?,?,?)
        ON CONFLICT(user_id,word) DO NOTHING""", (user_id, word, utc_now_iso()))
    # Retire rounds on every device without recording a review or losing SRS.
    conn.execute("""UPDATE study_attempts SET completed_at=?
        WHERE user_id=? AND completed_at IS NULL AND sense_id IN (
            SELECT s.id FROM word_senses s JOIN words w ON w.id=s.word_id
            WHERE lower(trim(w.word))=?)""", (utc_now_iso(), user_id, word))
    return {"word": word, "muted": True}


def restore_word(conn, user_id: int, word: str) -> dict:
    word = word.strip().lower()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM user_muted_words WHERE user_id=? AND word=?", (user_id, word))
    return {"word": word, "muted": False}
