"""Append recovered dictionary words without refreshing the existing catalog."""
from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path

from .semantic_review import chinese
from .senses import save_senses, validate_senses
from .vocabulary_json import catalog_hashes

SOURCE_COMPLETIONS_PATH = Path(__file__).resolve().parents[1] / 'data' / 'source_word_completions.json'
logger = logging.getLogger(__name__)


def seed_missing_sources(conn, path: Path, catalog_bytes: bytes) -> dict:
    counts = {'added': 0, 'preserved': 0, 'rejected': 0}
    if not path.exists():
        return counts
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(document, dict) or document.get('format_version') != 1
                or not isinstance(document.get('entries'), list)):
            raise ValueError('Invalid dictionary source completion bundle')
    except (OSError, ValueError) as error:
        logger.warning('Dictionary completion bundle preserved existing data: %s', error)
        return {**counts, 'rejected': 1}
    exact_catalog = document.get('catalog_raw_sha256') == hashlib.sha256(catalog_bytes).hexdigest()
    if not exact_catalog and document.get('catalog_sha256') not in catalog_hashes(catalog_bytes):
        logger.warning('Dictionary completions refer to another catalog; existing data preserved')
        return {**counts, 'stale_catalog': len(document['entries'])}
    seen = set()
    for entry in document['entries']:
        word = None
        conn.execute('SAVEPOINT missing_source_word')
        try:
            if not isinstance(entry, dict) or not isinstance(entry.get('source'), dict):
                raise ValueError('Source completion needs a dictionary record')
            source = entry['source']
            word = source.get('word')
            if (not isinstance(word, str) or not word.strip() or word != word.strip()
                    or word in seen or not entry.get('dictionary_evidence')):
                raise ValueError('Source completion needs a unique word and dictionary evidence')
            seen.add(word)
            if (not isinstance(source.get('senses'), list) or not source['senses']
                    or any(not isinstance(s, dict) or not isinstance(s.get('examples'), list)
                           or any(not isinstance(e, dict) for e in s['examples']) for s in source['senses'])):
                raise ValueError('Invalid recovered sense structure')
            validate_senses(source['senses'])
            if not isinstance(entry.get('memberships'), list) or not entry['memberships']:
                raise ValueError('Source completion needs a course membership')
            for sense in source['senses']:
                for example in sense['examples']:
                    if not chinese(example.get('translation_cn') or '') or not example.get('source'):
                        raise ValueError('Recovered examples need Chinese and explicit provenance')
            row = conn.execute('SELECT id FROM words WHERE owner_id IS NULL AND word=?', (word,)).fetchone()
            if row is None:
                first = source['senses'][0]
                example = first['examples'][0]
                cursor = conn.execute('''INSERT INTO words(owner_id,word,part_of_speech,definition_cn,
                    definition_en,example_sentence,example_translation_cn,pronunciation)
                    VALUES(NULL,?,?,?,?,?,?,?)''', (word, first['part_of_speech'], first['definition_cn'] or '待核查',
                    first.get('definition_en'), example['sentence'], example['translation_cn'], word))
                word_id = cursor.lastrowid
            else:
                word_id = row['id']
            for membership in entry['memberships']:
                if not conn.execute('SELECT 1 FROM vocabulary_lists WHERE list_id=?', (membership['list_id'],)).fetchone():
                    raise ValueError('Unknown course in recovered dictionary source')
                conn.execute('''INSERT OR IGNORE INTO word_list_memberships(list_id,word_id,position)
                    VALUES(?,?,?)''', (membership['list_id'], word_id, membership['position']))
            if conn.execute('SELECT 1 FROM word_senses WHERE word_id=? AND active=1', (word_id,)).fetchone():
                # A reviewed output or manual correction is never a missing source.
                counts['preserved'] += 1
            else:
                save_senses(conn, word_id, source['senses'])
                counts['added'] += 1
            conn.execute('RELEASE missing_source_word')
        except (ValueError, KeyError, TypeError) as error:
            conn.execute('ROLLBACK TO missing_source_word')
            conn.execute('RELEASE missing_source_word')
            counts['rejected'] += 1
            logger.warning('Dictionary completion preserved %s: %s', word, error)
    conn.execute('''UPDATE vocabulary_lists SET word_count=(SELECT COUNT(*) FROM word_list_memberships m
        WHERE m.list_id=vocabulary_lists.list_id AND EXISTS(SELECT 1 FROM word_senses s
        WHERE s.word_id=m.word_id AND s.active=1 AND s.learning_enabled=1))''')
    return counts
