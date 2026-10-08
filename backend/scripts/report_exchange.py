"""Export, review and import learning content reports without a model API key."""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from app.report_review import PROMPT, ReviewResult, digest, export_manifest, import_review, review_plan
from translation_exchange import DEFAULT_CATALOG, DEFAULT_MODEL, DEFAULT_REASONING, atomic_json, encoded, read_json, run_batches


def open_database(db: Path, writable: bool = False):
    if not db.is_file():
        raise ValueError('Database must already exist')
    conn = sqlite3.connect(db.resolve().as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def export_batches(db: Path, output: Path, batch_size: int = 20, limit: int = 100, after_id: int = 0) -> dict:
    if output.exists() or not 1 <= batch_size <= 1000 or not 1 <= limit <= 1000 or after_id < 0:
        raise ValueError('Use a new directory and valid batch size/limit (1..1000)')
    with closing(open_database(db)) as conn:
        manifest = export_manifest(conn,limit,after_id)
    output.mkdir(parents=True)
    (output/'prompt.txt').write_text(PROMPT,encoding='utf-8')
    records = manifest['records']
    batches = 0
    for start in range(0,len(records),batch_size):
        batches += 1
        rows = records[start:start+batch_size]
        batch = {**manifest,'records':rows,'batch':digest(rows)}
        stem = f'batch-{batches:04d}'
        atomic_json(output/f'{stem}.manifest.json',batch)
        schema = ReviewResult.model_json_schema()
        schema['properties']['batch']['const'] = batch['batch']
        atomic_json(output/f'{stem}.schema.json',schema)
        with (output/f'{stem}.input.jsonl').open('w',encoding='utf-8') as handle:
            handle.write(encoded({'batch':batch['batch']})+'\n')
            for row in rows:
                snapshot = row['reported_content'] if row['reported_content'] != row['expected_content'] else None
                handle.write(encoded([row['id'],row['category'],row['details'],row['expected_content'],snapshot,row['editable']])+'\n')
    report = {'exported_reports':len(records),'batches':batches,'directory':str(output),
              'last_report_id':records[-1]['id'] if records else after_id}
    atomic_json(output/'export-report.json',report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    exporter = commands.add_parser('export')
    exporter.add_argument('--db',type=Path,required=True)
    exporter.add_argument('--output',type=Path,required=True)
    exporter.add_argument('--batch-size',type=int,default=20)
    exporter.add_argument('--limit',type=int,default=100)
    exporter.add_argument('--after-id',type=int,default=0)
    runner = commands.add_parser('run',help='Create review proposals with Codex; never modify the database')
    runner.add_argument('--directory',type=Path,required=True)
    runner.add_argument('--model',default=DEFAULT_MODEL)
    runner.add_argument('--reasoning-effort',default=DEFAULT_REASONING,choices=('low','medium','high','xhigh','max'))
    runner.add_argument('--limit-batches',type=int)
    runner.add_argument('--dry-run',action='store_true')
    for name in ('validate','import'):
        command = commands.add_parser(name)
        command.add_argument('--db',type=Path,required=True)
        command.add_argument('--manifest',type=Path,required=True)
        command.add_argument('--result',type=Path,required=True)
        if name == 'import':
            command.add_argument('--admin-id',type=int,required=True)
            command.add_argument('--dry-run',action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'export':
            report = export_batches(args.db,args.output,args.batch_size,args.limit,args.after_id)
        elif args.command == 'run':
            report = run_batches(args.directory,DEFAULT_CATALOG,args.model,args.reasoning_effort,args.limit_batches,args.dry_run)
        else:
            manifest,result = read_json(args.manifest),read_json(args.result)
            with closing(open_database(args.db,args.command=='import' and not args.dry_run)) as conn:
                if args.command == 'validate':
                    report = {'items':review_plan(conn,manifest,result)}
                else:
                    report = import_review(conn,args.db,manifest,result,args.admin_id,args.dry_run)
    except HTTPException as error:
        parser.exit(1,f'{error.detail}\n')
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error,subprocess.CalledProcessError) as error:
        parser.exit(1,f'{error}\n')
    print(encoded(report))


if __name__ == '__main__':
    main()
