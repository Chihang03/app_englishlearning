"""Atomic native content packages on the existing schema and synthetic content."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from contextlib import closing
from app import database
import test_senses as fixtures

BINARY=Path(os.environ.get('ENGLISH_TEST_NATIVE_CATALOG',Path(__file__).resolve().parents[2]/'rust/target/debug/englishctl'))

@unittest.skipUnless(BINARY.is_file(),'build Rust workspace first')
class ContentPackageTests(unittest.TestCase):
    setUp=fixtures.SenseLearningTests.setUp

    def logical_state(self):
        with closing(sqlite3.connect(database.DB_PATH)) as conn:return '\n'.join(conn.iterdump())

    def command(self,*args):
        return subprocess.run([str(BINARY),'content',*map(str,args)],capture_output=True,text=True,timeout=15)

    def bundle(self,records):
        with database.connect() as conn:
            identity,revision=conn.execute('SELECT database_id,revision FROM meaning_catalog_revision').fetchone()
        source=self.directory/'patch.json'; output=self.directory/'patch.sqlite'
        source.write_text(json.dumps({'format_version':1,'database_id':identity,'base_revision':revision,'records':records},ensure_ascii=False))
        result=self.command('build','--source',source,'--output',output)
        self.assertEqual(result.returncode,0,result.stderr)
        return source,output,json.loads(result.stdout)

    def edit(self,invalid=False):
        with database.connect() as conn:
            row=conn.execute('SELECT id,translation_cn FROM sense_examples ORDER BY id LIMIT 1').fetchone()
        return {'table':'sense_examples','key':{'id':row[0]},'before':{'translation_cn':'conflict' if invalid else row[1]},'after':{'translation_cn':'更新译文 😀'}}

    def test_apply_preserves_history_and_repeated_apply_is_noop(self):
        self.client.get('/api/next')
        with database.connect() as conn: before=[tuple(row) for row in conn.execute('SELECT * FROM study_attempts')]
        _,package,_=self.bundle([self.edit()])
        first=self.command('apply','--package',package,'--database',database.DB_PATH)
        self.assertEqual(first.returncode,0,first.stderr)
        self.assertEqual(json.loads(first.stdout)['applied'],1)
        with database.connect() as conn:
            revision=conn.execute('SELECT revision FROM meaning_catalog_revision').fetchone()[0]
            self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM study_attempts')],before)
        second=self.command('apply','--package',package,'--database',database.DB_PATH)
        self.assertEqual(second.returncode,0,second.stderr)
        self.assertTrue(json.loads(second.stdout)['already_applied'])
        with database.connect() as conn: self.assertEqual(conn.execute('SELECT revision FROM meaning_catalog_revision').fetchone()[0],revision)

    def test_failure_after_first_update_rolls_back_everything(self):
        good=self.edit(); bad=self.edit(invalid=True)
        _,package,_=self.bundle([good,bad])
        before=self.logical_state()
        result=self.command('apply','--package',package,'--database',database.DB_PATH)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.logical_state(),before)

    def test_dry_run_and_fingerprint_tamper_never_write_target(self):
        _,package,_=self.bundle([self.edit()])
        before=self.logical_state()
        result=self.command('apply','--package',package,'--database',database.DB_PATH,'--dry-run')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.logical_state(),before)
        with sqlite3.connect(package) as conn: conn.execute("UPDATE records SET payload=replace(CAST(payload AS TEXT),'更新','篡改')")
        result=self.command('apply','--package',package,'--database',database.DB_PATH)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.logical_state(),before)

    def test_truncated_and_oversized_records_leave_no_package(self):
        source,package,_=self.bundle([self.edit()]); package.unlink()
        source.write_text(source.read_text()[:-2])
        self.assertNotEqual(self.command('build','--source',source,'--output',package).returncode,0)
        self.assertFalse(package.exists())
        source.write_text('{"records":[{"table":"'+('x'*2100000)+'"}]}')
        self.assertNotEqual(self.command('build','--source',source,'--output',package).returncode,0)
        self.assertFalse(package.exists())

    def test_content_package_cannot_modify_learning_tables(self):
        source=self.directory/'bad.json'; package=self.directory/'bad.sqlite'
        source.write_text(json.dumps({'format_version':1,'database_id':'a'*32,'base_revision':0,'records':[
            {'table':'review_history','key':{'id':1},'before':None,'after':{'word_id':1}}]}))
        self.assertNotEqual(self.command('build','--source',source,'--output',package).returncode,0)
        self.assertFalse(package.exists())

    def test_streaming_fingerprint_matches_python_unicode_numbers_and_whitespace(self):
        import hashlib,random
        rng=random.Random(20261022)
        values=[1.0,-0.0,1e-6,1e16,0.0001,2**100]
        values.extend(rng.uniform(-1,1)*10**rng.randint(-200,200) for _ in range(500))
        content={'中文 😀':values,'nested':[True,None,{'escapes':'\\"\n\u2028'}]}
        source=self.directory/'numbers.json';source.write_text(json.dumps(content,ensure_ascii=False,indent=2))
        expected=hashlib.sha256(json.dumps(content,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        result=subprocess.run([str(BINARY),'fingerprint',str(source)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['sha256'],expected)

    def test_diff_build_validate_apply_roundtrip_excludes_private_and_account_data(self):
        staged=self.directory/'after.db'
        with closing(sqlite3.connect(database.DB_PATH)) as source,closing(sqlite3.connect(staged)) as target:source.backup(target)
        with closing(sqlite3.connect(staged)) as target:
            with target:
                target.execute("UPDATE sense_examples SET translation_cn='新内容' WHERE id=(SELECT MIN(id) FROM sense_examples)")
                target.execute("DELETE FROM word_list_memberships WHERE rowid=(SELECT MIN(rowid) FROM word_list_memberships)")
                target.execute("INSERT INTO words(owner_id,word,part_of_speech,definition_cn,example_sentence,pronunciation) VALUES(?, 'private-secret','noun','private','private-secret','private-secret')",(self.uid,))
        source=self.directory/'diff.json';package=self.directory/'diff.sqlite'
        result=self.command('diff','--before',database.DB_PATH,'--after',staged,'--output',source)
        self.assertEqual(result.returncode,0,result.stderr)
        text=source.read_text();self.assertNotIn('private-secret',text);self.assertNotIn('password_hash',text)
        self.assertEqual(self.command('build','--source',source,'--output',package).returncode,0)
        self.assertEqual(self.command('validate','--package',package).returncode,0)
        result=self.command('apply','--package',package,'--database',database.DB_PATH)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['applied'],2)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT translation_cn FROM sense_examples ORDER BY id LIMIT 1').fetchone()[0],'新内容')
