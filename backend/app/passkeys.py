from __future__ import annotations

import ipaddress
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import parse_registration_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .auth import COOKIE_SECURE, _start_session, get_current_user, public_user
from .database import connect, row_to_dict
from .security import SESSION_COOKIE, hash_token, utc_iso_from, utc_now_iso, verify_password


router = APIRouter(prefix="/api/auth/passkeys", tags=["passkeys"])
RP_ID = os.environ.get("WEBAUTHN_RP_ID", "localhost").strip().lower()
RP_NAME = os.environ.get("WEBAUTHN_RP_NAME", "Context Vocabulary Trainer").strip()
ORIGINS = [value.strip() for value in os.environ.get(
    "WEBAUTHN_ORIGINS", "http://localhost:5173,http://localhost:8000"
).split(",") if value.strip()]
CHALLENGE_TTL_SECONDS = 300
MAX_PASSKEYS = 20
COOKIE_PATH = "/api/auth/passkeys"


def validate_configuration() -> None:
    # Never infer the RP or trusted origins from Host / forwarded headers.
    if not RP_ID or not RP_NAME or not ORIGINS:
        raise ValueError("WEBAUTHN_RP_ID, WEBAUTHN_RP_NAME and WEBAUTHN_ORIGINS must not be empty")
    try:
        ipaddress.ip_address(RP_ID)
    except ValueError:
        pass
    else:
        raise ValueError("WEBAUTHN_RP_ID must be a domain name, not an IP address")
    for origin in ORIGINS:
        parsed = urlsplit(origin)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.path or parsed.query or parsed.fragment or parsed.username is not None
                or parsed.password is not None or parsed.netloc != origin.split("://", 1)[1]
                or (parsed.scheme == "http" and host != "localhost")
                or not (host == RP_ID or host.endswith("." + RP_ID))):
            raise ValueError("WEBAUTHN_ORIGINS must be HTTPS origins within WEBAUTHN_RP_ID (HTTP localhost allowed)")
        # Forces invalid port numbers to fail at startup too.
        _ = parsed.port


validate_configuration()


class ManageInput(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)


class RegistrationInput(ManageInput):
    name: str = Field(default="我的通行密钥", min_length=1, max_length=64)


class CredentialInput(BaseModel):
    credential: dict[str, Any]


def trusted_origin(request: Request) -> str:
    origin = request.headers.get("origin", "")
    if origin not in ORIGINS:
        raise HTTPException(403, "当前站点未启用通行密钥，请使用密码登录或联系管理员配置 HTTPS 域名。")
    return origin


def confirm_password(payload: ManageInput, user: dict[str, Any]) -> None:
    if not verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(403, "当前密码不正确。")


def cookie_name(purpose: str) -> str:
    return "cvt_passkey_" + purpose


def store_challenge(
    response: Response, request: Request, purpose: str, challenge: bytes,
    user_id: int | None = None, name: str | None = None,
) -> None:
    token = secrets.token_urlsafe(32)
    origin = trusted_origin(request)
    session_hash = hash_token(request.cookies.get(SESSION_COOKIE, "")) if user_id else None
    with connect() as conn:
        conn.execute("DELETE FROM webauthn_challenges WHERE expires_at <= ?", (utc_now_iso(),))
        previous = request.cookies.get(cookie_name(purpose))
        if previous:
            conn.execute("DELETE FROM webauthn_challenges WHERE token_hash = ?", (hash_token(previous),))
        conn.execute(
            """INSERT INTO webauthn_challenges
               (token_hash, purpose, challenge, origin, user_id, session_hash, name, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (hash_token(token), purpose, challenge, origin, user_id, session_hash, name,
             utc_iso_from(datetime.now(timezone.utc) + timedelta(seconds=CHALLENGE_TTL_SECONDS))),
        )
    response.set_cookie(
        cookie_name(purpose), token, max_age=CHALLENGE_TTL_SECONDS,
        httponly=True, secure=COOKIE_SECURE, samesite="lax", path=COOKIE_PATH,
    )


def consume_challenge(request: Request, purpose: str, user_id: int | None = None) -> dict[str, Any]:
    origin = trusted_origin(request)
    token = request.cookies.get(cookie_name(purpose), "")
    # Commit consumption independently, even when verification subsequently fails.
    # BEGIN IMMEDIATE prevents two concurrent requests from consuming the same row.
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = row_to_dict(conn.execute(
            "SELECT * FROM webauthn_challenges WHERE token_hash = ?", (hash_token(token),)
        ).fetchone())
        conn.execute("DELETE FROM webauthn_challenges WHERE token_hash = ?", (hash_token(token),))
    if (row is None or row["purpose"] != purpose or row["expires_at"] <= utc_now_iso()
            or row["origin"] != origin or row["user_id"] != user_id
            or (user_id is not None and row["session_hash"] != hash_token(request.cookies.get(SESSION_COOKIE, "")))):
        raise HTTPException(400, "通行密钥请求已过期或失效，请重试。")
    return row


def check_client_data(credential: dict[str, Any]) -> None:
    if len(json.dumps(credential)) > 65536:
        raise ValueError("Credential too large")
    data = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
    # This app supports top-level pages only, not cross-origin iframe ceremonies.
    if data.get("crossOrigin", False) is not False or data.get("topOrigin") is not None:
        raise ValueError("Cross-origin credential")


def key_count(conn: sqlite3.Connection, user_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM passkeys WHERE user_id = ?", (user_id,)).fetchone()[0])


@router.get("/config")
def configuration() -> dict[str, Any]:
    return {"rp_id": RP_ID, "origins": ORIGINS}


@router.get("")
def list_passkeys(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with connect() as conn:
        keys = conn.execute(
            "SELECT id, name, created_at, last_used_at FROM passkeys WHERE user_id = ? ORDER BY id DESC",
            (user["id"],),
        ).fetchall()
    return {"passkeys": [dict(row) for row in keys]}


@router.post("/register/options")
def registration_options(
    payload: RegistrationInput, request: Request, response: Response,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    trusted_origin(request)
    confirm_password(payload, user)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "请输入通行密钥名称。")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if key_count(conn, user["id"]) >= MAX_PASSKEYS:
            raise HTTPException(400, "通行密钥数量已达上限，请先删除不再使用的密钥。")
        conn.execute("INSERT OR IGNORE INTO webauthn_users(user_id, user_handle) VALUES (?, ?)",
                     (user["id"], secrets.token_bytes(32)))
        handle = conn.execute("SELECT user_handle FROM webauthn_users WHERE user_id = ?", (user["id"],)).fetchone()[0]
        existing = conn.execute("SELECT credential_id FROM passkeys WHERE user_id = ?", (user["id"],)).fetchall()
    options = generate_registration_options(
        rp_id=RP_ID, rp_name=RP_NAME, user_id=handle, user_name=user["username"],
        timeout=120000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=row["credential_id"]) for row in existing],
    )
    store_challenge(response, request, "registration", options.challenge, user["id"], name)
    return {"options": json.loads(options_to_json(options))}


@router.post("/register/verify")
def registration_verify(
    payload: CredentialInput, request: Request, response: Response,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    challenge = consume_challenge(request, "registration", user["id"])
    try:
        check_client_data(payload.credential)
        verified = verify_registration_response(
            credential=payload.credential, expected_challenge=challenge["challenge"],
            expected_rp_id=RP_ID, expected_origin=challenge["origin"], require_user_verification=True,
        )
        parsed = parse_registration_credential_json(payload.credential)
    except (WebAuthnException, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(400, "通行密钥验证失败，请重新添加。") from exc
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if key_count(conn, user["id"]) >= MAX_PASSKEYS:
            raise HTTPException(400, "通行密钥数量已达上限。")
        try:
            conn.execute(
                """INSERT INTO passkeys
                   (user_id, credential_id, public_key, sign_count, device_type, backed_up,
                    transports, name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user["id"], verified.credential_id, verified.credential_public_key,
                 verified.sign_count, verified.credential_device_type.value,
                 int(verified.credential_backed_up),
                 json.dumps([transport.value for transport in (parsed.response.transports or [])]),
                 challenge["name"], utc_now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "此通行密钥已经添加，请使用其他密钥。") from exc
    response.delete_cookie(cookie_name("registration"), path=COOKIE_PATH)
    return {"status": "passkey added"}


@router.post("/login/options")
def authentication_options(request: Request, response: Response) -> dict[str, Any]:
    trusted_origin(request)
    options = generate_authentication_options(
        rp_id=RP_ID, timeout=120000, user_verification=UserVerificationRequirement.REQUIRED,
    )
    store_challenge(response, request, "authentication", options.challenge)
    return {"options": json.loads(options_to_json(options))}


@router.post("/login/verify")
def authentication_verify(payload: CredentialInput, request: Request, response: Response) -> dict[str, Any]:
    challenge = consume_challenge(request, "authentication")
    try:
        check_client_data(payload.credential)
        credential_id = base64url_to_bytes(payload.credential["id"])
        user_handle = base64url_to_bytes(payload.credential["response"]["userHandle"])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(400, "通行密钥登录失败，请重试或使用密码登录。") from exc
    with connect() as conn:
        # Serialize counter verification and update with deletion / other logins.
        conn.execute("BEGIN IMMEDIATE")
        key = conn.execute(
            """SELECT p.*, w.user_handle FROM passkeys p
               JOIN webauthn_users w ON w.user_id = p.user_id WHERE p.credential_id = ?""",
            (credential_id,),
        ).fetchone()
        if key is None or not secrets.compare_digest(user_handle, key["user_handle"]):
            raise HTTPException(400, "通行密钥登录失败，请重试或使用密码登录。")
        try:
            verified = verify_authentication_response(
                credential=payload.credential, expected_challenge=challenge["challenge"],
                expected_rp_id=RP_ID, expected_origin=challenge["origin"],
                credential_public_key=key["public_key"], credential_current_sign_count=key["sign_count"],
                require_user_verification=True,
            )
            if verified.credential_device_type.value != key["device_type"]:
                raise ValueError("Credential backup eligibility changed")
        except (WebAuthnException, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(400, "通行密钥登录失败，请重试或使用密码登录。") from exc
        conn.execute(
            "UPDATE passkeys SET sign_count = ?, backed_up = ?, last_used_at = ? WHERE id = ?",
            (verified.new_sign_count, int(verified.credential_backed_up), utc_now_iso(), key["id"]),
        )
        user = dict(conn.execute("SELECT * FROM users WHERE id = ?", (key["user_id"],)).fetchone())
        _start_session(conn, user["id"], response)
    response.delete_cookie(cookie_name("authentication"), path=COOKIE_PATH)
    return {"user": public_user(user)}


@router.delete("/{passkey_id}")
def delete_passkey(
    passkey_id: int, payload: ManageInput, request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    trusted_origin(request)
    confirm_password(payload, user)
    with connect() as conn:
        deleted = conn.execute("DELETE FROM passkeys WHERE id = ? AND user_id = ?", (passkey_id, user["id"]))
        if deleted.rowcount == 0:
            raise HTTPException(404, "通行密钥不存在。")
    return {"status": "passkey deleted"}
