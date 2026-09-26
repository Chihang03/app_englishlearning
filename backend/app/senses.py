"""Local sense/example storage, migration and independently scheduled cards."""
from __future__ import annotations

import hashlib
import re
from typing import Any


STATE_FIELDS = ("review_count", "correct_count", "wrong_count", "lapse_count",
                "easiness_factor", "interval_days", "next_review_date", "status")


def contains_target(sentence: str, target: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z'-]){re.escape(target)}(?![A-Za-z'-])", sentence, re.I))


def validate_senses(senses: list[dict[str, Any]]) -> None:
    if not senses:
        raise ValueError("Every learnable word needs paired senses and examples")
    keys = set()
    for sense in senses:
        key = sense["key"]
        if key in keys:
            raise ValueError(f"Duplicate sense key: {key}")
        keys.add(key)
        if not (str(sense.get("definition_cn") or "").strip() or str(sense.get("definition_en") or "").strip()):
            raise ValueError(f"Sense {key} has no definition")
        if not str(sense.get("part_of_speech") or "").strip() or not sense.get("examples"):
            raise ValueError(f"Sense {key} needs a part of speech and an example")
        for example in sense["examples"]:
            target = str(example.get("target_form") or "").strip()
            sentence = str(example.get("sentence") or "").strip()
            if not target or not sentence or not contains_target(sentence, target):
                raise ValueError(f"Sense {key} has an invalid target/example pair")


def save_senses(conn, word_id: int, senses: list[dict[str, Any]]) -> None:
    validate_senses(senses)
    # Keep identifiers and history when a catalog is refreshed. Removed senses
    # are archived instead of deleted, and stop contributing to active queues.
    conn.execute("UPDATE word_senses SET active = 0 WHERE word_id = ?", (word_id,))
    conn.execute("UPDATE sense_examples SET active = 0 WHERE sense_id IN (SELECT id FROM word_senses WHERE word_id = ?)", (word_id,))
    for position, sense in enumerate(senses):
        conn.execute("""
            INSERT INTO word_senses(word_id,sense_key,part_of_speech,definition_cn,definition_en,source,position,active)
            VALUES(?,?,?,?,?,?,?,1)
            ON CONFLICT(word_id,sense_key) DO UPDATE SET part_of_speech=excluded.part_of_speech,
                definition_cn=excluded.definition_cn,definition_en=excluded.definition_en,
                source=excluded.source,position=excluded.position,active=1
        """, (word_id, sense["key"], sense["part_of_speech"], sense.get("definition_cn") or "",
               sense.get("definition_en"), sense.get("source") or "本地词库", sense.get("position", position)))
        sense_id = conn.execute("SELECT id FROM word_senses WHERE word_id=? AND sense_key=?", (word_id,sense["key"])).fetchone()[0]
        for example in sense["examples"]:
            conn.execute("""
                INSERT INTO sense_examples(sense_id,sentence,translation_cn,target_form,source,active)
                VALUES(?,?,?,?,?,1) ON CONFLICT(sense_id,sentence) DO UPDATE SET
                    translation_cn=excluded.translation_cn,target_form=excluded.target_form,source=excluded.source,active=1
            """, (sense_id,example["sentence"],example.get("translation_cn"),example["target_form"],example.get("source") or sense.get("source") or "本地词库"))


def authored_sense(word: dict[str, Any], source: str = "用户词库") -> list[dict[str, Any]]:
    """An explicitly authored definition/example pair is one sense."""
    pos = {"adjective":"形容词","verb":"动词","noun":"名词","adverb":"副词",
           "n.":"名词","v.":"动词","adj.":"形容词","adv.":"副词"}.get(word["part_of_speech"],word["part_of_speech"])
    signature = f"{pos}|{word['definition_cn']}|{word.get('definition_en') or ''}"
    return [{"key": "authored:" + hashlib.sha256(signature.encode()).hexdigest()[:24],
             "part_of_speech": pos, "definition_cn": word["definition_cn"],
             "definition_en": word.get("definition_en"), "source": source,
             "examples": [{"sentence": word["example_sentence"], "translation_cn": word.get("example_translation_cn"),
                           "target_form": word["word"], "source": source}]}]


def migrate_legacy_progress(conn) -> None:
    rows = conn.execute("SELECT * FROM srs_state WHERE sense_migrated=0").fetchall()
    for row in rows:
        # Old history practiced precisely one stored sentence. Transfer its
        # schedule only if that sentence maps unambiguously to one active sense.
        examples = conn.execute("""
            SELECT e.id,e.sense_id FROM sense_examples e JOIN word_senses s ON s.id=e.sense_id
            WHERE s.word_id=? AND s.active=1 AND e.active=1 AND e.sentence=?
        """, (row["word_id"],row["legacy_example_sentence"])).fetchall()
        if len({e["sense_id"] for e in examples}) != 1:
            continue
        example = examples[0]
        write_state(conn, row["user_id"], example["sense_id"], dict(row), example["id"], ignore_existing=True)
        conn.execute("UPDATE review_history SET sense_id=?,example_id=? WHERE user_id=? AND word_id=? AND sense_id IS NULL",
                     (example["sense_id"],example["id"],row["user_id"],row["word_id"]))
        conn.execute("UPDATE srs_state SET sense_migrated=1 WHERE user_id=? AND word_id=?", (row["user_id"],row["word_id"]))


def write_state(conn, user_id: int, sense_id: int, state: dict[str, Any], example_id: int, *, ignore_existing: bool = False) -> None:
    action = "DO NOTHING" if ignore_existing else "DO UPDATE SET " + ",".join(f"{f}=excluded.{f}" for f in (*STATE_FIELDS,"last_example_id"))
    fields = ",".join(STATE_FIELDS)
    conn.execute(f"""INSERT INTO sense_srs_state(user_id,sense_id,{fields},last_example_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,sense_id) {action}""",
        (user_id,sense_id,*(state[f] for f in STATE_FIELDS),example_id))


def example_for_sense(conn, sense_id: int, user_id: int):
    # Least recently practiced example, independently for each learner. Rotate
    # only within this sense, never borrow a sentence from a different meaning.
    return conn.execute("""
        SELECT e.* FROM sense_examples e
        LEFT JOIN review_history h ON h.example_id=e.id AND h.user_id=?
        WHERE e.sense_id=? AND e.active=1
        GROUP BY e.id ORDER BY COALESCE(MAX(h.id),0),e.id LIMIT 1
    """, (user_id,sense_id)).fetchone()


def senses_for_word(conn, word_id: int, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("""SELECT s.*,COALESCE(p.status,'New') AS status,
        p.next_review_date FROM word_senses s LEFT JOIN sense_srs_state p ON p.sense_id=s.id AND p.user_id=?
        WHERE s.word_id=? AND s.active=1 ORDER BY s.position,s.id""", (user_id,word_id)).fetchall()
    return [{**dict(s), "examples": [dict(e) for e in conn.execute(
        "SELECT * FROM sense_examples WHERE sense_id=? AND active=1 ORDER BY id", (s["id"],))]} for s in rows]
