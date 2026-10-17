"""Local recovery, staged resumption and strict final conservation."""
import copy
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.semantic_review import digest, validate_word
from app.semantic_staging import (accept_translations, compose_review, example_index, grouping_input,
    normalize_grouping, translation_tasks, validate_stage_response, accept_verifications, verification_tasks)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import semantic_staged_exchange as exchange
from translation_exchange import atomic_json, run_batches


def source():
    return {'word': 'address', 'senses': [
        {'key': 'a', 'part_of_speech': '名词', 'definition_cn': '地址', 'definition_en': 'where someone lives',
         'source': 'dictionary', 'learning_enabled': 1,
         'examples': [{'sentence': 'Write your address here.', 'translation_cn': '在这里写下你的地址。',
                       'target_form': 'address', 'source': 'dictionary'}]},
        {'key': 'b', 'part_of_speech': '名词', 'definition_cn': '住址', 'definition_en': None,
         'source': 'dictionary', 'learning_enabled': 1,
         'examples': [{'sentence': 'What is your address?', 'translation_cn': None,
                       'target_form': 'address', 'source': 'dictionary'}]},
        {'key': 'c', 'part_of_speech': '动词', 'definition_cn': 'deal with', 'definition_en': 'deal with a problem',
         'source': 'dictionary', 'learning_enabled': 1,
         'examples': [{'sentence': 'We address this problem.', 'translation_cn': '我们处理这个问题。',
                       'target_form': 'address', 'source': 'dictionary'}]}]}


def group(senses, meaning='地址', pos='名词'):
    return {'senses': senses, 'definition_cn': meaning, 'part_of_speech': pos,
            'notes': '', 'evidence': '原句描述地址或处理问题的用法。'}


def result(groups, **kwargs):
    return {'word': 'address', 'groups': groups, 'pending': [], 'corrections': [], 'notes': '', **kwargs}


class SemanticStagingTests(unittest.TestCase):
    def test_ready_cache_that_splits_a_historical_core_is_queued_for_review(self):
        s=source();state=normalize_grouping(s,result([group([1]),group([2],'住址'),group([3],'处理','动词')]))
        state['translations']['2']='你的地址是什么？'
        ready=compose_review(s,state);self.assertEqual(ready['status'],'ready')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);bundle=root/'bundle.json';lists=root/'lists.json';snapshot=root/'sources.json'
            atomic_json(bundle,{'format_version':1,'entries':[{'source':s,'result':ready}]})
            atomic_json(lists,{'lists':[{'id':'cet4','words':['address']}]})
            atomic_json(snapshot,{'list_id':'cet4','all_words':['address'],'sources':[s],
                'protected_groups':{'address':[['a','b']]}})
            directory=root/'work';report=exchange.prepare(directory,bundle,lists=lists,source_snapshot=snapshot)
            self.assertEqual((report['cached_ready'],report['queued'],report['invalid_cache']),(0,1,1))
            checkpoint=json.loads(exchange.checkpoint_path(directory,'address').read_text())
            self.assertEqual(checkpoint['pending'],[1,2])
            self.assertIn('S1',checkpoint['issues'][-1])

    def test_missing_example_translation_only_holds_its_member_of_the_same_core(self):
        s = source();state = normalize_grouping(s, result([group([1, 2]), group([3], '处理', '动词')]))
        proposal = compose_review(s, state)
        self.assertEqual(proposal['status'], 'partial')
        self.assertEqual(proposal['pending_keys'], ['b'])
        self.assertEqual(proposal['groups'][0]['members'][0]['key'], 'a')
        self.assertEqual(proposal['groups'][0]['members'][0]['examples'][0]['sentence'],
                         s['senses'][0]['examples'][0]['sentence'])
        self.assertEqual(exchange.protect_existing_cores(s, proposal, [['a', 'b']])['pending_keys'], ['a', 'b'])

    def test_legacy_translation_task_is_bound_without_editing_its_manifest(self):
        s = source();state = normalize_grouping(s, result([group([1, 2])]))
        row = {'id': 1, **translation_tasks(s, state)[0]};row.pop('sense')
        original = copy.deepcopy(row)
        bound = exchange.bind_example_senses([row], {'address': s})
        self.assertEqual(bound[0]['sense'], 2)
        self.assertEqual(row, original)
        self.assertEqual(accept_translations(bound, {'items': [{'id': 1, 'cn': '你的地址是什么？'}]},
            {'address': state})['accepted'], 1)
        row['sentence'] = 'A changed sentence.'
        with self.assertRaisesRegex(ValueError, 'example identity'):
            exchange.bind_example_senses([row], {'address': s})

    def test_translation_from_a_previous_core_is_ignored_after_regrouping(self):
        s = source();state = normalize_grouping(s, result([group([1, 2])]))
        row = {'id': 1, **translation_tasks(s, state)[0]}
        state['groups'][0]['definition_cn'] = '通讯地址'
        accepted = accept_translations([row], {'items': [{'id': 1, 'cn': '你的地址是什么？'}]}, {'address': state})
        self.assertEqual(accepted['accepted'], 0)
        self.assertEqual(state['translations'], {})

    def test_audit_from_a_previous_core_cannot_reject_the_new_core(self):
        s = source();state = normalize_grouping(s, result([group([1])],
            corrections=[{'id': 'E1', 'reason': '原译疑似错义。'}]))
        state['translations']['1'] = '候选译文。'
        records = [{'id': 1, **verification_tasks(s, state)[0]}]
        state['groups'][0]['definition_cn'] = '通讯地址'
        accept_verifications(records, {'items': [{'id': 1, 'choice': 'pending',
            'meaning_matches': False, 'reason': '旧核心与句子不符。'}]}, {'address': state})
        self.assertEqual(state['groups'][0]['definition_cn'], '通讯地址')
        self.assertEqual(state['translations']['1'], '候选译文。')

    def test_identical_duplicate_ids_and_cores_are_normalized(self):
        s = source(); state = normalize_grouping(s, result([group([1, 1]), group([1, 2]), group([3], '处理', '动词')]))
        self.assertEqual([g['senses'] for g in state['groups']], [[1, 2], [3]])
        self.assertEqual(state['pending'], [])

    def test_conflicting_groups_only_quarantine_affected_senses(self):
        s = source(); state = normalize_grouping(s, result([group([1, 2]), group([2], '演说'), group([3], '处理', '动词')]))
        self.assertEqual(state['pending'], [1, 2])
        proposal = compose_review(s, state)
        self.assertEqual(proposal['status'], 'partial')
        self.assertEqual(proposal['pending_keys'], ['a', 'b'])
        self.assertEqual(proposal['groups'][0]['definition_cn'], '处理')

    def test_unknown_group_does_not_destroy_other_valid_groups(self):
        s = source();state = normalize_grouping(s, result([group([99, 1]), group([3], '处理', '动词')]))
        self.assertEqual(state['pending'], [1, 2])
        self.assertEqual(compose_review(s, state)['status'], 'partial')

    def test_one_malformed_group_does_not_drop_another_word_sense(self):
        s = source();bad = group([1]);bad['definition_cn'] = ''
        state = normalize_grouping(s, result([bad, group([3], '处理', '动词')]))
        self.assertEqual(state['pending'], [1, 2])
        self.assertEqual(compose_review(s, state)['status'], 'partial')

    def test_missing_sense_repair_retains_accepted_groups(self):
        s = source(); first = normalize_grouping(s, result([group([1])]))
        second = normalize_grouping(s, result([group([2]), group([3], '处理', '动词')]), first, first['pending'])
        self.assertEqual(second['pending'], [])
        self.assertEqual(second['groups'][0]['senses'], [1, 2])
        self.assertEqual(grouping_input(s, first)['focus'], ['S2', 'S3'])

    def test_translations_only_missing_and_existing_accurate_preserved(self):
        s = source(); before = copy.deepcopy(s)
        state = normalize_grouping(s, result([group([1, 2]), group([3], '处理', '动词')]))
        tasks = translation_tasks(s, state)
        self.assertEqual([t['example'] for t in tasks], [2])
        tasks = [{'id': 1, **tasks[0]}]
        self.assertEqual(accept_translations(tasks, {'items': [{'id': 1, 'cn': '你的地址是什么？'}]}, {'address': state})['missing'], 0)
        final = compose_review(s, state)
        self.assertEqual(final['status'], 'ready')
        self.assertEqual(final['groups'][0]['members'][0]['examples'][0]['translation_cn'], before['senses'][0]['examples'][0]['translation_cn'])
        self.assertEqual(s, before)
        self.assertEqual(translation_tasks(s, state), [])
        self.assertEqual(final['groups'][0]['definition_en'], s['senses'][0]['definition_en'])

    def test_existing_inaccurate_translation_needs_completed_correction(self):
        s = source();state = normalize_grouping(s, result([group([1]), group([3], '处理', '动词')],
            corrections=[{'id': 1, 'reason': '原译文把地址误译为演讲。'}]))
        self.assertEqual(compose_review(s, state)['pending_keys'], ['a', 'b'])
        self.assertEqual(translation_tasks(s, state)[0]['example'], 1)

    def test_existing_translation_changes_require_independent_confirmation(self):
        s = source();state = normalize_grouping(s, result([group([1]), group([3], '处理', '动词')],
            corrections=[{'id': 'E1', 'reason': '原译误解了名词语义。'}]))
        state['translations']['1'] = '请在此写下地址。'
        self.assertIn('a', compose_review(s, state)['pending_keys'])
        records = [{'id': 1, **verification_tasks(s, state)[0]}]
        accept_verifications(records, {'items': [{'id': 1, 'choice': 'original', 'meaning_matches': True,
            'reason': '原译自然准确，仅是表述不同。'}]}, {'address': state})
        proposal = compose_review(s, state)
        self.assertNotIn('a', proposal['pending_keys'])
        self.assertEqual(proposal['groups'][0]['members'][0]['examples'][0]['translation_cn'], s['senses'][0]['examples'][0]['translation_cn'])

    def test_self_declared_no_error_cannot_trigger_rewrite(self):
        s = source();state = normalize_grouping(s, result([group([1])],
            corrections=[{'id': 'E1', 'reason': '原译语义正确，无需更正。'}]))
        self.assertEqual(state['corrections'], {})
        state['corrections']['1'] = '原译语义正确，无需更正。'
        state['translations']['1'] = '无关的候选改写。'
        proposal = compose_review(s, state)
        self.assertEqual(proposal['groups'][0]['members'][0]['examples'][0]['translation_cn'], s['senses'][0]['examples'][0]['translation_cn'])

    def test_same_text_candidate_still_needs_independent_conflict_resolution(self):
        s = source();state = normalize_grouping(s, result([group([1])],
            corrections=[{'id': 'E1', 'reason': '原译疑似有实质错误。'}]))
        state['translations']['1'] = s['senses'][0]['examples'][0]['translation_cn']
        self.assertEqual(compose_review(s, state)['status'], 'manual')
        records = [{'id': 1, **verification_tasks(s, state)[0]}]
        accept_verifications(records, {'items': [{'id': 1, 'choice': 'original',
            'meaning_matches': True, 'reason': '核对句义后确认原译准确。'}]}, {'address': state})
        self.assertEqual(compose_review(s, state)['status'], 'partial')

    def test_independent_context_rejection_requeues_only_its_core(self):
        s = source();state = normalize_grouping(s, result([group([1, 2]), group([3], '处理', '动词')],
            corrections=[{'id': 'E1', 'reason': '原译疑似对应其他义项。'}]))
        state['translations']['1'] = '候选译文。'
        state['translations']['2'] = '同一旧核心的候选译文。'
        records = [{'id': 1, **verification_tasks(s, state)[0]}]
        accept_verifications(records, {'items': [{'id': 1, 'choice': 'original', 'meaning_matches': False,
            'reason': '原译准确，但核心释义与此例句语境不符。'}]}, {'address': state})
        self.assertEqual(state['pending'], [1, 2])
        self.assertEqual([g['senses'] for g in state['groups']], [[3]])
        self.assertEqual(compose_review(s, state)['pending_keys'], ['a', 'b'])
        self.assertNotIn('1', state['translations'])
        self.assertNotIn('1', state['verifications'])
        self.assertNotIn('2', state['translations'])

    def test_foreign_example_evidence_only_rejects_its_group(self):
        s = source();bad = group(['S1']);bad['evidence'] = 'E3 demonstrates this noun sense.'
        state = normalize_grouping(s, result([bad, group(['S3'], '处理', '动词')]))
        self.assertEqual(state['pending'], [1, 2])
        self.assertEqual(compose_review(s, state)['status'], 'partial')

    def test_recheck_clears_candidates_generated_for_an_invalidated_core(self):
        s = source();state = normalize_grouping(s, result([group([1, 2]), group([3], '处理', '动词')]))
        state['groups'][0]['evidence'] = 'E3 supports this group.'
        state['translations'] = {'1': '旧核心候选。', '2': '旧核心候选。', '3': '我们处理这个问题。'}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            atomic_json(directory/'snapshot.json', {'records': [s], 'all_words': ['address'],
                'cached_ready': [], 'missing_source': [], 'protected_groups': {}})
            atomic_json(exchange.checkpoint_path(directory, 'address'), state)
            self.assertEqual(exchange.recheck(directory)['changed_words'], 1)
            after = json.loads(exchange.checkpoint_path(directory, 'address').read_text())
            self.assertEqual(after['pending'], [1, 2])
            self.assertEqual(after['translations'], {'3': '我们处理这个问题。'})

    def test_conflicting_duplicate_translations_rejected_but_identical_deduped(self):
        s = source();state = normalize_grouping(s, result([group([1, 2])]))
        row = {'id': 1, **translation_tasks(s, state)[0]}
        response = {'items': [{'id': 1, 'cn': '你的地址？'}, {'id': 1, 'cn': '这是演讲。'}, {'id': 999, 'cn': '未知。'}]}
        self.assertEqual(accept_translations([row], response, {'address': state})['accepted'], 0)
        self.assertEqual(state['translations'], {})
        response['items'] = [{'id': 1, 'cn': '你的地址？'}] * 2
        self.assertEqual(accept_translations([row], response, {'address': state})['accepted'], 1)

    def test_partial_manifest_requires_exact_remaining_keys(self):
        s = source();state = normalize_grouping(s, result([group([3], '处理', '动词')]))
        proposal = compose_review(s, state)
        proposal['pending_keys'] = ['a']
        with self.assertRaisesRegex(ValueError, 'untouched'):
            validate_word(s, proposal)

    def test_source_changes_and_wrong_batch_are_rejected(self):
        s = source();state = normalize_grouping(s, result([group([1])]))
        changed = copy.deepcopy(s);changed['senses'][0]['definition_cn'] = '新地址'
        with self.assertRaisesRegex(ValueError, 'source changed'):
            compose_review(changed, state)
        manifest = {'format_version': 1, 'task': 'semantic_grouping', 'records': [s], 'batch': digest([s])}
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            validate_stage_response(manifest, {'batch': 'wrong', 'items': []})

    def test_sense_and_example_namespaces_cannot_be_confused(self):
        s = source()
        value = result([{**group(['S1']), 'senses': ['S1']}, group(['E2']), group(['S3'], '处理', '动词')])
        state = normalize_grouping(s, value)
        self.assertEqual(state['pending'], [2])
        compact = grouping_input(s)
        self.assertEqual(compact['senses'][0][0], 'S1')
        self.assertEqual(compact['senses'][0][4][0][0], 'E1')

    def test_staged_repair_and_resume_reuses_completed_requests(self):
        s = source()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            snapshot = {'all_words': ['address'], 'cached_ready': [], 'missing_source': [], 'records': [s]}
            atomic_json(directory / 'snapshot.json', snapshot)
            calls = []
            def fake_model(job, **kwargs):
                output = job / 'batch-0001.result.json'
                if output.exists(): return {}
                manifest = json.loads((job / 'batch-0001.manifest.json').read_text())
                calls.append(manifest['task'])
                if manifest['task'] == 'semantic_grouping':
                    record = manifest['records'][0]
                    groups = [group([1]), group([3], '处理', '动词')] if record['state'] is None else [group([2])]
                    response = {'batch': manifest['batch'], 'items': [result(groups)]}
                else:
                    response = {'batch': manifest['batch'], 'items': [{'id': r['id'], 'cn': '你的地址是什么？'} for r in manifest['records']]}
                atomic_json(output, response)
                return {}
            with patch.object(exchange, 'run_batches', side_effect=fake_model):
                report = exchange.run(directory, rounds=2)
                self.assertEqual(report['ready'], 1)
                count = len(calls)
                second = exchange.run(directory, rounds=2)
                self.assertEqual(second['ready'], 1)
                self.assertEqual(len(calls), count)
            entries = json.loads((directory / 'results.json').read_text())['entries']
            self.assertEqual(entries[0]['source'], s)
            validate_word(s, entries[0]['result'])

    def test_staged_runner_uses_existing_low_model_and_isolated_cli(self):
        s = source()
        with tempfile.TemporaryDirectory() as tmp:
            jobs = exchange.plan_jobs(Path(tmp), [{'source': s, 'state': None}], 'semantic_grouping', 18000)
            result = run_batches(jobs[0], dry_run=True)
            self.assertEqual(result['model'], 'gpt-6-luna')
            self.assertEqual(result['reasoning_effort'], 'low')
            self.assertIn('--ignore-user-config', result['commands'][0])

    def test_old_unconsumed_grouping_does_not_replace_newer_checkpoint(self):
        s = source()
        state = normalize_grouping(s, result([group([1]), group([3], '处理', '动词')]))
        state['translations']['1'] = '请在这里写下地址。'
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            atomic_json(directory/'snapshot.json', {'records': [s], 'all_words': ['address'],
                'cached_ready': [], 'missing_source': []})
            job = exchange.plan_jobs(directory/'round-0/grouping',
                [{'source': s, 'state': None}], 'semantic_grouping', 18000)[0]
            manifest = json.loads((job/'batch-0001.manifest.json').read_text())
            atomic_json(job/'batch-0001.result.json', {'batch': manifest['batch'],
                'items': [result([group([1, 2], '旧分组')])]})
            atomic_json(exchange.checkpoint_path(directory, 'address'), state)
            def retry(path, **kwargs):
                if not (path/'batch-0001.result.json').exists():
                    raise subprocess.CalledProcessError(1, 'codex')
            with patch.object(exchange, 'run_batches', side_effect=retry):
                exchange.run(directory, rounds=1)
            saved = json.loads(exchange.checkpoint_path(directory, 'address').read_text())
            self.assertEqual(saved, state)
            self.assertTrue((directory/'round-0/grouping/job-0002').exists())

    def test_consumed_omission_is_retried_when_resuming_the_same_round_count(self):
        s = source()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            atomic_json(directory/'snapshot.json', {'records': [s], 'all_words': ['address'],
                'cached_ready': [], 'missing_source': []})
            def empty(job, **kwargs):
                manifest = json.loads((job/'batch-0001.manifest.json').read_text())
                atomic_json(job/'batch-0001.result.json', {'batch': manifest['batch'], 'items': []})
            with patch.object(exchange, 'run_batches', side_effect=empty):
                self.assertEqual(exchange.run(directory, rounds=1)['manual'], 1)
            def succeed(job, **kwargs):
                if (job/'batch-0001.result.json').exists(): return
                manifest = json.loads((job/'batch-0001.manifest.json').read_text())
                items = ([result([group(['S1', 'S2']), group(['S3'], '处理', '动词')])]
                    if manifest['task']=='semantic_grouping' else
                    [{'id': r['id'], 'cn': '你的地址是什么？'} for r in manifest['records']])
                atomic_json(job/'batch-0001.result.json', {'batch': manifest['batch'], 'items': items})
            with patch.object(exchange, 'run_batches', side_effect=succeed):
                self.assertEqual(exchange.run(directory, rounds=1)['ready'], 1)
            old = directory/'round-0/grouping/job-0001/batch-0001.result.json'
            self.assertEqual(json.loads(old.read_text())['items'], [])
            self.assertTrue((directory/'round-0/grouping/job-0002/ingested.json').exists())

    def test_missing_translation_item_is_not_cached_as_completed(self):
        s = source()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            atomic_json(directory/'snapshot.json', {'records': [s], 'all_words': ['address'],
                'cached_ready': [], 'missing_source': []})
            fill = False
            def model(job, **kwargs):
                if (job/'batch-0001.result.json').exists(): return
                manifest = json.loads((job/'batch-0001.manifest.json').read_text())
                items = ([result([group(['S1', 'S2']), group(['S3'], '处理', '动词')])]
                    if manifest['task']=='semantic_grouping' else
                    ([{'id': r['id'], 'cn': '你的地址是什么？'} for r in manifest['records']] if fill else []))
                atomic_json(job/'batch-0001.result.json', {'batch': manifest['batch'], 'items': items})
            with patch.object(exchange, 'run_batches', side_effect=model):
                self.assertEqual(exchange.run(directory, rounds=1)['partial'], 1)
                fill = True
                self.assertEqual(exchange.run(directory, rounds=1)['ready'], 1)
            self.assertTrue((directory/'round-0/translations/job-0002/ingested.json').exists())

    def test_checkpoint_commit_failure_stops_later_stages(self):
        s = source()
        for failed_stage, later_stage in (
                ('grouping', 'translations'), ('translations', 'translation-audit')):
            with self.subTest(stage=failed_stage), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                atomic_json(directory/'snapshot.json', {'records': [s], 'all_words': ['address'],
                    'cached_ready': [], 'missing_source': []})

                def model(job, **kwargs):
                    manifest = json.loads((job/'batch-0001.manifest.json').read_text())
                    if manifest['task'] == 'semantic_grouping':
                        items = [result([group(['S1', 'S2']), group(['S3'], '处理', '动词')],
                            corrections=[{'id': 'E1', 'reason': '原译存在实质错义。'}])]
                    else:
                        items = [{'id': row['id'], 'cn': '已核查的中文译文。'}
                                 for row in manifest['records']]
                    atomic_json(job/'batch-0001.result.json',
                                {'batch': manifest['batch'], 'items': items})

                real_atomic_json = exchange.atomic_json
                def fail_receipt(path, value):
                    path = Path(path)
                    if path.name == 'ingested.json' and failed_stage in path.parts:
                        raise OSError('simulated receipt write failure')
                    return real_atomic_json(path, value)

                with patch.object(exchange, 'run_batches', side_effect=model), \
                     patch.object(exchange, 'atomic_json', side_effect=fail_receipt):
                    report = exchange.run(directory, rounds=1)

                self.assertTrue(any(item.get('checkpoint_failure')
                                    for item in report['request_failures']))
                self.assertFalse((directory/'round-0'/later_stage).exists())

    def test_impossible_output_schema_is_rejected_before_a_model_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = exchange.plan_jobs(Path(tmp), [{'source': source(), 'state': None}], 'semantic_grouping', 18000)
            schema = jobs[0]/'batch-0001.schema.json'
            value = json.loads(schema.read_text())
            value['properties']['items'].update(minItems=1, maxItems=0)
            atomic_json(schema, value)
            with patch('translation_exchange.subprocess.Popen') as process:
                with self.assertRaisesRegex(ValueError, 'Impossible output schema'):
                    run_batches(jobs[0])
                process.assert_not_called()

    def test_failed_stage_retry_preserves_previous_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = exchange.plan_jobs(Path(tmp), [{'source': source(), 'state': None}], 'semantic_grouping', 18000)[0]
            (job / 'batch-0001.result.pending.json').write_text('previous rejected response')
            (job / 'batch-0001.events.jsonl').write_text('previous transport trace')
            process = Mock(pid=123, returncode=1)
            process.communicate.return_value = ('', None)
            with patch('translation_exchange.subprocess.Popen', return_value=process):
                with self.assertRaises(subprocess.CalledProcessError):
                    run_batches(job)
            self.assertEqual(len(list(job.glob('failed-*.result.pending.json'))), 1)
            self.assertEqual(next(job.glob('failed-*.result.pending.json')).read_text(), 'previous rejected response')
            self.assertEqual(next(job.glob('failed-*.events.jsonl')).read_text(), 'previous transport trace')
            self.assertFalse((job / 'batch-0001.result.json').exists())

    def test_failed_grouping_can_resume_into_previously_empty_translation_plan(self):
        s = source()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            snapshot = {'all_words': ['address'], 'cached_ready': [], 'missing_source': [], 'records': [s]}
            atomic_json(directory / 'snapshot.json', snapshot)
            with patch.object(exchange, 'run_batches', side_effect=subprocess.CalledProcessError(1, 'codex')):
                first = exchange.run(directory, rounds=2)
                self.assertEqual(first['manual'], 1)
                self.assertEqual(len(first['request_failures']), 1)
            def success(job, **kwargs):
                manifest = json.loads((job / 'batch-0001.manifest.json').read_text())
                items = ([result([group(['S1', 'S2']), group(['S3'], '处理', '动词')])]
                         if manifest['task'] == 'semantic_grouping' else
                         [{'id': r['id'], 'cn': '你的地址是什么？'} for r in manifest['records']])
                atomic_json(job / 'batch-0001.result.json', {'batch': manifest['batch'], 'items': items})
                return {}
            with patch.object(exchange, 'run_batches', side_effect=success):
                second = exchange.run(directory, rounds=2)
                self.assertEqual(second['ready'], 1)
                self.assertEqual(second['saved_translations'], 1)

    def test_historical_core_protection_only_holds_back_conflicting_groups(self):
        s = source();state = normalize_grouping(s, result([group([1]), group([3], '处理', '动词')]))
        proposal = compose_review(s, state)
        filtered = exchange.protect_existing_cores(s, proposal, [['a', 'b']])
        self.assertEqual(filtered['status'], 'partial')
        self.assertEqual(filtered['pending_keys'], ['a', 'b'])
        self.assertEqual(filtered['groups'][0]['definition_cn'], '处理')

    def test_excluded_sense_cannot_poison_an_independent_valid_core(self):
        s = source();s['senses'][1]['learning_enabled'] = 0
        state = normalize_grouping(s, result([group([1, 2]), group([3], '处理', '动词')]))
        diagnostics = []
        proposal = compose_review(s, state, diagnostics)
        self.assertEqual(proposal['status'], 'partial')
        self.assertEqual(proposal['pending_keys'], ['a', 'b'])
        self.assertIn('enabled/excluded', diagnostics[0]['reason'])

    def test_live_source_rebase_requires_unchanged_baseline_and_keeps_original_source(self):
        old_source = source();old_entry = {'source': old_source, 'result':
            {'word': 'address', 'status': 'manual', 'groups': [], 'notes': '旧来源需核查'}}
        live = copy.deepcopy(old_source);live['senses'][0]['definition_cn'] = '通讯地址'
        state = normalize_grouping(live, result([group([1, 2]), group([3], '处理', '动词')]))
        state['translations']['2'] = '你的地址是什么？'
        proposal = compose_review(live, state)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp);bundle = directory / 'bundle.json'
            atomic_json(bundle, {'format_version': 1, 'entries': [old_entry]})
            atomic_json(directory / 'snapshot.json', {'baseline_entries': {'address': digest(old_entry)}})
            atomic_json(directory / 'results.json', {'format_version': 1, 'entries': [{'source': live, 'result': proposal}]})
            self.assertEqual(exchange.collect(directory, bundle)['ready'], 1)
            self.assertEqual(json.loads(bundle.read_text())['entries'][0]['source'], live)
            old_entry['result']['notes'] = '并行发生了修改'
            atomic_json(bundle, {'format_version': 1, 'entries': [old_entry]})
            with self.assertRaisesRegex(ValueError, 'Source bundle changed'):
                exchange.collect(directory, bundle)


if __name__ == '__main__':
    unittest.main()
