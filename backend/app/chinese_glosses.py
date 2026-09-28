"""Keep checked Chinese definitions effective across catalog refreshes."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SUPPLEMENTS = Path(__file__).resolve().parents[1] / 'data' / 'chinese_gloss_supplements.json'


def read_entries(path: Path) -> dict:
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('format_version') != 1:
        raise ValueError('Unsupported Chinese supplement format')
    entries = {}
    for entry in document['entries']:
        key = entry['word'], entry['sense_key']
        if key in entries:
            raise ValueError(f'Duplicate Chinese supplement: {key}')
        if not re.search(r'[\u3400-\u9fff]', entry['definition_cn']):
            raise ValueError(f'Chinese supplement has no Chinese text: {key}')
        entries[key] = entry
    return entries


def matches(sense, entry) -> bool:
    return (sense['definition_en'] == entry['expected_definition_en']
            and sense['part_of_speech'] == entry['expected_part_of_speech'])


def apply_supplements(words: list[dict], path: Path = DEFAULT_SUPPLEMENTS) -> int:
    entries, applied = read_entries(path), 0
    for word in words:
        for sense in word['senses']:
            entry = entries.get((word['word'], sense['key']))
            if not entry or str(sense.get('definition_cn') or '').strip():
                continue
            if not matches(sense, entry):
                raise ValueError(f"Sense changed; review Chinese supplement: {word['word']} {sense['key']}")
            sense['definition_cn'] = entry['definition_cn'].strip()
            applied += 1
        if word['senses']:
            word['definition_cn'] = word['senses'][0]['definition_cn']
    return applied


def content_hashes(conn) -> dict:
    """Verify restoration changes only Chinese word/sense definitions."""
    hashes = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info({quoted})')]
        if name in ('words', 'word_senses'):
            columns.remove('definition_cn')
        projection = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
        digest = hashlib.sha256()
        for row in conn.execute(f'SELECT {projection} FROM {quoted} ORDER BY rowid'):
            digest.update(repr(tuple(row)).encode('utf-8') + b'\n')
        hashes[name] = digest.hexdigest()
    return hashes


def planned_updates(conn, entries: dict, strict: bool) -> tuple[list, list, int, int]:
    overrides = {}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'admin_content_overrides' in tables:
        overrides = {(row[0], row[1]): json.loads(row[2]) for row in conn.execute(
            "SELECT entity,entity_id,values_json FROM admin_content_overrides WHERE entity IN ('sense','word')")}
    rows = conn.execute('''SELECT s.id,s.sense_key,s.definition_cn,s.definition_en,s.part_of_speech,
        w.id AS word_id,w.word,w.definition_cn AS word_cn,w.definition_en AS word_en
        FROM word_senses s JOIN words w ON w.id=s.word_id
        WHERE w.owner_id IS NULL AND s.active=1 ORDER BY s.word_id,s.position,s.id''').fetchall()
    index = {(row['word'], row['sense_key']): row for row in rows}
    updates, updated_text, flat, preserved, stale = [], {}, [], 0, 0
    for key, entry in entries.items():
        row = index.get(key)
        if row is None or not matches(row, entry):
            if strict:
                raise ValueError(f'Live sense differs: {key}')
            stale += 1
            continue
        if str(row['definition_cn'] or '').strip() or 'definition_cn' in overrides.get(('sense', row['id']), {}):
            preserved += 1
            continue
        text = entry['definition_cn'].strip()
        updates.append((text, row['id']))
        updated_text[row['id']] = text
    seen_words = set()
    for row in rows:
        if row['word_id'] in seen_words:
            continue
        seen_words.add(row['word_id'])
        text = updated_text.get(row['id'], row['definition_cn'])
        if (text and str(row['word_cn'] or '').strip() in ('', '词义待补充')
                and row['word_en'] == row['definition_en']
                and 'definition_cn' not in overrides.get(('word', row['word_id']), {})):
            flat.append((text, row['word_id']))
    return updates, flat, preserved, stale


def sync_database(db: Path, supplements: Path = DEFAULT_SUPPLEMENTS, backup: Path | None = None,
                  dry_run: bool = False, strict: bool = True) -> dict:
    if not db.is_file():
        raise ValueError('Database must already exist')
    entries = read_entries(supplements)
    conn = sqlite3.connect(db, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        conn.execute('BEGIN IMMEDIATE')
        updates, flat, preserved, stale = planned_updates(conn, entries, strict)
        report = {'updated_senses': len(updates), 'updated_word_glosses': len(flat),
                  'preserved': preserved, 'stale': stale, 'backup': None, 'dry_run': dry_run}
        if dry_run or not (updates or flat):
            conn.rollback()
            return report
        # Release the write transaction before SQLite's online backup.
        conn.rollback()
        if backup is None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = db.with_name(db.name + f'.bak-glosses-{stamp}')
        if backup.resolve() == db.resolve() or backup.exists():
            raise ValueError('Backup must be a new file')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(backup)) as destination:
            conn.backup(destination)
        conn.execute('BEGIN IMMEDIATE')
        updates, flat, preserved, stale = planned_updates(conn, entries, strict)
        before = content_hashes(conn)
        conn.executemany('UPDATE word_senses SET definition_cn=? WHERE id=?', updates)
        conn.executemany('UPDATE words SET definition_cn=? WHERE id=?', flat)
        if content_hashes(conn) != before:
            raise ValueError('Unexpected changes outside Chinese definitions')
        if conn.execute('PRAGMA foreign_key_check').fetchall() or conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed')
        conn.commit()
        return {**report, 'updated_senses': len(updates), 'updated_word_glosses': len(flat),
                'preserved': preserved, 'stale': stale, 'backup': str(backup), 'learning_history_unchanged': True}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
