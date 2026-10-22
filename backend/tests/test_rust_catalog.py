"""Real native subprocess parity against Python, always on temporary databases."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

from app import database, meaning_catalog
import test_senses as fixtures

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get("ENGLISH_TEST_NATIVE_CATALOG", ROOT / "rust/target/debug/englishctl"))


@unittest.skipUnless(BINARY.is_file(), "build Rust workspace before running native integration")
class NativeCatalogTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp

    def expected(self, uid=None):
        with database.connect() as conn:
            return b'{"words":'+json.dumps(list(meaning_catalog.catalog_words(conn, uid or self.uid)),
                ensure_ascii=False,separators=(",", ":")).encode()+b'}'

    def export(self, uid=None, revision_offset=0):
        with database.connect() as conn:
            identity = conn.execute("SELECT database_id,revision FROM meaning_catalog_revision").fetchone()
        output = self.directory / f"export-{uid or self.uid}-{revision_offset}.json"
        result = subprocess.run([str(BINARY), "catalog", "--database", str(database.DB_PATH),
            "--user-id",str(uid or self.uid),"--database-id",identity[0],"--revision",str(identity[1]+revision_offset),
            "--output",str(output)],capture_output=True,text=True,timeout=10)
        return result,output

    def test_exact_bytes_with_nulls_unicode_controls_and_semantic_notes(self):
        with database.connect() as conn:
            conn.execute("UPDATE word_senses SET definition_cn=? WHERE id=(SELECT MIN(id) FROM word_senses)",
                ('汉字😀\\\"\n\t\u2028 / café',))
            conn.execute("UPDATE learning_units SET unit_key='semantic:'||id,display_definition_cn=?,provenance_json=?",
                ('核心释义',json.dumps({"notes":'说明 😀\n"quote"',"review_pending":False},ensure_ascii=False)))
        before = database.DB_PATH.read_bytes()
        result,path = self.export()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(path.read_bytes(),self.expected())
        self.assertEqual(database.DB_PATH.read_bytes(),before)
        metadata=json.loads(result.stdout)
        self.assertEqual(metadata["sha256"],hashlib.sha256(path.read_bytes()).hexdigest())

    def test_private_words_are_account_scoped(self):
        fixtures.legacy_private_word(self.uid,{"word":"secret","part_of_speech":"名词","definition_cn":"秘密",
            "example_sentence":"This is a secret."})
        response=self.client.post('/api/auth/register',json={"username":"other","password":"test-password-123"})
        other=response.json()["user"]["id"]
        from app.learning_units import sync_learning_units
        with database.connect() as conn: sync_learning_units(conn)
        for uid in [self.uid,other]:
            result,path=self.export(uid)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(path.read_bytes(),self.expected(uid))
            self.assertEqual(b'"word":"secret"' in path.read_bytes(),uid==self.uid)

    def test_revision_mismatch_leaves_no_output(self):
        result,path=self.export(revision_offset=1)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(path.exists())

    def test_unknown_account_leaves_no_output(self):
        result,path=self.export(uid=999999)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(path.exists())

    def test_cli_refuses_overwriting_existing_output(self):
        first,path=self.export();self.assertEqual(first.returncode,0,first.stderr)
        before=path.read_bytes();second,_=self.export();self.assertNotEqual(second.returncode,0)
        self.assertEqual(path.read_bytes(),before)

    def test_public_seed_contains_content_and_no_accounts_or_private_words(self):
        fixtures.legacy_private_word(self.uid,{"word":"secret","part_of_speech":"名词","definition_cn":"秘密","example_sentence":"This is a secret."})
        from app.learning_units import sync_learning_units
        with database.connect() as conn:sync_learning_units(conn)
        output=self.directory/'public-seed.db'
        result=subprocess.run([str(BINARY),'content','export-seed','--database',str(database.DB_PATH),'--output',str(output)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        import sqlite3
        with sqlite3.connect(output) as conn:
            for table in ['users','sessions','passkeys','push_subscriptions','push_deliveries','review_history','sense_srs_state','study_attempts','content_reports','admin_content_edits']:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM words WHERE owner_id IS NOT NULL OR word='secret'").fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM words').fetchone()[0],2)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
