"""Read-only, account-visible dictionary snapshot for durable client caching."""
from __future__ import annotations

import hashlib
import json


def catalog_snapshot(conn, user_id: int) -> tuple[bytes, str]:
    rows = conn.execute("""
        SELECT w.id AS word_id, w.word, s.id AS sense_id, b.learning_unit_id,
               s.part_of_speech, s.definition_cn, s.definition_en,
               e.id AS example_id, e.sentence, e.translation_cn, e.target_form
        FROM words w
        JOIN word_senses s ON s.word_id=w.id AND s.active=1
        JOIN learning_unit_senses b ON b.sense_id=s.id
        LEFT JOIN sense_examples e ON e.sense_id=s.id AND e.active=1
        WHERE w.owner_id IS NULL OR w.owner_id=?
        ORDER BY w.id, s.position, s.id, e.id
    """, (user_id,))
    words: list[dict] = []
    previous_word = previous_sense = None
    senses: list[dict] = []
    examples: list[dict] = []
    for row in rows:
        if row["word_id"] != previous_word:
            words.append({"id": row["word_id"], "word": row["word"], "senses": []})
            senses = words[-1]["senses"]
            previous_word = row["word_id"]
            previous_sense = None
        if row["sense_id"] != previous_sense:
            senses.append({
                "id": row["sense_id"], "learning_unit_id": row["learning_unit_id"],
                "part_of_speech": row["part_of_speech"],
                "definition_cn": row["definition_cn"], "definition_en": row["definition_en"],
                "examples": [],
            })
            examples = senses[-1]["examples"]
            previous_sense = row["sense_id"]
        if row["example_id"] is not None:
            examples.append({
                "id": row["example_id"], "sentence": row["sentence"],
                "translation_cn": row["translation_cn"], "target_form": row["target_form"],
            })
    body = json.dumps({"words": words}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body, '"' + hashlib.sha256(body).hexdigest() + '"'
