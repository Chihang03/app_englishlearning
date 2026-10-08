"""Course scope, shared recall, source pairing and reversible historical merges."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fsrs import Card, State
import test_senses as fixtures
from app import database, learning_units

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = json.loads((ROOT/'data'/'learning_units.json').read_text())
ABANDON = next(w for w in json.loads((ROOT/'data'/'vocabulary_catalog.json').read_text())['words'] if w['word']=='abandon')
# These migration tests exercise the seven previously taught source senses.
# New dictionary evidence must not change the historical fixture or its order.
HISTORICAL_KEYS = [f'en:m_en_gbus0000850.{suffix}' for suffix in
                   ('006', '013', '015', '017', '018', '020', '022')]
ABANDON['senses'] = [next(s for s in ABANDON['senses'] if s['key'] == key)
                     for key in HISTORICAL_KEYS]


def catalog():
    word = copy.deepcopy(ABANDON)
    word['memberships'] = [{'list_id':'cet4','position':1},{'list_id':'cet6','position':1}]
    return {'format_version':2,'lists':[{'id':'cet4','title':'四级','default_selected':True},
        {'id':'cet6','title':'六级'}],'words':[word]}


class LearningUnitTests(unittest.TestCase):
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    ready_relearning = fixtures.SenseLearningTests.ready_relearning

    def setUp(self):
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name)/'groups.json'
        self.config.write_text(json.dumps({'format_version':1,'words':[]}))
        p=patch.object(learning_units,'CONFIG_PATH',self.config);p.start();self.addCleanup(p.stop)
        with patch.object(fixtures,'catalog',catalog):
            fixtures.SenseLearningTests.setUp(self)

    def enable_group(self, config=None):
        self.config.write_text(json.dumps(config or SHIPPED_CONFIG,ensure_ascii=False))
        with database.connect() as conn:
            learning_units.sync_learning_units(conn)

    @contextmanager
    def at(self, day):
        moment = datetime.fromisoformat(day+'T04:00:00+00:00')
        with patch('app.main.user_today',return_value=moment.date()), \
             patch('app.main.today_in',return_value=moment.date()), \
             patch('app.sense_learning.utc_now_iso',return_value=moment.isoformat()), \
             patch('app.sense_learning.datetime',wraps=datetime) as clock:
            clock.now.return_value=moment
            yield

    def sources(self,conn):
        return conn.execute('SELECT * FROM word_senses ORDER BY position,id').fetchall()

    def test_cet4_only_adds_reviewed_core_but_dictionary_keeps_all_seven(self):
        self.enable_group()
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'],1)
        first=self.next()
        self.assertEqual(first['definition_cn'],'放弃；抛弃；遗弃')
        details=self.client.post('/api/study/meanings',json={'attempt_id':first['attempt_id']}).json()
        self.assertEqual(len(details['senses']),7)
        self.assertEqual(len({s['learning_unit_id'] for s in details['senses']}),4)
        self.assertEqual(details['senses'][-1]['part_of_speech'],'名词')
        self.review(first,first['answer_form'])
        self.ready_relearning()
        retry=self.next();self.review(retry,retry['answer_form'])
        self.assertIsNone(self.next())
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sense_srs_state WHERE retired_at IS NULL').fetchone()[0],1)

    def test_delayed_reviews_rotate_context_but_reload_keeps_signed_round(self):
        self.enable_group()
        with self.at('2026-01-01'):
            first=self.next();answer=self.review(first,first['answer_form']).json()
            self.assertEqual(answer['memory']['confirmations'],1)
            self.assertIsNone(self.next())
        with self.at(answer['srs_state']['next_review_date']):
            second=self.next();resumed=self.next()
            self.assertEqual(second['learning_unit_id'],first['learning_unit_id'])
            self.assertNotEqual(second['sense_id'],first['sense_id'])
            self.assertNotEqual(second['example_id'],first['example_id'])
            self.assertEqual((second['attempt_id'],second['example_id']),(resumed['attempt_id'],resumed['example_id']))
            result=self.review(second,second['answer_form']).json()
            self.assertEqual(result['memory']['confirmations'],2)
            self.assertEqual(self.client.get('/api/stats').json()['today_success_senses'],1)

    def test_wrong_correction_and_wait_share_one_queue_and_keep_sentence(self):
        self.enable_group();first=self.next()
        self.assertFalse(self.review(first,'wrong').json()['is_correct'])
        resumed=self.next();self.assertEqual(first['attempt_id'],resumed['attempt_id'])
        corrected=self.review(resumed,resumed['answer_form']).json()
        self.assertFalse(corrected['is_independent'])
        self.assertIsNone(self.next())
        self.ready_relearning();retry=self.next()
        self.assertEqual(first['example_id'],retry['example_id'])
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM relearning_queue WHERE retired_at IS NULL').fetchone()[0],1)
        self.assertTrue(self.review(retry,retry['answer_form']).json()['is_independent'])

    def test_alias_cannot_bypass_daily_limit_or_source_pair_validation(self):
        self.enable_group();first=self.next()
        with database.connect() as conn:
            alternate=conn.execute('''SELECT s.id AS sense_id,e.id AS example_id,e.target_form
                FROM learning_unit_senses b JOIN word_senses s ON s.id=b.sense_id
                JOIN sense_examples e ON e.sense_id=s.id WHERE b.learning_unit_id=? AND s.id!=? LIMIT 1''',
                (first['learning_unit_id'],first['sense_id'])).fetchone()
        wrong_pair=self.client.post('/api/review',json={'word_id':first['id'],'sense_id':alternate['sense_id'],
            'example_id':first['example_id'],'user_answer':first['answer_form'],'attempt_id':first['attempt_id']})
        self.assertEqual(wrong_pair.status_code,404)
        mismatch=self.client.post('/api/review',json={'word_id':first['id'],'sense_id':first['sense_id'],
            'example_id':first['example_id'],'learning_unit_id':999999,'user_answer':first['answer_form'],'attempt_id':first['attempt_id']})
        self.assertEqual(mismatch.status_code,409)
        self.review(first,first['answer_form'])
        bypass=self.client.post('/api/review',json={'word_id':first['id'],'sense_id':alternate['sense_id'],
            'example_id':alternate['example_id'],'user_answer':alternate['target_form']})
        self.assertEqual(bypass.status_code,409)

    def test_excluded_noun_cannot_be_started_by_compatibility_payload(self):
        self.enable_group()
        with database.connect() as conn:
            noun=self.sources(conn)[-1]
            example=conn.execute('SELECT * FROM sense_examples WHERE sense_id=?',(noun['id'],)).fetchone()
        response=self.client.post('/api/review',json={'word_id':noun['word_id'],'sense_id':noun['id'],
            'example_id':example['id'],'user_answer':example['target_form']})
        self.assertEqual(response.status_code,409)

    def test_existing_noun_review_survives_course_scope_and_deselection(self):
        with database.connect() as conn:
            noun=self.sources(conn)[-1]
            conn.execute("INSERT INTO sense_srs_state(user_id,sense_id,next_review_date,status) VALUES(?,?,'2000-01-01','Reviewing')",(self.uid,noun['id']))
        self.enable_group();self.client.patch('/api/settings',json={'selected_word_list_ids':[]})
        card=self.next();self.assertEqual(card['sense_id'],noun['id'])
        self.assertEqual(card['part_of_speech'],'名词')

    def test_list_mastery_counts_only_covered_units_and_other_accounts_are_isolated(self):
        self.enable_group();first=self.next();self.review(first,first['answer_form'])
        with database.connect() as conn:
            conn.execute("UPDATE sense_srs_state SET status='Mature' WHERE user_id=? AND retired_at IS NULL",(self.uid,))
        lists={v['list_id']:v for v in self.client.get('/api/word-lists').json()['lists']}
        self.assertEqual(lists['cet4']['mastered_word_count'],1)
        self.assertEqual(lists['cet6']['mastered_word_count'],0)
        self.client.patch('/api/settings',json={'selected_word_list_ids':['cet4','cet6']})
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'],3)
        with fixtures.TestClient(fixtures.app) as other:
            other.post('/api/auth/register',json={'username':'other','password':'test-password-123'})
            self.assertEqual(other.get('/api/stats').json()['new_senses'],1)

    def test_single_existing_curve_and_fixed_example_are_preserved(self):
        old=self.next();self.review(old,old['answer_form'])
        with database.connect() as conn:
            before=dict(conn.execute('SELECT * FROM sense_srs_state').fetchone())
            memory=dict(conn.execute('SELECT * FROM adaptive_memory').fetchone())
        self.enable_group()
        with database.connect() as conn:
            after=conn.execute('SELECT * FROM sense_srs_state WHERE retired_at IS NULL').fetchone()
            for field in ('status','interval_days','next_review_date','correct_count','wrong_count','last_example_id'):
                self.assertEqual(before[field],after[field])
            m=conn.execute('SELECT * FROM adaptive_memory WHERE retired_at IS NULL').fetchone()
            for field in ('known_candidate','confirmations','first_independent_at','last_independent_at'):
                self.assertEqual(memory[field],m[field])
            self.assertEqual(json.loads(m['card_json'])['card_id'],after['learning_unit_id'])
            fixed=conn.execute('SELECT example_id FROM user_sense_examples WHERE retired_at IS NULL').fetchone()[0]
            self.assertEqual(fixed,old['example_id'])

    def test_multiple_curves_merge_conservatively_and_retire_cached_rounds(self):
        from app.sense_learning import start_attempt
        with database.connect() as conn:
            first,second=self.sources(conn)[:2]
            for i,s in enumerate((first,second)):
                example=conn.execute('SELECT id FROM sense_examples WHERE sense_id=?',(s['id'],)).fetchone()[0]
                conn.execute('''INSERT INTO sense_srs_state(user_id,sense_id,correct_count,wrong_count,interval_days,next_review_date,status,last_example_id)
                    VALUES(?,?,?,?,?,?,?,?)''',(self.uid,s['id'],3+i,i,90 if i==0 else 0,'2099-01-01' if i==0 else '2000-01-01','Mature' if i==0 else 'Learning',example))
                card=Card(card_id=s['id'],state=State.Review,stability=90 if i==0 else 3,difficulty=5,last_review=datetime.now(timezone.utc))
                conn.execute('INSERT INTO adaptive_memory(user_id,sense_id,card_json,confirmations) VALUES(?,?,?,3)',(self.uid,s['id'],card.to_json()))
                conn.execute('INSERT INTO user_sense_examples(user_id,sense_id,example_id) VALUES(?,?,?)',(self.uid,s['id'],example))
                conn.execute("INSERT INTO review_history(user_id,word_id,sense_id,example_id,review_time,user_answer,is_correct,is_independent) VALUES(?,?,?,?,'2026-01-01T00:00:00+00:00','abandoned',1,1)",(self.uid,s['word_id'],s['id'],example))
                round=start_attempt(conn,self.uid,s['id'],example)
            stale=dict(round)
            conn.execute("INSERT INTO relearning_queue(user_id,sense_id,queued_at,ready_at,queue_order) VALUES(?,?,'2026-01-01','2099-01-01T00:00:00+00:00',5)",(self.uid,second['id']))
            old_history=[tuple(r)[:6] for r in conn.execute('SELECT id,word_id,sense_id,example_id,review_time,user_answer FROM review_history')]
        self.enable_group()
        with database.connect() as conn:
            active=conn.execute('SELECT * FROM sense_srs_state WHERE retired_at IS NULL').fetchall()
            self.assertEqual(len(active),1);self.assertEqual(active[0]['status'],'Learning')
            self.assertEqual(active[0]['next_review_date'],'2000-01-01')
            memory=conn.execute('SELECT * FROM adaptive_memory WHERE retired_at IS NULL').fetchone()
            self.assertEqual(memory['confirmations'],0)
            self.assertLessEqual(json.loads(memory['card_json'])['stability'],3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM study_attempts WHERE completed_at IS NULL').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT ready_at FROM relearning_queue WHERE retired_at IS NULL').fetchone()[0],'2099-01-01T00:00:00+00:00')
            self.assertEqual(old_history,[tuple(r) for r in conn.execute('SELECT id,word_id,sense_id,example_id,review_time,user_answer FROM review_history')])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        response=self.client.post('/api/review',json={'word_id':second['word_id'],'sense_id':stale['sense_id'],
            'example_id':stale['example_id'],'attempt_id':stale['id'],'user_answer':'abandoned'})
        self.assertEqual(response.status_code,409)

    def test_reseed_is_idempotent_and_rollback_restores_without_erasing_new_answers(self):
        self.enable_group();card=self.next()
        with database.connect() as conn:
            receipt=conn.execute('SELECT id FROM learning_unit_merges').fetchone()[0]
            learning_units.sync_learning_units(conn)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM learning_unit_merges').fetchone()[0],1)
            learning_units.revert_merge(conn,receipt)
            self.assertEqual(conn.execute('SELECT COUNT(DISTINCT learning_unit_id) FROM learning_unit_senses').fetchone()[0],7)
        # The undone group is blocked on restart, but reviewed CET4 scope stays
        # limited to the original four approved source usages.
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'],4)
        stale=self.review(card,card['answer_form']);self.assertEqual(stale.status_code,409)
        with database.connect() as conn:
            conn.execute('DELETE FROM learning_group_blocks')
            learning_units.sync_learning_units(conn)
            receipt=conn.execute('SELECT MAX(id) FROM learning_unit_merges').fetchone()[0]
        fresh=self.next();self.review(fresh,fresh['answer_form'])
        with database.connect() as conn:
            count=conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0]
            with self.assertRaisesRegex(ValueError,'New answers'):
                learning_units.revert_merge(conn,receipt)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0],count)

    def test_changed_dictionary_evidence_rejects_group_without_committing(self):
        invalid=copy.deepcopy(SHIPPED_CONFIG)
        invalid['words'][0]['groups'][0]['senses'][0]['expected_definition_en']='unverified replacement'
        with self.assertRaisesRegex(ValueError,'Dictionary changed'):
            self.enable_group(invalid)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM learning_unit_merges').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(DISTINCT learning_unit_id) FROM learning_unit_senses').fetchone()[0],7)

    def test_future_senses_do_not_expand_reviewed_course_scope(self):
        from app.senses import save_senses
        self.enable_group()
        with database.connect() as conn:
            word=conn.execute("SELECT id FROM words WHERE word='abandon'").fetchone()[0]
            extra=fixtures.sense('new-use','新用法','They abandon old habits.',target='abandon',pos='动词')
            save_senses(conn,word,[*copy.deepcopy(ABANDON['senses']),extra])
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'],1)
        self.client.patch('/api/settings',json={'selected_word_list_ids':['cet6']})
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'],5)

    def test_explicit_empty_course_scope_stays_empty_when_content_is_added(self):
        from app.senses import save_senses
        config=copy.deepcopy(SHIPPED_CONFIG);config['words'][0]['coverage']['cet4']['units']=[]
        self.enable_group(config)
        with database.connect() as conn:
            word=conn.execute("SELECT id FROM words WHERE word='abandon'").fetchone()[0]
            extra=fixtures.sense('new-use','新用法','They abandon old habits.',target='abandon',pos='动词')
            save_senses(conn,word,[*copy.deepcopy(ABANDON['senses']),extra])
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'],0)
        self.assertIsNone(self.next())

    def test_cross_lexical_identity_group_is_rejected(self):
        with database.connect() as conn:
            senses=self.sources(conn)
            conn.execute('UPDATE word_senses SET lexical_unit_id=? WHERE id=?',(senses[-1]['lexical_unit_id'],senses[0]['id']))
        with self.assertRaisesRegex(ValueError,'crosses lexical'):
            self.enable_group()


if __name__=='__main__':
    unittest.main()
