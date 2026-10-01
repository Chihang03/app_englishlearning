"""Device subscriptions for the default 20:00 learning reminder."""
from __future__ import annotations

import base64
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .auth import get_learner_user
from .database import connect
from .security import SESSION_COOKIE, hash_token, utc_now_iso

router = APIRouter(prefix="/api/push", tags=["push"])


@lru_cache(maxsize=1)
def signing_key(path: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Web Push requires a P-256 signing key")
    return key


def push_config() -> dict[str, Any]:
    path = os.environ.get("WEB_PUSH_PRIVATE_KEY", "")
    subject = os.environ.get("WEB_PUSH_SUBJECT", "")
    origins = {value.strip().rstrip("/") for value in os.environ.get("WEB_PUSH_ORIGINS", "").split(",") if value.strip()}
    if not path or not subject or not origins:
        return {"enabled": False, "public_key": None}
    public = signing_key(path).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"enabled": True, "public_key": base64.urlsafe_b64encode(public).decode().rstrip("=")}


def require_origin(request: Request) -> str:
    origins = {value.strip().rstrip("/") for value in os.environ.get("WEB_PUSH_ORIGINS", "").split(",") if value.strip()}
    origin = request.headers.get("origin", "")
    if origin not in origins:
        raise HTTPException(403, "Notification origin is not allowed")
    return origin


def decode_key(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise ValueError("Invalid subscription key")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=80, max_length=100)
    auth: str = Field(min_length=20, max_length=30)

    @field_validator("p256dh")
    @classmethod
    def valid_public_key(cls, value: str) -> str:
        raw = decode_key(value)
        if len(raw) != 65 or raw[0] != 4:
            raise ValueError("Invalid P-256 subscription key")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        return value

    @field_validator("auth")
    @classmethod
    def valid_auth(cls, value: str) -> str:
        if len(decode_key(value)) != 16:
            raise ValueError("Invalid subscription authentication key")
        return value


class PushSubscriptionInput(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        url = urlsplit(value)
        host = url.hostname or ""
        allowed = (host.endswith(".push.apple.com")
                   or host in {"fcm.googleapis.com", "updates.push.services.mozilla.com"})
        if (url.scheme != "https" or not allowed or url.port not in (None, 443)
                or url.username or url.password or url.fragment or not url.path or "\\" in value):
            raise ValueError("Unsupported push service endpoint")
        return value


@router.get("/config")
def config(user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, Any]:
    return push_config()


@router.post("/subscription")
def subscribe(payload: PushSubscriptionInput, request: Request,
              user: dict[str, Any] = Depends(get_learner_user)) -> dict[str, str]:
    origin = require_origin(request)
    if not push_config()["enabled"]:
        raise HTTPException(503, "Learning reminders are unavailable")
    stamp = utc_now_iso()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        session_hash = hash_token(request.cookies[SESSION_COOKIE])
        if not conn.execute("SELECT 1 FROM sessions WHERE token_hash=? AND user_id=? AND expires_at>?",
                            (session_hash, user["id"], stamp)).fetchone():
            raise HTTPException(401, "Session expired")
        existing = conn.execute("SELECT * FROM push_subscriptions WHERE endpoint=?", (payload.endpoint,)).fetchone()
        if existing and (existing["p256dh"] != payload.keys.p256dh or existing["auth"] != payload.keys.auth):
            raise HTTPException(409, "Subscription keys do not match")
        if not existing or existing["user_id"] != user["id"]:
            count = conn.execute("SELECT COUNT(*) FROM push_subscriptions WHERE user_id=?", (user["id"],)).fetchone()[0]
            if count >= 10:
                raise HTTPException(409, "Too many notification devices")
        conn.execute("""INSERT INTO push_subscriptions
            (endpoint,user_id,session_hash,origin,p256dh,auth,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET
            user_id=excluded.user_id,session_hash=excluded.session_hash,
            origin=excluded.origin,updated_at=excluded.updated_at""",
            (payload.endpoint, user["id"], session_hash, origin,
             payload.keys.p256dh, payload.keys.auth, stamp, stamp))
    return {"status": "subscribed"}
