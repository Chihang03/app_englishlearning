"""Reviewed teaching groups; dictionary IDs remain the source of every answer.

Unreviewed content keeps one unit per sense and its existing list coverage.
State tables retain their old names/source anchors for historical compatibility,
but every live query is keyed by user + learning_unit_id.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .security import utc_now_iso
from .word_forms import pos_family

CONFIG_PATH = Path(__file__).resolve().parents[1] / 'data' / 'learning_units.json'
STATE_TABLES = ('sense_srs_state', 'adaptive_memory', 'relearning_queue', 'user_sense_examples')


def unit_for_sense(conn, sense_id: int) -> int:
    row = conn.execute('SELECT learning_unit_id FROM learning_unit_senses WHERE sense_id=?', (sense_id,)).fetchone()
    if row is None:
        word = conn.execute('SELECT word_id FROM word_senses WHERE id=?', (sense_id,)).fetchone()
        if word is None:
            raise ValueError('Unknown dictionary sense')
        ensure_word_units(conn, word[0])
        row = conn.execute('SELECT learning_unit_id FROM learning_unit_senses WHERE sense_id=?', (sense_id,)).fetchone()
    return row[0]


def anchor_for_unit(conn, unit_id: int) -> int:
    return conn.execute('SELECT representative_sense_id FROM learning_units WHERE id=?', (unit_id,)).fetchone()[0]


def ensure_word_units(conn, word_id: int) -> None:
    # Export/migration fixtures can author paired content on a pre-v17 schema.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_units'").fetchone() is None:
        return
    word = conn.execute('SELECT * FROM words WHERE id=?', (word_id,)).fetchone()
    for sense in conn.execute('SELECT * FROM word_senses WHERE word_id=? ORDER BY position,id', (word_id,)).fetchall():
        lexical = sense['lexical_unit_id']
        if lexical is None:
            family = pos_family(sense['part_of_speech']) or sense['part_of_speech'].strip().casefold()
            key = f'word:{word_id}:{family}'
            conn.execute('''INSERT OR IGNORE INTO lexical_units(owner_id,word_id,unit_key,headword,
                normalized_headword,pos_group,identity_status) VALUES(?,?,?,?,?,?,'legacy')''',
                (word['owner_id'], word_id, key, word['word'], word['word'].strip().casefold(), family))
            lexical = conn.execute('SELECT id FROM lexical_units WHERE word_id=? AND unit_key=?', (word_id,key)).fetchone()[0]
            conn.execute('UPDATE word_senses SET lexical_unit_id=? WHERE id=?', (lexical,sense['id']))
        binding = conn.execute('SELECT learning_unit_id FROM learning_unit_senses WHERE sense_id=?', (sense['id'],)).fetchone()
        if binding is None:
            key = 'sense:' + sense['sense_key']
            conn.execute('''INSERT OR IGNORE INTO learning_units(lexical_unit_id,word_id,unit_key,
                representative_sense_id,display_definition_cn,display_definition_en,position,verification)
                VALUES(?,?,?,?,?,?,?,'legacy')''',
                (lexical,word_id,key,sense['id'],sense['definition_cn'],sense['definition_en'],sense['position']))
            unit = conn.execute('SELECT id FROM learning_units WHERE word_id=? AND unit_key=?', (word_id,key)).fetchone()[0]
            conn.execute('INSERT INTO learning_unit_senses(sense_id,learning_unit_id) VALUES(?,?)', (sense['id'],unit))
            conn.execute('''INSERT OR IGNORE INTO word_list_learning_units(list_id,learning_unit_id,position,verification)
                SELECT m.list_id,?,m.position,'legacy' FROM word_list_memberships m WHERE m.word_id=?
                AND NOT EXISTS(SELECT 1 FROM word_list_learning_scopes scope
                    WHERE scope.list_id=m.list_id AND scope.word_id=m.word_id AND scope.verification='reviewed')''',(unit,word_id))
        else:
            # Translation corrections update singleton displays. Reviewed core
            # definitions are authored separately and never overwritten here.
            conn.execute('''UPDATE learning_units SET lexical_unit_id=?,display_definition_cn=?,
                display_definition_en=?,position=? WHERE id=? AND verification='legacy' ''',
                (lexical,sense['definition_cn'],sense['definition_en'],sense['position'],binding[0]))


def usable_unit_sql(alias: str = 'u') -> str:
    return f'''{alias}.active=1 AND EXISTS(SELECT 1 FROM learning_unit_senses us
        JOIN word_senses ds ON ds.id=us.sense_id JOIN sense_examples ex ON ex.sense_id=ds.id
        WHERE us.learning_unit_id={alias}.id AND ds.active=1 AND ds.learning_enabled=1 AND ex.active=1)'''


def _upsert_snapshot_row(conn, table: str, row: dict) -> None:
    columns = list(row)
    assignments = ','.join(f'{key}=excluded.{key}' for key in columns if key not in ('user_id','sense_id'))
    conn.execute(f'''INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})
        ON CONFLICT(user_id,sense_id) DO UPDATE SET {assignments}''', tuple(row.values()))


def _merge_group(conn, word, group: dict, senses: list, config_key: str) -> int:
    old_units = {s['id']:unit_for_sense(conn,s['id']) for s in senses}
    lexical = {s['lexical_unit_id'] for s in senses}
    if len(lexical) != 1 or None in lexical:
        raise ValueError(f'Learning group crosses lexical identities: {config_key}')
    existing = conn.execute('SELECT id,active FROM learning_units WHERE word_id=? AND unit_key=?', (word['id'],group['key'])).fetchone()
    if existing and existing['active'] and set(old_units.values()) != {existing['id']}:
        raise ValueError(f'Reviewed group membership changed; explicit migration required: {config_key}')
    provenance = json.dumps(group['provenance'],ensure_ascii=False)
    conn.execute('''INSERT INTO learning_units(lexical_unit_id,word_id,unit_key,representative_sense_id,
        display_definition_cn,position,example_strategy,verification,provenance_json)
        VALUES(?,?,?,?,?,?,?,'reviewed',?) ON CONFLICT(word_id,unit_key) DO UPDATE SET
        lexical_unit_id=excluded.lexical_unit_id,display_definition_cn=excluded.display_definition_cn,
        position=excluded.position,example_strategy=excluded.example_strategy,active=1,
        provenance_json=excluded.provenance_json''',
        (next(iter(lexical)),word['id'],group['key'],senses[0]['id'],group['definition_cn'],
         min(s['position'] for s in senses),group.get('example_strategy','fixed'),provenance))
    unit = conn.execute('SELECT * FROM learning_units WHERE word_id=? AND unit_key=?', (word['id'],group['key'])).fetchone()
    if existing and set(old_units.values()) == {unit['id']}:
        return unit['id']
    # A reviewed mapping may not silently absorb an existing different group.
    for sid, prior in old_units.items():
        members = {r[0] for r in conn.execute('SELECT sense_id FROM learning_unit_senses WHERE learning_unit_id=?', (prior,))}
        if not members <= set(old_units):
            raise ValueError(f'Group change needs an explicit migration: {config_key}')
    ids = list(old_units)
    marks = ','.join('?' for _ in ids)
    snapshot = {'word_id':word['id'],'bindings':old_units,'units':[dict(r) for r in conn.execute(
        f"SELECT * FROM learning_units WHERE id IN ({','.join('?' for _ in set(old_units.values()))})",tuple(set(old_units.values())))],
        'tables':{},'history_max_id':conn.execute('SELECT COALESCE(MAX(id),0) FROM review_history').fetchone()[0]}
    for table in STATE_TABLES:
        snapshot['tables'][table] = [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE sense_id IN ({marks})',ids)]
    snapshot['attempts'] = [dict(r) for r in conn.execute(f'SELECT * FROM study_attempts WHERE sense_id IN ({marks}) AND completed_at IS NULL',ids)]
    snapshot['exposures'] = [dict(r) for r in conn.execute(f'SELECT * FROM sense_exposures WHERE sense_id IN ({marks})',ids)]
    now = utc_now_iso()
    conn.execute('INSERT INTO learning_unit_merges(learning_unit_id,config_key,snapshot_json,created_at) VALUES(?,?,?,?)',
        (unit['id'],config_key,json.dumps(snapshot,ensure_ascii=False),now))
    for table in STATE_TABLES:
        conn.execute(f'UPDATE {table} SET retired_at=? WHERE sense_id IN ({marks}) AND retired_at IS NULL', (now,*ids))
    # Source events stay intact. A signed round is never changed underneath an
    # old phone; it must fetch a replacement after this group is activated.
    conn.execute(f'UPDATE study_attempts SET completed_at=? WHERE sense_id IN ({marks}) AND completed_at IS NULL',(now,*ids))
    conn.executemany('UPDATE learning_unit_senses SET learning_unit_id=? WHERE sense_id=?',[(unit['id'],sid) for sid in ids])
    for table in ('review_history','study_attempts','sense_exposures'):
        conn.execute(f'UPDATE {table} SET learning_unit_id=? WHERE sense_id IN ({marks})',(unit['id'],*ids))
    users = {r['user_id'] for rows in snapshot['tables'].values() for r in rows if r['retired_at'] is None}
    for user in users:
        rows = {table:[r for r in values if r['user_id']==user and r['retired_at'] is None]
                for table,values in snapshot['tables'].items()}
        states = rows['sense_srs_state']
        queues = rows['relearning_queue']
        selected = None
        if states:
            # Preserve the only existing curve exactly. With several curves,
            # pending correction/learning wins, then the earliest due estimate.
            pending = {r['sense_id'] for r in queues}
            selected = min(states,key=lambda r:(r['sense_id'] not in pending,r['status']!='Learning',r['next_review_date'],r['interval_days']))
            shared = {**selected,'sense_id':unit['representative_sense_id'],'learning_unit_id':unit['id'],'retired_at':None}
            if len(states)>1:
                shared['next_review_date'] = min(r['next_review_date'] for r in states)
                shared['interval_days'] = min(r['interval_days'] for r in states)
                shared['wrong_count'] = max(r['wrong_count'] for r in states)
                shared['lapse_count'] = max(r['lapse_count'] for r in states)
                shared['status'] = 'Learning' if queues or any(r['status']=='Learning' for r in states) else (
                    'Mature' if all(r['status']=='Mature' for r in states) else 'Reviewing')
                shared['correct_count'] = min(r['correct_count'] for r in states)
            _upsert_snapshot_row(conn,'sense_srs_state',shared)
        memories = rows['adaptive_memory']
        if memories:
            memory = next((r for r in memories if selected and r['sense_id']==selected['sense_id']),memories[0])
            shared = {**memory,'sense_id':unit['representative_sense_id'],'learning_unit_id':unit['id'],'retired_at':None}
            card = json.loads(memory['card_json']);card['card_id']=unit['id']
            if len(states)>1 or len(memories)>1:
                curves = [json.loads(r['card_json']) for r in memories]
                stabilities = [c.get('stability') or 1 for c in curves]
                if states:
                    stabilities.append(max(1,min(r['interval_days'] for r in states)))
                card['stability'] = min(stabilities)
                reviews = [c.get('last_review') for c in curves]
                card['last_review'] = min(reviews,key=datetime.fromisoformat) if all(reviews) else None
                card['difficulty'] = max(c.get('difficulty') or 5 for c in curves)
                shared.update(known_candidate=0,confirmations=0,first_independent_at=None,last_independent_at=None)
            shared['card_json'] = json.dumps(card)
            _upsert_snapshot_row(conn,'adaptive_memory',shared)
        if queues:
            # Never shorten the required wait after the most recent correction.
            queued = max(queues,key=lambda r:r['ready_at'])
            _upsert_snapshot_row(conn,'relearning_queue',{**queued,'sense_id':unit['representative_sense_id'],
                'learning_unit_id':unit['id'],'retired_at':None,'queue_order':min(r['queue_order'] for r in queues)})
        fixed = rows['user_sense_examples']
        if fixed:
            chosen = next((r for r in fixed if selected and r['sense_id']==selected['sense_id']),fixed[0])
            if queues:
                chosen = next((r for r in fixed if r['sense_id']==queued['sense_id']),chosen)
            _upsert_snapshot_row(conn,'user_sense_examples',{**chosen,'sense_id':unit['representative_sense_id'],
                'learning_unit_id':unit['id'],'retired_at':None})
    for prior in set(old_units.values())-{unit['id']}:
        conn.execute('UPDATE learning_units SET active=0 WHERE id=?',(prior,))
    return unit['id']


def sync_learning_units(conn, config_path: Path | None = None) -> None:
    for row in conn.execute('SELECT DISTINCT word_id FROM word_senses WHERE id NOT IN (SELECT sense_id FROM learning_unit_senses)').fetchall():
        ensure_word_units(conn,row[0])
    # Morphology can upgrade a legacy identity without changing dictionary IDs.
    conn.execute('''UPDATE learning_units SET lexical_unit_id=(SELECT lexical_unit_id FROM word_senses
        WHERE id=learning_units.representative_sense_id)''')
    for table in (*STATE_TABLES,'study_attempts','review_history','sense_exposures'):
        conn.execute(f'''UPDATE {table} SET learning_unit_id=(SELECT learning_unit_id FROM learning_unit_senses
            WHERE sense_id={table}.sense_id) WHERE learning_unit_id IS NULL''')
    path = config_path or CONFIG_PATH
    config = json.loads(path.read_text()) if path.exists() else {'format_version':1,'words':[]}
    if config.get('format_version') != 1:
        raise ValueError('Unsupported learning unit configuration')
    resolved = []
    for entry in config['words']:
        word = conn.execute('SELECT * FROM words WHERE owner_id IS NULL AND word=?',(entry['word'],)).fetchone()
        if word is None:
            continue
        source = {r['sense_key']:r for r in conn.execute('SELECT * FROM word_senses WHERE word_id=?',(word['id'],))}
        seen = set(); groups = {}
        for group in entry['groups']:
            if not group.get('provenance') or not group['definition_cn'].strip():
                raise ValueError('Reviewed learning units need evidence and a definition')
            keys = [s['key'] for s in group['senses']]
            if not keys or len(set(keys))!=len(keys) or seen.intersection(keys):
                raise ValueError('Dictionary sense belongs to more than one group')
            seen.update(keys); members=[]
            for expected in group['senses']:
                sense = source.get(expected['key'])
                if sense is None or sense['definition_en']!=expected['expected_definition_en'] or pos_family(sense['part_of_speech'])!=group['pos_group']:
                    raise ValueError(f"Dictionary changed; review learning group {group['key']}")
                members.append(sense)
            key = entry['word']+':'+group['key']
            blocked = conn.execute('SELECT 1 FROM learning_group_blocks WHERE config_key=?',(key,)).fetchone()
            groups[group['key']] = {unit_for_sense(conn,s['id']) for s in members} if blocked else {_merge_group(conn,word,group,members,key)}
        resolved.append((word,entry,groups))
    # Rebuild only content coverage, never user selections or progress. Unknown
    # words retain explicitly labelled legacy coverage until editorial review.
    conn.execute('DELETE FROM word_list_learning_units')
    conn.execute('DELETE FROM word_list_learning_scopes')
    conn.execute("INSERT INTO word_list_learning_scopes(list_id,word_id,verification) SELECT list_id,word_id,'legacy' FROM word_list_memberships")
    conn.execute('''INSERT INTO word_list_learning_units(list_id,learning_unit_id,position,verification)
        SELECT m.list_id,b.learning_unit_id,MIN(m.position),'legacy' FROM word_list_memberships m
        JOIN word_senses s ON s.word_id=m.word_id JOIN learning_unit_senses b ON b.sense_id=s.id
        JOIN learning_units u ON u.id=b.learning_unit_id WHERE u.active=1 GROUP BY m.list_id,b.learning_unit_id''')
    for word,entry,groups in resolved:
        for list_id,coverage in entry.get('coverage',{}).items():
            membership = conn.execute('SELECT position FROM word_list_memberships WHERE word_id=? AND list_id=?',(word['id'],list_id)).fetchone()
            if membership is None:
                continue
            conn.execute('DELETE FROM word_list_learning_units WHERE list_id=? AND learning_unit_id IN (SELECT id FROM learning_units WHERE word_id=?)',(list_id,word['id']))
            conn.execute("UPDATE word_list_learning_scopes SET verification='reviewed',provenance_json=? WHERE list_id=? AND word_id=?",
                (json.dumps(coverage['provenance'],ensure_ascii=False),list_id,word['id']))
            for key in coverage['units']:
                if key not in groups:
                    raise ValueError(f'Unknown reviewed course unit: {key}')
                for unit in groups[key]:
                    conn.execute('''INSERT INTO word_list_learning_units(list_id,learning_unit_id,position,verification,provenance_json)
                        VALUES(?,?,?,'reviewed',?)''',(list_id,unit,membership[0],json.dumps(coverage['provenance'],ensure_ascii=False)))


def example_for_unit(conn, unit_id: int, user_id: int, *, rotate: bool = False):
    unit = conn.execute('SELECT * FROM learning_units WHERE id=?',(unit_id,)).fetchone()
    examples = conn.execute('''SELECT e.* FROM learning_unit_senses us JOIN word_senses s ON s.id=us.sense_id
        JOIN sense_examples e ON e.sense_id=s.id JOIN words w ON w.id=s.word_id
        WHERE us.learning_unit_id=? AND s.active=1 AND s.learning_enabled=1 AND e.active=1
        ORDER BY lower(e.target_form)!=lower(w.word),s.position,e.id''',(unit_id,)).fetchall()
    if not examples:
        return None
    fixed = conn.execute('SELECT example_id FROM user_sense_examples WHERE user_id=? AND learning_unit_id=? AND retired_at IS NULL',(user_id,unit_id)).fetchone()
    chosen = next((e for e in examples if fixed and e['id']==fixed[0]),None)
    last = conn.execute('''SELECT example_id,is_independent FROM review_history WHERE user_id=? AND learning_unit_id=?
        ORDER BY id DESC LIMIT 1''',(user_id,unit_id)).fetchone()
    if chosen is None:
        seen = conn.execute('''SELECT example_id FROM (SELECT example_id,created_at AS seen_at,0 AS source,id AS record_id
            FROM study_attempts WHERE user_id=? AND learning_unit_id=? UNION ALL
            SELECT example_id,review_time,1,id FROM review_history WHERE user_id=? AND learning_unit_id=?)
            ORDER BY seen_at,source,record_id''',(user_id,unit_id,user_id,unit_id)).fetchall()
        valid = {e['id']:e for e in examples}
        chosen = next((valid[r[0]] for r in seen if r[0] in valid),examples[0])
    if rotate and unit['example_strategy']=='rotate' and last and last['is_independent']==1 and len(examples)>1:
        # Prefer a different source usage, then an unseen/least recently used
        # example. A retry of a wrong or assisted answer always keeps its sentence.
        previous = next((e for e in examples if e['id']==last['example_id']),chosen)
        uses = {r['example_id']:r['last_id'] for r in conn.execute('''SELECT example_id,MAX(id) AS last_id FROM review_history
            WHERE user_id=? AND learning_unit_id=? GROUP BY example_id''',(user_id,unit_id))}
        chosen = min(examples,key=lambda e:(e['sense_id']==previous['sense_id'],e['id']==previous['id'],uses.get(e['id'],0),e['id']))
    _upsert_snapshot_row(conn,'user_sense_examples',{'user_id':user_id,'sense_id':unit['representative_sense_id'],
        'example_id':chosen['id'],'learning_unit_id':unit_id,'retired_at':None})
    return chosen


def revert_merge(conn, receipt_id: int) -> None:
    """Restore a group receipt without ever overwriting later learning events.

    This intentionally refuses automatic undo after new answers. Such receipts
    still contain every source curve; a later migration can allocate those new
    events by their unchanged dictionary sense_id instead of losing them.
    """
    receipt = conn.execute('SELECT * FROM learning_unit_merges WHERE id=? AND reverted_at IS NULL',(receipt_id,)).fetchone()
    if receipt is None:
        raise ValueError('Unknown or already reverted merge receipt')
    snapshot = json.loads(receipt['snapshot_json'])
    ids = [int(sid) for sid in snapshot['bindings']]
    marks = ','.join('?' for _ in ids)
    if conn.execute(f'SELECT 1 FROM review_history WHERE id>? AND sense_id IN ({marks}) LIMIT 1',
                    (snapshot['history_max_id'],*ids)).fetchone():
        raise ValueError('New answers exist: retain the receipt and plan an event-preserving rollback')
    now = utc_now_iso()
    conn.execute(f'UPDATE study_attempts SET completed_at=? WHERE sense_id IN ({marks}) AND completed_at IS NULL',(now,*ids))
    for table in STATE_TABLES:
        conn.execute(f'DELETE FROM {table} WHERE sense_id IN ({marks})',ids)
        for row in snapshot['tables'][table]:
            _upsert_snapshot_row(conn,table,row)
    for row in snapshot['units']:
        conn.execute('UPDATE learning_units SET active=?,lexical_unit_id=? WHERE id=?',(row['active'],row['lexical_unit_id'],row['id']))
    for sid,unit in snapshot['bindings'].items():
        conn.execute('UPDATE learning_unit_senses SET learning_unit_id=? WHERE sense_id=?',(unit,int(sid)))
        for table in ('review_history','study_attempts','sense_exposures'):
            conn.execute(f'UPDATE {table} SET learning_unit_id=? WHERE sense_id=?',(unit,int(sid)))
    # Old signed rounds stay retired so cached clients fetch a fresh round.
    conn.execute('UPDATE learning_units SET active=0 WHERE id=?',(receipt['learning_unit_id'],))
    conn.execute('INSERT OR REPLACE INTO learning_group_blocks(config_key,blocked_at) VALUES(?,?)',(receipt['config_key'],now))
    conn.execute('UPDATE learning_unit_merges SET reverted_at=? WHERE id=?',(now,receipt_id))
    sync_learning_units(conn)
