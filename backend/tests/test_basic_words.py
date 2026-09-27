from __future__ import annotations

import unittest

from fastapi.testclient import TestClient
from app import database
from app.learning_filters import BASIC_WORDS
import test_senses as fixtures


class BasicWordsTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    ready_relearning = fixtures.SenseLearningTests.ready_relearning

    def skip(self, enabled=True):
        response = self.client.patch('/api/settings', json={'skip_basic_600': enabled})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['skip_basic_600'], enabled)

    def test_all_senses_across_lists_and_active_rounds_are_filtered_without_credit(self):
        card = self.next()  # address is NGSL rank 558; bank is rank 627.
        before = self.client.get('/api/stats').json()
        self.assertFalse(self.client.get('/api/settings').json()['skip_basic_600'])
        self.skip()
        self.skip()  # Retried settings writes are harmless.
        self.assertIsNone(self.next())
        self.assertEqual(self.review(card, 'address').status_code, 409)
        legacy = {'word_id': card['id'], 'sense_id': card['sense_id'],
                  'example_id': card['example_id'], 'user_answer': 'address'}
        self.assertEqual(self.client.post('/api/review', json=legacy).status_code, 409)
        stats = self.client.get('/api/stats').json()
        self.assertEqual((stats['new_words'], stats['new_senses'], stats['due_review']), (0, 0, 0))
        for name in ('today_learning', 'today_success', 'mastered', 'mastered_senses'):
            self.assertEqual(stats[name], before[name])
        self.client.patch('/api/settings', json={'selected_word_list_ids': ['cet6']})
        bank = self.next()
        self.assertEqual(bank['word'], 'bank')
        self.skip(False)
        # Toggling does not retire an unrelated round on this or another device.
        self.assertEqual(self.next()['attempt_id'], bank['attempt_id'])
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'], 3)
        self.assertEqual(self.client.get('/api/muted-words').json()['words'], [])

    def test_due_reviews_stop_and_original_srs_history_returns(self):
        first = self.next()
        self.review(first, 'address')
        second = self.next()
        self.review(second, 'addressed')
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01'")
            before = {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table}')]
                      for table in ('sense_srs_state', 'adaptive_memory', 'review_history')}
        self.assertEqual(self.client.get('/api/stats').json()['due_review'], 2)
        self.skip()
        self.assertIsNone(self.next())
        self.assertEqual(self.client.get('/api/stats').json()['due_review'], 0)
        self.skip(False)
        self.assertEqual(self.next()['remaining_today'], 2)
        with database.connect() as conn:
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in conn.execute(f'SELECT * FROM {table}')], rows)

    def test_relearning_wait_and_lapse_counts_stop_until_disabled(self):
        card = self.next()
        self.review(card, 'bad')
        self.skip()
        stats = self.client.get('/api/stats').json()
        self.assertIsNone(stats['next_relearning_at'])
        self.assertEqual((stats['learning'], stats['lapse_words']), (0, 0))
        self.ready_relearning()
        self.assertIsNone(self.next())
        self.assertEqual(self.client.get('/api/stats').json()['pending_relearning_senses'], 0)
        self.skip(False)
        restored = self.next()
        self.assertEqual(restored['sense_id'], card['sense_id'])
        self.assertNotEqual(restored['attempt_id'], card['attempt_id'])
        self.assertTrue(restored['is_relearning'])

    def test_exact_600_boundary_and_case_insensitive_private_imports(self):
        self.assertEqual(len(BASIC_WORDS), 600)
        self.assertIn('contact', BASIC_WORDS)  # rank 600
        self.assertNotIn('particularly', BASIC_WORDS)  # rank 601
        self.client.patch('/api/settings', json={'selected_word_list_ids': []})
        # upon has no catalog example; the full source list must still cover it.
        for word in ('Contact', 'upon', 'I', 'Address', 'particularly'):
            response = self.client.post('/api/words', json={
                'word': word, 'part_of_speech': '词汇', 'definition_cn': '测试',
                'example_sentence': f'This example contains {word}.'})
            self.assertEqual(response.status_code, 200, response.text)
        self.skip()
        stats = self.client.get('/api/stats').json()
        self.assertEqual((stats['new_words'], stats['new_senses']), (1, 1))
        self.assertEqual(self.next()['word'], 'particularly')
        self.skip(False)
        self.assertEqual(self.client.get('/api/stats').json()['new_words'], 5)

    def test_manual_mutes_remain_independent_and_do_not_restore_basic_words(self):
        card = self.next()
        self.client.post(f"/api/words/{card['id']}/mute")
        self.skip()
        self.skip(False)
        self.assertIsNone(self.next())
        self.assertEqual(self.client.get('/api/muted-words').json()['words'][0]['word'], 'address')
        self.skip()
        self.client.delete('/api/muted-words/address')
        self.assertIsNone(self.next())
        self.skip(False)
        self.assertEqual(self.next()['word'], 'address')

    def test_preference_persists_and_isolated_by_account_and_authentication(self):
        self.skip()
        database.init_database()
        self.client.post('/api/auth/logout')
        self.client.post('/api/auth/login', json={'username': 'learner', 'password': 'test-password-123'})
        self.assertTrue(self.client.get('/api/settings').json()['skip_basic_600'])
        with TestClient(fixtures.app) as other:
            self.assertEqual(other.patch('/api/settings', json={'skip_basic_600': True}).status_code, 401)
            other.post('/api/auth/register', json={'username': 'other', 'password': 'test-password-123'})
            self.assertFalse(other.get('/api/settings').json()['skip_basic_600'])
            self.assertEqual(other.get('/api/next').json()['card']['word'], 'address')
        self.assertIsNone(self.next())
        self.client.patch('/api/settings', json={'show_sentence_translation': True, 'speech_rate': 120})
        self.assertTrue(self.client.get('/api/settings').json()['skip_basic_600'])
