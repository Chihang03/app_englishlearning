from __future__ import annotations

import base64
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pywebpush import WebPushException
from requests import Response

from app import database, migrations, push
from app.learning_reminders import run_reminders, send_reminder
from app.main import app
import test_senses as fixtures

ORIGIN = "https://english.example"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)  # 20:00 Shanghai


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class LearningReminderTests(unittest.TestCase):
    def setUp(self):
        fixtures.SenseLearningTests.setUp(self)
        key = ec.generate_private_key(ec.SECP256R1())
        self.private = self.directory / "vapid.pem"
        self.private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.keys = {"p256dh": encoded(key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)), "auth": encoded(os.urandom(16))}
        environment = patch.dict(os.environ, {"WEB_PUSH_PRIVATE_KEY": str(self.private),
            "WEB_PUSH_SUBJECT": ORIGIN, "WEB_PUSH_ORIGINS": ORIGIN})
        environment.start(); self.addCleanup(environment.stop)
        push.signing_key.cache_clear(); self.addCleanup(push.signing_key.cache_clear)
        sender = patch("app.learning_reminders.send_reminder")
        self.sender = sender.start(); self.addCleanup(sender.stop)

    def subscribe(self, device="one", client=None, **changes):
        return (client or self.client).post("/api/push/subscription", headers={"Origin": ORIGIN},
            json={"endpoint": f"https://web.push.apple.com/{device}", "keys": self.keys, **changes})

    def test_time_window_daily_dedup_and_dry_run(self):
        self.assertEqual(self.subscribe().status_code, 200)
        self.assertEqual(run_reminders(NOW - timedelta(seconds=1))["sent"], 0)
        self.assertEqual(run_reminders(NOW, dry_run=True)["eligible"], 1)
        self.assertEqual(run_reminders(NOW)["sent"], 1)
        self.assertEqual(run_reminders(NOW + timedelta(minutes=1))["sent"], 0)
        self.assertEqual(run_reminders(NOW + timedelta(days=1, minutes=10))["sent"], 0)
        self.assertEqual(run_reminders(NOW + timedelta(days=1))["sent"], 1)
        self.assertEqual(self.sender.call_count, 2)

    def review_at(self, moment, answer="wrong"):
        card = fixtures.SenseLearningTests.next(self)
        response = fixtures.SenseLearningTests.review(self, card, answer)
        self.assertEqual(response.status_code, 200, response.text)
        with database.connect() as conn:
            conn.execute("UPDATE review_history SET review_time=? WHERE user_id=?", (moment.isoformat(), self.uid))

    def test_wrong_answer_counts_and_local_midnight_is_inclusive(self):
        self.subscribe()
        self.review_at(NOW.replace(hour=16) - timedelta(days=1))  # Shanghai midnight
        self.assertEqual(run_reminders(NOW)["sent"], 0)
        with database.connect() as conn:
            conn.execute("UPDATE review_history SET review_time='2026-09-30T15:59:59+00:00'")
        self.assertEqual(run_reminders(NOW)["sent"], 1)

    def test_correct_answer_also_suppresses(self):
        self.subscribe()
        self.review_at(NOW - timedelta(hours=1), "address")
        self.assertEqual(run_reminders(NOW)["sent"], 0)

    def test_timezone_and_users_are_independent(self):
        self.subscribe()
        self.review_at(NOW - timedelta(hours=1))
        with TestClient(app) as other:
            result = other.post("/api/auth/register", json={"username": "second", "password": "test-password-123", "timezone": "America/New_York"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.subscribe("two", other).status_code, 200)
            self.assertEqual(run_reminders(NOW)["sent"], 0)
            self.assertEqual(run_reminders(NOW + timedelta(hours=12))["sent"], 1)
            self.assertEqual(self.sender.call_args.args[0]["user_id"], result.json()["user"]["id"])

    def test_multiple_devices_and_logout_only_removes_current_session(self):
        self.subscribe("one")
        with TestClient(app) as other:
            other.post("/api/auth/login", json={"username": "learner", "password": "test-password-123"})
            self.subscribe("two", other)
            self.client.post("/api/auth/logout")
            self.assertEqual(run_reminders(NOW)["sent"], 1)
            self.assertTrue(self.sender.call_args.args[0]["endpoint"].endswith("/two"))

    def test_switching_account_rebinds_device_and_logout_does_not_repeat_day(self):
        self.subscribe()
        self.assertEqual(run_reminders(NOW)["sent"], 1)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "learner", "password": "test-password-123"})
        self.subscribe()
        self.assertEqual(run_reminders(NOW)["sent"], 0)
        self.client.post("/api/auth/register", json={"username": "second", "password": "test-password-123"})
        self.subscribe()
        with database.connect() as conn:
            self.assertNotEqual(conn.execute("SELECT user_id FROM push_subscriptions").fetchone()[0], self.uid)

    def test_concurrent_runs_claim_only_once(self):
        self.subscribe()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: run_reminders(NOW), range(2)))
        self.assertEqual(sum(result["sent"] for result in results), 1)
        self.sender.assert_called_once()

    def test_failure_retries_are_bounded_and_recheck_learning(self):
        self.subscribe()
        response = Response(); response.status_code = 503
        self.sender.side_effect = WebPushException("failed", response=response)
        self.assertEqual(run_reminders(NOW)["failed"], 1)
        self.assertEqual(run_reminders(NOW + timedelta(seconds=30))["failed"], 0)
        self.assertEqual(run_reminders(NOW + timedelta(minutes=1))["failed"], 1)
        self.assertEqual(run_reminders(NOW + timedelta(minutes=2))["failed"], 1)
        self.assertEqual(run_reminders(NOW + timedelta(minutes=3))["failed"], 0)
        self.assertEqual(self.sender.call_count, 3)

    def test_learning_before_retry_suppresses_it(self):
        self.subscribe()
        response = Response(); response.status_code = 503
        self.sender.side_effect = WebPushException("failed", response=response)
        run_reminders(NOW)
        self.review_at(NOW + timedelta(seconds=30))
        self.assertEqual(run_reminders(NOW + timedelta(minutes=1))["eligible"], 0)

    def test_expired_subscription_removed(self):
        self.subscribe()
        response = Response(); response.status_code = 410
        self.sender.side_effect = WebPushException("gone", response=response)
        self.assertEqual(run_reminders(NOW)["expired"], 1)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0], 0)

    def test_ambiguous_network_failure_does_not_resend(self):
        self.subscribe()
        self.sender.side_effect = TimeoutError("uncertain delivery")
        self.assertEqual(run_reminders(NOW)["failed"], 1)
        self.assertEqual(run_reminders(NOW + timedelta(minutes=1))["eligible"], 0)
        self.sender.assert_called_once()

    def test_auth_origin_endpoint_and_key_validation(self):
        with TestClient(app) as anonymous:
            self.assertEqual(anonymous.get("/api/push/config").status_code, 401)
        self.assertEqual(self.client.post("/api/push/subscription", json={"endpoint": "https://web.push.apple.com/a", "keys": self.keys}).status_code, 403)
        for endpoint in ("http://web.push.apple.com/a", "https://127.0.0.1/a", "https://web.push.apple.com.evil.com/a", "https://web.push.apple.com:8443/a"):
            self.assertEqual(self.subscribe(endpoint=endpoint).status_code, 422)
        self.assertEqual(self.subscribe(keys={"p256dh": self.keys["p256dh"], "auth": "x" * 24}).status_code, 422)
        self.assertEqual(self.client.get("/api/push/config").json()["enabled"], True)

    def test_real_encryption_and_vapid_headers_without_network(self):
        subscription = {"endpoint": "https://web.push.apple.com/test", **self.keys}
        response = Response(); response.status_code = 201
        with patch("requests.Session.request", return_value=response) as transport:
            send_reminder(subscription, "2026-10-01")
        args = transport.call_args.kwargs
        self.assertFalse(args["allow_redirects"])
        self.assertEqual(args["headers"]["ttl"], "600")
        self.assertIn("vapid", args["headers"]["authorization"])
        self.assertGreater(len(args["data"]), 100)

    def test_v19_upgrade_is_additive_backed_up_and_idempotent(self):
        with database.connect() as conn:
            before = tuple(conn.execute("SELECT * FROM users").fetchone())
            conn.execute("DROP TABLE push_deliveries")
            conn.execute("DROP TABLE push_subscriptions")
            conn.execute("PRAGMA user_version=19")
        migrations.run_migrations(database.DB_PATH)
        migrations.run_migrations(database.DB_PATH)
        with database.connect() as conn:
            self.assertEqual(tuple(conn.execute("SELECT * FROM users").fetchone()), before)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 20)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        backup = next(self.directory.glob("test.db.bak-v19-*"))
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 19)
