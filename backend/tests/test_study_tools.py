from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import database, migrations
from app.main import app
from test_senses import catalog


class StudyToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        source = self.directory / "catalog.json"
        source.write_text(json.dumps(catalog(), ensure_ascii=False))
        for name, value in [("DATA_DIR", self.directory), ("DB_PATH", self.directory / "test.db"),
                            ("SEED_PATH", self.directory / "missing"), ("VOCABULARY_CATALOG_PATH", source)]:
            patched = patch.object(database, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        response = self.client.post("/api/auth/register", json={"username": "tools", "password": "test-password-123"})
        self.assertEqual(response.status_code, 200, response.text)
        self.uid = response.json()["user"]["id"]
        self.card = self.client.get("/api/next").json()["card"]

    def report(self, category="definition", details="释义需要核对"):
        return self.client.post("/api/content-reports", json={
            "attempt_id": self.card["attempt_id"], "category": category, "details": details})

    def test_report_persists_context_and_does_not_reveal_answer(self):
        response = self.report(details="  释义需要核对  ")
        self.assertEqual(response.status_code, 200, response.text)
        with database.connect() as conn:
            row = conn.execute("SELECT * FROM content_reports").fetchone()
            self.assertEqual(row["user_id"], self.uid)
            self.assertEqual(row["sense_id"], self.card["sense_id"])
            self.assertEqual(row["example_id"], self.card["example_id"])
            self.assertEqual(row["details"], "释义需要核对")
            self.assertEqual(row["status"], "pending")
            snapshot = json.loads(row["content_snapshot"])
            self.assertEqual(snapshot["sentence"], self.card["example_sentence"])
            self.assertEqual(snapshot["definition_cn"], self.card["definition_cn"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_history").fetchone()[0], 0)
        resumed = self.client.get("/api/next").json()["card"]
        self.assertFalse(resumed["answer_exposed"])
        self.assertEqual(resumed["attempt_id"], self.card["attempt_id"])

    def test_retries_are_deduplicated_and_all_categories_accepted(self):
        first = self.report().json()
        self.assertEqual(self.report().json()["id"], first["id"])
        for category in ("sentence", "translation", "pronunciation", "other"):
            self.assertEqual(self.report(category).status_code, 200)
        self.assertEqual(self.report("unknown").status_code, 422)
        self.assertEqual(self.report(details="x" * 2001).status_code, 422)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM content_reports").fetchone()[0], 5)

    def test_meanings_before_answer_persist_exposure_and_prevent_independent_credit(self):
        response = self.client.post("/api/study/meanings", json={"attempt_id": self.card["attempt_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["word"], "address")
        self.assertEqual(len(response.json()["senses"]), 2)
        self.assertTrue(self.client.get("/api/next").json()["card"]["answer_exposed"])
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sense_exposures WHERE user_id=?", (self.uid,)).fetchone()[0], 1)
        result = self.client.post("/api/review", json={"word_id": self.card["id"],
            "sense_id": self.card["sense_id"], "example_id": self.card["example_id"],
            "attempt_id": self.card["attempt_id"], "user_answer": self.card["answer_form"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["is_independent"])

    def test_completed_attempt_can_show_meanings_and_be_reported(self):
        response = self.client.post("/api/review", json={"word_id": self.card["id"],
            "sense_id": self.card["sense_id"], "example_id": self.card["example_id"],
            "attempt_id": self.card["attempt_id"], "user_answer": self.card["answer_form"]})
        self.assertTrue(response.json()["is_independent"])
        self.assertEqual(self.report().status_code, 200)
        self.assertEqual(self.client.post("/api/study/meanings", json={"attempt_id": self.card["attempt_id"]}).status_code, 200)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT is_independent FROM review_history").fetchone()[0], 1)

    def test_authentication_and_attempt_ownership(self):
        with TestClient(app) as other:
            payload = {"attempt_id": self.card["attempt_id"], "category": "sentence"}
            self.assertEqual(other.post("/api/content-reports", json=payload).status_code, 401)
            self.assertEqual(other.post("/api/study/meanings", json=payload).status_code, 401)
            other.post("/api/auth/register", json={"username": "other", "password": "test-password-123"})
            self.assertEqual(other.post("/api/content-reports", json=payload).status_code, 404)
            self.assertEqual(other.post("/api/study/meanings", json=payload).status_code, 409)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM content_reports").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT SUM(answer_exposed) FROM study_attempts").fetchone()[0], 0)

    def test_v8_upgrade_preserves_existing_data_and_makes_backup(self):
        with database.connect() as conn:
            conn.execute("DROP TABLE content_reports")
            conn.execute("PRAGMA user_version=7")
            before = conn.execute("SELECT * FROM study_attempts").fetchall()
        migrations.run_migrations(database.DB_PATH)
        migrations.run_migrations(database.DB_PATH)
        with database.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM study_attempts")], [tuple(row) for row in before])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        backups = list(self.directory.glob("test.db.bak-v7-*"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertEqual(self.report().status_code, 200)
