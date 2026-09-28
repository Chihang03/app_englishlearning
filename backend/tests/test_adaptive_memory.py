"""Delayed recall, hint integrity, maturity audits and chronological calibration."""
from __future__ import annotations

import math
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_senses as fixtures
from app import database, migrations
from app.adaptive_memory import fit_multiplier, log_loss, maybe_calibrate, scheduler
from fsrs import Card, Rating


class AdaptiveMemoryTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    ready_relearning = fixtures.SenseLearningTests.ready_relearning

    @contextmanager
    def at(self, day):
        moment = datetime.fromisoformat(day + 'T04:00:00+00:00')
        with patch('app.main.user_today',return_value=moment.date()), \
             patch('app.main.today_in',return_value=moment.date()), \
             patch('app.sense_learning.utc_now_iso',return_value=moment.isoformat()), \
             patch('app.sense_learning.datetime',wraps=datetime) as clock:
            clock.now.return_value = moment
            yield

    def hint(self, card, kind='pronunciation', client=None):
        return (client or self.client).post('/api/study/hint',json={'attempt_id':card['attempt_id'],'kind':kind})

    def test_known_word_three_delayed_answers_and_mature_audit_failure(self):
        with self.at('2026-01-01'):
            first=self.next();result=self.review(first,'address').json()
            self.assertTrue(result['memory']['known_candidate'])
            self.assertEqual(result['srs_state']['next_review_date'],'2026-01-31')
            self.assertIsNone(result['memory']['predicted_recall_probability'])
            self.review(self.next(),'addressed')
        with self.at('2026-01-30'):
            self.assertIsNone(self.next())
            self.assertEqual(self.client.get('/api/stats').json()['due_review'],0)
            # Compatibility requests also cannot manufacture early confirmations.
            response=self.client.post('/api/review',json={'word_id':first['id'],'sense_id':first['sense_id'],
                'example_id':first['example_id'],'user_answer':'address'})
            self.assertEqual(response.status_code,409)
        with self.at('2026-01-31'):
            second=self.next();self.assertEqual(second['example_id'],first['example_id'])
            self.assertEqual(second['confirmations'],1)
            result=self.review(second,'address').json()
            self.assertEqual(result['memory']['confirmations'],2)
            self.assertAlmostEqual(result['memory']['predicted_recall_probability'],0.9,places=4)
            self.assertEqual(result['srs_state']['next_review_date'],'2026-04-01')
            self.review(self.next(),'addressed')
        with self.at('2026-04-01'):
            third=self.next();result=self.review(third,'address').json()
            self.assertEqual(result['srs_state']['status'],'Mature')
            self.assertGreaterEqual(result['memory']['stability_days'],180)
            self.assertEqual(result['memory']['confirmations'],3)
            self.assertEqual(result['srs_state']['next_review_date'],'2026-09-28')
            self.review(self.next(),'addressed')
            self.assertEqual(self.client.get('/api/stats').json()['mature'],1)
        with self.at('2026-09-27'):
            self.assertEqual(self.client.get('/api/stats').json()['due_review'],0)
        with self.at('2026-09-28'):
            self.assertEqual(self.client.get('/api/stats').json()['due_review'],2)
            audit=self.next();self.assertEqual(audit['status'],'Mature')
            failed=self.review(audit,'bad').json()
            self.assertEqual(failed['srs_state']['status'],'Learning')
            self.assertFalse(failed['memory']['known_candidate'])
            self.assertEqual(failed['memory']['confirmations'],0)
            self.assertEqual(self.client.get('/api/stats').json()['mature'],0)

    def test_pronunciation_survives_reload_is_assisted_and_does_not_train(self):
        card=self.next();self.assertEqual(self.hint(card).status_code,200)
        resumed=self.next();self.assertTrue(resumed['pronunciation_used'])
        result=self.review(resumed,'address').json()
        self.assertEqual(result['outcome'],'assisted')
        self.assertFalse(result['is_independent'])
        self.assertFalse(result['memory']['known_candidate'])
        self.assertEqual(result['srs_state']['correct_count'],0)
        self.assertEqual(self.client.get('/api/stats').json()['today_success'],0)
        self.assertEqual(self.hint(card).status_code,409)
        with database.connect() as conn:
            row=conn.execute('SELECT * FROM review_history').fetchone()
            self.assertEqual(row['pronunciation_used'],1)
            self.assertIsNone(row['base_recall_probability'])
        self.ready_relearning()
        retry=self.next();self.assertFalse(retry['pronunciation_used'])
        result=self.review(retry,'address').json()
        self.assertTrue(result['is_independent'])
        self.assertFalse(result['memory']['known_candidate'])
        self.assertLess(result['srs_state']['interval_days'],30)

    def test_known_failure_returns_to_learning_and_correction_cannot_restore_prior(self):
        with self.at('2026-01-01'):
            self.review(self.next(),'address');self.review(self.next(),'addressed')
        with self.at('2026-01-31'):
            card=self.next();failed=self.review(card,'bad').json()
            self.assertEqual(failed['memory']['confirmations'],0)
            corrected=self.review(card,'address').json()
            self.assertEqual(corrected['memory']['stability_days'],failed['memory']['stability_days'])
            self.assertFalse(corrected['is_independent'])
            with database.connect() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history WHERE base_recall_probability IS NOT NULL').fetchone()[0],1)

    def test_hint_account_isolation_and_other_sense_preview(self):
        card=self.next()
        with fixtures.TestClient(fixtures.app) as other:
            other.post('/api/auth/register',json={'username':'other','password':'test-password-123'})
            self.assertEqual(self.hint(card,client=other).status_code,409)
            self.assertEqual(other.post('/api/study/related-exposure',json={'attempt_id':card['attempt_id']}).status_code,409)
        self.review(card,'address')
        response=self.client.post('/api/study/related-exposure',json={'attempt_id':card['attempt_id']})
        self.assertEqual(response.status_code,200,response.text)
        next_card=self.next();self.assertTrue(next_card['answer_exposed'])
        result=self.review(next_card,'addressed').json()
        self.assertEqual(result['outcome'],'assisted')
        self.assertFalse(result['memory']['known_candidate'])

    def test_active_duration_validation_and_storage(self):
        card=self.next()
        payload={'word_id':card['id'],'sense_id':card['sense_id'],'example_id':card['example_id'],
                 'attempt_id':card['attempt_id'],'user_answer':'address','active_response_ms':300001}
        self.assertEqual(self.client.post('/api/review',json=payload).status_code,422)
        payload['active_response_ms']=4200
        self.assertEqual(self.client.post('/api/review',json=payload).status_code,200)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT active_response_ms FROM review_history').fetchone()[0],4200)

    def test_open_round_from_before_upgrade_cannot_establish_known_word_prior(self):
        card=self.next()
        with database.connect() as conn:
            conn.execute('UPDATE study_attempts SET tracking_version=0 WHERE id=?',(card['attempt_id'],))
        result=self.review(card,'address').json()
        self.assertTrue(result['is_independent'])
        self.assertFalse(result['memory']['known_candidate'])
        self.assertLess(result['srs_state']['interval_days'],30)

    def test_existing_schedules_and_historical_mature_words_remain_intact(self):
        with database.connect() as conn:
            conn.execute("""INSERT INTO sense_srs_state(user_id,sense_id,interval_days,next_review_date,status)
                SELECT ?,id,90,'2000-01-01','Mature' FROM word_senses WHERE sense_key='place'""",(self.uid,))
        self.assertEqual(self.next()['definition_cn'],'处理')
        with database.connect() as conn:
            row=conn.execute("SELECT * FROM sense_srs_state WHERE status='Mature'").fetchone()
            self.assertEqual(row['interval_days'],90)
            self.assertEqual(row['next_review_date'],'2000-01-01')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM adaptive_memory').fetchone()[0],0)

    def test_calibration_uses_chronological_holdout_and_only_improves_if_supported(self):
        with database.connect() as conn:
            word_id=conn.execute("SELECT id FROM words WHERE word='address'").fetchone()[0]
            sense_ids=[]
            for i in range(20):
                sid=conn.execute("""INSERT INTO word_senses(word_id,sense_key,part_of_speech,definition_cn,source,position)
                    VALUES(?,?,'名词','校准测试','test',?) RETURNING id""",(word_id,f'calibration-{i}',i)).fetchone()[0]
                sense_ids.append(sid)
            start=datetime(2026,1,1,tzinfo=timezone.utc)
            def add(count, offset, success_rate):
                for i in range(count):
                    conn.execute("""INSERT INTO review_history(user_id,word_id,sense_id,review_time,user_answer,
                        is_correct,is_independent,is_first_attempt,base_recall_probability)
                        VALUES(?,?,?,?,?,?,?,?,?)""",(self.uid,word_id,sense_ids[i%20],
                        (start+timedelta(days=(offset+i)/3)).isoformat(),'test',int(i%10<success_rate),
                        int(i%10<success_rate),1,0.9))
            add(149,0,8)
            maybe_calibrate(conn,self.uid,(start+timedelta(days=50)).isoformat())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memory_profiles').fetchone()[0],0)
            add(1,149,0)
            maybe_calibrate(conn,self.uid,(start+timedelta(days=50)).isoformat())
            row=conn.execute('SELECT * FROM memory_profiles').fetchone()
            self.assertEqual((row['training_count'],row['validation_count']),(120,30))
            self.assertTrue(row['adopted'])
            self.assertGreater(row['forgetting_multiplier'],1)
            self.assertLess(row['candidate_log_loss'],row['baseline_log_loss'])
            # A future distribution change must be evaluated against its own
            # holdout rather than silently adopting the training-only optimum.
            add(50,150,10)
            maybe_calibrate(conn,self.uid,(start+timedelta(days=67)).isoformat())
            updated=conn.execute('SELECT * FROM memory_profiles').fetchone()
            self.assertFalse(updated['adopted'])
            self.assertEqual(updated['forgetting_multiplier'],1.0)


class MemoryModelMathTests(unittest.TestCase):
    def test_personal_forgetting_speed_changes_intervals_and_probability(self):
        moment=datetime(2026,1,1,tzinfo=timezone.utc)
        initial=Card(state=2,step=None,stability=30,difficulty=5,last_review=moment)
        later=moment+timedelta(days=30)
        normal,_=scheduler().review_card(initial,Rating.Good,later)
        faster,_=scheduler(2).review_card(initial,Rating.Good,later)
        slower,_=scheduler(0.5).review_card(initial,Rating.Good,later)
        self.assertLess(faster.due,normal.due)
        self.assertGreater(slower.due,normal.due)
        self.assertEqual(normal.stability,faster.stability)
        self.assertAlmostEqual(scheduler().get_card_retrievability(initial,later),0.9,places=6)

    def test_calibration_is_finite_bounded_and_regularized(self):
        self.assertTrue(math.isfinite(log_loss([(0,True),(1,False)],1)))
        self.assertLessEqual(fit_multiplier([(0.9,False)]*120),2)
        self.assertGreaterEqual(fit_multiplier([(0.9,True)]*120),0.5)


if __name__ == '__main__':
    unittest.main()
