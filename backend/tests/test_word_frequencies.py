"""Frequency ordering, queue boundaries and additive migration preservation."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import test_senses as fixtures
from app import database, migrations
from app.word_frequencies import WORD_FREQUENCIES_PATH, sync_word_frequencies


BASE_CATALOG = fixtures.catalog()
SCORES = {'address': 450, 'bank': 550, 'business': 600, 'problem': 550,
          'abashed': None, 'unlisted': None}


def frequency_catalog():
    catalog = copy.deepcopy(BASE_CATALOG)
    for word, lists in [('business', ['cet4']), ('problem', ['cet6']),
                        ('abashed', ['cet4']), ('unlisted', ['cet4'])]:
        sentence = f'This example contains {word}.'
        senses = [fixtures.sense(word, '测试义项', sentence, target=word)]
        if word == 'business':
            senses.append(fixtures.sense('business-other', '另一个义项',
                                        'Her business was successful.', target=word))
        catalog['words'].append({
            'word': word, 'pronunciation': word, 'part_of_speech': '名词',
            'definition_cn': '测试义项', 'example_sentence': sentence,
            'memberships': [{'list_id': list_id, 'position': 100} for list_id in lists],
            'senses': senses,
        })
    # Shared words belong to both selected lists, but have one learning state.
    catalog['words'][1]['memberships'].append({'list_id': 'cet4', 'position': 2})
    return catalog


class WordFrequencyTests(unittest.TestCase):
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    ready_relearning = fixtures.SenseLearningTests.ready_relearning

    def setUp(self):
        with patch.object(fixtures, 'catalog', frequency_catalog):
            fixtures.SenseLearningTests.setUp(self)
        self.frequency_path = self.directory/'frequencies.json'
        frequency_patch = patch.object(database, 'WORD_FREQUENCIES_PATH', self.frequency_path)
        frequency_patch.start();self.addCleanup(frequency_patch.stop)
        self.write_frequencies()
        self.sync()

    def write_frequencies(self, scores=None):
        self.frequency_path.write_text(json.dumps({
            'format_version': 1,
            'source': {'name': 'test', 'version': '1', 'language': 'en', 'score_scale': 100},
            'words': SCORES if scores is None else scores,
        }))

    def sync(self):
        with database.connect() as conn:
            return sync_word_frequencies(conn, self.frequency_path)

    def table_snapshots(self, conn):
        return {row[0]: [tuple(r) for r in conn.execute(f'SELECT * FROM "{row[0]}" ORDER BY rowid')]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if row[0] not in ('word_frequencies', 'word_frequency_state')}

    def test_high_frequency_beats_database_ids_and_learning_unit_position(self):
        card = self.next()
        self.assertEqual(card['word'], 'business')
        self.assertEqual(card['definition_cn'], '测试义项')
        self.assertTrue(card['is_new_word'])
        self.review(card, card['answer_form'])
        # problem is in an unselected list; bank is shared and therefore visible.
        self.assertEqual(self.next()['word'], 'bank')

    def test_new_headwords_precede_other_units_of_an_already_started_word(self):
        card = self.next()
        self.review(card, card['answer_form'])
        following = self.next()
        self.assertEqual(following['word'], 'bank')
        self.assertTrue(following['is_new_word'])

    def test_same_word_units_keep_their_source_order(self):
        with database.connect() as conn:
            conn.execute("UPDATE learning_units SET position=10 WHERE word_id=(SELECT id FROM words WHERE word='business') AND unit_key LIKE '%business-other%'")
        card = self.next()
        self.assertEqual(card['definition_cn'], '测试义项')
        self.review(card, card['answer_form'])
        for _ in range(4):
            card = self.next()
            self.review(card, card['answer_form'])
        self.assertEqual(self.next()['word'], 'business')
        self.assertEqual(self.next()['definition_cn'], '另一个义项')

    def test_multilist_sequence_is_monotone_and_shared_words_are_not_reintroduced(self):
        self.client.patch('/api/settings', json={'selected_word_list_ids': ['cet4', 'cet6']})
        introduced = []
        for _ in range(12):
            card = self.next()
            if card is None:
                break
            if card['is_new_word']:
                introduced.append(card['word'])
            response = self.review(card, card['answer_form'])
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(introduced), set(SCORES))
        self.assertEqual(len(introduced), len(SCORES))
        measured = [SCORES[word] for word in introduced if SCORES[word] is not None]
        self.assertEqual(measured, [600, 550, 550, 450])
        self.assertEqual(set(introduced[-2:]), {'abashed', 'unlisted'})
        self.assertIsNone(self.next())

    def test_ties_and_missing_values_keep_the_same_order_across_restarts(self):
        self.client.patch('/api/settings', json={'selected_word_list_ids': ['cet4', 'cet6']})
        self.write_frequencies({word: 500 for word in SCORES});self.sync()
        first = self.next()
        database.init_database()
        self.assertEqual(self.next()['attempt_id'], first['attempt_id'])
        with TestClient(fixtures.app) as other:
            other.post('/api/auth/register', json={'username': 'other', 'password': 'test-password-123'})
            other.patch('/api/settings', json={'selected_word_list_ids': ['cet4', 'cet6']})
            self.assertEqual(other.get('/api/next').json()['card']['word'], first['word'])
        self.review(first, first['answer_form'])
        following = self.next()
        database.init_database()
        self.assertEqual(self.next()['attempt_id'], following['attempt_id'])

    def test_missing_scores_remain_available_without_becoming_measured_zeroes(self):
        with database.connect() as conn:
            self.assertIsNone(conn.execute("SELECT zipf_cent FROM word_frequencies WHERE headword='abashed'").fetchone()[0])
        self.write_frequencies({'abashed': None, 'unlisted': None});self.sync()
        # Heads omitted from a custom snapshot are also eligible and unmeasured.
        seen = set()
        while (card := self.next()) is not None:
            seen.add(card['word'])
            self.review(card, card['answer_form'])
        self.assertEqual(seen, {'address', 'bank', 'business', 'abashed', 'unlisted'})

    def test_frequency_update_resumes_the_existing_unanswered_round(self):
        first = self.next()
        self.client.post('/api/study/hint', json={'attempt_id': first['attempt_id'], 'kind': 'answer'})
        self.write_frequencies({**SCORES, 'address': 800, 'business': 100});self.sync()
        resumed = self.next()
        self.assertEqual(resumed['attempt_id'], first['attempt_id'])
        self.assertEqual(resumed['word'], first['word'])
        self.review(resumed, resumed['answer_form'])
        self.assertEqual(self.next()['word'], 'address')

    def test_due_review_and_ready_relearning_precede_high_frequency_new_words(self):
        first = self.next()
        self.review(first, first['answer_form'])
        self.write_frequencies({**SCORES, 'business': 100});self.sync()
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01' WHERE user_id=?", (self.uid,))
        # An independent pass cannot be repeated on the same local day.
        tomorrow = date.today() + timedelta(days=1)
        with patch('app.main.user_today', return_value=tomorrow), \
                patch('app.main.today_in', return_value=tomorrow):
            due = self.next()
            self.assertEqual(due['learning_unit_id'], first['learning_unit_id'])
            self.assertEqual(self.review(due, 'wrong').status_code, 200)
            self.assertEqual(self.review(due, due['answer_form']).status_code, 200)
            self.ready_relearning()
            correction = self.next()
            self.assertEqual(correction['learning_unit_id'], first['learning_unit_id'])
            self.assertTrue(correction['is_relearning'])

    def test_filters_and_account_isolation_apply_before_frequency_ordering(self):
        with database.connect() as conn:
            business = conn.execute("SELECT id FROM words WHERE word='business'").fetchone()[0]
        self.client.post(f'/api/words/{business}/mute')
        self.client.patch('/api/settings', json={'skip_basic_600': True})
        self.assertEqual(self.next()['word'], 'bank')  # address/business are basic heads.
        with TestClient(fixtures.app) as other:
            other.post('/api/auth/register', json={'username': 'other', 'password': 'test-password-123'})
            self.assertEqual(other.get('/api/next').json()['card']['word'], 'business')

    def test_additive_v19_migration_and_snapshot_updates_preserve_every_existing_table(self):
        first = self.next();self.review(first, first['answer_form'])
        self.next()  # Also preserve an open attempt, settings and the user's session.
        with database.connect() as conn:
            conn.execute('DROP TABLE word_frequencies')
            conn.execute('DROP TABLE word_frequency_state')
            conn.execute('PRAGMA user_version=18')
            before = self.table_snapshots(conn)
        migrations.run_migrations(database.DB_PATH)
        self.sync()
        with database.connect() as conn:
            self.assertEqual(self.table_snapshots(conn), before)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], migrations.SCHEMA_VERSION)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertTrue(list(self.directory.glob('test.db.bak-v18-*')))
        self.assertFalse(self.sync())
        self.write_frequencies({**SCORES, 'business': 100});self.sync()
        with database.connect() as conn:
            self.assertEqual(self.table_snapshots(conn), before)

    def test_invalid_snapshot_is_rejected_before_existing_frequencies_are_changed(self):
        with database.connect() as conn:
            before = [tuple(row) for row in conn.execute('SELECT * FROM word_frequencies ORDER BY headword')]
        for invalid in (0, -10, 901, 1.5, True):
            self.write_frequencies({**SCORES, 'business': invalid})
            with self.assertRaises(ValueError):
                self.sync()
            with database.connect() as conn:
                self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM word_frequencies ORDER BY headword')], before)
        self.frequency_path.unlink()
        self.assertFalse(self.sync())


class ShippedFrequencyTests(unittest.TestCase):
    def test_snapshot_covers_every_catalog_and_seed_head_with_embedded_credits(self):
        root = Path(__file__).resolve().parents[1]/'data'
        catalog = json.loads((root/'vocabulary_catalog.json').read_text())
        seed = json.loads((root/'seed_words.json').read_text())
        payload = json.loads(WORD_FREQUENCIES_PATH.read_text())
        self.assertEqual(set(payload['words']), {w['word'].strip().casefold() for w in [*catalog['words'], *seed]})
        self.assertEqual(payload['source']['version'], '3.1.1')
        self.assertEqual(payload['source']['data_license'], 'CC-BY-SA-4.0')
        self.assertIn('SUBTLEX', payload['source']['notice'])
        self.assertGreater(payload['words']['the'], payload['words']['business'])
        self.assertGreater(payload['words']['business'], payload['words']['abashed'])
