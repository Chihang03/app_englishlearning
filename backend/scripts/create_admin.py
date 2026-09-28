#!/usr/bin/env python3
"""Provision the reserved account from the server console, never via public signup."""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import DB_PATH, connect
from app.security import DEFAULT_TIMEZONE, hash_password, utc_now_iso


def create_admin(password: str) -> bool:
    if not password or len(password) > 128:
        raise ValueError("Password must contain 1–128 characters")
    if not DB_PATH.is_file():
        raise ValueError("Start the application to initialize its database first")
    with connect() as conn:
        # Serializes provisioning with public registration and other operators.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT role FROM users WHERE lower(username)='admin'").fetchall()
        if existing:
            if len(existing) == 1 and existing[0]["role"] == "admin":
                return False
            raise ValueError("A learner already uses this name; existing account was left unchanged")
        conn.execute("""INSERT INTO users(username,password_hash,timezone,created_at,role)
            VALUES('admin',?,?,?,'admin')""", (hash_password(password), DEFAULT_TIMEZONE, utc_now_iso()))
    return True


if __name__ == "__main__":
    password = getpass.getpass("Initial admin password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
    created = create_admin(password)
    print("Admin account created" if created else "Admin account already exists; password unchanged")
