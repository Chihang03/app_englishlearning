from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


logger = logging.getLogger(__name__)

# Timezone used for accounts that do not set one. Every "today" in the app — due
# dates, daily stats, the streak — is resolved in the account's own timezone, so
# a server running in UTC does not shift a user's day boundary.
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Asia/Shanghai")

SESSION_COOKIE = "cvt_session"
SESSION_TTL_DAYS = 30

# scrypt comes from the standard library, so authentication adds no dependency.
# The parameters are stored alongside the hash, which means they can be raised
# later without invalidating existing passwords.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(derived, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Sessions are stored hashed so a database leak does not hand out logins."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Return the named timezone, falling back to UTC if it is unavailable.

    Slim container images often ship without the tz database, and an account
    should not become unusable because of that.
    """
    for candidate in (name, DEFAULT_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Timezone %r is unavailable; falling back", candidate)
    return ZoneInfo("UTC") if _utc_available() else timezone.utc  # type: ignore[return-value]


def validate_timezone(name: str) -> None:
    """Raise ValueError for a timezone this machine cannot resolve.

    Unlike resolve_timezone this does not fall back, so bad input is rejected at
    the edge instead of silently shifting someone's day boundary. If the tz
    database is missing altogether every name would fail, so validation is
    skipped rather than making sign-up impossible.
    """
    if not _utc_available():
        return
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc


def _utc_available() -> bool:
    try:
        ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def utc_now_iso() -> str:
    """Timestamps are stored in UTC so they mean the same thing to every user."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_iso_from(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def today_in(tz) -> date:
    return datetime.now(tz).date()


def local_day_bounds(day: date, tz) -> tuple[str, str]:
    """UTC bounds of one local calendar day, as stored-timestamp strings.

    Stored timestamps all carry a +00:00 offset, so comparing them as strings is
    equivalent to comparing the instants.
    """
    start = datetime.combine(day, time.min, tzinfo=tz)
    return utc_iso_from(start), utc_iso_from(start + timedelta(days=1))


def session_expiry_iso() -> str:
    return utc_iso_from(datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS))
