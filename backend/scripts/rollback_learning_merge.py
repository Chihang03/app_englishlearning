"""Preview or undo a reviewed learning group while preserving source history.

Stop the app before --apply. New answers cause a refusal instead of data loss.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.learning_units import revert_merge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--receipt',type=int)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    with closing(sqlite3.connect(f'file:{args.db.resolve()}?mode=rw',uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        if not args.apply:
            for row in conn.execute('SELECT id,config_key,created_at,reverted_at FROM learning_unit_merges ORDER BY id'):
                print(dict(row))
            return
        if args.receipt is None:
            parser.error('--apply requires --receipt')
        backup = args.db.with_name(args.db.name+'.bak-learning-rollback-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
        with closing(sqlite3.connect(backup)) as target:
            conn.backup(target)
        conn.execute('BEGIN IMMEDIATE')
        try:
            revert_merge(conn,args.receipt)
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Foreign key validation failed')
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f'Reverted receipt {args.receipt}; backup: {backup}')


if __name__=='__main__':
    main()
