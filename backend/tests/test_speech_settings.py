from __future__ import annotations

import unittest

from fastapi.testclient import TestClient
from app import database
from app.main import app
import test_senses as fixtures


class SpeechSettingsTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp

    def test_three_speeds_persist_across_login_and_database_restart(self):
        self.assertIsNone(self.client.get("/api/settings").json()["speech_rate"])
        for rate in (90, 120, 175):
            response = self.client.patch("/api/settings", json={"speech_rate": rate})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["speech_rate"], rate)
        database.init_database()
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "learner", "password": "test-password-123"})
        self.assertEqual(self.client.get("/api/settings").json()["speech_rate"], 175)

    def test_invalid_speeds_are_rejected_and_other_settings_preserve_speed(self):
        self.client.patch("/api/settings", json={"speech_rate": 120})
        for rate in (0, 80, 110, 121, 320, "fast"):
            response = self.client.patch("/api/settings", json={"speech_rate": rate})
            self.assertEqual(response.status_code, 422, response.text)
        response = self.client.patch("/api/settings", json={"show_sentence_translation": True})
        self.assertEqual(response.json()["speech_rate"], 120)
        self.assertTrue(response.json()["show_sentence_translation"])
        self.assertEqual(self.client.patch("/api/settings", json={"speech_rate": None}).json()["speech_rate"], 120)

    def test_authentication_and_account_isolation(self):
        self.client.patch("/api/settings", json={"speech_rate": 90})
        with TestClient(app) as other:
            self.assertEqual(other.get("/api/settings").status_code, 401)
            self.assertEqual(other.patch("/api/settings", json={"speech_rate": 175}).status_code, 401)
            response = other.post("/api/auth/register", json={"username": "other", "password": "test-password-123"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(other.get("/api/settings").json()["speech_rate"])
            self.assertEqual(other.patch("/api/settings", json={"speech_rate": 175}).json()["speech_rate"], 175)
        self.assertEqual(self.client.get("/api/settings").json()["speech_rate"], 90)
