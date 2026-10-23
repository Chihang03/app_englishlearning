"""Whole-word validation, cache invalidation and event-preserving application."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_senses as fixtures
from app import database, learning_units
from app.semantic_review import (apply_word, digest, original_source, source_word,
                                 exact_duplicate_review, model_input, retryable_review, sync_bundle,
                                 validate_response, validate_word, RETRYABLE_NOTE_PREFIX)
from app.senses import save_senses

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from semantic_exchange import apply_database, collect, export_batches
from translation_exchange import run_batches


class SemanticReviewTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review
    def install(self, senses):
        with database.connect() as conn:
            self.word_id=conn.execute("SELECT id FROM words WHERE word='address'").fetchone()[0]
            save_senses(conn,self.word_id,senses)
            return source_word(conn,self.word_id)

    def source(self):
        with database.connect() as conn:
            self.word_id=conn.execute("SELECT id FROM words WHERE word='address'").fetchone()[0]
            return source_word(conn,self.word_id)

    def proposal(self, source, groups=None):
        groups=groups or [[s['key']] for s in source['senses']]
        index={s['key']:s for s in source['senses']}
        return {'word':source['word'],'status':'ready','notes':'','groups':[
            {'part_of_speech':index[keys[0]]['part_of_speech'],
             'definition_cn':index[keys[0]]['definition_cn'] or '地址',
             'definition_en':index[keys[0]]['definition_en'], 'notes':'',
             'evidence':'结合原定义和例句核对',
             'members':[{'key':key,'examples':[{'sentence':e['sentence'],
                 'translation_cn':e['translation_cn'] or '请告诉我你的地址。'} for e in index[key]['examples']]} for key in keys]}
            for keys in groups]}

    def apply(self, source, result):
        path=self.directory/'semantic.json'
        path.write_text(json.dumps({'format_version':1,'entries':[{'source':source,'result':result}]},ensure_ascii=False))
        with database.connect() as conn:
            return sync_bundle(conn,path)

    def test_exact_duplicates_share_one_core_and_dedup_display_examples(self):
        source=self.install([fixtures.sense('a','地址','Please give me your address.'),
                             fixtures.sense('b','地址','Please give me your address.')])
        self.assertEqual(self.apply(source,self.proposal(source,[['a','b']]))['applied'],1)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(DISTINCT b.learning_unit_id) FROM learning_unit_senses b JOIN word_senses s ON s.id=b.sense_id WHERE s.word_id=? AND s.active=1",(self.word_id,)).fetchone()[0],1)
            self.assertEqual(len(original_source(conn,self.word_id)['senses']),2)
            self.assertEqual(conn.execute("SELECT example_strategy FROM learning_units WHERE word_id=? AND active=1",(self.word_id,)).fetchone()[0],'fixed')

    def test_exact_bilingual_duplicates_use_rules_but_equal_chinese_alone_does_not(self):
        a=fixtures.sense('a','地址','Please give me your address.')
        a['examples'][0]['translation_cn']='请告诉我你的地址。'
        b=copy.deepcopy(a);b['key']='b'
        source=self.install([a,b])
        self.assertIsNotNone(exact_duplicate_review(source))
        b['examples'][0]['sentence']='Write your address here.'
        self.assertIsNone(exact_duplicate_review({'word':'address','senses':[a,b]}))

    def test_compact_model_response_maps_numbers_to_original_examples(self):
        source=self.source();data=model_input(source)
        groups=[];translations=[]
        for sid,pos,cn,en,examples in data['senses']:
            groups.append({'senses':[sid],'part_of_speech':pos,'definition_cn':cn,
                'english_from':sid if en else 0,'notes':'','evidence':'依据当前例句核对'})
            translations.extend({'id':number,'cn':translation or '请告诉我你的地址。'}
                for number,sentence,translation,target in examples)
        manifest={'format_version':1,'task':'semantic_review','records':[source],'batch':digest([source])}
        result={'batch':manifest['batch'],'items':[{'word':'address','status':'ready','groups':groups,
            'translations':translations,'notes':''}]}
        expanded=validate_response(manifest,result)[0]
        self.assertEqual(expanded['groups'][0]['members'][0]['key'],source['senses'][0]['key'])
        result['items'][0]['translations'].pop()
        with self.assertRaises(ValueError):validate_response(manifest,result)

    def test_existing_translation_requires_a_reason_to_change(self):
        from app.semantic_review import ModelWord, expand_model_word
        source=self.install([fixtures.sense('a','地址','Please give me your address.')])
        source['senses'][0]['examples'][0]['translation_cn']='请告诉我你的地址。'
        model={'word':'address','status':'ready','notes':'','groups':[{'senses':[1],
            'part_of_speech':'名词','definition_cn':'地址','english_from':0,'notes':'','evidence':'例句表示地址'}],
            'translations':[{'id':1,'cn':'请给我你的地址。'}]}
        expanded=expand_model_word(source,ModelWord.model_validate(model))
        self.assertEqual(expanded['groups'][0]['members'][0]['examples'][0]['translation_cn'],'请告诉我你的地址。')

    def test_broken_bundle_preserves_content_and_application_startup(self):
        source=self.source();path=self.directory/'broken.json';path.write_text('{')
        with database.connect() as conn:
            self.assertEqual(sync_bundle(conn,path)['stale'],1)
            self.assertEqual(source_word(conn,self.word_id),source)

    def test_different_chinese_same_core_merge_but_independent_verb_remains(self):
        source=self.install([fixtures.sense('a','地址','Please give me your address.'),
            fixtures.sense('b','住址','Write your address here.'),
            fixtures.sense('c','处理','We address the problem.',pos='动词')])
        result=self.proposal(source,[['a','b'],['c']]);result['groups'][0]['definition_cn']='地址；住址'
        result['groups'][0]['notes']='可指通讯地址或居住地点。'
        self.assertEqual(self.apply(source,result)['applied'],1)
        with database.connect() as conn:
            self.assertEqual(conn.execute('''SELECT COUNT(DISTINCT b.learning_unit_id) FROM learning_unit_senses b
                JOIN word_senses s ON s.id=b.sense_id WHERE s.word_id=? AND s.active=1''',(self.word_id,)).fetchone()[0],2)
            self.assertEqual(original_source(conn,self.word_id),source)

    def test_english_in_chinese_field_is_replaced_and_definition_is_archived(self):
        source=self.install([fixtures.sense('a','a place where someone lives','Write your address here.')])
        result=self.proposal(source);result['groups'][0]['definition_cn']='地址'
        self.assertEqual(self.apply(source,result)['applied'],1)
        with database.connect() as conn:
            self.assertEqual(original_source(conn,self.word_id),source)
            self.assertEqual(source_word(conn,self.word_id)['senses'][0]['definition_cn'],'地址')

    def test_missing_translations_completed_and_accurate_existing_preserved(self):
        source=self.source();source['senses'][0]['examples'][0]['translation_cn']='我们的警员去了那个地址。'
        with database.connect() as conn:
            sid=conn.execute('SELECT id FROM word_senses WHERE sense_key=?',(source['senses'][0]['key'],)).fetchone()[0]
            conn.execute('UPDATE sense_examples SET translation_cn=? WHERE sense_id=? AND sentence=?',('我们的警员去了那个地址。',sid,source['senses'][0]['examples'][0]['sentence']))
        self.apply(source,self.proposal(source))
        with database.connect() as conn:
            after=source_word(conn,self.word_id)
            self.assertTrue(all(e['translation_cn'] for s in after['senses'] for e in s['examples']))
            self.assertEqual(after['senses'][0]['examples'][0]['translation_cn'],'我们的警员去了那个地址。')

    def test_pos_correction_uses_context_and_updates_lexical_identity(self):
        source=self.install([fixtures.sense('a','专业的','a highly address administrator.',pos='名词')])
        result=self.proposal(source);result['groups'][0]['part_of_speech']='形容词'
        with self.assertRaisesRegex(ValueError,'Noun label'):
            validate_word(source,self.proposal(source))
        self.assertEqual(self.apply(source,result)['applied'],1)
        with database.connect() as conn:
            row=conn.execute('SELECT l.pos_group FROM word_senses s JOIN lexical_units l ON l.id=s.lexical_unit_id WHERE s.word_id=? AND s.active=1',(self.word_id,)).fetchone()
            self.assertEqual(row[0],'adjective')
            self.assertEqual(original_source(conn,self.word_id)['senses'][0]['part_of_speech'],'名词')

    def test_manual_keeps_all_original_data_and_no_learning_merges(self):
        source=self.source()
        self.assertEqual(self.apply(source,{'word':'address','status':'manual','notes':'例句证据不足','groups':[]})['manual'],1)
        with database.connect() as conn:
            self.assertEqual(source_word(conn,self.word_id),source)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM learning_unit_merges').fetchone()[0],0)

    def test_partial_application_retains_pending_content_and_finishes_without_losing_history(self):
        source=self.install([fixtures.sense('a','地址','Please give me your address.'),
                             fixtures.sense('b','住址','Write your address here.'),
                             fixtures.sense('c','处理','We address the problem.',pos='动词')])
        card=self.next();self.review(card,card['answer_form'])
        history_fields='id,user_id,word_id,sense_id,example_id,review_time,user_answer,is_correct'
        with database.connect() as conn:
            before=[tuple(r) for r in conn.execute('SELECT '+history_fields+' FROM review_history')]
            schedule=dict(conn.execute('SELECT * FROM sense_srs_state WHERE retired_at IS NULL').fetchone())
        result=self.proposal(source,[['a','b'],['c']])
        result['groups'][0]['definition_cn']='地址；住址'
        partial=copy.deepcopy(result);partial['status']='partial'
        partial['groups']=partial['groups'][:1];partial['pending_keys']=['c']
        self.assertEqual(self.apply(source,partial)['applied'],1)
        self.assertEqual(self.apply(source,partial)['cached'],1)
        with database.connect() as conn:
            after=source_word(conn,self.word_id)
            self.assertEqual(next(s for s in after['senses'] if s['key']=='c'),source['senses'][2])
            self.assertEqual(original_source(conn,self.word_id),source)
        self.assertEqual(self.apply(source,result)['applied'],1)
        self.assertEqual(self.apply(source,result)['cached'],1)
        with database.connect() as conn:
            self.assertEqual(before,[tuple(r) for r in conn.execute('SELECT '+history_fields+' FROM review_history')])
            after=conn.execute('SELECT * FROM sense_srs_state WHERE retired_at IS NULL').fetchone()
            for field in ('correct_count','interval_days','next_review_date','last_example_id'):
                self.assertEqual(schedule[field],after[field])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(len(original_source(conn,self.word_id)['senses']),3)

    def test_missing_sense_can_join_a_partially_applied_core(self):
        source=self.install([fixtures.sense('a','地址','Please give me your address.'),
                             fixtures.sense('b','住址','Write your address here.')])
        partial=self.proposal(source,[['a']]);partial.update(status='partial',pending_keys=['b'])
        self.assertEqual(self.apply(source,partial)['applied'],1)
        self.assertEqual(self.apply(source,self.proposal(source,[['a','b']]))['applied'],1)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(DISTINCT b.learning_unit_id) FROM learning_unit_senses b JOIN word_senses s ON s.id=b.sense_id WHERE s.word_id=? AND s.active=1',(self.word_id,)).fetchone()[0],1)
            self.assertEqual(original_source(conn,self.word_id),source)

    def test_partial_cannot_hide_source_edits_or_split_an_accepted_core(self):
        source=self.install([fixtures.sense('a','地址','Please give me your address.'),
                             fixtures.sense('b','住址','Write your address here.'),
                             fixtures.sense('c','处理','We address the problem.',pos='动词')])
        partial=self.proposal(source,[['a','b']]);partial.update(status='partial',pending_keys=['c'])
        self.assertEqual(self.apply(source,partial)['applied'],1)
        self.assertEqual(self.apply(source,self.proposal(source))['stale'],1)
        with database.connect() as conn:
            conn.execute("UPDATE word_senses SET definition_cn='手动修改' WHERE word_id=? AND sense_key='c'",(self.word_id,))
        self.assertEqual(self.apply(source,self.proposal(source,[['a','b'],['c']]))['stale'],1)

    def test_invalid_structures_and_content_never_overwrite(self):
        source=self.source();valid=self.proposal(source)
        variants=[]
        for field,value in [('definition_cn','English'),('part_of_speech','bad'),('definition_en','invented dictionary definition')]:
            result=copy.deepcopy(valid);result['groups'][0][field]=value;variants.append(result)
        result=copy.deepcopy(valid);result['groups'][0]['members'][0]['examples'][0]['sentence']='Unrelated sentence.';variants.append(result)
        result=copy.deepcopy(valid);result['groups'].pop();variants.append(result)
        result=copy.deepcopy(valid);result['groups'][0]['members'][0]['examples'][0]['translation_cn']='待翻译';variants.append(result)
        result=copy.deepcopy(valid);result['unexpected']=True;variants.append(result)
        for result in variants:
            with self.subTest(result=result):
                self.assertEqual(self.apply(source,result)['stale'],1)
                with database.connect() as conn:
                    self.assertEqual(source_word(conn,self.word_id),source)

    def test_repeat_application_cache_and_restart_leave_history_unchanged(self):
        source=self.source();result=self.proposal(source)
        self.assertEqual(self.apply(source,result)['applied'],1)
        with database.connect() as conn:
            count=conn.execute('SELECT COUNT(*) FROM learning_unit_merges').fetchone()[0]
            learning_units.sync_learning_units(conn)
        self.assertEqual(self.apply(source,result)['cached'],1)
        database.init_database()
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM learning_unit_merges').fetchone()[0],count)

    def test_changed_source_rejected_before_mutation(self):
        source=self.source()
        with database.connect() as conn:
            conn.execute("UPDATE word_senses SET definition_cn='新数据' WHERE word_id=?",(self.word_id,))
            changed=source_word(conn,self.word_id)
        self.assertEqual(self.apply(source,self.proposal(source))['stale'],1)
        with database.connect() as conn:
            self.assertEqual(source_word(conn,self.word_id),changed)

    def test_changed_reviewed_source_stops_showing_stale_core_and_keeps_progress(self):
        from app.senses import senses_for_word
        source=self.source();result=self.proposal(source)
        self.apply(source,result)
        with database.connect() as conn:
            conn.execute("UPDATE word_senses SET definition_cn='新地址释义' WHERE word_id=? AND sense_key='place'",(self.word_id,))
        self.assertEqual(self.apply(source,result)['stale'],1)
        with database.connect() as conn:
            senses=senses_for_word(conn,self.word_id,self.uid)
            self.assertTrue(all(s['core_definition_cn'] is None for s in senses))
            self.assertEqual(original_source(conn,self.word_id),source_word(conn,self.word_id))

    def test_compact_exact_core_normalization_keeps_every_source(self):
        from app.semantic_review import ModelWord, expand_model_word
        source=self.install([fixtures.sense('a','地址','Please give me your address.'),
                             fixtures.sense('b','住址','Write your address here.')])
        review=ModelWord.model_validate({'word':'address','status':'ready','notes':'','groups':[
            {'senses':[sid],'part_of_speech':'名词','definition_cn':'地址','english_from':0,'notes':'','evidence':'例句表示地址'}
            for sid in (1,2)],'translations':[{'id':1,'cn':'请告诉我你的地址。'},{'id':2,'cn':'在这里写下你的地址。'}]})
        expanded=expand_model_word(source,review)
        self.assertEqual(len(expanded['groups']),1)
        self.assertEqual(len(expanded['groups'][0]['members']),2)
        validate_word(source,expanded)

    def test_merge_preserves_answer_events_curves_and_list_scope(self):
        source=self.install([fixtures.sense('a','地址','Please give me your address.'),
                             fixtures.sense('b','住址','Write your address here.')])
        card=self.next();self.review(card,card['answer_form'])
        with database.connect() as conn:
            before=[tuple(r) for r in conn.execute('SELECT id,user_id,word_id,sense_id,example_id,review_time,user_answer,is_correct FROM review_history')]
            state=dict(conn.execute('SELECT * FROM sense_srs_state WHERE retired_at IS NULL').fetchone())
        self.assertEqual(self.apply(source,self.proposal(source,[['a','b']]))['applied'],1)
        with database.connect() as conn:
            self.assertEqual(before,[tuple(r) for r in conn.execute('SELECT id,user_id,word_id,sense_id,example_id,review_time,user_answer,is_correct FROM review_history')])
            after=conn.execute('SELECT * FROM sense_srs_state WHERE retired_at IS NULL').fetchone()
            for field in ('correct_count','interval_days','next_review_date','last_example_id'):
                self.assertEqual(state[field],after[field])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM semantic_review_receipts').fetchone()[0],1)
        self.assertEqual(self.client.get('/api/stats').json()['new_senses'],0)

    def test_prior_core_cannot_be_split_by_new_result(self):
        source=self.install([fixtures.sense('a','地址','Please give me your address.'),
                             fixtures.sense('b','住址','Write your address here.')])
        self.apply(source,self.proposal(source,[['a','b']]))
        with database.connect() as conn:
            changed=source_word(conn,self.word_id)
        result=self.proposal(changed);result['groups'][1]['definition_cn']='住址'
        self.assertEqual(self.apply(changed,result)['stale'],1)

    def test_full_manifest_requires_all_words_and_source_hash(self):
        source=self.source();manifest={'format_version':1,'task':'semantic_review','records':[source],'batch':digest([source])}
        result={'batch':manifest['batch'],'items':[self.proposal(source)]}
        validate_response(manifest,result)
        with self.assertRaises(ValueError):validate_response(manifest,{**result,'batch':'other'})
        with self.assertRaises(ValueError):validate_response(manifest,{**result,'items':result['items']*2})

    def test_exchange_reuses_completed_words_without_model_call(self):
        source=self.source();result=self.proposal(source);bundle=self.directory/'cache.json'
        bundle.write_text(json.dumps({'format_version':1,'entries':[{'source':source,'result':result}]}))
        self.assertEqual(export_batches(database.DB_PATH,self.directory/'export',bundle,words=['address'])['exported_words'],0)
        with database.connect() as conn:
            conn.execute("UPDATE word_senses SET definition_cn='变化' WHERE word_id=?",(self.word_id,))
        self.assertEqual(export_batches(database.DB_PATH,self.directory/'export2',bundle,words=['address'])['exported_words'],1)

    def test_quarantined_model_output_is_retryable_separately_from_manual_review(self):
        source=self.source();bundle=self.directory/'cache.json'
        quarantined={'word':'address','status':'manual','groups':[],
            'notes':RETRYABLE_NOTE_PREFIX+'Every original sense must be mapped'}
        human_review={'word':'address','status':'manual','groups':[],
            'notes':'例句与词义冲突，需要人工核查'}
        self.assertTrue(retryable_review(quarantined))
        self.assertFalse(retryable_review(human_review))
        bundle.write_text(json.dumps({'format_version':1,'entries':[{'source':source,'result':quarantined}]}))
        skipped=export_batches(database.DB_PATH,self.directory/'skip',bundle,words=['address'])
        self.assertEqual((skipped['exported_words'],skipped['retry_mode']),(0,'new'))
        retried=export_batches(database.DB_PATH,self.directory/'retry',bundle,words=['address'],
                               batch_size=1,retry_quarantined=True)
        self.assertEqual((retried['exported_words'],retried['retry_mode']),(1,'quarantined'))

    def test_explicit_empty_course_scope_survives_semantic_merge_and_restart_sync(self):
        source=self.source()
        with database.connect() as conn:
            conn.execute("UPDATE word_list_learning_scopes SET verification='reviewed' WHERE word_id=?",(self.word_id,))
            conn.execute('DELETE FROM word_list_learning_units WHERE learning_unit_id IN (SELECT id FROM learning_units WHERE word_id=?)',(self.word_id,))
        self.assertEqual(self.apply(source,self.proposal(source))['applied'],1)
        with database.connect() as conn:
            learning_units.sync_learning_units(conn)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM word_list_learning_units WHERE learning_unit_id IN (SELECT id FROM learning_units WHERE word_id=?)',(self.word_id,)).fetchone()[0],0)

    def test_failure_timeout_and_invalid_results_never_mark_cache_complete(self):
        source=self.source();folder=self.directory/'batches'
        export_batches(database.DB_PATH,folder,self.directory/'no-cache.json',words=['address'])
        for kind in ('failure','timeout','invalid'):
            proc=Mock(pid=123,returncode=1 if kind=='failure' else 0)
            if kind=='timeout':proc.communicate.side_effect=[subprocess.TimeoutExpired('codex',1),('',None)]
            else:proc.communicate.return_value=('',None)
            def start(command,**kwargs):
                Path(command[command.index('--output-last-message')+1]).write_text('{}')
                self.assertIn('model_reasoning_effort="low"',command)
                return proc
            with patch('translation_exchange.subprocess.Popen',side_effect=start), patch('translation_exchange.os.killpg') as kill:
                with self.assertRaises((subprocess.SubprocessError,ValueError)):
                    run_batches(folder,timeout=1)
                if kind=='timeout':kill.assert_called_once()
            self.assertFalse((folder/'batch-0001.result.json').exists())
            with database.connect() as conn:self.assertEqual(source_word(conn,self.word_id),source)

    def test_dry_run_exercises_merge_but_preserves_database(self):
        source=self.source();bundle=self.directory/'dry.json'
        bundle.write_text(json.dumps({'format_version':1,'entries':[{'source':source,'result':self.proposal(source)}]}))
        self.assertEqual(apply_database(database.DB_PATH,bundle,True)['applied'],1)
        with database.connect() as conn:self.assertEqual(source_word(conn,self.word_id),source)

    def test_display_notes_preserve_semantic_difference_without_model_ids(self):
        source=self.source();result=self.proposal(source)
        result['groups'][0]['notes']='可指居住地点或通讯地址；S1 和 S2 是相同来源概念。'
        self.assertEqual(self.apply(source,result)['applied'],1)
        with database.connect() as conn:
            notes=[json.loads(row[0]).get('notes') for row in conn.execute(
                "SELECT provenance_json FROM learning_units WHERE word_id=? AND active=1",(self.word_id,))]
            self.assertIn('可指居住地点或通讯地址',notes)
            self.assertFalse(any('S1' in (note or '') for note in notes))

    def test_legacy_collector_cannot_regress_ready_or_partial_coverage(self):
        source=self.source();ready=self.proposal(source)
        first=copy.deepcopy(ready);first['status']='partial';first['groups']=first['groups'][:1]
        first['pending_keys']=[s['key'] for s in source['senses'][1:]]
        second=copy.deepcopy(ready);second['status']='partial';second['groups']=second['groups'][1:]
        second['pending_keys']=[source['senses'][0]['key']]
        folder=self.directory/'legacy-collect';folder.mkdir()
        manifest={'format_version':1,'task':'semantic_review','batch':digest([source]),'records':[source]}
        (folder/'batch-0001.manifest.json').write_text(json.dumps(manifest))
        (folder/'batch-0001.run.json').write_text(json.dumps({'model':'gpt-6-luna','reasoning_effort':'low'}))
        cache=self.directory/'legacy-cache.json'
        for old,new in [(ready,first),(first,second)]:
            document={'format_version':1,'entries':[{'source':source,'result':old}]}
            cache.write_text(json.dumps(document))
            (folder/'batch-0001.result.json').write_text(json.dumps({'batch':manifest['batch'],'items':[new]}))
            collect(folder,cache)
            self.assertEqual(json.loads(cache.read_text()),document)


if __name__=='__main__':unittest.main()
