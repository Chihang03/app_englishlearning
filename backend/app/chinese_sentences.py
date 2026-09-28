"""Persistent sentence translations, applied without replacing examples or progress."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

DEFAULT_SENTENCES = Path(__file__).resolve().parents[1] / 'data' / 'chinese_sentence_supplements.json'


def entry_key(entry: dict) -> tuple:
    return entry['word'], entry['sense_key'], entry['sentence']


def read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('format_version') != 1:
        raise ValueError('Unsupported sentence supplement format')
    seen = set()
    for entry in document['entries']:
        key = entry_key(entry)
        if key in seen or not re.search(r'[\u3400-\u9fff]', entry['translation_cn']):
            raise ValueError(f'Duplicate or invalid sentence supplement: {key}')
        seen.add(key)
    return document['entries']


def example_index(words: list[dict]) -> dict:
    index = {}
    for word in words:
        for sense in word['senses']:
            for example in sense.get('examples', []):
                key = word['word'], sense['key'], example['sentence']
                if key in index:
                    raise ValueError(f'Duplicate example: {key}')
                index[key] = sense, example
    return index


def matches(sense, example, entry) -> bool:
    return (sense.get('definition_en') == entry['expected_definition_en']
            and sense['part_of_speech'] == entry['expected_part_of_speech']
            and example['target_form'] == entry['target_form'])


def apply_sentence_supplements(words: list[dict], path: Path = DEFAULT_SENTENCES) -> int:
    index, count = example_index(words), 0
    for entry in read_entries(path):
        pair = index.get(entry_key(entry))
        if pair is None or not matches(*pair, entry):
            raise ValueError(f'Sentence source changed: {entry_key(entry)}')
        _, example = pair
        existing = str(example.get('translation_cn') or '').strip()
        if existing and existing != entry['translation_cn'].strip():
            raise ValueError(f'Existing sentence translation is preserved: {entry_key(entry)}')
        if not existing:
            example['translation_cn'] = entry['translation_cn'].strip()
            count += 1
    for word in words:
        primary = (word.get('senses') or [{}])[0].get('examples') or []
        if (primary and primary[0]['sentence'] == word.get('example_sentence')
                and not str(word.get('example_translation_cn') or '').strip()
                and primary[0].get('translation_cn')):
            word['example_translation_cn'] = primary[0]['translation_cn']
    return count


def content_hashes(conn) -> dict:
    """Compare every persisted column except the two translation fields."""
    hashes = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info({quoted})')]
        excluded = {'words': 'example_translation_cn', 'sense_examples': 'translation_cn'}.get(name)
        columns = [c for c in columns if c != excluded]
        projection = ','.join('"' + c.replace('"', '""') + '"' for c in columns)
        digest = hashlib.sha256()
        for row in conn.execute(f'SELECT {projection} FROM {quoted} ORDER BY rowid'):
            digest.update(repr(tuple(row)).encode('utf-8') + b'\n')
        hashes[name] = digest.hexdigest()
    return hashes


def planned_updates(conn, entries: list[dict], strict: bool) -> tuple[list, list, int, int]:
    examples, flat, preserved, stale = [], {}, 0, 0
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    overrides = {}
    if 'admin_content_overrides' in tables:
        overrides = {row[0]: json.loads(row[1]) for row in conn.execute(
            "SELECT entity_id,values_json FROM admin_content_overrides WHERE entity='example'")}
    for entry in entries:
        row = conn.execute('''SELECT e.id,e.translation_cn,e.target_form,s.definition_en,s.part_of_speech,
            w.id,w.example_sentence,w.example_translation_cn FROM sense_examples e
            JOIN word_senses s ON s.id=e.sense_id JOIN words w ON w.id=s.word_id
            WHERE w.owner_id IS NULL AND w.word=? AND s.sense_key=? AND e.sentence=?
            AND s.active=1 AND e.active=1''', entry_key(entry)).fetchone()
        if not row or row[2] != entry['target_form'] or row[3] != entry['expected_definition_en'] or row[4] != entry['expected_part_of_speech']:
            if strict:
                raise ValueError(f'Live sentence differs: {entry_key(entry)}')
            stale += 1
            continue
        if str(row[1] or '').strip() or 'translation_cn' in overrides.get(row[0], {}):
            preserved += 1
            continue
        examples.append((entry['translation_cn'].strip(), row[0]))
        if row[6] == entry['sentence'] and not str(row[7] or '').strip():
            candidate = entry['translation_cn'].strip()
            if row[5] in flat and flat[row[5]] != candidate:
                raise ValueError(f'Ambiguous flat example translation: {entry["word"]}')
            flat[row[5]] = candidate
    return examples, [(text, number) for number, text in flat.items()], preserved, stale


def sync_database(db: Path, supplements: Path = DEFAULT_SENTENCES, backup: Path | None = None,
                  dry_run: bool = False, strict: bool = True) -> dict:
    if not db.is_file():
        raise ValueError('Database must already exist')
    entries = read_entries(supplements)
    if not entries:
        return {'updated_examples': 0, 'updated_flat_examples': 0, 'preserved': 0, 'stale': 0, 'backup': None}
    conn = sqlite3.connect(db, timeout=15)
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        examples, flat, preserved, stale = planned_updates(conn, entries, strict)
        report = {'updated_examples': len(examples), 'updated_flat_examples': len(flat),
                  'preserved': preserved, 'stale': stale, 'backup': None, 'dry_run': dry_run}
        if dry_run or not examples:
            return report
        if backup is None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = db.with_name(db.name + f'.bak-sentences-{stamp}')
        if backup.resolve() == db.resolve() or backup.exists():
            raise ValueError('Backup must be a new file')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(backup)) as destination:
            conn.backup(destination)
        conn.execute('BEGIN IMMEDIATE')
        examples, flat, preserved, stale = planned_updates(conn, entries, strict)
        before = content_hashes(conn)
        conn.executemany('UPDATE sense_examples SET translation_cn=? WHERE id=?', examples)
        conn.executemany('UPDATE words SET example_translation_cn=? WHERE id=?', flat)
        if content_hashes(conn) != before:
            raise ValueError('Unexpected changes outside sentence translations')
        if conn.execute('PRAGMA foreign_key_check').fetchall() or conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed')
        conn.commit()
        return {**report, 'updated_examples': len(examples), 'updated_flat_examples': len(flat),
                'preserved': preserved, 'stale': stale, 'backup': str(backup),
                'learning_history_unchanged': True}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
