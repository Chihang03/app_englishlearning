from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient
from app import database
from app.learning_filters import BASIC_WORDS
from app.senses import save_senses
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

    def test_exact_600_boundary_and_case_insensitive_legacy_private_words(self):
        self.assertEqual(len(BASIC_WORDS), 600)
        self.assertIn('contact', BASIC_WORDS)  # rank 600
        self.assertNotIn('particularly', BASIC_WORDS)  # rank 601
        self.client.patch('/api/settings', json={'selected_word_list_ids': []})
        # upon has no catalog example; the full source list must still cover it.
        for word in ('Contact', 'upon', 'I', 'Address', 'particularly'):
            fixtures.legacy_private_word(self.uid, {
                'word': word, 'part_of_speech': '词汇', 'definition_cn': '测试',
                'example_sentence': f'This example contains {word}.'})
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


class BasicWordFormTests(unittest.TestCase):
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    ready_relearning = fixtures.SenseLearningTests.ready_relearning
    skip = BasicWordsTests.skip

    def setUp(self):
        fixtures.SenseLearningTests.setUp(self)
        self.client.patch('/api/settings', json={'selected_word_list_ids': []})

    def word(self, word, pos='代词'):
        return fixtures.legacy_private_word(self.uid, {
            'word': word, 'part_of_speech': pos, 'definition_cn': '测试',
            'example_sentence': f'This example contains {word}.'})

    def snapshots(self):
        with database.connect() as conn:
            return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in ('sense_srs_state', 'adaptive_memory', 'relearning_queue',
                                  'review_history', 'user_sense_examples')}

    def test_object_possessive_reflexive_and_demonstrative_forms_are_skipped(self):
        # An explicit regression list independent of the implementation mapping.
        forms = ('he', 'I', 'him', 'me', 'his', 'my', 'mine', 'myself', 'her', 'hers',
                 'herself', 'us', 'our', 'ours', 'ourselves', 'them', 'their', 'theirs',
                 'themselves', 'themself', 'your', 'yours', 'yourself', 'yourselves',
                 'its', 'itself', 'whom', 'whose', 'these', 'those', 'oneself')
        for word in forms:
            with self.subTest(word=word):
                pos = 'possessive determiner' if word == 'our' else (
                    '限定词' if word in ('my', 'your', 'their', 'its') else (
                    '形容词' if word == 'whose' else '代词'))
                self.word(word, pos)
        self.assertEqual(self.client.get('/api/stats').json()['new_words'], len(forms))
        self.skip()
        self.assertIsNone(self.next())
        stats = self.client.get('/api/stats').json()
        self.assertEqual((stats['new_words'], stats['new_senses'], stats['due_senses']), (0, 0, 0))
        self.assertEqual(len(BASIC_WORDS), 600)
        self.skip(False)
        self.assertEqual(self.client.get('/api/stats').json()['new_words'], len(forms))

    def test_case_insensitive_untyped_legacy_forms_are_also_skipped(self):
        for word in ('HIM', 'Me', 'OUR'):
            self.word(word, '词汇')
        self.skip()
        self.assertIsNone(self.next())

    def test_active_form_rounds_and_legacy_answers_stop_immediately(self):
        self.word('him')
        card = self.next()
        self.assertEqual(card['word'], 'him')
        self.skip()
        self.assertIsNone(self.next())
        self.assertEqual(self.review(card, 'him').status_code, 409)
        self.assertEqual(self.client.post('/api/review', json={
            'word_id': card['id'], 'sense_id': card['sense_id'],
            'example_id': card['example_id'], 'user_answer': 'him'}).status_code, 409)
        with database.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT completed_at FROM study_attempts WHERE id=?',
                                             (card['attempt_id'],)).fetchone()[0])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0], 0)
        self.skip(False)
        restored = self.next()
        self.assertNotEqual(restored['attempt_id'], card['attempt_id'])
        self.assertEqual(restored['word'], 'him')

    def test_due_forms_and_progress_are_hidden_without_changing_history(self):
        self.word('me')
        card = self.next()
        self.assertEqual(self.review(card, 'me').status_code, 200)
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01'")
        before = self.snapshots()
        self.skip()
        self.assertIsNone(self.next())
        stats = self.client.get('/api/stats').json()
        for key in ('total_learned', 'learned_senses', 'mastered_senses', 'mastered', 'mature',
                    'due_senses', 'due_words', 'learning', 'lapse_words'):
            self.assertEqual(stats[key], 0, key)
        self.skip(False)
        self.assertEqual(self.next()['word'], 'me')
        self.assertEqual(self.snapshots(), before)

    def test_form_relearning_queue_is_hidden_and_restored(self):
        self.word('him')
        card = self.next()
        self.assertEqual(self.review(card, 'wrong').status_code, 200)
        self.ready_relearning()
        before = self.snapshots()
        self.skip()
        self.assertIsNone(self.next())
        stats = self.client.get('/api/stats').json()
        self.assertIsNone(stats['next_relearning_at'])
        self.assertEqual((stats['pending_relearning_senses'], stats['lapse_words']), (0, 0))
        self.skip(False)
        restored = self.next()
        self.assertEqual(restored['word'], 'him')
        self.assertTrue(restored['is_relearning'])
        self.assertEqual(self.snapshots(), before)

    def test_mixed_word_skips_only_the_pronoun_unit_and_counts_remaining_progress(self):
        word = self.word('mine')
        with database.connect() as conn:
            save_senses(conn, word['id'], [
                fixtures.sense('possessive', '我的', 'This book is mine.', target='mine', pos='代词'),
                fixtures.sense('noun', '矿井', 'They worked in the mine.', target='mine', pos='名词')])
        pronoun = self.next()
        self.assertEqual(pronoun['definition_cn'], '我的')
        self.assertEqual(self.review(pronoun, 'mine').status_code, 200)
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01'")
        before = self.snapshots()
        self.skip()
        stats = self.client.get('/api/stats').json()
        self.assertEqual((stats['new_senses'], stats['total_learned'], stats['learned_senses'],
                          stats['due_senses']), (1, 0, 0, 0))
        self.assertEqual(self.snapshots(), before)
        noun = self.next()
        self.assertEqual((noun['word'], noun['part_of_speech'], noun['definition_cn']), ('mine', '名词', '矿井'))
        before_rejection = self.snapshots()
        # Even an unsigned compatibility submission cannot bypass unit filtering.
        self.assertEqual(self.client.post('/api/review', json={
            'word_id': pronoun['id'], 'sense_id': pronoun['sense_id'],
            'example_id': pronoun['example_id'], 'user_answer': 'mine'}).status_code, 409)
        self.assertEqual(self.snapshots(), before_rejection)
        self.assertEqual(self.review(noun, 'mine').status_code, 200)
        stats = self.client.get('/api/stats').json()
        self.assertEqual((stats['total_learned'], stats['learned_senses'], stats['mastered_senses'],
                          stats['mastered']), (1, 1, 1, 1))
        self.skip(False)
        stats = self.client.get('/api/stats').json()
        self.assertEqual((stats['learned_senses'], stats['due_senses']), (2, 1))

    def test_enabling_skip_preserves_an_active_independent_noun_round(self):
        self.word('mine', '名词')
        noun = self.next()
        self.skip()
        self.assertEqual(self.next()['attempt_id'], noun['attempt_id'])
        self.assertEqual(self.review(noun, 'mine').status_code, 200)

    def test_independent_nouns_and_derivatives_remain_learnable(self):
        retained = ('mine', 'mining', 'meeting', 'building', 'saying', 'particularly')
        for word in retained:
            self.word(word, '名词')
        self.skip()
        issued = []
        for _ in retained:
            card = self.next()
            self.assertIsNotNone(card)
            issued.append(card['word'])
            self.assertEqual(self.review(card, card['answer_form']).status_code, 200)
        self.assertCountEqual(issued, retained)
        self.assertIsNone(self.next())

    def test_public_exam_forms_are_skipped_across_lists_and_settings_stay_user_scoped(self):
        catalog = fixtures.catalog()
        catalog['words'] = [{
            'word': 'him', 'pronunciation': 'him', 'part_of_speech': '代词',
            'definition_cn': '他', 'example_sentence': 'I saw him.',
            'memberships': [{'list_id': 'cet4', 'position': 1}, {'list_id': 'cet6', 'position': 1}],
            'senses': [fixtures.sense('object', '他', 'I saw him.', target='him', pos='代词')]}]
        self.catalog_path.write_text(json.dumps(catalog, ensure_ascii=False))
        database.init_database()
        self.skip()
        for selected in (['cet4'], ['cet6'], ['cet4', 'cet6']):
            self.client.patch('/api/settings', json={'selected_word_list_ids': selected})
            self.assertIsNone(self.next())
        with TestClient(fixtures.app) as other:
            response = other.post('/api/auth/register', json={'username': 'other', 'password': 'test-password-123'})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(other.get('/api/settings').json()['skip_basic_600'])
            self.assertEqual(other.get('/api/next').json()['card']['word'], 'him')
