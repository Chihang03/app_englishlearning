from __future__ import annotations

import sqlite3
import unittest

from fastapi.testclient import TestClient
from app import database, migrations
from app.senses import save_senses
import test_senses as fixtures


class MutedWordsTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    ready_relearning = fixtures.SenseLearningTests.ready_relearning

    def mute(self, card):
        response = self.client.post(f"/api/words/{card['id']}/mute")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def restore(self, word="address"):
        response = self.client.delete(f"/api/muted-words/{word}")
        self.assertEqual(response.status_code, 200, response.text)

    def test_all_senses_across_lists_and_refresh_are_muted_with_no_credit(self):
        card = self.next()
        before = self.client.get('/api/stats').json()
        self.assertEqual(before['new_senses'], 2)
        self.mute(card)
        self.mute(card)  # Network retries are harmless.
        self.assertEqual(self.client.get('/api/muted-words').json()['words'][0]['word'], 'address')
        self.assertIsNone(self.next())
        self.assertEqual(self.review(card, 'address').status_code, 409)
        payload = {'word_id': card['id'], 'sense_id': card['sense_id'],
                   'example_id': card['example_id'], 'user_answer': 'address'}
        self.assertEqual(self.client.post('/api/review', json=payload).status_code, 409)
        stats = self.client.get('/api/stats').json()
        self.assertEqual((stats['new_words'], stats['new_senses'], stats['due_review']), (0, 0, 0))
        for name in ('today_learning', 'today_success', 'today_success_senses', 'mastered', 'mastered_senses'):
            self.assertEqual(stats[name], before[name])
        self.client.patch('/api/settings', json={'selected_word_list_ids': ['cet6']})
        self.assertEqual(self.next()['word'], 'bank')
        database.init_database()
        self.assertEqual(len(self.client.get('/api/muted-words').json()['words']), 1)
        self.assertEqual(self.next()['word'], 'bank')
        self.restore('ADDRESS')
        self.restore()  # Restore is also idempotent.
        self.assertEqual(self.client.get('/api/muted-words').json()['words'], [])
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'], 3)

    def test_wrong_answer_queue_stops_and_original_progress_returns(self):
        card = self.next()
        self.review(card, 'bad')
        with database.connect() as conn:
            state = tuple(conn.execute('SELECT * FROM sense_srs_state').fetchone())
            memory = tuple(conn.execute('SELECT * FROM adaptive_memory').fetchone())
            history = [tuple(row) for row in conn.execute('SELECT * FROM review_history')]
        self.mute(card)
        stats = self.client.get('/api/stats').json()
        self.assertIsNone(stats['next_relearning_at'])
        self.assertEqual((stats['learning'], stats['lapse_words']), (0, 0))
        self.ready_relearning()
        self.assertIsNone(self.next())
        self.assertEqual(self.client.get('/api/stats').json()['pending_relearning_senses'], 0)
        self.restore()
        restored = self.next()
        self.assertEqual(restored['sense_id'], card['sense_id'])
        self.assertNotEqual(restored['attempt_id'], card['attempt_id'])
        self.assertTrue(restored['is_relearning'])
        with database.connect() as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM sense_srs_state').fetchone()), state)
            self.assertEqual(tuple(conn.execute('SELECT * FROM adaptive_memory').fetchone()), memory)
            self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM review_history')], history)

    def test_due_reviews_and_remaining_count_exclude_every_muted_sense(self):
        first = self.next()
        self.review(first, 'address')
        second = self.next()
        self.review(second, 'addressed')
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01'")
        self.assertEqual(self.client.get('/api/stats').json()['due_review'], 2)
        card = self.next()
        self.mute(card)
        self.assertEqual(self.client.get('/api/stats').json()['due_review'], 0)
        self.assertIsNone(self.next())
        self.restore()
        restored = self.next()
        self.assertEqual(restored['remaining_today'], 2)
        self.assertEqual(self.client.get('/api/stats').json()['total_learned'], 1)

    def test_future_senses_and_private_duplicates_share_the_word_preference(self):
        card = self.next()
        self.mute(card)
        with database.connect() as conn:
            updated = fixtures.catalog()['words'][0]['senses'] + [
                fixtures.sense('speech', '演讲', 'He gave an address.')]
            save_senses(conn, card['id'], updated)
        response = self.client.post('/api/words', json={
            'word': 'Address', 'part_of_speech': '名词', 'definition_cn': '地址',
            'example_sentence': 'Please give me your Address.'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.next())
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'], 0)
        self.restore()
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'], 4)
        self.assertIsNotNone(self.next())

    def test_authentication_account_isolation_and_private_word_ownership(self):
        card = self.next()
        private = self.client.post('/api/words', json={
            'word': 'private', 'part_of_speech': '形容词', 'definition_cn': '私有',
            'example_sentence': 'This is private.'}).json()['word']
        self.mute(card)
        with TestClient(fixtures.app) as other:
            self.assertEqual(other.get('/api/muted-words').status_code, 401)
            self.assertEqual(other.post(f"/api/words/{card['id']}/mute").status_code, 401)
            self.assertEqual(other.delete('/api/muted-words/address').status_code, 401)
            other.post('/api/auth/register', json={'username': 'other', 'password': 'test-password-123'})
            self.assertEqual(other.get('/api/next').json()['card']['word'], 'address')
            self.assertEqual(other.get('/api/muted-words').json()['words'], [])
            self.assertEqual(other.post(f"/api/words/{private['id']}/mute").status_code, 404)
            self.assertEqual(other.post('/api/words/999999/mute').status_code, 404)
            other.delete('/api/muted-words/address')
        self.assertEqual(len(self.client.get('/api/muted-words').json()['words']), 1)

    def test_migration_backs_up_v8_and_preserves_existing_learning_data(self):
        card = self.next()
        self.review(card, 'address')
        tables = ('users', 'sense_srs_state', 'adaptive_memory', 'review_history', 'study_attempts')
        with database.connect() as conn:
            before = {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table}')] for table in tables}
            conn.execute('DROP TABLE user_muted_words')
            conn.execute("DROP TABLE admin_content_edits")
            conn.execute("DROP TABLE admin_content_overrides")
            conn.execute('ALTER TABLE users DROP COLUMN role')
            for column in ('resolution_notes', 'resolved_at', 'resolved_by'):
                conn.execute(f'ALTER TABLE content_reports DROP COLUMN {column}')
            conn.execute('PRAGMA user_version=8')
        migrations.run_migrations(database.DB_PATH)
        migrations.run_migrations(database.DB_PATH)
        with database.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], migrations.SCHEMA_VERSION)
            for table in tables:
                self.assertEqual([tuple(row) for row in conn.execute(f'SELECT * FROM {table}')], before[table])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        backups = list(self.directory.glob('test.db.bak-v8-*'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 8)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0], 1)
