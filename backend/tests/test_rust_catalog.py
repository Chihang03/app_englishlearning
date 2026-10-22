"""Real native subprocess parity against Python, always on temporary databases."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from app import database, meaning_catalog
import test_senses as fixtures

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get("ENGLISH_TEST_NATIVE_CATALOG", ROOT / "rust/target/debug/englishctl"))


class NativeFallbackTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp

    def test_unavailable_native_binary_preserves_python_behavior(self):
        with patch.dict(os.environ, {"ENGLISH_NATIVE_CATALOG": "/missing/englishctl"}):
            with self.assertLogs("app.meaning_catalog", level="WARNING"):
                response = self.client.get("/api/study/meaning-catalog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["words"]), 2)
        self.assertEqual(response.headers["etag"], '"'+hashlib.sha256(response.content).hexdigest()+'"')


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

    def test_real_native_cold_export_then_cached_304_without_subprocess(self):
        expected=self.expected()
        with patch.dict(os.environ,{"ENGLISH_NATIVE_CATALOG":str(BINARY)}), patch.object(
                meaning_catalog,'catalog_words',side_effect=AssertionError('Python cold export used')):
            first=self.client.get('/api/study/meaning-catalog')
            self.assertEqual(first.status_code,200,first.text)
            self.assertEqual(first.content,expected)
            with patch('app.meaning_catalog.subprocess.run',side_effect=AssertionError('cached rebuild')):
                cached=self.client.get('/api/study/meaning-catalog',headers={'If-None-Match':first.headers['etag']})
                self.assertEqual(cached.status_code,304)
        self.assertFalse(list(self.directory.rglob('.native-*')))
