"""Completed cards count successful rounds, including correction and assistance."""
from datetime import date
import unittest
from unittest.mock import patch

from app import database
import test_senses as fixtures


class CompletedCardStatsTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    ready_relearning = fixtures.SenseLearningTests.ready_relearning

    def stats(self):
        response = self.client.get('/api/stats')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_repeated_errors_correction_and_retries_count_one_completed_round(self):
        card = self.next()
        for _ in range(10):
            response = self.review(card, 'wrong')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['is_correct'])
        before = self.stats()
        self.assertEqual((before['today_learning'], before['today_completed_cards']), (10, 0))
        resumed = self.next()
        self.assertEqual(resumed['attempt_id'], card['attempt_id'])
        corrected = self.review(resumed, resumed['answer_form'])
        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertFalse(corrected.json()['is_independent'])
        after = self.stats()
        self.assertEqual((after['today_learning'], after['today_completed_cards']), (11, 1))
        self.assertEqual(after['today_success_senses'], 0)
        self.assertEqual(self.review(resumed, resumed['answer_form']).status_code, 409)
        self.assertEqual(self.stats()['today_completed_cards'], 1)
        other = self.next()
        self.assertNotEqual(other['attempt_id'], card['attempt_id'])
        self.assertEqual(self.stats()['today_completed_cards'], 1)

    def test_assisted_round_and_later_independent_round_each_count_as_a_card(self):
        card = self.next()
        hint = self.client.post('/api/study/hint', json={
            'attempt_id': card['attempt_id'], 'kind': 'pronunciation'})
        self.assertEqual(hint.status_code, 200, hint.text)
        response = self.review(card, card['answer_form'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['is_independent'])
        first = self.stats()
        self.assertEqual((first['today_completed_cards'], first['today_success_senses']), (1, 0))
        self.ready_relearning()
        retry = self.next()
        self.assertEqual(retry['learning_unit_id'], card['learning_unit_id'])
        self.assertNotEqual(retry['attempt_id'], card['attempt_id'])
        self.assertTrue(self.review(retry, retry['answer_form']).json()['is_independent'])
        second = self.stats()
        self.assertEqual((second['today_completed_cards'], second['today_success_senses']), (2, 1))

    def test_completion_uses_answer_day_in_the_learners_timezone(self):
        self.client.patch('/api/auth/me', json={'timezone': 'Asia/Shanghai'})
        with patch('app.main.today_in', return_value=date(2026, 1, 1)), \
             patch('app.main.user_today', return_value=date(2026, 1, 1)), \
             patch('app.sense_learning.utc_now_iso', return_value='2026-01-01T15:59:59+00:00'):
            card = self.next()
            self.assertEqual(self.review(card, 'wrong').status_code, 200)
            self.assertEqual(self.stats()['today_completed_cards'], 0)
        with patch('app.main.today_in', return_value=date(2026, 1, 2)), \
             patch('app.main.user_today', return_value=date(2026, 1, 2)), \
             patch('app.sense_learning.utc_now_iso', return_value='2026-01-01T16:00:00+00:00'):
            self.assertEqual(self.review(card, card['answer_form']).status_code, 200)
            current = self.stats()
            self.assertEqual((current['today_learning'], current['today_completed_cards']), (1, 1))
        with patch('app.main.today_in', return_value=date(2026, 1, 3)):
            self.assertEqual(self.stats()['today_completed_cards'], 0)

    def test_retiring_unanswered_round_is_not_completion_and_legacy_answers_remain(self):
        card = self.next()
        muted = self.client.post(f"/api/words/{card['id']}/mute")
        self.assertEqual(muted.status_code, 200, muted.text)
        self.assertEqual(self.stats()['today_completed_cards'], 0)
        self.client.delete(f"/api/muted-words/{card['word']}")
        replacement = self.next()
        self.assertTrue(self.review(replacement, replacement['answer_form']).json()['is_correct'])
        with database.connect() as conn:
            conn.execute('UPDATE review_history SET attempt_id=NULL WHERE user_id=?', (self.uid,))
        self.assertEqual(self.stats()['today_completed_cards'], 1)

    def test_immediate_review_deduplicates_words_and_waits_for_relearning(self):
        first = self.next()
        self.assertTrue(self.review(first, first['answer_form']).json()['is_independent'])
        self.assertEqual(self.stats()['learning'], 1)
        second = self.next()
        self.assertEqual(second['id'], first['id'])
        self.assertNotEqual(second['learning_unit_id'], first['learning_unit_id'])
        self.assertFalse(self.review(second, 'wrong').json()['is_correct'])
        self.assertEqual(self.stats()['learning'], 1)
        self.assertTrue(self.review(second, second['answer_form']).json()['is_correct'])
        before = self.stats()
        self.assertEqual((before['due_senses'], before['due_words']), (0, 0))
        self.assertEqual(before['learning'], 1)
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01' WHERE user_id=? AND learning_unit_id=?",
                         (self.uid, first['learning_unit_id']))
        regular = self.stats()
        self.assertEqual((regular['due_senses'], regular['due_words']), (1, 1))
        self.assertEqual(regular['learning'], 0)
        self.ready_relearning()
        ready = self.stats()
        self.assertEqual((ready['due_senses'], ready['due_words']), (2, 1))
        self.assertEqual(ready['pending_relearning_senses'], 1)
        self.assertEqual(ready['learning'], 0)

    def test_learning_requires_a_correct_answer_and_excludes_due_and_mature_words(self):
        first = self.next()
        self.assertFalse(self.review(first, 'wrong').json()['is_correct'])
        self.assertEqual(self.stats()['learning'], 0)
        self.assertTrue(self.review(first, first['answer_form']).json()['is_correct'])
        self.assertEqual(self.stats()['learning'], 1)
        self.assertEqual(self.client.patch('/api/settings', json={'skip_basic_600': True}).status_code, 200)
        self.assertEqual(self.stats()['learning'], 0)
        self.assertEqual(self.client.patch('/api/settings', json={'skip_basic_600': False}).status_code, 200)
        self.assertEqual(self.stats()['learning'], 1)
        self.assertEqual(self.client.post(f"/api/words/{first['id']}/mute").status_code, 200)
        self.assertEqual(self.stats()['learning'], 0)
        self.assertEqual(self.client.delete(f"/api/muted-words/{first['word']}").status_code, 200)
        self.assertEqual(self.stats()['learning'], 1)
        self.ready_relearning()
        due = self.stats()
        self.assertEqual((due['learning'], due['due_words']), (0, 1))
        retry = self.next()
        self.assertTrue(self.review(retry, retry['answer_form']).json()['is_independent'])
        later = self.stats()
        self.assertEqual((later['learning'], later['due_words']), (1, 0))
        second = self.next()
        self.assertEqual(second['id'], first['id'])
        self.assertTrue(self.review(second, second['answer_form']).json()['is_independent'])
        self.assertEqual(self.stats()['learning'], 1)
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET status='Mature' WHERE user_id=?", (self.uid,))
        mature = self.stats()
        self.assertEqual((mature['learning'], mature['due_words'], mature['mature']), (0, 0, 1))


    def test_all_progress_counters_exclude_skipped_and_muted_words_without_erasing_history(self):
        first = self.next()
        self.assertTrue(self.review(first, first['answer_form']).json()['is_independent'])
        second = self.next()
        self.assertEqual(second['id'], first['id'])
        self.assertTrue(self.review(second, second['answer_form']).json()['is_independent'])
        keys = ('total_learned', 'learned_senses', 'mastered_senses', 'mastered', 'mature', 'learning')
        def counts():
            stats = self.stats()
            return tuple(stats[key] for key in keys)
        active = (1, 2, 2, 1, 0, 1)
        self.assertEqual(counts(), active)
        with database.connect() as conn:
            before_history = [tuple(row) for row in conn.execute('SELECT * FROM review_history WHERE user_id=? ORDER BY id', (self.uid,))]
            before_progress = [tuple(row) for row in conn.execute('SELECT * FROM sense_srs_state WHERE user_id=? ORDER BY learning_unit_id', (self.uid,))]
        self.assertEqual(self.client.patch('/api/settings', json={'skip_basic_600': True}).status_code, 200)
        self.assertEqual(counts(), (0, 0, 0, 0, 0, 0))
        self.assertEqual(self.client.patch('/api/settings', json={'skip_basic_600': False}).status_code, 200)
        self.assertEqual(counts(), active)
        self.assertEqual(self.client.post(f"/api/words/{first['id']}/mute").status_code, 200)
        self.assertEqual(counts(), (0, 0, 0, 0, 0, 0))
        self.assertEqual(self.client.delete(f"/api/muted-words/{first['word']}").status_code, 200)
        self.assertEqual(counts(), active)
        # Deselection affects new words, not already started reviews.
        self.assertEqual(self.client.patch('/api/settings', json={'selected_word_list_ids': []}).status_code, 200)
        self.assertEqual(counts(), active)
        with database.connect() as conn:
            self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM review_history WHERE user_id=? ORDER BY id', (self.uid,))], before_history)
            self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM sense_srs_state WHERE user_id=? ORDER BY learning_unit_id', (self.uid,))], before_progress)
            conn.execute("UPDATE sense_srs_state SET status='Mature' WHERE user_id=?", (self.uid,))
        self.assertEqual(counts(), (1, 2, 2, 1, 1, 0))
        self.assertEqual(self.client.patch('/api/settings', json={'skip_basic_600': True}).status_code, 200)
        self.assertEqual(counts(), (0, 0, 0, 0, 0, 0))
        self.assertEqual(self.client.patch('/api/settings', json={'skip_basic_600': False}).status_code, 200)
        self.assertEqual(counts(), (1, 2, 2, 1, 1, 0))


if __name__ == '__main__':
    unittest.main()
