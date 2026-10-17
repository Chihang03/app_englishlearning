"""Resumable whole-course Luna review with local repairs and sentence checkpoints."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.semantic_review import BUNDLE_PATH, digest, original_source, validate_word
from app.semantic_staging import (GROUPING_PROMPT, TRANSLATION_PROMPT, TRANSLATION_AUDIT_PROMPT,
    GroupingResult, TranslationResult, TranslationAuditResult, accept_translations, accept_verifications,
    compose_review, example_index, grouping_input, normalize_grouping, stage_schema,
    validate_stage_response, verification_tasks)
from report_exchange import open_database
from translation_exchange import DEFAULT_MODEL, DEFAULT_REASONING, atomic_json, encoded, read_json, run_batches

ROOT = Path(__file__).resolve().parents[1]


def checkpoint_path(directory: Path, word: str) -> Path:
    return directory / 'checkpoints' / (digest(word) + '.json')


def cached_state(source: dict, result: dict) -> dict:
    keys = {s['key']: i for i, s in enumerate(source['senses'], 1)}
    ids = {(source['senses'][e['sense'] - 1]['key'], e['sentence']): eid
           for eid, e in example_index(source).items()}
    groups = [{**{k: g[k] for k in ('part_of_speech', 'definition_cn', 'notes', 'evidence')},
               'senses': [keys[m['key']] for m in g['members']]} for g in result['groups']]
    state = normalize_grouping(source, {'word': source['word'], 'groups': groups,
        'pending': [], 'corrections': [], 'notes': result['notes']})
    for group in result['groups']:
        for member in group['members']:
            for example in member['examples']:
                eid = str(ids[(member['key'], example['sentence'])])
                state['translations'][eid] = example['translation_cn']
                state['corrections'][eid] = '历史核查已确认该译文修正。'
                state['verifications'][eid] = 'candidate'
    return state


def prepare(directory: Path, bundle: Path = BUNDLE_PATH, list_id: str = 'cet4',
            lists: Path = ROOT / 'data/source_word_lists.json', db: Path | None = None,
            source_snapshot: Path | None = None) -> dict:
    if directory.exists():
        raise ValueError('Use a new work directory; run the same directory to resume')
    words = next((x['words'] for x in read_json(lists)['lists'] if x['id'] == list_id), None)
    if not words or len(words) != len(set(words)):
        raise ValueError('Missing or duplicate course word list')
    document = read_json(bundle)
    if document.get('format_version') != 1:
        raise ValueError('Unsupported source bundle')
    entries = {e['source']['word']: e for e in document['entries']}
    if len(entries) != len(document['entries']):
        raise ValueError('Duplicate cached source words')
    sources = {word: entries[word]['source'] for word in words if word in entries}
    protected = {}
    if source_snapshot is not None:
        if db is not None:
            raise ValueError('Choose a database or an exported source snapshot')
        exported = read_json(source_snapshot)
        if exported.get('list_id') != list_id or set(exported.get('all_words', [])) != set(words):
            raise ValueError('Source snapshot must cover the requested course')
        sources = {s['word']: s for s in exported['sources']}
        if len(sources) != len(exported['sources']) or not set(sources) <= set(words):
            raise ValueError('Duplicate or unknown exported source word')
        protected = exported.get('protected_groups', {})
    if db is not None:
        sources = {}
        with closing(open_database(db)) as conn:
            conn.execute('BEGIN')
            for row in conn.execute('SELECT id,word FROM words WHERE owner_id IS NULL').fetchall():
                if row['word'] in words:
                    source = original_source(conn, row['id'])
                    if source['senses']:
                        sources[row['word']] = source
                    units = {}
                    for sense in conn.execute('''SELECT s.sense_key,b.learning_unit_id FROM word_senses s
                        JOIN learning_unit_senses b ON b.sense_id=s.id WHERE s.word_id=?''', (row['id'],)):
                        units.setdefault(sense['learning_unit_id'], []).append(sense['sense_key'])
                    protected[row['word']] = [keys for keys in units.values() if len(keys) > 1]
    records, cached, invalid = [], [], []
    directory.mkdir(parents=True)
    for word in words:
        source = sources.get(word)
        if not source or not source['senses']:
            continue
        prior = entries.get(word)
        if prior and digest(prior['source']) == digest(source):
            try:
                validate_word(source, prior['result'])
                if prior['result']['status'] == 'ready':
                    compatible = protect_existing_cores(source, prior['result'], protected.get(word, []))
                    if compatible['status'] == 'ready':
                        cached.append(word)
                        continue
                    invalid.append(word)
                    state = cached_state(source, compatible)
                    numbers = {s['key']: 'S' + str(i) for i, s in enumerate(source['senses'], 1)}
                    boundaries = [[numbers[key] for key in keys if key in numbers]
                                  for keys in protected.get(word, [])]
                    state['issues'].append('已有共享学习核心：' + encoded(boundaries)
                        + '。若核心语义相通则优先保持整体；确需拆分时保留待核查，不强行合并独立概念。')
                    atomic_json(checkpoint_path(directory, word), state)
                if prior['result']['status'] == 'partial':
                    atomic_json(checkpoint_path(directory, word), cached_state(source, prior['result']))
            except (ValueError, KeyError, TypeError):
                invalid.append(word)
        records.append(source)
    snapshot = {'format_version': 1, 'list_id': list_id,
                'source_mode': 'production-export' if source_snapshot else ('database' if db else 'review-source-snapshots'),
                'all_words': words, 'cached_ready': cached, 'invalid_cache': invalid,
                'missing_source': [w for w in words if not sources.get(w, {}).get('senses')], 'records': records,
                'protected_groups': protected,
                'baseline_entries': {s['word']: digest(entries[s['word']]) for s in records if s['word'] in entries}}
    atomic_json(directory / 'snapshot.json', snapshot)
    report = {'course_words': len(words), 'source_words': len(sources), 'cached_ready': len(cached),
              'queued': len(records), 'missing_source': len(snapshot['missing_source']), 'invalid_cache': len(invalid)}
    atomic_json(directory / 'prepare-report.json', report)
    return report


def states_for(directory: Path, sources: list[dict]) -> dict:
    result = {}
    for source in sources:
        path = checkpoint_path(directory, source['word'])
        if path.exists():
            state = read_json(path)
            if state['source_hash'] != digest(source):
                raise ValueError('Checkpoint does not match frozen source')
            result[source['word']] = state
    return result


def plan_jobs(path: Path, records: list[dict], task: str, max_chars: int, max_words: int = 8) -> list[Path]:
    plan = path / 'plan.json'
    names = []
    if plan.exists():
        names = read_json(plan)['jobs']
        # Current records are only unresolved work. A consumed response may
        # have omitted an item, so it must not permanently cache that omission.
        # Unconsumed jobs are retried in place; completed raw outputs stay intact.
        def identity(row):
            return digest({k: v for k, v in row.items() if k != 'id'})
        known = {identity(row) for name in names if not (path / name / 'ingested.json').exists()
                 for row in read_json(path / name / 'batch-0001.manifest.json')['records']}
        records = [r for r in records if identity(r) not in known]
    is_grouping = task == 'semantic_grouping'
    batches, batch, size = [], [], 0
    for record in records:
        compact = grouping_input(record['source'], record['state']) if is_grouping else record
        length = len(encoded(compact))
        if batch and (size + length > max_chars or len(batch) >= (max_words if is_grouping else 40)):
            batches.append(batch); batch, size = [], 0
        batch.append(record); size += length
    if batch:
        batches.append(batch)
    for number, rows in enumerate(batches, len(names) + 1):
        job = path / f'job-{number:04d}'
        job.mkdir(parents=True)
        rows = rows if is_grouping else [{'id': i, **r} for i, r in enumerate(rows, 1)]
        manifest = {'format_version': 1, 'task': task, 'batch': digest(rows), 'records': rows}
        atomic_json(job / 'batch-0001.manifest.json', manifest)
        model = {'semantic_grouping': GroupingResult, 'semantic_translations': TranslationResult,
                 'semantic_translation_audit': TranslationAuditResult}[task]
        atomic_json(job / 'batch-0001.schema.json', stage_schema(model, manifest['batch'], rows))
        compact = ([grouping_input(r['source'], r['state']) for r in rows] if is_grouping
                   else [{k: v for k, v in r.items() if k not in (
                       {'source_hash', 'example', 'sense', 'core_examples', 'reason'} if task == 'semantic_translation_audit'
                       else {'source_hash', 'example', 'sense'})} for r in rows])
        (job / 'batch-0001.input.jsonl').write_text(encoded({'batch': manifest['batch'], 'items': compact}) + '\n', encoding='utf-8')
        prompt = {'semantic_grouping': GROUPING_PROMPT, 'semantic_translations': TRANSLATION_PROMPT,
                  'semantic_translation_audit': TRANSLATION_AUDIT_PROMPT}[task]
        (job / 'prompt.txt').write_text(prompt, encoding='utf-8')
        names.append(job.name)
    atomic_json(plan, {'jobs': names})
    return [path / name for name in names]


def execute_jobs(jobs: list[Path], workers: int, timeout: int, consume) -> list[dict]:
    failures = []
    def execute(path):
        return run_batches(path, model=DEFAULT_MODEL, reasoning=DEFAULT_REASONING, timeout=timeout)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(execute, job): job for job in jobs}
        for number, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            try:
                future.result()
                manifest = read_json(job / 'batch-0001.manifest.json')
                response = read_json(job / 'batch-0001.result.json')
                validate_stage_response(manifest, response)
                receipt = {'batch': manifest['batch'], 'result': digest(response)}
                ingested = job / 'ingested.json'
                if not ingested.exists() or read_json(ingested) != receipt:
                    try:
                        consume(manifest, response)
                        atomic_json(ingested, receipt)
                    except Exception as error:
                        # Checkpoints may already have been written. Do not let
                        # later stages build on a partially committed batch.
                        record = {'job': str(job), 'error': str(error)[:1500],
                                  'checkpoint_failure': True}
                        failures.append(record)
                        print(encoded(record), flush=True)
                        continue
                print(encoded({'stage': job.parent.name, 'completed': number, 'total': len(jobs), 'job': job.name}), flush=True)
            except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError) as error:
                record = {'job': str(job), 'error': str(error)[:1500]}
                failures.append(record)
                print(encoded(record), flush=True)
    return failures


def checkpoint_failed(failures: list[dict]) -> bool:
    return any(failure.get('checkpoint_failure') for failure in failures)


def bind_example_senses(records: list[dict], sources: dict[str, dict]) -> list[dict]:
    """Bind legacy tasks to frozen sources without changing signed manifests."""
    bound = []
    indexes = {}
    for record in records:
        word = record['word']
        source = sources[word]
        if record['source_hash'] != digest(source):
            raise ValueError('Task does not match its frozen source')
        if word not in indexes:
            indexes[word] = example_index(source)
        example = indexes[word][record['example']]
        if (record['sentence'] != example['sentence'] or record['target'] != example['target_form']
                or record.get('sense', example['sense']) != example['sense']):
            raise ValueError('Task example identity changed')
        bound.append({**record, 'sense': example['sense']})
    return bound


def run(directory: Path, workers: int = 3, rounds: int = 3, timeout: int = 300, max_chars: int = 18000) -> dict:
    if not 1 <= workers <= 4 or not 1 <= rounds <= 5 or timeout < 1 or max_chars < 1000:
        raise ValueError('Invalid workers/rounds/timeout/input budget')
    snapshot = read_json(directory / 'snapshot.json')
    sources = snapshot['records']
    sources_by_word = {s['word']: s for s in sources}
    states = states_for(directory, sources)
    failures = []
    for turn in range(rounds):
        grouped = [{'source': source, 'state': states.get(source['word'])} for source in sources
                   if source['word'] not in states or states[source['word']]['pending']]
        grouping_path = directory / f'round-{turn}' / 'grouping'
        jobs = plan_jobs(grouping_path, grouped, 'semantic_grouping', max_chars)
        def consume_groups(manifest, response):
            by_word = {}
            conflicts = set()
            for value in response['items']:
                if not isinstance(value, dict) or not isinstance(value.get('word'), str):
                    continue
                word = value['word']
                if word in by_word and value != by_word[word]:
                    conflicts.add(word)
                by_word[word] = value
            for record in manifest['records']:
                source, previous = record['source'], record['state']
                word = source['word']
                # A recheck or an interrupted receipt write may have advanced
                # this checkpoint since the job was planned. Its old context
                # must not replace newer groups, translations or audit results.
                if digest(previous) != digest(states.get(word)):
                    continue
                value = by_word.get(word) if word not in conflicts else None
                value = value or {'word': word, 'groups': [], 'pending': [], 'corrections': [], 'notes': '模型未返回可靠的单词结果。'}
                state = normalize_grouping(source, value, previous, previous['pending'] if previous else None)
                state['translations'].update(states.get(word, {}).get('translations', {}))
                states[word] = state
                atomic_json(checkpoint_path(directory, word), state)
        stage_failures = execute_jobs(jobs, workers, timeout, consume_groups)
        failures.extend(stage_failures)
        if checkpoint_failed(stage_failures):
            report = materialize(directory, snapshot, states, failures)
            print(encoded({'round': turn, **report}), flush=True)
            break
        translations = []
        from app.semantic_staging import translation_tasks
        for source in sources:
            if source['word'] in states:
                translations.extend(translation_tasks(source, states[source['word']]))
        jobs = plan_jobs(directory / f'round-{turn}' / 'translations', translations, 'semantic_translations', max_chars)
        def consume_translations(manifest, response):
            accept_translations(bind_example_senses(manifest['records'], sources_by_word), response, states)
            for word in {r['word'] for r in manifest['records']}:
                atomic_json(checkpoint_path(directory, word), states[word])
        stage_failures = execute_jobs(jobs, workers, timeout, consume_translations)
        failures.extend(stage_failures)
        if not checkpoint_failed(stage_failures):
            failures.extend(audit_changes(directory, snapshot, states, workers, timeout, max_chars,
                                          directory / f'round-{turn}' / 'translation-audit'))
        report = materialize(directory, snapshot, states, failures)
        print(encoded({'round': turn, **report}), flush=True)
        if failures:
            # Retry failed jobs in this round before planning later repair stages.
            break
    return report


def audit_changes(directory: Path, snapshot: dict, states: dict, workers: int = 3,
                  timeout: int = 300, max_chars: int = 18000, stage: Path | None = None) -> list:
    sources_by_word = {s['word']: s for s in snapshot['records']}
    tasks = [task for source in snapshot['records'] if source['word'] in states
             for task in verification_tasks(source, states[source['word']])]
    jobs = plan_jobs(stage or directory / 'verification', tasks, 'semantic_translation_audit', max_chars)
    def consume(manifest, response):
        records = bind_example_senses(manifest['records'], sources_by_word)
        for record in records:
            if 'core_examples' not in record:
                group = next((g for g in states[record['word']]['groups'] if record['sense'] in g['senses']), None)
                record['core_examples'] = [eid for eid, e in example_index(sources_by_word[record['word']]).items()
                    if group and e['sense'] in group['senses']]
        accept_verifications(records, response, states)
        for word in {r['word'] for r in manifest['records']}:
            atomic_json(checkpoint_path(directory, word), states[word])
    return execute_jobs(jobs, workers, timeout, consume)


def recheck(directory: Path) -> dict:
    snapshot = read_json(directory / 'snapshot.json')
    states = states_for(directory, snapshot['records'])
    changed = {}
    for source in snapshot['records']:
        old = states.get(source['word'])
        if not old:
            continue
        value = {'word': source['word'], 'groups': old['groups'], 'pending': old['pending'],
                 'corrections': [{'id': int(k), 'reason': v} for k, v in old['corrections'].items()],
                 'notes': old['notes']}
        state = normalize_grouping(source, value)
        for field in ('translations', 'verifications', 'verification_notes'):
            state[field] = dict(old.get(field, {}))
        invalidated = {sid for group in old['groups'] for sid in group['senses']} - {
            sid for group in state['groups'] for sid in group['senses']}
        for eid, example in example_index(source).items():
            if example['sense'] in invalidated:
                state['translations'].pop(str(eid), None)
                state['verifications'].pop(str(eid), None)
        if state['pending'] != old['pending'] or state['corrections'] != old['corrections']:
            changed[source['word']] = {'previous_pending': old['pending'], 'pending': state['pending'], 'issues': state['issues']}
        states[source['word']] = state
        atomic_json(checkpoint_path(directory, source['word']), state)
    atomic_json(directory / 'recheck-report.json', changed)
    materialize(directory, snapshot, states, [])
    return {'changed_words': len(changed)}


def materialize(directory: Path, snapshot: dict, states: dict, failures: list) -> dict:
    entries = []
    pending_report = {}
    for source in snapshot['records']:
        state = states.get(source['word'])
        if not state:
            state = normalize_grouping(source, {'word': source['word'], 'groups': [], 'pending': [],
                                                'corrections': [], 'notes': '模型调用未完成，原数据保留。'})
        diagnostics = []
        value = compose_review(source, state, diagnostics)
        value = protect_existing_cores(source, value, snapshot.get('protected_groups', {}).get(source['word'], []))
        if value['status'] != 'ready':
            pending_report[source['word']] = {'status': value['status'], 'pending_keys':
                value.get('pending_keys', [s['key'] for s in source['senses']] if value['status'] == 'manual' else []),
                'semantic_pending': state['pending'], 'issues': state['issues'],
                'notes': value['notes'], 'validation': diagnostics}
        entries.append({'source': source, 'result': value, 'model': DEFAULT_MODEL,
                        'reasoning_effort': DEFAULT_REASONING, 'review_version': 1, 'review_method': 'staged-v1'})
    atomic_json(directory / 'results.json', {'format_version': 1, 'entries': entries})
    atomic_json(directory / 'pending-report.json', pending_report)
    counts = Counter(e['result']['status'] for e in entries)
    report = {'course_words': len(snapshot['all_words']), 'cached_ready': len(snapshot['cached_ready']),
              'ready': counts['ready'], 'partial': counts['partial'], 'manual': counts['manual'],
              'source_missing': len(snapshot['missing_source']), 'request_failures': failures,
              'pending_senses': sum(len(s['senses']) - sum(len(g['members']) for g in e['result']['groups'])
                                    for e in entries for s in [e['source']]),
              'saved_translations': sum(len(s.get('translations', {})) for s in states.values())}
    atomic_json(directory / 'run-report.json', report)
    return report


def protect_existing_cores(source: dict, value: dict, boundaries: list[list[str]]) -> dict:
    """A historical core may only merge further. Hold back only affected groups."""
    accepted = []
    for group in value['groups']:
        keys = {m['key'] for m in group['members']}
        if any(keys & set(bound) and not set(bound) <= keys for bound in boundaries):
            continue
        accepted.append(group)
    if len(accepted) == len(value['groups']):
        return value
    covered = {m['key'] for g in accepted for m in g['members']}
    pending = [s['key'] for s in source['senses'] if s['key'] not in covered]
    result = {'word': source['word'], 'status': 'partial' if accepted else 'manual', 'groups': accepted,
              'notes': '部分候选分组会拆分已有学习核心，保留该部分原数据与进度。'}
    if accepted:
        result['pending_keys'] = pending
    validate_word(source, result)
    return result


def collect(directory: Path, bundle: Path = BUNDLE_PATH) -> dict:
    original = bundle.read_bytes()
    document = json.loads(original)
    existing = {e['source']['word']: e for e in document['entries']}
    baseline = read_json(directory / 'snapshot.json').get('baseline_entries', {})
    counts = Counter()
    for entry in read_json(directory / 'results.json')['entries']:
        source, result = entry['source'], entry['result']
        validate_word(source, result)
        old = existing.get(source['word'])
        changed_source = old and digest(old['source']) != digest(source)
        if changed_source and baseline.get(source['word']) != digest(old):
            raise ValueError('Source bundle changed during review; rebase the candidate explicitly')
        if old and not changed_source and (old['result']['status'] == 'ready' or
                    old['result']['status'] == 'partial' and result['status'] == 'manual'):
            counts['protected'] += 1
            continue
        if old and not changed_source and old['result']['status'] == 'partial':
            old_keys = {m['key'] for g in old['result']['groups'] for m in g['members']}
            new_keys = {m['key'] for g in result['groups'] for m in g['members']}
            if not old_keys <= new_keys:
                counts['protected'] += 1
                continue
        if result['status'] == 'manual':
            counts['manual_preserved'] += 1
            continue
        existing[source['word']] = entry
        counts[result['status']] += 1
    if bundle.read_bytes() != original:
        raise ValueError('Bundle changed during collection')
    atomic_json(bundle, {'format_version': 1, 'entries': list(existing.values())})
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('prepare')
    export.add_argument('--directory', type=Path, required=True)
    export.add_argument('--bundle', type=Path, default=BUNDLE_PATH)
    export.add_argument('--list-id', default='cet4')
    export.add_argument('--db', type=Path)
    export.add_argument('--source-snapshot', type=Path)
    runner = commands.add_parser('run')
    runner.add_argument('--directory', type=Path, required=True)
    runner.add_argument('--workers', type=int, default=3)
    runner.add_argument('--rounds', type=int, default=3)
    runner.add_argument('--timeout', type=int, default=300)
    runner.add_argument('--max-chars', type=int, default=18000)
    importer = commands.add_parser('collect')
    importer.add_argument('--directory', type=Path, required=True)
    importer.add_argument('--bundle', type=Path, default=BUNDLE_PATH)
    summary = commands.add_parser('summarize')
    summary.add_argument('--directory', type=Path, required=True)
    revalidator = commands.add_parser('recheck')
    revalidator.add_argument('--directory', type=Path, required=True)
    auditor = commands.add_parser('audit-translations')
    auditor.add_argument('--directory', type=Path, required=True)
    auditor.add_argument('--workers', type=int, default=3)
    auditor.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            report = prepare(args.directory, args.bundle, args.list_id, db=args.db, source_snapshot=args.source_snapshot)
        elif args.command == 'run':
            report = run(args.directory, args.workers, args.rounds, args.timeout, args.max_chars)
        elif args.command == 'summarize':
            snapshot = read_json(args.directory / 'snapshot.json')
            failures = read_json(args.directory / 'run-report.json').get('request_failures', []) if (args.directory / 'run-report.json').exists() else []
            report = materialize(args.directory, snapshot, states_for(args.directory, snapshot['records']), failures)
        elif args.command == 'recheck':
            report = recheck(args.directory)
        elif args.command == 'audit-translations':
            snapshot = read_json(args.directory / 'snapshot.json')
            states = states_for(args.directory, snapshot['records'])
            failures = audit_changes(args.directory, snapshot, states, args.workers, args.timeout)
            report = materialize(args.directory, snapshot, states, failures)
        else:
            report = collect(args.directory, args.bundle)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f'{error}\n')
    print(encoded(report))


if __name__ == '__main__':
    main()
