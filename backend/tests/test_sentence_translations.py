from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sentence_translation_exchange import export_sentences, import_sentences, materialize_sentences, validate_sentences
from translation_exchange import run_batches
from app.chinese_sentences import apply_sentence_supplements, content_hashes, sync_database


class SentenceTranslationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = self.root / 'catalog.json'
        self.glosses = self.root / 'glosses.json'
        self.supplements = self.root / 'sentences.json'
        self.output = self.root / 'batches'
        self.document = {'format_version': 2, 'words': [{'word': 'bank', 'definition_cn': '河岸',
            'example_sentence': 'We sat on the bank.', 'example_translation_cn': None, 'senses': [
                {'key': 'river', 'part_of_speech': '名词', 'definition_en': 'land beside a river', 'definition_cn': '河岸',
                 'examples': [{'sentence': 'We sat on the bank.', 'target_form': 'bank', 'translation_cn': None},
                              {'sentence': 'The bank was steep.', 'target_form': 'bank', 'translation_cn': '河岸很陡。'}]},
                {'key': 'money', 'part_of_speech': '名词', 'definition_en': 'a financial institution', 'definition_cn': '银行',
                 'examples': [{'sentence': 'The bank is open.', 'target_form': 'bank', 'translation_cn': ''}]},
            ]}]}
        self.write(self.catalog, self.document)
        self.write(self.glosses, {'format_version': 1, 'entries': []})
        self.write(self.supplements, {'format_version': 1, 'entries': []})
        export_sentences(self.catalog, self.supplements, self.output, glosses=self.glosses)
        self.manifest = self.output / 'batch-0001.manifest.json'
        self.result = self.output / 'batch-0001.result.json'
        self.batch = json.loads(self.manifest.read_text())['batch']
        self.response = {'batch': self.batch, 'items': [[1, '我们坐在河岸上。'], [2, '银行开门营业。']]}
        self.write(self.result, self.response)

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def import_result(self, dry_run=False):
        return import_sentences(self.catalog, self.supplements, self.manifest, self.result, dry_run, self.glosses)

    def test_export_uses_original_sentence_target_and_specific_meaning(self):
        rows = (self.output / 'batch-0001.input.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(rows[1]), [1, 'We sat on the bank.', 'bank', '名词', 'bank', '河岸'])
        self.assertEqual(json.loads(rows[2]), [2, 'The bank is open.', 'bank', '名词', 'bank', '银行'])
        self.assertEqual(len(rows), 3)

    def test_import_preserves_catalog_and_existing_translations_and_caches(self):
        before = self.catalog.read_bytes(), self.supplements.read_bytes()
        self.assertEqual(self.import_result(True)['added_examples'], 2)
        self.assertEqual(self.supplements.read_bytes(), before[1])
        report = self.import_result()
        self.assertEqual(Path(report['backup']).read_bytes(), before[1])
        self.assertEqual(self.import_result()['added_examples'], 0)
        self.assertEqual(self.catalog.read_bytes(), before[0])
        generated = self.root / 'materialized.json'
        self.assertEqual(materialize_sentences(self.catalog, self.supplements, generated)['applied_examples'], 2)
        expected = copy.deepcopy(self.document)
        expected['words'][0]['example_translation_cn'] = '我们坐在河岸上。'
        expected['words'][0]['senses'][0]['examples'][0]['translation_cn'] = '我们坐在河岸上。'
        expected['words'][0]['senses'][1]['examples'][0]['translation_cn'] = '银行开门营业。'
        self.assertEqual(json.loads(generated.read_text()), expected)
        report = export_sentences(self.catalog, self.supplements, self.root / 'cached', glosses=self.glosses)
        self.assertEqual(report['exported_examples'], 0)

    def test_wrong_batch_duplicate_missing_id_and_non_chinese_rejected(self):
        before = self.supplements.read_bytes()
        for response in [
            {'batch': 'wrong', 'items': self.response['items']},
            {'batch': self.batch, 'items': [[1, '译文'], [1, '译文']]},
            {'batch': self.batch, 'items': [[1, '译文']]},
            {'batch': self.batch, 'items': [[1, 'translation'], [2, '译文']]},
        ]:
            with self.subTest(response=response):
                self.write(self.result, response)
                with self.assertRaises(ValueError):
                    self.import_result()
                self.assertEqual(self.supplements.read_bytes(), before)

    def test_changed_source_and_existing_translation_rejected(self):
        for field, value, in_example in [('sentence', 'We left the bank.', True), ('target_form', 'sat', True),
                                        ('definition_en', 'different meaning', False), ('definition_cn', '银行', False),
                                        ('translation_cn', '我们坐在岸边。', True)]:
            with self.subTest(field=field):
                document = copy.deepcopy(self.document)
                target = document['words'][0]['senses'][0]
                if in_example:
                    target = target['examples'][0]
                target[field] = value
                self.write(self.catalog, document)
                with self.assertRaises(ValueError):
                    self.import_result()

    def test_null_retry_contains_only_pending_sentence(self):
        self.response['items'][1][1] = None
        self.write(self.result, self.response)
        report = self.import_result()
        self.assertEqual((report['added_examples'], report['pending_examples']), (1, 1))
        report = export_sentences(self.catalog, self.supplements, self.root / 'retry', glosses=self.glosses,
                                  retry_manifest=self.manifest, retry_result=self.result)
        self.assertEqual(report['exported_examples'], 1)

    def test_conflicting_cached_translation_is_rejected(self):
        self.import_result()
        self.response['items'][0][1] = '我们在岸边坐着。'
        self.write(self.result, self.response)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.import_result()

    def database(self):
        db = self.root / 'live.sqlite'
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript('''CREATE TABLE words(id INTEGER PRIMARY KEY,owner_id INTEGER,word TEXT,
                    example_sentence TEXT,example_translation_cn TEXT);
                CREATE TABLE word_senses(id INTEGER PRIMARY KEY,word_id INTEGER REFERENCES words(id),
                    sense_key TEXT,definition_en TEXT,part_of_speech TEXT,active INTEGER);
                CREATE TABLE sense_examples(id INTEGER PRIMARY KEY,sense_id INTEGER REFERENCES word_senses(id),
                    sentence TEXT,target_form TEXT,translation_cn TEXT,active INTEGER);
                CREATE TABLE review_history(id INTEGER PRIMARY KEY,example_id INTEGER REFERENCES sense_examples(id),answer TEXT);
                CREATE TABLE user_sense_examples(user_id INTEGER,sense_id INTEGER,example_id INTEGER);
                CREATE TABLE admin_content_overrides(entity TEXT,entity_id INTEGER,values_json TEXT);
                INSERT INTO words VALUES(1,NULL,'bank','We sat on the bank.',NULL);
                INSERT INTO words VALUES(2,9,'bank','We sat on the bank.',NULL);
                INSERT INTO word_senses VALUES(11,1,'river','land beside a river','名词',1);
                INSERT INTO word_senses VALUES(12,1,'money','a financial institution','名词',1);
                INSERT INTO word_senses VALUES(13,2,'river','land beside a river','名词',1);
                INSERT INTO sense_examples VALUES(21,11,'We sat on the bank.','bank',NULL,1);
                INSERT INTO sense_examples VALUES(22,12,'The bank is open.','bank','已有人工翻译',1);
                INSERT INTO sense_examples VALUES(23,13,'We sat on the bank.','bank',NULL,1);
                INSERT INTO review_history VALUES(1,21,'bank');
                INSERT INTO user_sense_examples VALUES(9,11,21);''')
        return db

    def test_database_backup_history_fixed_ids_private_and_existing_are_preserved(self):
        self.import_result()
        db = self.database()
        with closing(sqlite3.connect(db)) as conn, conn:
            before = content_hashes(conn)
        preview = sync_database(db, self.supplements, dry_run=True)
        self.assertEqual(preview['updated_examples'], 1)
        report = sync_database(db, self.supplements)
        self.assertTrue(report['learning_history_unchanged'])
        with closing(sqlite3.connect(Path(report['backup']))) as conn:
            self.assertIsNone(conn.execute('SELECT translation_cn FROM sense_examples WHERE id=21').fetchone()[0])
        with closing(sqlite3.connect(db)) as conn, conn:
            self.assertEqual(content_hashes(conn), before)
            self.assertEqual(conn.execute('SELECT translation_cn FROM sense_examples ORDER BY id').fetchall(),
                             [('我们坐在河岸上。',), ('已有人工翻译',), (None,)])
            self.assertEqual(conn.execute('SELECT example_translation_cn FROM words WHERE id=1').fetchone()[0], '我们坐在河岸上。')
        self.assertEqual(sync_database(db, self.supplements)['updated_examples'], 0)

    def test_database_stale_source_and_explicit_admin_override(self):
        self.import_result()
        db = self.database()
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("INSERT INTO admin_content_overrides VALUES('example',21,'{\"translation_cn\":null}')")
        self.assertEqual(sync_database(db, self.supplements)['updated_examples'], 0)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('DELETE FROM admin_content_overrides')
            conn.execute("UPDATE sense_examples SET target_form='sat' WHERE id=21")
        with self.assertRaisesRegex(ValueError, 'Live sentence differs'):
            sync_database(db, self.supplements)
        report = sync_database(db, self.supplements, strict=False)
        self.assertEqual((report['stale'], report['updated_examples']), (1, 0))

    def test_runner_defaults_resume_and_rejects_invalid_model_output(self):
        self.result.unlink()
        def fake_run(command, **kwargs):
            self.assertEqual(command[command.index('--model') + 1], 'gpt-6-luna')
            self.assertIn('model_reasoning_effort="low"', command)
            self.assertIn('read-only', command)
            self.assertNotIn('manifest', kwargs['input'])
            self.write(Path(command[command.index('--output-last-message') + 1]), self.response)
        with patch('translation_exchange.subprocess.run', side_effect=fake_run) as run:
            report = run_batches(self.output, self.catalog, glosses=self.glosses)
            self.assertEqual(report['completed_batches'], 1)
            self.assertEqual(run_batches(self.output, self.catalog, glosses=self.glosses)['skipped_batches'], 1)
            self.assertEqual(run.call_count, 1)
        self.result.unlink()
        self.response['batch'] = 'wrong'
        with patch('translation_exchange.subprocess.run', side_effect=fake_run):
            with self.assertRaises(ValueError):
                run_batches(self.output, self.catalog, glosses=self.glosses)
        self.assertFalse(self.result.exists())

    def test_database_error_rolls_back_every_translation(self):
        self.import_result()
        db = self.database()
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('UPDATE sense_examples SET translation_cn=NULL WHERE id=22')
            conn.execute("CREATE TRIGGER stop_second BEFORE UPDATE ON sense_examples WHEN OLD.id=22 BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        backup = self.root / 'rollback-backup.sqlite'
        with self.assertRaises(sqlite3.IntegrityError):
            sync_database(db, self.supplements, backup)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute('SELECT translation_cn FROM sense_examples ORDER BY id').fetchall(), [(None,), (None,), (None,)])
            self.assertIsNone(conn.execute('SELECT example_translation_cn FROM words WHERE id=1').fetchone()[0])
        self.assertTrue(backup.exists())


if __name__ == '__main__':
    unittest.main()
