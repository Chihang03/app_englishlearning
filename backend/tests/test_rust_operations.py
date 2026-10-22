"""Standalone production CLI: clean bootstrap, backup, permission and failure behavior."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
BINARY=Path(os.environ.get('ENGLISH_TEST_NATIVE_CATALOG',ROOT/'rust/target/debug/englishctl'))
@unittest.skipUnless(BINARY.is_file(),'build Rust CLI first')
class NativeOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.directory=Path(self.temp.name);self.db=self.directory/'vocabulary.db'
    def run_cli(self,*args,input=None,ok=True):
        result=subprocess.run([str(BINARY),*map(str,args)],input=input,text=True,capture_output=True,timeout=30)
        if ok:self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
        self.assertNotEqual(result.returncode,0);return result
    def test_fresh_runtime_schema_admin_and_consistent_backup_without_python(self):
        result=self.run_cli('init','--database',self.db);self.assertEqual(result['schema_version'],22);self.assertEqual(result['words'],0)
        self.assertTrue(self.run_cli('create-admin','--database',self.db,'--password-stdin',input='secure-password-123\n')['created'])
        self.assertFalse(self.run_cli('create-admin','--database',self.db,'--password-stdin',input='new-password-123\n')['created'])
        with sqlite3.connect(self.db) as c:
            c.execute('PRAGMA journal_mode=WAL');c.execute("INSERT INTO vocabulary_catalog_state(key,value) VALUES('cli-test','WAL content')");c.commit()
            output=self.directory/'backup.db';self.run_cli('backup','--database',self.db,'--output',output)
            with sqlite3.connect(output) as copy:self.assertEqual(copy.execute("SELECT value FROM vocabulary_catalog_state WHERE key='cli-test'").fetchone()[0],'WAL content')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0],1)
        manifest=self.directory/'reports.json';result=self.run_cli('report-review','--database',self.db,'--admin-id','1','export','--output',manifest)
        self.assertEqual(result['records'],0);self.assertTrue(manifest.is_file())
        self.run_cli('report-review','--database',self.db,'--admin-id','999','export','--output',self.directory/'denied.json',ok=False)
        self.run_cli('init','--database',self.db,ok=False)
        self.run_cli('backup','--database',self.db,'--output',output,ok=False)
    def test_unknown_schema_and_missing_database_never_created_or_migrated(self):
        self.run_cli('inspect','--database',self.db,ok=False);self.assertFalse(self.db.exists())
        with sqlite3.connect(self.db) as c:c.execute('PRAGMA user_version=999')
        before=self.db.read_bytes();self.run_cli('inspect','--database',self.db,ok=False);self.assertEqual(self.db.read_bytes(),before)
