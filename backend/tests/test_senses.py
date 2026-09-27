"""Sense/example correctness, independent schedules and legacy preservation."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import database, migrations
from app.main import app


def sense(key, meaning, sentence, target="address", pos="名词", extra=None):
    examples = [{"sentence": sentence, "target_form": target, "translation_cn": None}]
    if extra:
        examples.append({"sentence": extra, "target_form": target, "translation_cn": None})
    return {"key": key,"definition_cn": meaning,"definition_en": None,"part_of_speech":pos,
            "source":"macOS Dictionary","examples":examples}


def catalog():
    return {"format_version":2,"lists":[
        {"id":"cet4","title":"四级","default_selected":True,"source_word_count":1},
        {"id":"cet6","title":"六级","source_word_count":2}],"words":[
        {"word":"address","pronunciation":"address","part_of_speech":"名词","definition_cn":"地址",
         "example_sentence":"Our officers went to the address.",
         "memberships":[{"list_id":"cet4","position":1},{"list_id":"cet6","position":1}],
         "senses":[sense("place","地址","Our officers went to the address.",extra="Please give me your address."),
                   sense("tackle","处理","The problem has been addressed.",target="addressed",pos="动词")]},
        {"word":"bank","pronunciation":"bank","part_of_speech":"名词","definition_cn":"银行",
         "example_sentence":"She went to the bank.","memberships":[{"list_id":"cet6","position":2}],
         "senses":[sense("finance","银行","She went to the bank.",target="bank")]}]}


class SenseLearningTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name);self.catalog_path=self.directory/'catalog.json'
        self.catalog_path.write_text(json.dumps(catalog(),ensure_ascii=False))
        for name,value in [("DATA_DIR",self.directory),("DB_PATH",self.directory/'test.db'),
                           ("SEED_PATH",self.directory/'no-seed.json'),("VOCABULARY_CATALOG_PATH",self.catalog_path)]:
            p=patch.object(database,name,value);p.start();self.addCleanup(p.stop)
        self.client=TestClient(app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        registered=self.client.post('/api/auth/register',json={"username":"learner","password":"test-password-123"})
        self.assertEqual(registered.status_code,200,registered.text);self.uid=registered.json()['user']['id']

    def next(self):
        r=self.client.get('/api/next');self.assertEqual(r.status_code,200,r.text);return r.json()['card']

    def review(self,card,answer):
        return self.client.post('/api/review',json={"word_id":card['id'],"sense_id":card['sense_id'],
            "example_id":card['example_id'],"attempt_id":card['attempt_id'],"user_answer":answer})

    def ready_relearning(self):
        with database.connect() as conn:
            conn.execute("UPDATE relearning_queue SET ready_at='2000-01-01T00:00:00+00:00' WHERE user_id=?",(self.uid,))

    def test_vocabulary_list_progress_counts_exposure_overlap_and_all_mature_senses(self):
        def lists(client=None):
            response=(client or self.client).get('/api/word-lists')
            self.assertEqual(response.status_code,200,response.text)
            return {item['list_id']:item for item in response.json()['lists']}
        initial=lists()
        self.assertEqual((initial['cet4']['word_count'],initial['cet6']['word_count']),(1,2))
        self.assertEqual(initial['cet4']['learned_word_count'],0)
        first=self.next()  # Seeing a card counts even before submitting an answer.
        exposed=lists()
        self.assertEqual(exposed['cet4']['learned_word_count'],1)
        self.assertEqual(exposed['cet6']['learned_word_count'],1)
        self.review(first,'bad')
        self.assertEqual(lists()['cet4']['learned_word_count'],1)
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET status='Mature' WHERE user_id=?",(self.uid,))
        self.assertEqual(lists()['cet4']['mastered_word_count'],0)  # Another meaning remains unlearned.
        with database.connect() as conn:
            conn.execute("""INSERT INTO sense_srs_state(user_id,sense_id,next_review_date,status)
                SELECT ?,id,'2099-01-01','Reviewing' FROM word_senses WHERE sense_key='tackle'""",(self.uid,))
        self.assertEqual(lists()['cet4']['mastered_word_count'],0)  # Entering review is not long-term mastery.
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET status='Mature' WHERE user_id=?",(self.uid,))
        mature=lists()
        self.assertEqual(mature['cet4']['mastered_word_count'],1)
        self.assertEqual(mature['cet6']['mastered_word_count'],1)
        self.client.patch('/api/settings',json={'selected_word_list_ids':[]})
        unselected=lists()['cet4']
        self.assertFalse(unselected['selected'])
        self.assertEqual((unselected['learned_word_count'],unselected['mastered_word_count']),(1,1))
        with TestClient(app) as other:
            other.post('/api/auth/register',json={'username':'other','password':'test-password-123'})
            other_list=lists(other)['cet4']
            self.assertEqual((other_list['learned_word_count'],other_list['mastered_word_count']),(0,0))

    def test_vocabulary_list_progress_keeps_legacy_exposure_and_excludes_archived_words(self):
        with database.connect() as conn:
            bank=conn.execute("SELECT id FROM words WHERE word='bank'").fetchone()[0]
            conn.execute("""INSERT INTO review_history(user_id,word_id,review_time,user_answer,is_correct)
                VALUES(?,?,'2000-01-01T00:00:00+00:00','bad',0)""",(self.uid,bank))
        items={item['list_id']:item for item in self.client.get('/api/word-lists').json()['lists']}
        self.assertEqual(items['cet6']['learned_word_count'],1)
        self.assertEqual(items['cet4']['learned_word_count'],0)
        with database.connect() as conn:
            conn.execute('UPDATE word_senses SET active=0 WHERE word_id=?',(bank,))
        items={item['list_id']:item for item in self.client.get('/api/word-lists').json()['lists']}
        self.assertEqual((items['cet6']['word_count'],items['cet6']['learned_word_count']),(1,0))

    def test_matching_hint_and_independent_sense_progress(self):
        first=self.next();self.assertEqual(first['definition_cn'],'地址');self.assertNotIn('处理',first['definition_cn'])
        response=self.review(first,'address');self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['other_senses'][0]['definition_cn'],'处理')
        second=self.next();self.assertEqual(second['definition_cn'],'处理');self.assertFalse(second['is_new_word'])
        self.assertIn('_______',second['cloze_sentence']);self.assertNotIn('addressed',second['cloze_sentence'])
        self.assertFalse(self.review(second,'address').json()['is_correct'])
        with database.connect() as conn:
            states=conn.execute('SELECT sense_id,correct_count,wrong_count FROM sense_srs_state ORDER BY sense_id').fetchall()
            self.assertEqual((states[0]['correct_count'],states[0]['wrong_count']),(1,0))
            self.assertEqual((states[1]['correct_count'],states[1]['wrong_count']),(0,1))
        self.assertTrue(self.review(second,'addressed').json()['is_correct'])
        stats=self.client.get('/api/stats').json();self.assertEqual(stats['total_learned'],1)
        self.assertEqual(stats['learned_senses'],2);self.assertEqual(stats['mastered'],0)
        self.assertEqual(stats['today_success_senses'],1);self.assertEqual(stats['pending_relearning_senses'],1)
        self.ready_relearning()
        retry=self.next();self.assertTrue(retry['is_relearning']);self.assertFalse(retry['needs_correction'])
        self.assertNotEqual(retry['attempt_id'],second['attempt_id'])
        response=self.review(retry,'addressed').json();self.assertTrue(response['is_independent'])
        stats=self.client.get('/api/stats').json()
        self.assertEqual(stats['mastered'],1);self.assertEqual(stats['today_success_senses'],2)
        self.assertEqual(stats['today_success'],1);self.assertEqual(stats['pending_relearning'],0)

    def test_revealed_answer_survives_refresh_and_correction_does_not_advance_srs(self):
        card=self.next()
        failed=self.review(card,'bad').json();self.assertEqual(failed['outcome'],'incorrect')
        database.init_database()
        resumed=self.next()
        self.assertEqual(resumed['attempt_id'],card['attempt_id']);self.assertEqual(resumed['example_id'],card['example_id'])
        self.assertTrue(resumed['needs_correction'])
        corrected=self.review(resumed,'address').json()
        self.assertEqual(corrected['outcome'],'corrected');self.assertFalse(corrected['is_independent'])
        self.assertEqual(corrected['srs_state']['status'],'Learning')
        self.assertEqual(corrected['srs_state']['correct_count'],0)
        self.assertEqual(corrected['srs_state']['interval_days'],0)
        self.assertEqual(corrected['srs_state']['easiness_factor'],failed['srs_state']['easiness_factor'])
        stats=self.client.get('/api/stats').json()
        self.assertEqual(stats['today_learning'],2);self.assertEqual(stats['today_accuracy'],50)
        self.assertEqual(stats['today_independent_accuracy'],0);self.assertEqual(stats['today_success'],0)

    def test_other_due_reviews_before_relearning_and_relearning_before_new_senses(self):
        first=self.next();self.review(first,'address')
        second=self.next();self.review(second,'addressed')
        self.client.patch('/api/settings',json={'selected_word_list_ids':['cet6']})
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01'")
            conn.execute("UPDATE review_history SET review_time='2000-01-01T00:00:00+00:00'")
        first=self.next();self.review(first,'bad');self.review(first,'address')
        self.ready_relearning()
        other=self.next();self.assertEqual(other['sense_id'],second['sense_id'])
        self.assertFalse(other['is_relearning']);self.review(other,'addressed')
        retry=self.next();self.assertEqual(retry['sense_id'],first['sense_id'])
        self.assertTrue(retry['is_relearning']);self.assertNotEqual(retry['example_id'],first['example_id'])
        self.review(retry,'address')
        self.assertEqual(self.next()['word'],'bank')

    def test_only_pending_word_waits_at_least_twenty_minutes(self):
        card=self.next();self.review(card,'');self.review(card,'address')
        self.client.patch('/api/settings',json={'selected_word_list_ids':[]})
        waiting=self.client.get('/api/next').json()
        self.assertIsNone(waiting['card']);self.assertGreater(waiting['retry_after_seconds'],0)
        self.assertGreaterEqual(waiting['retry_after_seconds'],1200)
        self.assertLessEqual(waiting['retry_after_seconds'],1201)
        self.ready_relearning()
        retry=self.next();self.assertEqual(retry['sense_id'],card['sense_id'])
        self.assertFalse(retry['needs_correction'])

    def test_new_senses_can_continue_during_twenty_minute_wait(self):
        card=self.next();self.review(card,'bad');self.review(card,'address')
        next_card=self.next();self.assertNotEqual(next_card['sense_id'],card['sense_id'])
        self.assertFalse(next_card['is_relearning'])

    def test_failed_relearning_moves_to_tail_and_does_not_disappear_on_next_day(self):
        first=self.next();self.review(first,'bad');self.review(first,'address')
        with database.connect() as conn:
            pair=conn.execute("SELECT s.id AS sense_id,e.id AS example_id,s.word_id FROM word_senses s JOIN sense_examples e ON e.sense_id=s.id WHERE s.sense_key='tackle'").fetchone()
        # A compatibility client can also create a second pending sense.
        self.client.post('/api/review',json={**dict(pair),'user_answer':'bad'})
        self.client.post('/api/review',json={**dict(pair),'user_answer':'addressed'})
        with database.connect() as conn:
            conn.execute("UPDATE relearning_queue SET queued_at='2000-01-01T00:00:00+00:00' WHERE sense_id=?",(first['sense_id'],))
        self.ready_relearning()
        retry=self.next();self.assertEqual(retry['sense_id'],first['sense_id'])
        self.review(retry,'bad');self.review(retry,'address');self.ready_relearning()
        with patch('app.main.user_today',return_value=date(2099,1,1)):
            next_day=self.next();self.assertEqual(next_day['sense_id'],pair['sense_id'])
            self.assertTrue(next_day['is_relearning'])
        self.assertEqual(self.client.get('/api/stats').json()['pending_relearning_senses'],2)

    def test_duplicate_round_and_second_success_cannot_advance_twice(self):
        card=self.next();response=self.review(card,'address').json()
        self.assertTrue(response['is_independent']);self.assertEqual(response['srs_state']['correct_count'],1)
        self.assertEqual(self.review(card,'address').status_code,409)
        self.assertEqual(self.client.post('/api/review',json={'word_id':card['id'],'sense_id':card['sense_id'],
            'example_id':card['example_id'],'user_answer':'address'}).status_code,409)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT correct_count FROM sense_srs_state').fetchone()[0],1)

    def test_concurrent_devices_share_one_round_and_only_one_success(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            cards=list(workers.map(lambda _: self.next(),range(2)))
            self.assertEqual(cards[0]['attempt_id'],cards[1]['attempt_id'])
            responses=list(workers.map(lambda card: self.review(card,'address'),cards))
        self.assertEqual(sorted(r.status_code for r in responses),[200,409])
        self.assertEqual(self.client.get('/api/stats').json()['today_learning'],1)

    def test_compatibility_client_cannot_turn_correction_into_independent_success(self):
        card=self.next()
        payload={'word_id':card['id'],'sense_id':card['sense_id'],'example_id':card['example_id']}
        self.client.post('/api/review',json={**payload,'user_answer':'bad'})
        corrected=self.client.post('/api/review',json={**payload,'user_answer':'address'}).json()
        self.assertEqual(corrected['outcome'],'corrected')
        self.assertEqual(self.client.post('/api/review',json={**payload,'user_answer':'address'}).status_code,409)

    def test_attempt_id_is_bound_to_user_and_example(self):
        card=self.next()
        with database.connect() as conn:
            other_example=conn.execute('SELECT id FROM sense_examples WHERE sense_id=? AND id!=?',(card['sense_id'],card['example_id'])).fetchone()[0]
        mismatched={**card,'example_id':other_example}
        self.assertEqual(self.review(mismatched,'address').status_code,409)
        with TestClient(app) as other:
            other.post('/api/auth/register',json={'username':'other','password':'test-password-123'})
            payload={'word_id':card['id'],'sense_id':card['sense_id'],'example_id':card['example_id'],
                     'attempt_id':card['attempt_id'],'user_answer':'address'}
            self.assertEqual(other.post('/api/review',json=payload).status_code,409)
            self.assertEqual(other.get('/api/stats').json()['pending_relearning'],0)
        self.assertEqual(self.client.get('/api/stats').json()['today_learning'],0)

    def test_local_midnight_resets_success_count_and_allows_next_days_srs_step(self):
        self.client.patch('/api/auth/me',json={'timezone':'Asia/Shanghai'})
        with patch('app.main.user_today',return_value=date(2026,1,1)), \
             patch('app.main.today_in',return_value=date(2026,1,1)), \
             patch('app.sense_learning.utc_now_iso',return_value='2026-01-01T15:59:59+00:00'):
            card=self.next();self.review(card,'address')
            self.assertEqual(self.client.get('/api/stats').json()['today_success'],1)
            self.assertEqual(self.review(card,'address').status_code,409)
        with patch('app.main.user_today',return_value=date(2026,1,2)), \
             patch('app.main.today_in',return_value=date(2026,1,2)), \
             patch('app.sense_learning.utc_now_iso',return_value='2026-01-01T16:00:01+00:00'):
            self.assertEqual(self.client.get('/api/stats').json()['today_success'],0)
            card=self.next();response=self.review(card,'address').json()
            self.assertEqual(response['srs_state']['correct_count'],2)
            self.assertEqual(response['srs_state']['next_review_date'],'2026-01-05')
            self.assertEqual(self.client.get('/api/stats').json()['today_success'],1)

    def test_sentence_rotation_and_due_reviews_survive_list_switch(self):
        first=self.next();self.review(first,'address')
        self.client.patch('/api/settings',json={'selected_word_list_ids':[]})
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET next_review_date='2000-01-01' WHERE sense_id=?",(first['sense_id'],))
        second=self.next();self.assertEqual(second['sense_id'],first['sense_id'])
        self.assertNotEqual(second['example_id'],first['example_id']);self.assertEqual(second['definition_cn'],'地址')

    def test_cet6_does_not_repeat_completed_cet4_senses(self):
        self.review(self.next(),'address');self.review(self.next(),'addressed')
        self.assertIsNone(self.next())
        self.client.patch('/api/settings',json={'selected_word_list_ids':['cet6']})
        next_card=self.next();self.assertEqual(next_card['word'],'bank');self.assertTrue(next_card['is_new_word'])
        stats=self.client.get('/api/stats').json();self.assertEqual(stats['new_words'],1);self.assertEqual(stats['new_senses'],1)

    def test_example_cannot_be_submitted_for_another_sense(self):
        first=self.next()
        with database.connect() as conn:
            other=conn.execute("SELECT id FROM word_senses WHERE sense_key='tackle'").fetchone()[0]
        result=self.client.post('/api/review',json={'word_id':first['id'],'sense_id':other,'example_id':first['example_id'],'user_answer':'address'})
        self.assertEqual(result.status_code,404)
        with database.connect() as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0],0)
        self.assertEqual(self.client.post('/api/review',json={'word_id':first['id'],'user_answer':'address'}).status_code,422)

    def test_private_words_are_isolated_and_have_a_learning_sense(self):
        data={'word':'custom','part_of_speech':'名词','definition_cn':'习惯','example_sentence':'This is an old custom.'}
        added=self.client.post('/api/words',json=data);self.assertEqual(added.status_code,200,added.text)
        with database.connect() as conn:
            word_id=added.json()['word']['id'];pair=conn.execute('SELECT s.id AS sense_id,e.id AS example_id FROM word_senses s JOIN sense_examples e ON e.sense_id=s.id WHERE s.word_id=?',(word_id,)).fetchone()
        other=TestClient(app);other.post('/api/auth/register',json={'username':'other','password':'test-password-123'})
        result=other.post('/api/review',json={'word_id':word_id,**dict(pair),'user_answer':'custom'})
        self.assertEqual(result.status_code,404);self.assertFalse(other.get('/api/dictionary/custom').json()['available'])

    def test_reimport_keeps_ids_history_and_archives_removed_senses(self):
        first=self.next();self.review(first,'address')
        before=self.client.get('/api/stats').json()['learned_senses']
        database.init_database();self.assertEqual(self.client.get('/api/stats').json()['learned_senses'],before)
        updated=catalog();updated['words'][0]['senses']=updated['words'][0]['senses'][1:]
        self.catalog_path.write_text(json.dumps(updated));database.seed_vocabulary_catalog()
        with database.connect() as conn:
            old=conn.execute('SELECT active FROM word_senses WHERE id=?',(first['sense_id'],)).fetchone()
            self.assertEqual(old[0],0);self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0],1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(self.next()['definition_cn'],'处理')

    def test_archived_open_round_does_not_block_remaining_senses(self):
        first=self.next()
        updated=catalog();updated['words'][0]['senses']=updated['words'][0]['senses'][1:]
        self.catalog_path.write_text(json.dumps(updated));database.seed_vocabulary_catalog()
        second=self.next();self.assertEqual(second['definition_cn'],'处理')
        with database.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT completed_at FROM study_attempts WHERE id=?',(first['attempt_id'],)).fetchone()[0])


class LegacySenseMigrationTests(unittest.TestCase):
    def test_v5_upgrade_preserves_schedules_and_history_and_queues_learning_senses(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'v5.db'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.row_factory=sqlite3.Row
                for migrate in (migrations._migrate_to_v1,migrations._migrate_to_v2,
                                migrations._migrate_to_v3,migrations._migrate_to_v4,migrations._migrate_to_v5):
                    migrate(conn)
                conn.execute("INSERT INTO users VALUES(1,'original','original-hash','Asia/Shanghai','old')")
                conn.execute("INSERT INTO words(id,word,part_of_speech,definition_cn,example_sentence) VALUES(1,'address','名词','地址','Our officers went to the address.')")
                from app.senses import save_senses
                save_senses(conn,1,catalog()['words'][0]['senses'])
                conn.execute("""INSERT INTO sense_srs_state(user_id,sense_id,review_count,correct_count,wrong_count,next_review_date,status)
                    VALUES(1,1,3,0,2,'2026-01-01','Learning'),(1,2,4,4,0,'2026-12-01','Reviewing')""")
                conn.execute("""INSERT INTO review_history(user_id,word_id,sense_id,example_id,review_time,user_answer,is_correct)
                    VALUES(1,1,1,1,'2026-01-01T00:00:00+00:00','address',1)""")
                conn.execute('PRAGMA user_version=5')
            migrations.run_migrations(path);migrations.run_migrations(path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],6)
                self.assertEqual(conn.execute('SELECT sense_id FROM relearning_queue').fetchall(),[(1,)])
                self.assertEqual(conn.execute('SELECT correct_count,status,next_review_date FROM sense_srs_state ORDER BY sense_id').fetchall(),
                                 [(0,'Learning','2026-01-01'),(4,'Reviewing','2026-12-01')])
                self.assertEqual(conn.execute('SELECT user_answer,is_correct,is_independent,is_first_attempt FROM review_history').fetchall(),
                                 [('address',1,None,None)])
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(len(list(Path(t).glob('v5.db.bak-v5-*'))),1)

    def test_only_exact_old_example_inherits_schedule(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t);path=folder/'legacy.db';source=folder/'catalog.json';source.write_text(json.dumps(catalog()))
            conn=sqlite3.connect(path);conn.row_factory=sqlite3.Row
            migrations._migrate_to_v1(conn);migrations._migrate_to_v2(conn);migrations._migrate_to_v3(conn);migrations._migrate_to_v4(conn)
            conn.execute("INSERT INTO users VALUES(1,'original','original-hash','Asia/Shanghai','old')")
            conn.execute("INSERT INTO words(id,owner_id,word,part_of_speech,definition_cn,example_sentence) VALUES(1,NULL,'address','名词','地址；处理','Our officers went to the address.')")
            conn.execute("INSERT INTO words(id,owner_id,word,part_of_speech,definition_cn,example_sentence) VALUES(2,NULL,'bank','名词','银行','An unmatched bank example.')")
            for wid in (1,2):
                conn.execute("INSERT INTO srs_state(user_id,word_id,review_count,correct_count,next_review_date,status) VALUES(1,?,8,8,'2099-01-01','Mature')",(wid,))
                conn.execute("INSERT INTO review_history(user_id,word_id,review_time,user_answer,is_correct) VALUES(1,?,'old','old-answer',1)",(wid,))
            conn.execute('PRAGMA user_version=4');conn.commit();conn.close()
            with patch.object(database,'DATA_DIR',folder),patch.object(database,'DB_PATH',path),patch.object(database,'SEED_PATH',folder/'missing'),patch.object(database,'VOCABULARY_CATALOG_PATH',source):
                database.init_database();database.init_database()
                with database.connect() as conn:
                    states=conn.execute('SELECT s.sense_key,p.status FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id').fetchall()
                    self.assertEqual([tuple(s) for s in states],[('place','Mature')])
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM srs_state').fetchone()[0],2)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0],2)
                    self.assertIsNone(conn.execute('SELECT sense_id FROM review_history WHERE word_id=2').fetchone()[0])
                    self.assertEqual(conn.execute('SELECT password_hash FROM users').fetchone()[0],'original-hash')
                    self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            backup=list(folder.glob('legacy.db.bak-v4-*'));self.assertEqual(len(backup),1)
