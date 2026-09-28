"""Keep operator corrections effective when the shipped catalog is refreshed."""
from __future__ import annotations

import json

from .security import utc_now_iso


def override_values(conn, entity: str, entity_id: int) -> dict:
    row = conn.execute("SELECT values_json FROM admin_content_overrides WHERE entity=? AND entity_id=?", (entity, entity_id)).fetchone()
    return json.loads(row[0]) if row else {}


def store_override(conn, entity: str, entity_id: int, original: dict, changes: dict, admin_id: int) -> None:
    if not changes:
        return
    values = {**override_values(conn, entity, entity_id), **changes}
    conn.execute("""INSERT INTO admin_content_overrides(entity,entity_id,original_json,values_json,updated_by,updated_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(entity,entity_id) DO UPDATE SET
        values_json=excluded.values_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
        (entity, entity_id, json.dumps(original, ensure_ascii=False), json.dumps(values, ensure_ascii=False), admin_id, utc_now_iso()))


def overridden_example_id(conn, sense_id: int, original_sentence: str) -> int | None:
    rows = conn.execute("""SELECT e.id,o.original_json FROM admin_content_overrides o
        JOIN sense_examples e ON o.entity='example' AND o.entity_id=e.id WHERE e.sense_id=?""", (sense_id,)).fetchall()
    for row in rows:
        if json.loads(row['original_json']).get('sentence') == original_sentence:
            return row['id']
    return None


def apply_word_overrides(conn) -> None:
    for row in conn.execute("SELECT entity_id,values_json FROM admin_content_overrides WHERE entity='word'").fetchall():
        changes = json.loads(row['values_json'])
        columns = [key for key in ('pronunciation', 'definition_cn', 'definition_en') if key in changes]
        if columns:
            conn.execute('UPDATE words SET ' + ','.join(key+'=?' for key in columns) + ' WHERE id=?',
                (*(changes[key] for key in columns), row['entity_id']))
