from __future__ import annotations

import copy
import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from chinese_glosses import apply_supplements
from apply_chinese_glosses_to_db import apply as apply_to_db


class ChineseGlossTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "supplements.json"
        self.words = [{"word": "bank", "definition_cn": "", "senses": [
            {"key": "en:river", "definition_en": "the land alongside a river", "definition_cn": "", "part_of_speech": "名词", "examples": [{"sentence": "They sat on the bank.", "target_form": "bank"}]},
            {"key": "en:finance", "definition_en": "a financial institution", "definition_cn": "银行", "part_of_speech": "名词", "examples": []},
        ]}]
        self.entry = {"word": "bank", "sense_key": "en:river", "expected_definition_en": "the land alongside a river", "expected_part_of_speech": "名词", "definition_cn": "河岸"}

    def write(self, entries):
        self.path.write_text(json.dumps({"format_version": 1, "entries": entries}), encoding="utf-8")

    def test_only_missing_gloss_changes_and_identifiers_examples_stay(self):
        original = copy.deepcopy(self.words)
        self.write([self.entry])
        self.assertEqual(apply_supplements(self.words, self.path), 1)
        self.assertEqual(self.words[0]["definition_cn"], "河岸")
        self.assertEqual(self.words[0]["senses"][1], original[0]["senses"][1])
        for before, after in zip(original[0]["senses"], self.words[0]["senses"]):
            self.assertEqual(before["key"], after["key"])
            self.assertEqual(before["examples"], after["examples"])
        self.assertEqual(apply_supplements(self.words, self.path), 0)

    def test_existing_chinese_is_preserved(self):
        self.words[0]["senses"][0]["definition_cn"] = "岸边"
        self.write([self.entry])
        self.assertEqual(apply_supplements(self.words, self.path), 0)
        self.assertEqual(self.words[0]["senses"][0]["definition_cn"], "岸边")

    def test_changed_meaning_or_pos_requires_review(self):
        self.write([self.entry])
        for field, value in [("definition_en", "a financial institution"), ("part_of_speech", "动词")]:
            words = copy.deepcopy(self.words)
            words[0]["senses"][0][field] = value
            with self.assertRaisesRegex(ValueError, "Sense changed"):
                apply_supplements(words, self.path)

    def test_duplicate_or_non_chinese_supplement_is_rejected(self):
        for entries in [[self.entry, self.entry], [{**self.entry, "definition_cn": "river bank"}]]:
            self.write(entries)
            with self.assertRaises(ValueError):
                apply_supplements(self.words, self.path)

    def database_fixture(self):
        folder = Path(self.folder.name)
        previous, current, seed, db = [folder / name for name in ("previous.json", "catalog.json", "seed.json", "db.sqlite")]
        document = {"format_version": 2, "lists": [], "words": self.words}
        previous.write_text(json.dumps(document), encoding="utf-8")
        seed.write_text("[]", encoding="utf-8")
        self.write([self.entry])
        apply_supplements(self.words, self.path)
        current.write_text(json.dumps(document), encoding="utf-8")
        fingerprint = hashlib.sha256(previous.read_bytes() + b"\0" + seed.read_bytes()).hexdigest()
        with sqlite3.connect(db) as conn:
            conn.executescript("""CREATE TABLE words(id INTEGER PRIMARY KEY,owner_id INTEGER,word TEXT,definition_cn TEXT,definition_en TEXT);
                CREATE TABLE word_senses(id INTEGER PRIMARY KEY,word_id INTEGER REFERENCES words(id),sense_key TEXT,definition_cn TEXT,definition_en TEXT,part_of_speech TEXT,active INTEGER);
                CREATE TABLE vocabulary_catalog_state(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE review_history(id INTEGER PRIMARY KEY,sense_id INTEGER REFERENCES word_senses(id),answer TEXT);
                INSERT INTO words VALUES(1,NULL,'bank','词义待补充',NULL);
                INSERT INTO word_senses VALUES(11,1,'en:river','','the land alongside a river','名词',1);
                INSERT INTO word_senses VALUES(12,1,'en:finance','银行','a financial institution','名词',1);
                INSERT INTO review_history VALUES(1,11,'bank');""")
            conn.execute("INSERT INTO vocabulary_catalog_state VALUES('fingerprint',?)", (fingerprint,))
        return db, current, previous, seed, folder / "backup.sqlite"

    def test_live_update_keeps_history_ids_and_existing_chinese(self):
        db, current, previous, seed, backup = self.database_fixture()
        result = apply_to_db(db, current, previous, self.path, seed, backup)
        self.assertEqual(result["updated_senses"], 1)
        self.assertTrue(result["other_content_and_learning_progress_unchanged"])
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT id,definition_cn FROM word_senses ORDER BY id").fetchall(), [(11,"河岸"),(12,"银行")])
            self.assertEqual(conn.execute("SELECT * FROM review_history").fetchall(), [(1,11,"bank")])
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute("SELECT definition_cn FROM word_senses WHERE id=11").fetchone()[0], "")

    def test_live_changed_meaning_rolls_back(self):
        db, current, previous, seed, backup = self.database_fixture()
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE word_senses SET definition_en='a changed meaning' WHERE id=11")
        with self.assertRaisesRegex(ValueError, "Live sense differs"):
            apply_to_db(db, current, previous, self.path, seed, backup)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT definition_cn FROM word_senses WHERE id=11").fetchone()[0], "")


if __name__ == "__main__":
    unittest.main()
