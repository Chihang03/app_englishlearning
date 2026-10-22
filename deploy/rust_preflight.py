"""Compare native/Python catalogue bytes on a server-local isolated SQLite copy.

No database, catalogue text or account credentials are sent off the host.
Use --snapshot to create the isolated copy via SQLite's backup API first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            result.update(chunk)
    return result.hexdigest()


def connect_readonly(path):
    return sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)


def python_export(args):
    sys.path.insert(0,str(args.backend))
    from app.meaning_catalog import catalog_words
    with connect_readonly(args.database) as conn, args.output.open("xb") as output:
        conn.row_factory=sqlite3.Row
        conn.execute("BEGIN")
        output.write(b'{"words":[')
        for index,word in enumerate(catalog_words(conn,args.user_id)):
            if index: output.write(b',')
            output.write(json.dumps(word,ensure_ascii=False,separators=(',',':')).encode())
        output.write(b']}')


def compare(args):
    args.directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    database=args.database
    if args.snapshot:
        database=args.directory/'snapshot.db'
        with connect_readonly(args.database) as source, sqlite3.connect(database) as target:
            source.backup(target)
        args.database=database
    before=digest(database)
    with connect_readonly(database) as conn:
        assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        identity,revision=conn.execute('SELECT database_id,revision FROM meaning_catalog_revision').fetchone()
        uid=conn.execute("SELECT id FROM users WHERE role='learner' ORDER BY id LIMIT 1").fetchone()[0]
    samples=[]
    for index in range(args.repeat):
        hashes=[]
        for kind in ['python','rust']:
            output=args.directory/f'{kind}-{index}.json'
            metrics=args.directory/f'{kind}-{index}.time'
            if kind=='python':
                command=[sys.executable,str(Path(__file__).resolve()),'python-export','--backend',str(args.backend),
                    '--database',str(database),'--user-id',str(uid),'--output',str(output)]
            else:
                command=[str(args.native),'catalog','--database',str(database),'--user-id',str(uid),
                    '--database-id',identity,'--revision',str(revision),'--output',str(output)]
            subprocess.run(['/usr/bin/time','-f','%e %M %U %S','-o',str(metrics),*command],
                check=True,capture_output=True,text=True,timeout=40)
            elapsed,rss,user,system=metrics.read_text().split()
            checksum=digest(output)
            hashes.append(checksum)
            samples.append({'kind':kind,'elapsed_seconds':float(elapsed),'peak_rss_kib':int(rss),
                'cpu_user_seconds':float(user),'cpu_system_seconds':float(system),'bytes':output.stat().st_size,'sha256':checksum})
            output.unlink()
        assert hashes[0]==hashes[1], 'Native/Python bytes differ'
    assert digest(database)==before,'Read-only exporters changed database file'
    report={'parity':True,'database_unchanged':True,'schema_version':22,'samples':samples,
        'native_sha256':digest(args.native),'reference_python':sys.version.split()[0]}
    (args.directory/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ['compare','python-export']:
        item=sub.add_parser(name)
        item.add_argument('--backend',type=Path,required=True)
        item.add_argument('--database',type=Path,required=True)
        if name=='compare':
            item.add_argument('--native',type=Path,required=True)
            item.add_argument('--directory',type=Path,required=True)
            item.add_argument('--snapshot',action='store_true')
            item.add_argument('--repeat',type=int,choices=range(1,11),default=5)
        else:
            item.add_argument('--user-id',type=int,required=True)
            item.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='compare': compare(args)
    else: python_export(args)


if __name__=='__main__': main()
