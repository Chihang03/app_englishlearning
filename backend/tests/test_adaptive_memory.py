"""Delayed recall, hint integrity, maturity audits and chronological calibration."""
from __future__ import annotations

import math
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_senses as fixtures
from app import database, migrations
from app.adaptive_memory import TARGET_RETENTION, fit_multiplier, log_loss, maybe_calibrate, scheduler
from fsrs import Card, Rating, State


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

    def test_model_only_first_answer_and_delayed_mature_audit_failure(self):
        moment = datetime(2026,1,1,4,tzinfo=timezone.utc)
        expected, _ = scheduler().review_card(Card(), Rating.Good, moment)
        with self.at('2026-01-01'):
            first=self.next();result=self.review(first,'address').json()
            self.assertFalse(result['memory']['known_candidate'])
            self.assertAlmostEqual(result['memory']['stability_days'],expected.stability,places=2)
            self.assertEqual(result['memory']['target_retention'],0.8)
            self.assertEqual(result['srs_state']['interval_days'],8)
            self.assertEqual(result['srs_state']['status'],'Reviewing')
            self.assertIsNone(result['memory']['predicted_recall_probability'])
            self.review(self.next(),'addressed')
        with self.at('2026-01-08'):
            self.assertIsNone(self.next())
            self.assertEqual(self.client.get('/api/stats').json()['due_review'],0)
            response=self.client.post('/api/review',json={'word_id':first['id'],'sense_id':first['sense_id'],
                'example_id':first['example_id'],'user_answer':'address'})
            self.assertEqual(response.status_code,409)
        for _ in range(10):
            due=result['srs_state']['next_review_date']
            with self.at(due):
                card=self.next();self.assertEqual(card['example_id'],first['example_id'])
                result=self.review(card,'address').json()
                self.review(self.next(),'addressed')
                if result['srs_state']['status']=='Mature':
                    self.assertGreaterEqual(result['memory']['audit_recall_probability'],0.8)
                    self.assertEqual(result['srs_state']['interval_days'],180)
                    self.assertEqual(self.client.get('/api/stats').json()['mature'],1)
                    break
                self.assertLess(result['memory']['audit_recall_probability'],0.8)
        else:
            self.fail('Repeated delayed independent answers never reached maturity')
        audit_day=result['srs_state']['next_review_date']
        with self.at((datetime.fromisoformat(audit_day)-timedelta(days=1)).date().isoformat()):
            self.assertEqual(self.client.get('/api/stats').json()['due_review'],0)
        with self.at(audit_day):
            self.assertEqual(self.client.get('/api/stats').json()['due_review'],2)
            audit=self.next();self.assertEqual(audit['status'],'Mature')
            failed=self.review(audit,'bad').json()
            self.assertEqual(failed['srs_state']['status'],'Learning')
            self.assertFalse(failed['memory']['known_candidate'])
            self.assertEqual(failed['memory']['confirmations'],0)
            self.assertEqual(self.client.get('/api/stats').json()['mature'],0)

    def seeded_review(self, stability, multiplier=1.0):
        with self.at('2026-01-01'):
            first=self.next();self.review(first,'address')
        moment=datetime(2026,1,1,4,tzinfo=timezone.utc)
        card=Card(card_id=first['learning_unit_id'],state=State.Review,step=None,
                  stability=stability,difficulty=5,last_review=moment)
        with database.connect() as conn:
            conn.execute("UPDATE adaptive_memory SET card_json=?,known_candidate=1,confirmations=0,first_independent_at=NULL,last_independent_at=NULL WHERE learning_unit_id=?",
                         (card.to_json(),first['learning_unit_id']))
            conn.execute("UPDATE sense_srs_state SET next_review_date='2026-01-02' WHERE learning_unit_id=?",(first['learning_unit_id'],))
            if multiplier!=1:
                conn.execute("INSERT INTO memory_profiles(user_id,forgetting_multiplier) VALUES(?,?)",(self.uid,multiplier))
        with self.at('2026-01-02'):
            resumed=self.next();self.assertFalse(resumed['known_candidate'])
            result=self.review(resumed,'address').json()
        return first, result

    def test_maturity_uses_180_day_probability_without_confirmation_or_age_gate(self):
        _, result=self.seeded_review(100)
        self.assertEqual(result['memory']['confirmations'],1)
        self.assertGreaterEqual(result['memory']['audit_recall_probability'],0.8)
        self.assertEqual(result['srs_state']['status'],'Mature')
        self.assertEqual(result['srs_state']['interval_days'],180)

    def test_current_recall_does_not_make_a_short_lived_memory_mature(self):
        _, result=self.seeded_review(30)
        self.assertLess(result['memory']['audit_recall_probability'],0.8)
        self.assertEqual(result['srs_state']['status'],'Reviewing')
        self.assertGreater(result['srs_state']['interval_days'],60)

    def test_maturity_uses_personal_faster_forgetting(self):
        _, result=self.seeded_review(100,2)
        self.assertLess(result['memory']['audit_recall_probability'],0.8)
        self.assertEqual(result['srs_state']['status'],'Reviewing')

    def test_maturity_uses_personal_slower_forgetting(self):
        _, result=self.seeded_review(30,0.5)
        self.assertGreaterEqual(result['memory']['audit_recall_probability'],0.8)
        self.assertEqual(result['srs_state']['status'],'Mature')
        self.assertEqual(result['srs_state']['interval_days'],180)

    def test_recent_failure_does_not_add_an_extra_maturity_gate(self):
        with self.at('2026-01-01'):
            first=self.next();self.review(first,'wrong');self.review(first,'address')
        moment=datetime(2026,1,1,4,tzinfo=timezone.utc)
        card=Card(card_id=first['learning_unit_id'],state=State.Review,step=None,
                  stability=100,difficulty=5,last_review=moment)
        with database.connect() as conn:
            conn.execute("UPDATE adaptive_memory SET card_json=? WHERE learning_unit_id=?",(card.to_json(),first['learning_unit_id']))
            conn.execute("UPDATE relearning_queue SET ready_at='2000-01-01T00:00:00+00:00'")
        with self.at('2026-01-02'):
            result=self.review(self.next(),'address').json()
        self.assertGreaterEqual(result['memory']['audit_recall_probability'],0.8)
        self.assertEqual(result['srs_state']['status'],'Mature')

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

    def test_failure_returns_to_learning_and_correction_does_not_increase_stability(self):
        with self.at('2026-01-01'):
            self.review(self.next(),'address');self.review(self.next(),'addressed')
        with self.at('2026-01-09'):
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

    def test_open_round_from_before_upgrade_uses_the_same_model(self):
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
        self.assertEqual(scheduler().desired_retention,TARGET_RETENTION)
        self.assertAlmostEqual(scheduler(2).desired_retention**2,TARGET_RETENTION)
        self.assertAlmostEqual(scheduler(0.5).desired_retention**0.5,TARGET_RETENTION)
        self.assertAlmostEqual(scheduler().get_card_retrievability(initial,later),0.9,places=6)

    def test_calibration_is_finite_bounded_and_regularized(self):
        self.assertTrue(math.isfinite(log_loss([(0,True),(1,False)],1)))
        self.assertLessEqual(fit_multiplier([(0.9,False)]*120),2)
        self.assertGreaterEqual(fit_multiplier([(0.9,True)]*120),0.5)


if __name__ == '__main__':
    unittest.main()
