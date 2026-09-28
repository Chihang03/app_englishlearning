"""Real exported identities, conservative resolution and lossless v14 migration."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import database, migrations
from app.main import app
from app.morphology import resolve_lexical_entries, validate_bundle
from app.sense_learning import start_attempt
from app.word_forms import pos_family
from test_senses import legacy_private_word


class MorphologyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.bundle = json.loads(database.WORD_FORMS_PATH.read_text())
        full = json.loads(database.VOCABULARY_CATALOG_PATH.read_text())
        words = {u['word'] for u in self.bundle['lexical_units']}
        words.update(('better', 'best', 'walking', 'gone', 'late'))
        subset = deepcopy([w for w in full['words'] if w['word'] in words])
        subset.sort(key=lambda w: (w['word'] != 'say', w['word']))
        for position, word in enumerate(subset):
            word['memberships'] = [{'list_id': 'starter_examples', 'position': position + 1}]
        catalog = self.directory / 'catalog.json'
        catalog.write_text(json.dumps({'format_version': 2, 'lists': [], 'words': subset}, ensure_ascii=False))
        self.bundle['catalog_sha256'] = hashlib.sha256(catalog.read_bytes()).hexdigest()
        self.forms = self.directory / 'forms.json'
        self.forms.write_text(json.dumps(self.bundle, ensure_ascii=False))
        for name, value in [('DATA_DIR', self.directory), ('DB_PATH', self.directory / 'test.db'),
                            ('SEED_PATH', self.directory / 'no-seed'), ('VOCABULARY_CATALOG_PATH', catalog),
                            ('WORD_FORMS_PATH', self.forms)]:
            p = patch.object(database, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        registered = self.client.post('/api/auth/register', json={'username': 'morphology', 'password': 'test-password-123'})
        self.assertEqual(registered.status_code, 200, registered.text)
        self.uid = registered.json()['user']['id']

    def lookup(self, word):
        result = self.client.get('/api/dictionary/' + word)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def card_for(self, word, family=None):
        with database.connect() as conn:
            rows = conn.execute('''SELECT s.id AS sense_id,s.lexical_unit_id,w.id AS word_id,s.part_of_speech
                FROM word_senses s JOIN words w ON w.id=s.word_id WHERE w.word=? AND s.active=1
                AND w.owner_id IS NULL ORDER BY s.position,s.id''', (word,)).fetchall()
            row = next(r for r in rows if family is None or pos_family(r['part_of_speech']) == family)
            example = conn.execute('SELECT * FROM sense_examples WHERE sense_id=? AND active=1 ORDER BY id LIMIT 1', (row['sense_id'],)).fetchone()
            attempt = start_attempt(conn, self.uid, row['sense_id'], example['id'])
            return {'word_id': row['word_id'], 'sense_id': row['sense_id'], 'example_id': example['id'],
                    'attempt_id': attempt['id'], 'user_answer': example['target_form'], 'unit_id': row['lexical_unit_id']}

    def review(self, card):
        return self.client.post('/api/review', json={k: v for k, v in card.items() if k != 'unit_id'})

    def test_pos_labels_do_not_conflate_adverb_and_verb(self):
        for pos in ('adverb', '副词', 'ADVERB'):
            self.assertEqual(pos_family(pos), 'adverb')
        for pos in ('verb', 'intransitive verb', 'auxiliary verb', '及物动词'):
            self.assertEqual(pos_family(pos), 'verb')
        self.assertEqual(pos_family('proverb'), '')

    def test_regular_and_irregular_forms_reuse_exact_canonical_senses(self):
        for base, family, forms in [('say', 'verb', ['says', 'said', 'saying']),
            ('walk', 'verb', ['walked', 'walking']), ('book', 'noun', ['books']),
            ('small', 'adjective', ['smaller', 'smallest']), ('good', 'adjective', ['better', 'best']),
            ('go', 'verb', ['went', 'gone']), ('mouse', 'noun', ['mice']), ('child', 'noun', ['children'])]:
            canonical = next(e for e in self.lookup(base)['entries'] if e['headword'] == base and e['pos_group'] == family)
            for spelling in forms:
                with self.subTest(base=base, spelling=spelling):
                    resolved = next(e for e in self.lookup(spelling)['entries'] if e['classification'] == 'INFLECTION'
                                    and e['headword'] == base and e['pos_group'] == family)
                    self.assertEqual(resolved['lexical_unit_id'], canonical['lexical_unit_id'])
                    self.assertEqual([s['id'] for s in resolved['senses']], [s['id'] for s in canonical['senses']])
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sense_srs_state').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM words WHERE word IN ('says','said')").fetchone()[0], 0)

    def test_lexicalized_nouns_coexist_with_verb_forms(self):
        for noun, verb in [('saying', 'say'), ('building', 'build'), ('meeting', 'meet'),
                           ('painting', 'paint'), ('beginning', 'begin')]:
            entries = self.lookup(noun)['entries']
            independent = next(e for e in entries if e['headword'] == noun and e['pos_group'] == 'noun')
            form = next(e for e in entries if e['headword'] == verb and e['classification'] == 'INFLECTION')
            self.assertEqual(independent['classification'], 'LEXICALIZED_FORM')
            self.assertNotEqual(independent['lexical_unit_id'], form['lexical_unit_id'])
            self.assertTrue(independent['learnable'])
            self.assertTrue({s['id'] for s in independent['senses']}.isdisjoint(s['id'] for s in form['senses']))
        saying = self.lookup('saying')['definition']
        self.assertEqual(saying['word'], 'saying')
        self.assertIn('格言', saying['definition_cn'])
        self.assertEqual(saying['example_sentence'], 'as the saying goes.')

    def test_derivatives_are_independent_even_when_semantics_shift(self):
        for base, derived in [('absolute', 'absolutely'), ('real', 'really'), ('actual', 'actually'),
                              ('hard', 'hardly'), ('late', 'lately'), ('near', 'nearly')]:
            entry = next(e for e in self.lookup(derived)['entries'] if e['headword'] == derived and e['pos_group'] == 'adverb')
            self.assertEqual(entry['classification'], 'DERIVED')
            self.assertTrue(entry['learnable'])
            self.assertTrue(any(p['headword'] == base for p in entry['morphology']['derived_words']))
            self.assertFalse(any(e['classification'] == 'INFLECTION' for e in self.lookup(derived)['entries']))

    def test_multiple_pos_and_senses_and_unknown_are_preserved(self):
        entries = self.lookup('better')['entries']
        self.assertGreater(len({e['pos_group'] for e in entries}), 1)
        direct = [e for e in entries if e['headword'] == 'better']
        self.assertGreater(sum(len(e['senses']) for e in direct), 1)
        self.assertEqual(self.lookup('inventeding')['classification'], 'UNKNOWN')
        # v1 contains thousands of spelling hints: an unreviewed one is not proof.
        self.assertFalse(any(e['classification'] == 'INFLECTION' for e in self.lookup('addressing')['entries']))
        with database.connect() as conn:
            filtered = resolve_lexical_entries(conn, 'saying', self.uid, 'noun')
            self.assertTrue(all(e['pos_group'] == 'noun' for e in filtered))

    def test_candidate_and_scoped_evidence_do_not_overmerge(self):
        with database.connect() as conn:
            unit = conn.execute("SELECT id FROM lexical_units WHERE headword='say' AND pos_group='verb'").fetchone()[0]
            scope = conn.execute('SELECT id FROM word_senses WHERE lexical_unit_id=? ORDER BY id LIMIT 1', (unit,)).fetchone()[0]
            conn.execute("UPDATE word_forms SET scope_sense_id=? WHERE spelling='says'", (scope,))
            entry = next(e for e in resolve_lexical_entries(conn, 'says', self.uid) if e['classification'] == 'INFLECTION')
            self.assertEqual([s['id'] for s in entry['senses']], [scope])
            conn.execute("UPDATE word_forms SET verification='candidate' WHERE spelling='said'")
            self.assertFalse(any(e['classification'] == 'INFLECTION' for e in resolve_lexical_entries(conn, 'said', self.uid)))
        invalid = deepcopy(self.bundle)
        invalid['forms'][0]['evidence_kind'] = 'rule_only'
        with self.assertRaises(ValueError):
            validate_bundle(invalid)

    def test_viewing_family_marks_displayed_senses_and_open_attempts_exposed(self):
        source = self.card_for('absolute', 'adjective')
        derived = self.card_for('absolutely', 'adverb')
        unrelated = self.card_for('mouse', 'noun')
        result = self.client.post('/api/study/meanings', json={'attempt_id': source['attempt_id']})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn(derived['sense_id'], result.json()['exposed_sense_ids'])
        self.assertNotIn(unrelated['sense_id'], result.json()['exposed_sense_ids'])
        self.assertFalse(self.review(derived).json()['is_independent'])
        self.assertTrue(self.review(unrelated).json()['is_independent'])
        target = self.client.post('/api/study/meanings', json={'attempt_id': source['attempt_id'],
                                  'target_lexical_unit_id': derived['unit_id']})
        self.assertEqual(target.json()['word'], 'absolutely')
        rejected = self.client.post('/api/study/meanings', json={'attempt_id': source['attempt_id'],
                                    'target_lexical_unit_id': unrelated['unit_id']})
        self.assertEqual(rejected.status_code, 404)

    def test_later_family_card_cannot_gain_known_candidate_from_exposure(self):
        source = self.card_for('absolute', 'adjective')
        self.client.post('/api/study/meanings', json={'attempt_id': source['attempt_id']})
        derived = self.card_for('absolutely', 'adverb')
        result = self.review(derived)
        self.assertFalse(result.json()['is_independent'])
        self.assertFalse(result.json()['memory']['known_candidate'])

    def test_derived_first_independent_answer_uses_own_prior(self):
        base = self.card_for('absolute', 'adjective')
        self.assertTrue(self.review(base).json()['is_independent'])
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET status='Mature',interval_days=180 WHERE sense_id=? AND user_id=?",
                         (base['sense_id'], self.uid))
        derived = self.card_for('absolutely', 'adverb')
        result = self.review(derived)
        self.assertTrue(result.json()['is_independent'])
        self.assertTrue(result.json()['memory']['known_candidate'])
        self.assertEqual(result.json()['memory']['confirmations'], 1)
        self.assertNotEqual(result.json()['srs_state']['status'], 'Mature')
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM adaptive_memory').fetchone()[0], 2)
            self.assertEqual(tuple(conn.execute('SELECT status,interval_days FROM sense_srs_state WHERE sense_id=? AND user_id=?',
                                               (base['sense_id'], self.uid)).fetchone()), ('Mature', 180))

    def test_private_old_forms_and_two_trained_schedules_are_never_merged(self):
        original = self.card_for('say', 'verb')
        self.assertEqual(self.review(original).status_code, 200)
        private = legacy_private_word(self.uid, {'word': 'said', 'part_of_speech': '动词',
            'definition_cn': '说过', 'example_sentence': 'She said hello to me.'})
        database.init_database()
        with database.connect() as conn:
            row = conn.execute('SELECT s.id,e.id AS example FROM word_senses s JOIN sense_examples e ON e.sense_id=s.id WHERE s.word_id=?', (private['id'],)).fetchone()
            attempt = start_attempt(conn, self.uid, row['id'], row['example'])
        self.assertEqual(self.review({'word_id': private['id'], 'sense_id': row['id'], 'example_id': row['example'],
                                    'attempt_id': attempt['id'], 'user_answer': 'said'}).status_code, 200)
        before = self.snapshot()
        database.init_database()
        self.assertEqual(before, self.snapshot())
        entries = self.lookup('said')['entries']
        self.assertTrue(any(e['word_id'] == private['id'] and e['classification'] == 'UNKNOWN' for e in entries))
        self.assertTrue(any(e['classification'] == 'INFLECTION' and e['headword'] == 'say' for e in entries))
        self.assertEqual(self.lookup('said')['definition']['id'], private['id'])

    def snapshot(self):
        tables = ('sense_srs_state', 'srs_state', 'review_history', 'adaptive_memory', 'relearning_queue',
                  'study_attempts', 'sense_exposures', 'user_sense_examples', 'memory_profiles')
        with database.connect() as conn:
            return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}

    def test_v14_additive_upgrade_backup_and_idempotency(self):
        self.review(self.card_for('say', 'verb'))
        before = self.snapshot()
        with database.connect() as conn:
            conn.execute('DROP TABLE lexical_relations')
            conn.execute('DROP TABLE word_forms')
            conn.execute('DROP INDEX idx_word_senses_lexical_unit')
            conn.execute('ALTER TABLE word_senses DROP COLUMN lexical_unit_id')
            conn.execute('DROP TABLE lexical_units')
            conn.execute('PRAGMA user_version=13')
        migrations.run_migrations(database.DB_PATH)
        migrations.run_migrations(database.DB_PATH)
        self.assertEqual(before, self.snapshot())
        with database.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 14)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        backups = list(self.directory.glob('test.db.bak-v13-*'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 13)

    def test_bundle_only_refresh_and_catalog_mismatch_disable_links_safely(self):
        before = self.snapshot()
        self.bundle['forms'][0]['verification'] = 'candidate'
        self.forms.write_text(json.dumps(self.bundle))
        database.init_database()
        self.assertFalse(any(e['classification'] == 'INFLECTION' for e in self.lookup('says')['entries']))
        self.assertEqual(before, self.snapshot())
        self.bundle['catalog_sha256'] = 'changed-source'
        self.forms.write_text(json.dumps(self.bundle))
        database.init_database()
        self.assertFalse(any(e['classification'] == 'INFLECTION' for e in self.lookup('said')['entries']))
        self.assertEqual(before, self.snapshot())

    def test_repeated_refresh_keeps_source_ids_and_fixed_examples(self):
        card = self.card_for('saying', 'noun')
        self.review(card)
        before = self.snapshot()
        self.bundle['resolver_version'] = 'refresh-test'
        self.forms.write_text(json.dumps(self.bundle))
        database.init_database()
        database.init_database()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.lookup('saying')['definition']['senses'][0]['id'], card['sense_id'])
        self.assertEqual(self.client.post('/api/words/import', json={'words': []}).status_code, 410)
