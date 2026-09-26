"""Sense/example correctness, independent schedules and legacy preservation."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
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
            "example_id":card['example_id'],"user_answer":answer})

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
        self.assertEqual(stats['learned_senses'],2);self.assertEqual(stats['mastered'],1)

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


class LegacySenseMigrationTests(unittest.TestCase):
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
