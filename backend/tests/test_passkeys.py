"""Exercise real WebAuthn verification with a software ES256 authenticator.

No verification methods are mocked. All databases live in temporary directories.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import database, migrations, passkeys
from app.main import app


ORIGIN = "http://localhost:5173"
PASSWORD = "test-password-123"


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class SoftwareAuthenticator:
    def __init__(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.user_handle = ""

    def client_data(self, options, ceremony, origin, cross_origin=False):
        return json.dumps({"type": ceremony, "challenge": options["challenge"],
                           "origin": origin, "crossOrigin": cross_origin}).encode()

    def registration(self, options, *, origin=ORIGIN, flags=0x45, rp_id="localhost", cross_origin=False):
        self.user_handle = options["user"]["id"]
        numbers = self.private_key.public_key().public_numbers()
        public_key = cbor2.dumps({1: 2, 3: -7, -1: 1,
                                 -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        data = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + (0).to_bytes(4, "big")
                + bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + public_key)
        return {"id": encode(self.credential_id), "rawId": encode(self.credential_id),
                "type": "public-key", "clientExtensionResults": {"credProps": {"rk": True}},
                "response": {"clientDataJSON": encode(self.client_data(options, "webauthn.create", origin, cross_origin)),
                             "attestationObject": encode(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})),
                             "transports": ["internal", "hybrid"]}}

    def authentication(self, options, *, origin=ORIGIN, flags=0x05, count=1, rp_id="localhost", cross_origin=False):
        data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")
        client = self.client_data(options, "webauthn.get", origin, cross_origin)
        signature = self.private_key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": encode(self.credential_id), "rawId": encode(self.credential_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": encode(client), "authenticatorData": encode(data),
                             "signature": encode(signature), "userHandle": self.user_handle}}


class PasskeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        for name, value in [("DATA_DIR", directory), ("DB_PATH", directory / "test.db")]:
            patcher = patch.object(database, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in [("RP_ID", "localhost"), ("ORIGINS", [ORIGIN, "http://localhost:8000"])]:
            patcher = patch.object(passkeys, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        result = self.client.post("/api/auth/register", json={"username": "alice", "password": PASSWORD})
        self.assertEqual(result.status_code, 200, result.text)
        self.user = result.json()["user"]
        self.authenticator = SoftwareAuthenticator()

    def options(self, purpose="login"):
        body = {"name": "Alice 的电脑", "current_password": PASSWORD} if purpose == "register" else None
        result = self.client.post(f"/api/auth/passkeys/{purpose}/options", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["options"]

    def verify(self, purpose, credential, **kwargs):
        return self.client.post(f"/api/auth/passkeys/{purpose}/verify", json={"credential": credential}, **kwargs)

    def add_key(self, authenticator=None, flags=0x45):
        authenticator = authenticator or self.authenticator
        options = self.options("register")
        result = self.verify("register", authenticator.registration(options, flags=flags))
        self.assertEqual(result.status_code, 200, result.text)
        return self.client.get("/api/auth/passkeys").json()["passkeys"][0]["id"]

    def test_registration_and_passwordless_login_keep_original_account(self):
        options = self.options("register")
        self.assertEqual(options["authenticatorSelection"]["residentKey"], "required")
        self.assertEqual(options["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(len(base64.urlsafe_b64decode(options["user"]["id"] + "=")), 32)
        result = self.verify("register", self.authenticator.registration(options))
        self.assertEqual(result.status_code, 200, result.text)
        keys = self.client.get("/api/auth/passkeys").json()["passkeys"]
        self.assertEqual(keys[0]["name"], "Alice 的电脑")
        self.assertIsNone(keys[0]["last_used_at"])
        self.assertNotIn("public_key", keys[0])
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        options = self.options()
        self.assertEqual(options["userVerification"], "required")
        self.assertFalse(options.get("allowCredentials"))
        result = self.verify("login", self.authenticator.authentication(options))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["user"], self.user)
        self.assertIn("httponly", result.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/api/auth/me").json()["user"], self.user)
        self.assertEqual(self.client.get("/api/stats").status_code, 200)
        self.assertIsNotNone(self.client.get("/api/auth/passkeys").json()["passkeys"][0]["last_used_at"])

    def test_multiple_keys_stable_handle_and_exclusion(self):
        self.add_key()
        options = self.options("register")
        self.assertEqual(options["user"]["id"], self.authenticator.user_handle)
        self.assertEqual(options["excludeCredentials"][0]["id"], encode(self.authenticator.credential_id))
        other = SoftwareAuthenticator()
        result = self.verify("register", other.registration(options))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.client.get("/api/auth/passkeys").json()["passkeys"]), 2)

    def test_management_requires_password_and_session(self):
        response = self.client.post("/api/auth/passkeys/register/options", json={"current_password": "wrong"})
        self.assertEqual(response.status_code, 403)
        key_id = self.add_key()
        self.assertEqual(self.client.request("DELETE", f"/api/auth/passkeys/{key_id}",
                                           json={"current_password": "wrong"}).status_code, 403)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/auth/passkeys").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/passkeys/register/options",
                                         json={"current_password": PASSWORD}).status_code, 401)

    def test_account_isolation_for_listing_deletion_and_user_handle(self):
        key_id = self.add_key()
        self.client.post("/api/auth/register", json={"username": "bob", "password": PASSWORD})
        self.assertEqual(self.client.get("/api/auth/passkeys").json()["passkeys"], [])
        self.assertEqual(self.client.request("DELETE", f"/api/auth/passkeys/{key_id}",
                                           json={"current_password": PASSWORD}).status_code, 404)
        self.add_key(SoftwareAuthenticator())
        options = self.options()
        credential = self.authenticator.authentication(options)
        with database.connect() as conn:
            bob_handle = conn.execute("SELECT user_handle FROM webauthn_users WHERE user_id != ?",
                                      (self.user["id"],)).fetchone()[0]
        credential["response"]["userHandle"] = encode(bob_handle)
        self.assertEqual(self.verify("login", credential).status_code, 400)

    def test_delete_last_key_and_password_fallback(self):
        key_id = self.add_key()
        options = self.options()
        credential = self.authenticator.authentication(options)
        result = self.client.request("DELETE", f"/api/auth/passkeys/{key_id}", json={"current_password": PASSWORD})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.verify("login", credential).status_code, 400)
        self.client.post("/api/auth/logout")
        result = self.client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        self.assertEqual(result.status_code, 200)

    def test_replay_is_rejected(self):
        self.add_key()
        options = self.options()
        cookie = "cvt_passkey_authentication=" + self.client.cookies.get("cvt_passkey_authentication")
        credential = self.authenticator.authentication(options)
        self.assertEqual(self.verify("login", credential).status_code, 200)
        self.assertEqual(self.verify("login", credential, headers={"Cookie": cookie}).status_code, 400)

    def test_failed_verification_consumes_challenge(self):
        self.add_key()
        options = self.options()
        credential = self.authenticator.authentication(options)
        credential["response"]["signature"] = encode(b"bad signature")
        self.assertEqual(self.verify("login", credential).status_code, 400)
        self.assertEqual(self.verify("login", self.authenticator.authentication(options)).status_code, 400)

    def test_expired_and_missing_challenges(self):
        self.add_key()
        options = self.options()
        with database.connect() as conn:
            conn.execute("UPDATE webauthn_challenges SET expires_at = '2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.verify("login", self.authenticator.authentication(options)).status_code, 400)
        self.assertEqual(self.verify("login", self.authenticator.authentication(options)).status_code, 400)

    def test_challenge_bound_to_browser(self):
        self.add_key()
        options = self.options()
        with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as other:
            self.assertEqual(other.post("/api/auth/passkeys/login/verify",
                                        json={"credential": self.authenticator.authentication(options)}).status_code, 400)
        self.assertEqual(self.verify("login", self.authenticator.authentication(options)).status_code, 200)

    def test_old_registration_cannot_attach_after_account_switch(self):
        options = self.options("register")
        credential = self.authenticator.registration(options)
        self.client.post("/api/auth/register", json={"username": "bob", "password": PASSWORD})
        self.assertEqual(self.verify("register", credential).status_code, 400)
        self.assertEqual(self.client.get("/api/auth/passkeys").json()["passkeys"], [])

    def test_registration_cannot_survive_session_rotation(self):
        options = self.options("register")
        credential = self.authenticator.registration(options)
        self.client.patch("/api/auth/password", json={"current_password": PASSWORD, "new_password": "new-password-123"})
        self.assertEqual(self.verify("register", credential).status_code, 400)

    def test_challenge_bound_to_exact_allowed_origin(self):
        self.add_key()
        options = self.options()
        credential = self.authenticator.authentication(options, origin="http://localhost:8000")
        self.assertEqual(self.verify("login", credential, headers={"Origin": "http://localhost:8000"}).status_code, 400)

    def test_untrusted_and_missing_request_origins(self):
        for origin in ["https://evil.example", "null", ""]:
            with self.subTest(origin=origin):
                self.assertEqual(self.client.post("/api/auth/passkeys/login/options", headers={"Origin": origin}).status_code, 403)
                self.assertEqual(self.client.post("/api/auth/passkeys/register/options", headers={"Origin": origin},
                                                 json={"current_password": PASSWORD}).status_code, 403)

    def test_registration_rejects_origin_rp_uv_and_cross_origin(self):
        for changes in [{"origin": "https://evil.example"}, {"rp_id": "evil.example"},
                        {"flags": 0x41}, {"flags": 0x44}, {"cross_origin": True}]:
            with self.subTest(changes=changes):
                options = self.options("register")
                result = self.verify("register", self.authenticator.registration(options, **changes))
                self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.client.get("/api/auth/passkeys").json()["passkeys"], [])

    def test_login_rejects_origin_rp_uv_presence_cross_origin_and_backup_change(self):
        self.add_key()
        for changes in [{"origin": "https://evil.example"}, {"rp_id": "evil.example"},
                        {"flags": 0x01}, {"flags": 0x04}, {"cross_origin": True}, {"flags": 0x1D}]:
            with self.subTest(changes=changes):
                options = self.options()
                result = self.verify("login", self.authenticator.authentication(options, **changes))
                self.assertEqual(result.status_code, 400, result.text)

    def test_wrong_challenges(self):
        options = self.options("register")
        options["challenge"] = encode(secrets.token_bytes(32))
        self.assertEqual(self.verify("register", self.authenticator.registration(options)).status_code, 400)
        self.add_key()
        options = self.options()
        options["challenge"] = encode(secrets.token_bytes(32))
        self.assertEqual(self.verify("login", self.authenticator.authentication(options)).status_code, 400)

    def test_counter_must_increase_and_synced_keys_can_use_zero(self):
        self.add_key()
        options = self.options()
        self.assertEqual(self.verify("login", self.authenticator.authentication(options, count=1)).status_code, 200)
        options = self.options()
        self.assertEqual(self.verify("login", self.authenticator.authentication(options, count=1)).status_code, 400)
        options = self.options()
        self.assertEqual(self.verify("login", self.authenticator.authentication(options, count=2)).status_code, 200)
        synced = SoftwareAuthenticator()
        self.add_key(synced, flags=0x5D)
        for _ in range(2):
            options = self.options()
            result = self.verify("login", synced.authentication(options, flags=0x1D, count=0))
            self.assertEqual(result.status_code, 200, result.text)

    def test_duplicate_credentials_and_limit(self):
        self.add_key()
        options = self.options("register")
        self.assertEqual(self.verify("register", self.authenticator.registration(options)).status_code, 409)
        with patch.object(passkeys, "MAX_PASSKEYS", 1):
            result = self.client.post("/api/auth/passkeys/register/options", json={"current_password": PASSWORD})
            self.assertEqual(result.status_code, 400)

    def test_malformed_credentials_return_client_errors(self):
        self.add_key()
        malformed = [{}, {"id": None, "response": {}}, {"id": "bad", "response": {"clientDataJSON": "%%"}},
                     {"response": {"clientDataJSON": encode(b"[]")}}, {"garbage": "x" * 66000}]
        for purpose in ["register", "login"]:
            for credential in malformed:
                with self.subTest(purpose=purpose, credential_type=list(credential)):
                    self.options(purpose)
                    self.assertEqual(self.verify(purpose, credential).status_code, 400)

    def test_concurrent_replay_only_succeeds_once(self):
        self.add_key()
        options = self.options()
        cookie = "cvt_passkey_authentication=" + self.client.cookies.get("cvt_passkey_authentication")
        credential = self.authenticator.authentication(options)

        def attempt():
            with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN, "Cookie": cookie}) as client:
                return client.post("/api/auth/passkeys/login/verify", json={"credential": credential}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(results), [200, 400])

    def test_config_is_public_and_invalid_deployments_fail_validation(self):
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/auth/passkeys/config").json()["rp_id"], "localhost")
        for rp, origins in [("127.0.0.1", ["https://127.0.0.1"]),
                            ("example.com", ["http://example.com"]),
                            ("example.com", ["https://evil.example"]),
                            ("example.com", ["https://example.com/"]),
                            ("example.com", ["https://user@example.com"]),
                            ("example.com", ["https://example.com:invalid"]), ("localhost", [])]:
            with self.subTest(rp=rp, origins=origins), patch.object(passkeys, "RP_ID", rp), patch.object(passkeys, "ORIGINS", origins):
                with self.assertRaises(ValueError):
                    passkeys.validate_configuration()
        with patch.object(passkeys, "RP_ID", "example.com"), patch.object(passkeys, "ORIGINS", ["https://learn.example.com"]):
            passkeys.validate_configuration()


class MigrationTests(unittest.TestCase):
    def test_v2_data_preserved_and_backup_includes_uncheckpointed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            writer = sqlite3.connect(path)
            writer.row_factory = sqlite3.Row
            try:
                migrations._migrate_to_v1(writer)
                migrations._migrate_to_v2(writer)
                writer.execute("PRAGMA user_version = 2")
                writer.commit()
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute("INSERT INTO users VALUES (1, 'existing', 'existing-hash', 'Asia/Shanghai', 'existing-time')")
                writer.execute("INSERT INTO words(id, word, part_of_speech, definition_cn, example_sentence) VALUES (1, 'test', 'n', '测试', 'a test')")
                writer.execute("INSERT INTO review_history(user_id, word_id, review_time, user_answer, is_correct) VALUES (1, 1, 'existing-time', 'test', 1)")
                writer.commit()
                migrations.run_migrations(path)
                migrations.run_migrations(path)
                with closing(sqlite3.connect(path)) as upgraded:
                    self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 3)
                    self.assertEqual(upgraded.execute("SELECT username, password_hash FROM users").fetchone(), ("existing", "existing-hash"))
                    self.assertEqual(upgraded.execute("SELECT user_answer FROM review_history").fetchone()[0], "test")
                    self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
                backup = list(Path(directory).glob("legacy.db.bak-v2-*"))
                self.assertEqual(len(backup), 1)
                with closing(sqlite3.connect(backup[0])) as snapshot:
                    self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 2)
                    self.assertEqual(snapshot.execute("SELECT username FROM users").fetchone()[0], "existing")
                    self.assertEqual(snapshot.execute("SELECT user_answer FROM review_history").fetchone()[0], "test")
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
