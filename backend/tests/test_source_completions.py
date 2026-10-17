"""Append-only dictionary recovery, course counts and catalog binding."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import test_senses as fixtures
from app import database
from app.semantic_review import source_word
from app.source_completions import seed_missing_sources
from app.vocabulary_json import json_content_bytes


class SourceCompletionTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp

    def entry(self, word='jetlag'):
        sense = fixtures.sense('recovered:one', '时差反应', 'Jetlag made her tired.', target='jetlag')
        sense['learning_enabled'] = 1
        sense['examples'][0].update(translation_cn='时差反应让她很疲惫。',
                                    source='Luna AI-generated example')
        return {'source': {'word': word, 'senses': [sense]},
                'dictionary_evidence': {'dictionary': 'verified local dictionary', 'headword': 'jet lag'},
                'memberships': [{'list_id': 'cet4', 'position': 2}]}

    def seed(self, entries, catalog_sha256=None):
        path = self.directory/'source-completions.json'
        catalog = self.catalog_path.read_bytes()
        path.write_text(json.dumps({'format_version': 1,
            'catalog_sha256': catalog_sha256 or hashlib.sha256(json_content_bytes(catalog)).hexdigest(),
            'entries': entries}, ensure_ascii=False))
        with database.connect() as conn:
            return seed_missing_sources(conn, path, catalog)

    def test_adds_missing_word_once_and_preserves_source_provenance(self):
        entry = self.entry()
        self.assertEqual(self.seed([entry])['added'], 1)
        self.assertEqual(self.seed([entry])['preserved'], 1)
        with database.connect() as conn:
            word_id = conn.execute("SELECT id FROM words WHERE word='jetlag' AND owner_id IS NULL").fetchone()[0]
            self.assertEqual(source_word(conn, word_id), entry['source'])
            self.assertEqual(conn.execute("SELECT word_count FROM vocabulary_lists WHERE list_id='cet4'").fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM word_list_memberships WHERE word_id=?', (word_id,)).fetchone()[0], 1)

    def test_existing_active_content_and_progress_remain_unchanged(self):
        with database.connect() as conn:
            word_id = conn.execute("SELECT id FROM words WHERE word='address'").fetchone()[0]
            before = source_word(conn, word_id)
            memberships = [tuple(r) for r in conn.execute('SELECT * FROM word_list_memberships')]
        entry = self.entry('address')
        self.assertEqual(self.seed([entry])['preserved'], 1)
        with database.connect() as conn:
            self.assertEqual(source_word(conn, word_id), before)
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM word_list_memberships')], memberships)

    def test_stale_catalog_never_adds_or_overwrites(self):
        self.assertEqual(self.seed([self.entry()], 'different-catalog')['stale_catalog'], 1)
        with database.connect() as conn:
            self.assertIsNone(conn.execute("SELECT id FROM words WHERE word='jetlag'").fetchone())

    def test_exact_catalog_hash_avoids_expanding_the_large_catalog(self):
        self.seed([self.entry()])
        path=self.directory/'source-completions.json';document=json.loads(path.read_text())
        document['catalog_raw_sha256']=hashlib.sha256(self.catalog_path.read_bytes()).hexdigest()
        path.write_text(json.dumps(document))
        with database.connect() as conn, patch('app.source_completions.catalog_hashes',
                side_effect=AssertionError('The exact catalog must not be expanded')):
            self.assertEqual(seed_missing_sources(conn,path,self.catalog_path.read_bytes())['preserved'],1)

    def test_broken_bundle_preserves_application_startup(self):
        path = self.directory/'broken.json';path.write_text('{')
        with database.connect() as conn:
            before = conn.execute('SELECT COUNT(*) FROM words').fetchone()[0]
            self.assertEqual(seed_missing_sources(conn, path, self.catalog_path.read_bytes())['rejected'], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM words').fetchone()[0], before)

    def test_startup_can_seed_completion_and_refresh_morphology(self):
        self.seed([self.entry()])
        with database.connect() as conn:
            conn.execute("DELETE FROM vocabulary_catalog_state WHERE key='morphology_fingerprint'")
        with patch.object(database, 'SOURCE_COMPLETIONS_PATH', self.directory/'source-completions.json'):
            database.init_database()
            database.init_database()
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM words WHERE word='jetlag' AND owner_id IS NULL").fetchone()[0], 1)
            self.assertIsNotNone(conn.execute("SELECT value FROM vocabulary_catalog_state WHERE key='morphology_fingerprint'").fetchone())

    def test_invalid_record_rolls_back_locally_and_other_words_continue(self):
        bad = self.entry('broken');bad['memberships'][0]['list_id'] = 'unknown'
        missing_cn = self.entry('untranslated');missing_cn['source']['senses'][0]['examples'][0]['translation_cn'] = None
        report = self.seed([bad, missing_cn, None, self.entry()])
        self.assertEqual((report['added'], report['rejected']), (1, 3))
        with database.connect() as conn:
            self.assertIsNone(conn.execute("SELECT id FROM words WHERE word IN ('broken','untranslated')").fetchone())
            self.assertFalse(conn.execute('PRAGMA foreign_key_check').fetchall())

    def test_public_recovery_does_not_touch_private_word_with_same_spelling(self):
        private = fixtures.legacy_private_word(self.uid, {'word': 'jetlag', 'part_of_speech': '名词',
            'definition_cn': '个人词义', 'example_sentence': 'Jetlag is my private word.',
            'example_translation_cn': '这是我的私人词条。'})
        with database.connect() as conn:
            before = copy.deepcopy(source_word(conn, private['id']))
        self.assertEqual(self.seed([self.entry()])['added'], 1)
        with database.connect() as conn:
            self.assertEqual(source_word(conn, private['id']), before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM words WHERE word='jetlag'").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
