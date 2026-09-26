from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .database import connect, row_to_dict
from .security import (
    DEFAULT_TIMEZONE,
    SESSION_COOKIE,
    SESSION_TTL_DAYS,
    hash_password,
    hash_token,
    new_session_token,
    session_expiry_iso,
    utc_now_iso,
    validate_timezone,
    verify_password,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,32}$"

# Set REGISTRATION_CODE to require an invite code when signing up. Leaving it
# unset means anyone who can reach the server can create an account; setting it
# to a value nobody else knows effectively closes registration.
REGISTRATION_CODE = os.environ.get("REGISTRATION_CODE", "")

# Send the session cookie only over HTTPS. Enable this once the server sits
# behind TLS; it would break a plain-HTTP setup.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"}

# Compared against when the username does not exist, so a missing account costs
# the same time as a wrong password and cannot be told apart from one.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class RegisterInput(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    registration_code: str | None = Field(default=None, max_length=128)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class ProfileInput(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)


class PasswordInput(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """The account fields safe to hand back to the browser."""
    return {"id": user["id"], "username": user["username"], "timezone": user["timezone"]}


def get_current_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    with connect() as conn:
        row = conn.execute(
            """
            SELECT u.*
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (hash_token(token), utc_now_iso()),
        ).fetchone()

    user = row_to_dict(row)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )


def _start_session(conn, user_id: int, response: Response) -> None:
    # Clearing expired rows on sign-in keeps the table from growing without
    # needing a scheduled job.
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now_iso(),))
    token = new_session_token()
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES(?, ?, ?, ?)",
        (hash_token(token), user_id, utc_now_iso(), session_expiry_iso()),
    )
    _set_session_cookie(response, token)


@router.post("/register")
def register(payload: RegisterInput, response: Response) -> dict[str, Any]:
    if REGISTRATION_CODE and payload.registration_code != REGISTRATION_CODE:
        raise HTTPException(status_code=403, detail="Invalid registration code")

    # Rejected at sign-up rather than silently degrading every due date later.
    timezone_name = payload.timezone or DEFAULT_TIMEZONE
    try:
        validate_timezone(timezone_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (payload.username,)
        ).fetchone()
        if existing is not None:
            raise HTTPException(status_code=409, detail="Username already taken")

        cursor = conn.execute(
            "INSERT INTO users(username, password_hash, timezone, created_at) VALUES(?, ?, ?, ?)",
            (payload.username, hash_password(payload.password), timezone_name, utc_now_iso()),
        )
        user_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO user_vocabulary_lists(user_id, list_id, selected)
            SELECT ?, list_id, default_selected FROM vocabulary_lists
            """,
            (user_id,),
        )
        _start_session(conn, user_id, response)
        user = row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    return {"user": public_user(user)}


@router.post("/login")
def login(payload: LoginInput, response: Response) -> dict[str, Any]:
    with connect() as conn:
        user = row_to_dict(
            conn.execute("SELECT * FROM users WHERE username = ?", (payload.username,)).fetchone()
        )
        if not verify_password(payload.password, user["password_hash"] if user else _DUMMY_HASH):
            raise HTTPException(status_code=401, detail="Incorrect username or password")
        _start_session(conn, int(user["id"]), response)

    return {"user": public_user(user)}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed out"}


@router.get("/me")
def me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return {"user": public_user(user)}


@router.patch("/me")
def update_profile(
    payload: ProfileInput,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        validate_timezone(payload.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with connect() as conn:
        conn.execute("UPDATE users SET timezone = ? WHERE id = ?", (payload.timezone, user["id"]))
        updated = row_to_dict(
            conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        )
    return {"user": public_user(updated)}


@router.patch("/password")
def change_password(
    payload: PasswordInput,
    response: Response,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    if not verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect password")

    with connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(payload.new_password), user["id"]),
        )
        # A password change should log out everywhere else, so every session is
        # dropped and a fresh one is issued to the browser making the change.
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        _start_session(conn, int(user["id"]), response)

    return {"status": "password changed"}
