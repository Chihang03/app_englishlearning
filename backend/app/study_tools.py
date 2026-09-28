from __future__ import annotations

import json

from fastapi import HTTPException

from .security import utc_now_iso
from .sense_learning import record_related_exposure
from .senses import senses_for_word
from .morphology import morphology_for_unit, resolve_lexical_entries, _visible_unit


def attempt_content(conn, user_id: int, attempt_id: str):
    row = conn.execute("""SELECT a.id AS attempt_id,a.completed_at,a.sense_id,a.example_id,
        w.id AS word_id,w.word,w.pronunciation,s.lexical_unit_id,s.part_of_speech,s.definition_cn,s.definition_en,
        e.sentence,e.translation_cn,e.target_form
        FROM study_attempts a JOIN word_senses s ON s.id=a.sense_id
        JOIN words w ON w.id=s.word_id JOIN sense_examples e ON e.id=a.example_id AND e.sense_id=s.id
        WHERE a.id=? AND a.user_id=? AND (w.owner_id IS NULL OR w.owner_id=?)""",
        (attempt_id, user_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="找不到这道题目，请重新进入学习。")
    return row


def word_meanings(conn, user_id: int, attempt_id: str, target_unit_id: int | None = None):
    # Record exposure before returning any answer-bearing definitions/examples.
    record_related_exposure(conn, user_id, attempt_id)
    content = attempt_content(conn, user_id, attempt_id)
    morphology = morphology_for_unit(conn, content['lexical_unit_id'], user_id)
    senses = senses_for_word(conn, content['word_id'], user_id)
    entries = resolve_lexical_entries(conn, content['word'], user_id)
    word = content['word']
    if target_unit_id is not None and target_unit_id != content['lexical_unit_id']:
        allowed = {item['lexical_unit_id'] for group in ('derived_words', 'related_words') for item in morphology[group]}
        if target_unit_id not in allowed:
            raise HTTPException(status_code=404, detail='找不到这条词族关系。')
        unit = _visible_unit(conn, target_unit_id, user_id)
        if unit is None:
            raise HTTPException(status_code=404, detail='找不到这条词族关系。')
        entry = next(e for e in resolve_lexical_entries(conn, unit['headword'], user_id)
                     if e['lexical_unit_id'] == target_unit_id and e['classification'] != 'INFLECTION')
        word, senses, entries, morphology = unit['headword'], entry['senses'], [entry], entry['morphology']
    if content["completed_at"] is None:
        conn.execute("UPDATE study_attempts SET answer_exposed=1 WHERE id=?", (attempt_id,))
    exposed = {s['id'] for s in senses}
    # Even a related spelling can reveal the exact cloze answer. Account only
    # for spellings actually returned, not the entire transitive word family.
    visible_spellings = {word, *(f['spelling'] for f in morphology['inflected_forms'])}
    visible_spellings.update(item['headword'] for group in ('derived_words', 'related_words') for item in morphology[group])
    for entry in entries:
        metadata = entry['morphology']
        visible_spellings.update(f['spelling'] for f in metadata['inflected_forms'])
        visible_spellings.update(p['headword'] for group in ('derived_words', 'related_words') for p in metadata[group])
    for spelling in visible_spellings:
        for entry in resolve_lexical_entries(conn, spelling, user_id):
            exposed.update(s['id'] for s in entry['senses'])
    now = utc_now_iso()
    conn.executemany('''INSERT INTO sense_exposures(user_id,sense_id,exposed_at) VALUES(?,?,?)
        ON CONFLICT(user_id,sense_id) DO UPDATE SET exposed_at=excluded.exposed_at''',
        [(user_id, sid, now) for sid in exposed if sid != content['sense_id']])
    conn.executemany('UPDATE study_attempts SET answer_exposed=1 WHERE user_id=? AND sense_id=? AND completed_at IS NULL',
                     [(user_id, sid) for sid in exposed])
    exposed.add(content['sense_id'])
    return {'word': word, 'senses': senses, 'entries': entries, 'morphology': morphology,
            'exposed_sense_ids': sorted(exposed)}


def report_content(conn, user_id: int, attempt_id: str, category: str, details: str):
    conn.execute("BEGIN IMMEDIATE")
    content = attempt_content(conn, user_id, attempt_id)
    conn.execute("""INSERT INTO content_reports(user_id,attempt_id,word_id,sense_id,example_id,
        category,details,content_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,attempt_id,category) DO NOTHING""",
        (user_id, attempt_id, content["word_id"], content["sense_id"], content["example_id"],
         category, details.strip(), json.dumps(dict(content), ensure_ascii=False), utc_now_iso()))
    report = conn.execute("SELECT id FROM content_reports WHERE user_id=? AND attempt_id=? AND category=?",
                          (user_id, attempt_id, category)).fetchone()
    return {"id": report["id"], "status": "received"}
