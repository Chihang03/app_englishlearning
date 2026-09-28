"""Conservative lexical identities; learning still belongs to the original sense.

The v1 spelling projection is only a hint. Only reviewed v2 evidence can resolve
an inflection. This module never creates, combines or updates memory states.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata

from .senses import save_senses, senses_for_word, validate_senses
from .word_forms import FORM_LABELS, pos_family


VERIFIED_EVIDENCE = {
    'dictionary_inflection', 'dictionary_grammar_reference', 'reviewed_override',
    'dictionary_example', 'dictionary_derivative',
}
RELATION_TYPES = {'derived_adverb', 'derived_noun', 'derived_adjective', 'lexicalized_from', 'related'}


def normalize_spelling(value: str) -> str:
    return unicodedata.normalize('NFC', value.strip()).casefold()


def validate_bundle(bundle: dict) -> None:
    if bundle.get('format_version') != 2:
        raise ValueError('Morphology requires word_forms format version 2')
    units = {u['key']: u for u in bundle['lexical_units']}
    if len(units) != len(bundle['lexical_units']):
        raise ValueError('Duplicate lexical unit key')
    for unit in units.values():
        if not unit['word'] or not unit['pos_group'] or not unit.get('provenance'):
            raise ValueError('Missing lexical identity evidence')
    for form in bundle['forms']:
        if (form['unit_key'] not in units or form['form_type'] not in FORM_LABELS
                or normalize_spelling(form['spelling']) == normalize_spelling(units[form['unit_key']]['word'])):
            raise ValueError('Invalid word form binding')
        if form['verification'] not in ('verified', 'candidate', 'rejected'):
            raise ValueError('Invalid verification')
        if form['verification'] == 'verified' and (
                form['evidence_kind'] not in VERIFIED_EVIDENCE or not form.get('provenance')):
            raise ValueError('Unattested inflection cannot be verified')
    for edge in bundle['relations']:
        if (edge['from'] not in units or edge['to'] not in units or edge['from'] == edge['to']
                or edge['relation_type'] not in RELATION_TYPES or not edge.get('provenance')
                or edge.get('verification') not in ('verified', 'candidate', 'rejected')):
            raise ValueError('Invalid lexical relation')
    for word in bundle.get('supplemental_words', []):
        validate_senses(word['senses'])


def backfill_lexical_units(conn: sqlite3.Connection) -> None:
    # Metadata grouping only. Same-POS homonyms retain every separate sense and
    # every separate schedule. Flat private content receives no lemma inference.
    rows = conn.execute('''SELECT s.id,s.part_of_speech,w.id AS word_id,w.word,w.owner_id
        FROM word_senses s JOIN words w ON w.id=s.word_id
        WHERE s.lexical_unit_id IS NULL''').fetchall()
    groups: dict[tuple, list] = {}
    for row in rows:
        family = pos_family(row['part_of_speech']) or row['part_of_speech'].strip().casefold()
        groups.setdefault((row['word_id'], family), []).append(row)
    for (word_id, family), senses in groups.items():
        word = senses[0]
        key = f'word:{word_id}:{family}'
        conn.execute('''INSERT OR IGNORE INTO lexical_units(owner_id,word_id,unit_key,headword,
            normalized_headword,pos_group,identity_status) VALUES(?,?,?,?,?,?, 'legacy')''',
            (word['owner_id'], word_id, key, word['word'], normalize_spelling(word['word']), family))
        unit = conn.execute('SELECT id FROM lexical_units WHERE word_id=? AND unit_key=?', (word_id, key)).fetchone()[0]
        conn.executemany('UPDATE word_senses SET lexical_unit_id=? WHERE id=?', [(unit, s['id']) for s in senses])


def seed_morphology(conn: sqlite3.Connection, bundle_bytes: bytes, catalog_bytes: bytes) -> None:
    catalog_hash = hashlib.sha256(catalog_bytes).hexdigest()
    fingerprint = hashlib.sha256(bundle_bytes + b'\0' + catalog_bytes).hexdigest()
    previous = conn.execute("SELECT value FROM vocabulary_catalog_state WHERE key='morphology_fingerprint'").fetchone()
    if previous and previous[0] == fingerprint:
        backfill_lexical_units(conn)
        return
    bundle = json.loads(bundle_bytes) if bundle_bytes else {}
    compatible = bundle.get('format_version') == 2 and bundle.get('catalog_sha256') == catalog_hash
    if compatible:
        validate_bundle(bundle)
    conn.execute('BEGIN IMMEDIATE')
    # Retire shipped evidence on re-export rather than leaving stale links live.
    conn.execute('UPDATE word_forms SET active=0 WHERE lexical_unit_id IN (SELECT id FROM lexical_units WHERE owner_id IS NULL)')
    conn.execute('UPDATE lexical_relations SET active=0 WHERE from_unit_id IN (SELECT id FROM lexical_units WHERE owner_id IS NULL)')
    conn.execute("UPDATE lexical_units SET identity_status='legacy',provenance_json='{}' WHERE owner_id IS NULL")
    conn.execute('UPDATE lexical_units SET active=0 WHERE owner_id IS NULL AND word_id IS NULL')
    if compatible:
        _save_supplemental_words(conn, bundle)
    backfill_lexical_units(conn)
    if compatible:
        units = _bind_verified_units(conn, bundle)
        for form in bundle['forms']:
            unit = units.get(form['unit_key'])
            if unit is None:
                continue
            scope = None
            if form.get('scope_sense_key'):
                scope_row = conn.execute('SELECT id FROM word_senses WHERE lexical_unit_id=? AND sense_key=? AND active=1',
                                         (unit, form['scope_sense_key'])).fetchone()
                if scope_row is None:
                    raise ValueError('Missing form sense scope')
                scope = scope_row[0]
            conn.execute('''INSERT INTO word_forms(lexical_unit_id,spelling,normalized_form,form_type,
                scope_sense_id,verification,evidence_kind,provenance_json,active) VALUES(?,?,?,?,?,?,?,?,1)
                ON CONFLICT DO UPDATE SET verification=excluded.verification,evidence_kind=excluded.evidence_kind,
                    provenance_json=excluded.provenance_json,active=1''',
                (unit, form['spelling'], normalize_spelling(form['spelling']), form['form_type'], scope,
                 form['verification'], form['evidence_kind'], json.dumps(form['provenance'], ensure_ascii=False)))
        for edge in bundle['relations']:
            source, target = units.get(edge['from']), units.get(edge['to'])
            if source is None or target is None:
                continue
            conn.execute('''INSERT INTO lexical_relations(from_unit_id,to_unit_id,relation_type,verification,
                provenance_json,active) VALUES(?,?,?,?,?,1) ON CONFLICT(from_unit_id,to_unit_id,relation_type)
                DO UPDATE SET verification=excluded.verification,provenance_json=excluded.provenance_json,active=1''',
                (source, target, edge['relation_type'], edge['verification'], json.dumps(edge['provenance'], ensure_ascii=False)))
    conn.execute("INSERT OR REPLACE INTO vocabulary_catalog_state VALUES('morphology_status',?)",
                 ('verified' if compatible else 'catalog_mismatch_or_legacy_bundle',))
    conn.execute("INSERT OR REPLACE INTO vocabulary_catalog_state VALUES('morphology_fingerprint',?)", (fingerprint,))


def _save_supplemental_words(conn, bundle):
    for item in bundle.get('supplemental_words', []):
        primary, example = item['senses'][0], item['senses'][0]['examples'][0]
        conn.execute('''INSERT OR IGNORE INTO words(owner_id,word,part_of_speech,definition_cn,definition_en,
            example_sentence,example_translation_cn,pronunciation) VALUES(NULL,?,?,?,?,?,?,?)''',
            (item['word'], primary['part_of_speech'], primary['definition_cn'], primary.get('definition_en'),
             example['sentence'], example.get('translation_cn'), item.get('pronunciation') or item['word']))
        word_id = conn.execute('SELECT id FROM words WHERE owner_id IS NULL AND word=?', (item['word'],)).fetchone()[0]
        # Supplement rather than archive unrelated active senses at this spelling.
        existing = senses_for_word(conn, word_id, 0)
        incoming_keys = {s['key'] for s in item['senses']}
        preserved = [{**s, 'key': s['sense_key']} for s in existing if s['sense_key'] not in incoming_keys]
        save_senses(conn, word_id, [*preserved, *item['senses']])
        for membership in item.get('memberships', []):
            conn.execute('INSERT OR IGNORE INTO word_list_memberships(list_id,word_id,position) VALUES(?,?,?)',
                         (membership['list_id'], word_id, membership['position']))
    conn.execute('''UPDATE vocabulary_lists SET word_count=(SELECT COUNT(*) FROM word_list_memberships m
        WHERE m.list_id=vocabulary_lists.list_id AND EXISTS
        (SELECT 1 FROM word_senses s WHERE s.word_id=m.word_id AND s.active=1))''')


def _bind_verified_units(conn, bundle):
    result = {}
    for unit in bundle['lexical_units']:
        rows = conn.execute('''SELECT u.id FROM lexical_units u JOIN words w ON w.id=u.word_id
            WHERE w.owner_id IS NULL AND w.word=? AND u.pos_group=?''', (unit['word'], unit['pos_group'])).fetchall()
        if not rows and not unit.get('expected_senses'):
            conn.execute('''INSERT INTO lexical_units(unit_key,headword,normalized_headword,pos_group,
                identity_status,provenance_json,active) VALUES(?,?,?,?,'verified',?,1)
                ON CONFLICT DO UPDATE SET identity_status='verified',provenance_json=excluded.provenance_json,active=1''',
                (unit['key'],unit['word'],normalize_spelling(unit['word']),unit['pos_group'],json.dumps(unit['provenance'])))
            result[unit['key']] = conn.execute('SELECT id FROM lexical_units WHERE owner_id IS NULL AND unit_key=?', (unit['key'],)).fetchone()[0]
            continue
        if len(rows) != 1:
            continue  # No source identity or ambiguous homograph: never guess.
        uid = rows[0][0]
        expected = unit.get('expected_senses', [])
        for sense in expected:
            row = conn.execute('''SELECT part_of_speech,definition_cn,definition_en FROM word_senses
                WHERE lexical_unit_id=? AND sense_key=? AND active=1''', (uid, sense['key'])).fetchone()
            # Content corrections affect display, not the audited source identity.
            # The bundled fingerprint guards the original definition and sense key.
            if row is None or pos_family(row['part_of_speech']) != unit['pos_group']:
                break
        else:
            conn.execute("UPDATE lexical_units SET identity_status='verified',provenance_json=? WHERE id=?",
                         (json.dumps(unit['provenance'], ensure_ascii=False), uid))
            result[unit['key']] = uid
    return result


def _visible_unit(conn, unit_id: int, user_id: int):
    return conn.execute('''SELECT * FROM lexical_units WHERE id=? AND active=1
        AND (owner_id IS NULL OR owner_id=?)''', (unit_id, user_id)).fetchone()


def unit_senses(conn, unit, user_id: int, scope_ids: set[int] | None = None):
    if unit['word_id'] is None:
        return []
    ids = {r[0] for r in conn.execute('SELECT id FROM word_senses WHERE lexical_unit_id=? AND active=1', (unit['id'],))}
    if scope_ids is not None:
        ids &= scope_ids
    return [s for s in senses_for_word(conn, unit['word_id'], user_id) if s['id'] in ids]


def morphology_for_unit(conn, unit_id: int | None, user_id: int) -> dict:
    result = {'inflected_forms': [], 'derived_words': [], 'related_words': []}
    if unit_id is None or _visible_unit(conn, unit_id, user_id) is None:
        return result
    forms = conn.execute('''SELECT spelling,form_type,scope_sense_id FROM word_forms
        WHERE lexical_unit_id=? AND active=1 AND verification='verified' ORDER BY normalized_form,id''', (unit_id,))
    groups = {}
    for form in forms:
        key = (form['spelling'], form['scope_sense_id'])
        groups.setdefault(key, []).append(form['form_type'])
    result['inflected_forms'] = [{'spelling': spelling, 'form_types': kinds,
        'label': '／'.join(FORM_LABELS[k] for k in kinds), 'scope_sense_id': scope}
        for (spelling, scope), kinds in groups.items()]
    edges = conn.execute('''SELECT * FROM lexical_relations WHERE (from_unit_id=? OR to_unit_id=?)
        AND active=1 AND verification='verified' ORDER BY id''', (unit_id, unit_id))
    for edge in edges:
        target = edge['to_unit_id'] if edge['from_unit_id'] == unit_id else edge['from_unit_id']
        unit = _visible_unit(conn, target, user_id)
        if unit is None:
            continue
        item = {'lexical_unit_id': target, 'headword': unit['headword'], 'pos_group': unit['pos_group'],
                'relation_type': edge['relation_type'], 'direction': 'outgoing' if edge['from_unit_id'] == unit_id else 'incoming',
                'learnable': bool(unit_senses(conn, unit, user_id))}
        result['derived_words' if edge['relation_type'].startswith('derived_') else 'related_words'].append(item)
    return result


def resolve_lexical_entries(conn, spelling: str, user_id: int, pos: str | None = None) -> list[dict]:
    normalized = normalize_spelling(spelling)
    units = conn.execute('''SELECT * FROM lexical_units WHERE normalized_headword=? AND active=1
        AND (owner_id IS NULL OR owner_id=?) ORDER BY owner_id IS NULL,id''', (normalized, user_id)).fetchall()
    entries = []
    for unit in units:
        if pos and unit['pos_group'] != pos_family(pos):
            continue
        incoming = conn.execute('''SELECT relation_type FROM lexical_relations WHERE to_unit_id=?
            AND active=1 AND verification='verified' ''', (unit['id'],)).fetchall()
        types = {e[0] for e in incoming}
        classification = ('LEXICALIZED_FORM' if 'lexicalized_from' in types else
                          'DERIVED' if any(t.startswith('derived_') for t in types) else
                          'INDEPENDENT' if unit['identity_status'] == 'verified' else 'UNKNOWN')
        entries.append(_entry(conn, unit, user_id, classification))
    forms = conn.execute('''SELECT f.*,u.pos_group FROM word_forms f JOIN lexical_units u ON u.id=f.lexical_unit_id
        WHERE f.normalized_form=? AND f.active=1 AND f.verification='verified' AND u.active=1
        AND u.identity_status='verified' AND (u.owner_id IS NULL OR u.owner_id=?) ORDER BY f.id''', (normalized, user_id)).fetchall()
    grouped = {}
    for form in forms:
        if pos and form['pos_group'] != pos_family(pos):
            continue
        grouped.setdefault(form['lexical_unit_id'], []).append(form)
    for uid, matched in grouped.items():
        unit = _visible_unit(conn, uid, user_id)
        scopes = None if any(f['scope_sense_id'] is None for f in matched) else {f['scope_sense_id'] for f in matched}
        entry = _entry(conn, unit, user_id, 'INFLECTION', scopes)
        entry['matched_form'] = {'spelling': spelling.strip(), 'form_types': list(dict.fromkeys(f['form_type'] for f in matched))}
        entries.append(entry)
    return entries


def _entry(conn, unit, user_id, classification, scope_ids=None):
    senses = unit_senses(conn, unit, user_id, scope_ids)
    return {'lexical_unit_id': unit['id'], 'word_id': unit['word_id'], 'headword': unit['headword'],
            'pos_group': unit['pos_group'], 'classification': classification, 'learnable': bool(senses),
            'senses': senses, 'morphology': morphology_for_unit(conn, unit['id'], user_id)}
