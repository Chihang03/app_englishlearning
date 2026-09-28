"""Reserved account provisioning, authorization and timezone-aware overview."""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import database, migrations
from app.main import app
from app.security import local_day_bounds, resolve_timezone, today_in
import test_senses as fixtures

sys.path.insert(0, str(database.BASE_DIR / "scripts"))
import create_admin as provision


class AdminTests(unittest.TestCase):
    setUp = fixtures.SenseLearningTests.setUp
    next = fixtures.SenseLearningTests.next
    review = fixtures.SenseLearningTests.review

    def admin_client(self):
        with patch.object(provision, "DB_PATH", database.DB_PATH):
            provision.create_admin("admin")
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["role"], "admin")
        return client

    def test_reserved_name_and_public_payload_cannot_grant_admin(self):
        for name in ("admin", "Admin", "ADMIN"):
            result = self.client.post("/api/auth/register", json={"username": name, "password": "password-123"})
            self.assertEqual(result.status_code, 409)
        result = self.client.post("/api/auth/register", json={"username": "ordinary", "password": "password-123", "role": "admin"})
        self.assertEqual(result.json()["user"]["role"], "learner")
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
        anonymous = TestClient(app)
        self.assertEqual(anonymous.get("/api/admin/overview").status_code, 401)
        self.assertEqual(anonymous.get("/api/admin/users").status_code, 401)

    def test_admin_login_account_security_and_learning_blocked(self):
        admin = self.admin_client()
        self.assertEqual(admin.get("/api/auth/me").json()["user"]["role"], "admin")
        for path in ("stats", "next", "settings", "word-lists", "muted-words", "dictionary/address"):
            self.assertEqual(admin.get(f"/api/{path}").status_code, 403, path)
        mutations = [
            ("POST", "/api/review", {"word_id": 1}),
            ("POST", "/api/study/hint", {"attempt_id": "x", "kind": "answer"}),
            ("POST", "/api/words/1/mute", None),
            ("DELETE", "/api/muted-words/address", None),
            ("PATCH", "/api/settings", {"skip_basic_600": True}),
            ("POST", "/api/words/import", {"words": []}),
        ]
        for method, path, body in mutations:
            self.assertEqual(admin.request(method, path, json=body).status_code, 403, path)
        self.assertEqual(admin.get("/api/auth/passkeys").status_code, 200)
        changed = admin.patch("/api/auth/password", json={"current_password": "admin", "new_password": "new-password-123"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(admin.get("/api/admin/overview").status_code, 200)
        with patch.object(provision, "DB_PATH", database.DB_PATH):
            self.assertFalse(provision.create_admin("admin"))
        self.assertEqual(admin.post("/api/auth/login", json={"username": "admin", "password": "admin"}).status_code, 401)
        admin.post("/api/auth/logout")
        self.assertEqual(admin.get("/api/admin/users").status_code, 401)
        with database.connect() as conn:
            aid = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
            for table in ("review_history", "sense_srs_state", "study_attempts", "settings", "user_vocabulary_lists"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (aid,)).fetchone()[0], 0)

    def test_overview_counts_dates_privacy_and_pagination(self):
        card = self.next()
        self.review(card, "address")
        report = self.client.post("/api/content-reports", json={"attempt_id": card["attempt_id"], "category": "definition"})
        self.assertEqual(report.status_code, 200, report.text)
        admin = self.admin_client()
        overview = admin.get("/api/admin/overview")
        self.assertEqual(overview.headers["cache-control"], "no-store")
        self.assertEqual(overview.json(), {"users": 1, "today_active_users": 1, "reviews": 1,
            "today_reviews": 1, "public_words": 2, "pending_reports": 1})
        start, _ = local_day_bounds(today_in(resolve_timezone("Pacific/Honolulu")) + timedelta(days=1), resolve_timezone("Pacific/Honolulu"))
        admin.patch("/api/auth/me", json={"timezone": "Pacific/Honolulu"})
        with database.connect() as conn:
            conn.execute("UPDATE review_history SET review_time=?", (start,))
        overview = admin.get("/api/admin/overview").json()
        self.assertEqual((overview["today_reviews"], overview["today_active_users"], overview["reviews"]), (0, 0, 1))
        for i in range(3):
            self.client.post("/api/auth/register", json={"username": f"learner{i}", "password": "password-123"})
        page = admin.get("/api/admin/users?limit=2").json()
        next_page = admin.get("/api/admin/users?limit=2&offset=2").json()
        self.assertEqual(page["total"], 4)
        self.assertEqual(len(page["users"]), 2)
        self.assertFalse({u['id'] for u in page['users']} & {u['id'] for u in next_page['users']})
        all_users = page["users"] + next_page["users"]
        self.assertEqual(next(u for u in all_users if u["id"] == self.uid)["reviews"], 1)
        for user in all_users:
            self.assertEqual(set(user), {"id", "username", "timezone", "created_at", "reviews", "last_review_at"})
            self.assertNotEqual(user["username"], "admin")
        self.assertEqual(admin.get("/api/admin/users?limit=101").status_code, 422)
        self.assertEqual(admin.get("/api/admin/users?offset=-1").status_code, 422)

    def test_migration_and_provisioning_preserve_existing_learner(self):
        card = self.next()
        self.review(card, "address")
        with database.connect() as conn:
            before = tuple(conn.execute("SELECT * FROM review_history").fetchone())
            conn.execute("UPDATE users SET username='admin' WHERE id=?", (self.uid,))
            conn.execute("ALTER TABLE users DROP COLUMN role")
            for column in ('resolution_notes', 'resolved_at', 'resolved_by'):
                conn.execute(f'ALTER TABLE content_reports DROP COLUMN {column}')
            conn.execute("PRAGMA user_version=9")
        migrations.run_migrations(database.DB_PATH)
        migrations.run_migrations(database.DB_PATH)
        with database.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], migrations.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT role FROM users WHERE id=?", (self.uid,)).fetchone()[0], "learner")
            self.assertEqual(tuple(conn.execute("SELECT * FROM review_history").fetchone()), before)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertTrue(list(self.directory.glob("test.db.bak-v9-*")))
        with patch.object(provision, "DB_PATH", database.DB_PATH), self.assertRaises(ValueError):
            provision.create_admin("admin")
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 403)

    def test_user_details_include_learning_but_exclude_credentials(self):
        card = self.next()
        self.review(card, "address")
        admin = self.admin_client()
        response = admin.get(f"/api/admin/users/{self.uid}")
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()
        self.assertEqual(set(detail['user']), {'id', 'username', 'timezone', 'created_at'})
        self.assertEqual((detail['activity']['reviews'], detail['activity']['correct_reviews'], detail['activity']['learned_words']), (1, 1, 1))
        self.assertEqual(detail['activity']['today_reviews'], 1)
        self.assertEqual(detail['activity']['passkeys'], 0)
        self.assertIn('cet4', {item['list_id'] for item in detail['word_lists']})
        self.assertEqual(admin.get('/api/admin/users/999999').status_code, 404)
        aid = admin.get('/api/auth/me').json()['user']['id']
        self.assertEqual(admin.get(f'/api/admin/users/{aid}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/admin/users/{self.uid}').status_code, 403)

    def test_feedback_detail_resolution_reopening_and_permissions(self):
        card = self.next()
        response = self.client.post('/api/content-reports', json={'attempt_id': card['attempt_id'], 'category': 'definition', 'details': '请核对释义'})
        rid = response.json()['id']
        paths = ('/api/admin/reports', f'/api/admin/reports/{rid}')
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(TestClient(app).get(path).status_code, 401)
        self.assertEqual(self.client.patch(paths[1], json={'status': 'resolved'}).status_code, 403)
        self.assertEqual(TestClient(app).patch(paths[1], json={'status': 'resolved'}).status_code, 401)
        admin = self.admin_client()
        listed = admin.get('/api/admin/reports').json()
        self.assertEqual((listed['total'], listed['reports'][0]['word']), (1, 'address'))
        self.assertEqual(admin.get('/api/admin/reports?status=resolved').json()['total'], 0)
        original = admin.get(paths[1]).json()
        self.assertEqual(original['details'], '请核对释义')
        self.assertEqual(original['content']['word'], 'address')
        self.assertNotIn('attempt_id', original['content'])
        with database.connect() as conn:
            attempt_before = tuple(conn.execute('SELECT * FROM study_attempts').fetchone())
        done = admin.patch(paths[1], json={'status': 'resolved', 'resolution_notes': ' 已核对 '}).json()
        self.assertEqual((done['status'], done['resolution_notes'], done['resolved_by']), ('resolved', '已核对', 'admin'))
        self.assertTrue(done['resolved_at'])
        again = admin.patch(paths[1], json={'status': 'resolved'}).json()
        self.assertEqual(again['resolved_at'], done['resolved_at'])
        self.assertEqual(admin.get('/api/admin/overview').json()['pending_reports'], 0)
        self.assertEqual(admin.get('/api/admin/reports?status=resolved').json()['total'], 1)
        reopened = admin.patch(paths[1], json={'status': 'pending'}).json()
        self.assertEqual(reopened['resolution_notes'], '已核对')
        self.assertIsNone(reopened['resolved_at'])
        self.assertIsNone(reopened['resolved_by'])
        self.assertEqual(reopened['content'], original['content'])
        self.assertEqual(admin.get('/api/admin/overview').json()['pending_reports'], 1)
        self.assertEqual(admin.get('/api/admin/reports?limit=101').status_code, 422)
        self.assertEqual(admin.get('/api/admin/reports?status=invalid').status_code, 422)
        self.assertEqual(admin.patch(paths[1], json={'status': 'invalid'}).status_code, 422)
        self.assertEqual(admin.get('/api/admin/reports/999999').status_code, 404)
        self.assertEqual(admin.patch('/api/admin/reports/999999', json={'status': 'resolved'}).status_code, 404)
        with database.connect() as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM study_attempts').fetchone()), attempt_before)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM review_history').fetchone()[0], 0)
