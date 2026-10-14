"""Incremental whole-word Luna review using the existing structured CLI runner."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.semantic_review import (BUNDLE_PATH, MODEL_PROMPT, ModelResult, VERSION, digest,
                                 exact_duplicate_review, issues, model_input, original_source, sync_bundle, validate_response)
from report_exchange import open_database
from translation_exchange import DEFAULT_MODEL, DEFAULT_REASONING, atomic_json, encoded, read_json, run_batches


def export_batches(db: Path, output: Path, bundle_path: Path = BUNDLE_PATH,
                   words: list[str] | None = None, batch_size: int = 5, limit: int | None = None,
                   retry_manual: bool = False) -> dict:
    if output.exists() or not 1 <= batch_size <= 50 or (limit is not None and limit < 1):
        raise ValueError('Use a new output directory and valid batch size/limit')
    cache = read_json(bundle_path) if bundle_path.exists() else {'entries': []}
    cached = {digest(e['source']): e for e in cache['entries']}
    records = []
    skipped = 0
    with closing(open_database(db)) as conn:
        for row in conn.execute('SELECT id,word FROM words WHERE owner_id IS NULL ORDER BY id').fetchall():
            if words and row['word'] not in words:
                continue
            source = original_source(conn,row['id'])
            if not source['senses']:
                continue
            previous = cached.get(digest(source))
            if previous:
                from app.semantic_review import validate_word
                validate_word(source,previous['result'])
                if previous['result']['status'] == 'ready' or not retry_manual:
                    skipped += 1
                    continue
            records.append(source)
            if limit is not None and len(records) >= limit:
                break
    output.mkdir(parents=True)
    (output/'prompt.txt').write_text(MODEL_PROMPT, encoding='utf-8')
    deterministic = [(w,exact_duplicate_review(w)) for w in records]
    ai_records = [w for w,result in deterministic if result is None]
    planned = [ai_records[start:start+batch_size] for start in range(0,len(ai_records),batch_size)]
    rule_results = {w['word']:result for w,result in deterministic if result is not None}
    rule_records = [w for w,result in deterministic if result is not None]
    planned += [rule_records[start:start+batch_size] for start in range(0,len(rule_records),batch_size)]
    for number,rows in enumerate(planned,1):
        manifest = {'format_version': VERSION, 'task': 'semantic_review', 'batch': digest(rows), 'records': rows}
        stem = f'batch-{number:04d}'
        atomic_json(output/f'{stem}.manifest.json',manifest)
        schema = ModelResult.model_json_schema()
        schema['properties']['batch']['const'] = manifest['batch']
        schema['properties']['items'].update(minItems=len(rows),maxItems=len(rows))
        schema['$defs']['ModelWord']['properties']['word']['enum'] = [w['word'] for w in rows]
        atomic_json(output/f'{stem}.schema.json',schema)
        # Source metadata stays in the manifest but is unnecessary model input.
        compact = [model_input(w) for w in rows]
        (output/f'{stem}.input.jsonl').write_text(encoded({'batch':manifest['batch'],'words':compact})+'\n',encoding='utf-8')
        if all(w['word'] in rule_results for w in rows):
            atomic_json(output/f'{stem}.result.json',{'batch':manifest['batch'],'items':[rule_results[w['word']] for w in rows]})
            atomic_json(output/f'{stem}.run.json',{'model':'deterministic','reasoning_effort':None,'batch':manifest['batch']})
    report = {'exported_words':len(records),'cached_words':skipped,
              'batches':len(planned),'ai_words':len(ai_records),'rule_words':len(rule_records),
              'issues':{w['word']:issues(w) for w in records}}
    atomic_json(output/'export-report.json',report)
    return report


def collect(directory: Path, bundle_path: Path = BUNDLE_PATH) -> dict:
    bundle = read_json(bundle_path) if bundle_path.exists() else {'format_version':VERSION,'entries':[]}
    if bundle.get('format_version') != VERSION:
        raise ValueError('Unsupported semantic bundle')
    entries = {e['source']['word']:e for e in bundle['entries']}
    ready = manual = 0
    # Validate every completed proposal before changing the durable cache.
    for path in sorted(directory.glob('batch-*.result.json')):
        stem = path.name.removesuffix('.result.json')
        manifest = read_json(directory/f'{stem}.manifest.json')
        reviews = validate_response(manifest,read_json(path))
        by_word = {r['word']:r for r in reviews}
        run = read_json(directory/f'{stem}.run.json')
        for source in manifest['records']:
            result = by_word[source['word']]
            old = entries.get(source['word'])
            # A manual retry cannot replace a reliable completed revision.
            if old and old['result']['status']=='ready' and result['status']=='manual':
                continue
            entries[source['word']] = {'source':source,'result':result,'model':run['model'],
                'reasoning_effort':run['reasoning_effort'],'review_version':VERSION}
            ready += result['status']=='ready'
            manual += result['status']=='manual'
    atomic_json(bundle_path,{'format_version':VERSION,'entries':list(entries.values())})
    return {'ready_words':ready,'manual_words':manual,'cached_words':len(entries)}


def apply_database(db: Path, bundle: Path, dry_run: bool = False) -> dict:
    from datetime import datetime, timezone
    with closing(open_database(db,not dry_run)) as conn:
        backup = None
        if not dry_run:
            backup = db.with_name(db.name+'.bak-semantic-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            with closing(sqlite3.connect(backup)) as destination:
                conn.backup(destination)
        # Dry-run uses an in-memory copy so it exercises the real merge logic.
        if dry_run:
            preview = sqlite3.connect(':memory:');preview.row_factory=sqlite3.Row
            conn.backup(preview)
            conn.close();conn=preview
        try:
            conn.execute('BEGIN IMMEDIATE')
            report = sync_bundle(conn,bundle)
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Foreign key check failed')
            conn.rollback() if dry_run else conn.commit()
            return {**report,'dry_run':dry_run,'backup':str(backup) if backup else None}
        except BaseException:
            conn.rollback()
            raise
        finally:
            if dry_run:
                conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    export=commands.add_parser('export')
    export.add_argument('--db',type=Path,required=True)
    export.add_argument('--output',type=Path,required=True)
    export.add_argument('--bundle',type=Path,default=BUNDLE_PATH)
    export.add_argument('--words',nargs='+')
    export.add_argument('--batch-size',type=int,default=5)
    export.add_argument('--limit',type=int)
    export.add_argument('--retry-manual',action='store_true')
    run=commands.add_parser('run')
    run.add_argument('--directory',type=Path,required=True)
    run.add_argument('--limit-batches',type=int)
    run.add_argument('--timeout',type=int,default=240)
    run.add_argument('--dry-run',action='store_true')
    collect_parser=commands.add_parser('collect')
    collect_parser.add_argument('--directory',type=Path,required=True)
    collect_parser.add_argument('--bundle',type=Path,default=BUNDLE_PATH)
    apply=commands.add_parser('apply-db')
    apply.add_argument('--db',type=Path,required=True)
    apply.add_argument('--bundle',type=Path,default=BUNDLE_PATH)
    apply.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='export':
            report=export_batches(args.db,args.output,args.bundle,args.words,args.batch_size,args.limit,args.retry_manual)
        elif args.command=='run':
            if args.timeout < 1:
                raise ValueError('Timeout must be positive')
            report=run_batches(args.directory,model=DEFAULT_MODEL,reasoning=DEFAULT_REASONING,
                limit_batches=args.limit_batches,dry_run=args.dry_run,timeout=args.timeout)
        elif args.command=='collect':
            report=collect(args.directory,args.bundle)
        else:
            report=apply_database(args.db,args.bundle,args.dry_run)
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error,subprocess.SubprocessError) as error:
        parser.exit(1,f'{error}\n')
    print(encoded(report))


if __name__=='__main__':
    main()
