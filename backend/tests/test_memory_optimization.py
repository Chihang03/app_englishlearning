"""OOM regressions: bounded exports, cache invalidation and restart recovery."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import database, meaning_catalog
from app.json_stream import array_items, object_metadata
from app.learning_reminders import run_reminders
from app.vocabulary_json import json_content_bytes, json_fingerprint
import test_senses as fixtures


class JsonStreamTests(unittest.TestCase):
    def test_large_items_unicode_escapes_and_metadata_after_array(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            entries = [{"text": '汉字\\\"[]' * 18000}, {"text": "small", "nested": [1, {"ok": True}]}]
            document = {"entries": entries, "format_version": 1}
            path.write_text(json.dumps(document, ensure_ascii=False))
            self.assertEqual(list(array_items(path, "entries")), entries)
            self.assertEqual(object_metadata(path, "entries"), {"format_version": 1})

    def test_invalid_or_truncated_document_is_rejected_before_application(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            for value in ('{"entries":[{}', '{"entries":[{},]}', '{"entries":{}}',
                          '{"entries":[]}junk', '{"version":1}', '{"entries":[],"entries":[]}'):
                with self.subTest(value=value):
                    path.write_text(value)
                    with self.assertRaises(ValueError):
                        object_metadata(path, "entries")

    def test_incremental_fingerprint_matches_previous_bytes_exactly(self):
        documents = [b'', b'{"escaped":"\\u6c49","float":1.25,"nested":[true,null]}',
                     json.dumps({"中文": [1, "words"]}, ensure_ascii=False).encode()]
        expected = hashlib.sha256(b"\0".join(json_content_bytes(item) for item in documents)).hexdigest()
        self.assertEqual(json_fingerprint(*documents), expected)


class MemoryOptimizationTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp

    def snapshot(self):
        with database.connect() as conn:
            return meaning_catalog.catalog_snapshot(conn, self.uid, self.directory / "catalog-cache")

    def test_cached_etag_avoids_catalog_query_even_after_learning_activity(self):
        first = self.client.get('/api/study/meaning-catalog')
        self.assertEqual(first.status_code, 200)
        expected = b'{"words":' + json.dumps(first.json()['words'], ensure_ascii=False, separators=(',', ':')).encode() + b'}'
        self.assertEqual(first.content, expected)
        self.assertEqual(first.headers['etag'], '"' + hashlib.sha256(expected).hexdigest() + '"')
        self.client.get('/api/next')
        with patch.object(meaning_catalog, 'catalog_words', side_effect=AssertionError('unnecessary rebuild')):
            response = self.client.get('/api/study/meaning-catalog', headers={'If-None-Match': first.headers['etag']})
            self.assertEqual(response.status_code, 304)
            self.assertEqual(response.content, b'')
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_content_changes_invalidate_snapshot_and_keep_inflight_file_immutable(self):
        before = self.snapshot()
        body = before.path.read_bytes()
        with database.connect() as conn:
            conn.execute("UPDATE sense_examples SET translation_cn='更新译文' WHERE id=(SELECT MIN(id) FROM sense_examples)")
        after = self.snapshot()
        self.assertNotEqual(before.etag, after.etag)
        self.assertNotEqual(before.path, after.path)
        self.assertEqual(before.path.read_bytes(), body)
        self.assertIn('更新译文', after.path.read_text())
        with database.connect() as conn:
            conn.execute("UPDATE learning_units SET provenance_json=json_set(provenance_json,'$.review_pending',1)")
        self.assertNotEqual(self.snapshot().path, after.path)

    def test_simultaneous_cold_requests_share_one_complete_export(self):
        with patch.object(meaning_catalog, 'catalog_words', wraps=meaning_catalog.catalog_words) as build:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.snapshot(), range(4)))
            self.assertEqual(build.call_count, 1)
        self.assertEqual(len({item.path for item in results}), 1)
        self.assertEqual(len(json.loads(results[0].path.read_bytes())['words']), 2)

    def test_unchanged_restart_skips_synchronization_but_changed_bundle_does_not(self):
        with patch.object(database, '_sync_database_content', side_effect=AssertionError('unnecessary sync')):
            database.init_database()
        changed = fixtures.catalog()
        changed['words'][0]['senses'][0]['definition_cn'] = '更新地址'
        self.catalog_path.write_text(json.dumps(changed, ensure_ascii=False))
        with patch.object(database, '_sync_database_content', wraps=database._sync_database_content) as sync:
            database.init_database()
            self.assertEqual(sync.call_count, 1)
        with patch.object(database, '_sync_database_content', side_effect=AssertionError('unnecessary sync')):
            database.init_database()

    def test_failed_sync_is_retried_and_reminders_skip_while_starting(self):
        with database.connect() as conn:
            conn.execute("DELETE FROM vocabulary_catalog_state WHERE key='startup_sync'")
        with patch.object(database, '_sync_database_content', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                database.init_database()
        with patch.object(database, '_sync_database_content', wraps=database._sync_database_content) as sync:
            database.init_database()
            self.assertEqual(sync.call_count, 1)
        with database.startup_lock(), patch('app.learning_reminders._run_reminders', side_effect=AssertionError('database contention')):
            self.assertEqual(run_reminders(), {'eligible': 0, 'sent': 0, 'failed': 0, 'expired': 0})


if __name__ == '__main__':
    unittest.main()
