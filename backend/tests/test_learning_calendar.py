"""Calendar history must match daily stats without crossing account/day boundaries."""
from datetime import date
import unittest
from unittest.mock import patch

from app import database
import test_senses as fixtures


class LearningCalendarTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review

    def calendar(self, month="2026-10"):
        response = self.client.get('/api/learning-calendar', params={'month': month})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return response.json()

    def record(self, at, correct, attempt=None, milliseconds=None, first=None):
        card = getattr(self, 'calendar_card', None)
        if card is None:
            card = self.calendar_card = self.next()
        with database.connect() as conn:
            if attempt is not None:
                conn.execute('''INSERT OR IGNORE INTO study_attempts
                    (id,user_id,sense_id,example_id,learning_unit_id,created_at,completed_at)
                    VALUES(?,?,?,?,?,?,?)''',
                    (attempt,self.uid,card['sense_id'],card['example_id'],card['learning_unit_id'],at,at))
            conn.execute('''INSERT INTO review_history
                (user_id,word_id,review_time,user_answer,is_correct,attempt_id,active_response_ms,is_first_attempt)
                VALUES(?,?,?,?,?,?,?,?)''',
                (self.uid, card['id'], at, 'answer', correct, attempt, milliseconds, first))

    def test_empty_leap_month_and_default_account_date(self):
        payload = self.calendar('2024-02')
        self.assertEqual(len(payload['days']), 29)
        self.assertEqual(payload['days'][-1]['date'], '2024-02-29')
        self.assertEqual(payload['summary'], {'learning_days': 0, 'completed_cards': 0})
        self.assertEqual(payload['days'][0]['study_time_ms'], 0)
        self.assertIsNone(payload['days'][0]['first_attempt_accuracy'])
        with patch('app.learning_calendar.today_in', return_value=date(2026, 10, 8)):
            current = self.client.get('/api/learning-calendar').json()
        self.assertEqual((current['month'], current['today']), ('2026-10', '2026-10-08'))

    def test_month_validation_and_authentication(self):
        for month in ('2026-00', '2026-13', '2026-1', '26-10', '0000-01', '9999-12', 'garbage', ''):
            with self.subTest(month=month):
                self.assertEqual(self.client.get('/api/learning-calendar', params={'month': month}).status_code, 422)
        self.client.post('/api/auth/logout')
        self.assertEqual(self.client.get('/api/learning-calendar').status_code, 401)

    def test_rounds_timing_and_accuracy_match_existing_stats(self):
        self.client.patch('/api/auth/me', json={'timezone': 'Asia/Shanghai'})
        self.record('2026-10-08T01:00:00+00:00', 0, 'round-a', 4200, 1)
        self.record('2026-10-08T01:01:00+00:00', 0, 'round-a', 1000, 0)
        self.record('2026-10-08T01:02:00+00:00', 1, 'round-a', 1800, 0)
        # Duplicate successes count once per round, while legacy records count individually.
        self.record('2026-10-08T01:03:00+00:00', 1, 'round-a', None, 0)
        self.record('2026-10-08T01:04:00+00:00', 1, 'round-b', 3000, 1)
        self.record('2026-10-08T01:05:00+00:00', 1)
        self.record('2026-10-08T01:06:00+00:00', 1)
        day = self.calendar()['days'][7]
        self.assertEqual((day['reviews'], day['completed_cards'], day['study_time_ms'],
                          day['first_attempt_accuracy']), (7, 4, 10000, 50))
        with patch('app.main.today_in', return_value=date(2026, 10, 8)):
            stats = self.client.get('/api/stats').json()
        self.assertEqual(day['completed_cards'], stats['today_completed_cards'])
        self.assertEqual(day['study_time_ms'], stats['total_study_time_ms'])
        self.assertEqual(day['first_attempt_accuracy'], stats['today_independent_accuracy'])

    def test_local_month_boundaries_and_error_only_learning_day(self):
        self.client.patch('/api/auth/me', json={'timezone': 'Asia/Shanghai'})
        self.record('2026-09-30T15:59:59+00:00', 1, 'earlier', 100)
        self.record('2026-09-30T16:00:00+00:00', 0, 'cross-day', 200, 1)
        self.record('2026-10-01T16:00:00+00:00', 1, 'cross-day', 300, 0)
        self.record('2026-10-31T15:59:59+00:00', 1, 'last', 400, 1)
        self.record('2026-10-31T16:00:00+00:00', 1, 'later', 500)
        payload = self.calendar()
        self.assertEqual(payload['summary'], {'learning_days': 3, 'completed_cards': 2})
        self.assertEqual(payload['days'][0]['completed_cards'], 0)
        self.assertEqual(payload['days'][0]['reviews'], 1)
        self.assertEqual(payload['days'][0]['first_attempt_accuracy'], 0)
        self.assertEqual(payload['days'][1]['completed_cards'], 1)
        self.assertEqual(payload['days'][-1]['study_time_ms'], 400)

    def test_dst_boundary_and_missing_legacy_timing(self):
        self.client.patch('/api/auth/me', json={'timezone': 'America/New_York'})
        self.record('2026-11-01T03:59:59+00:00', 1, 'october')
        self.record('2026-11-01T04:00:00+00:00', 1, 'start')
        self.record('2026-11-01T05:30:00+00:00', 1, 'before-dst')
        self.record('2026-11-01T06:30:00+00:00', 1, 'after-dst')
        self.record('2026-11-02T04:59:59+00:00', 1, 'end')
        self.record('2026-11-02T05:00:00+00:00', 1, 'next-day', 0)
        payload = self.calendar('2026-11')
        self.assertEqual(payload['days'][0]['completed_cards'], 4)
        self.assertIsNone(payload['days'][0]['study_time_ms'])
        self.assertIsNone(payload['days'][0]['first_attempt_accuracy'])
        self.assertEqual(payload['days'][1]['completed_cards'], 1)
        self.assertEqual(payload['days'][1]['study_time_ms'], 0)

    def test_account_isolation_and_preserved_muted_history(self):
        card = self.next()
        with patch('app.sense_learning.utc_now_iso', return_value='2026-10-08T01:00:00+00:00'):
            self.assertEqual(self.review(card, card['answer_form']).status_code, 200)
        expected = self.calendar()
        self.client.post(f"/api/words/{card['id']}/mute")
        self.client.patch('/api/settings', json={'skip_basic_600': True, 'selected_word_list_ids': []})
        self.assertEqual(self.calendar(), expected)
        self.client.post('/api/auth/logout')
        self.assertEqual(self.client.post('/api/auth/register', json={
            'username': 'other-calendar-user', 'password': 'test-password-123'}).status_code, 200)
        self.assertEqual(self.calendar()['summary'], {'learning_days': 0, 'completed_cards': 0})


if __name__ == '__main__':
    unittest.main()
