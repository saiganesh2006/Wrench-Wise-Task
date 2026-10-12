"""Authentication: username/password login with lockout, JWT sessions."""
from __future__ import annotations

import os
import secrets
import time

import jwt

from . import db, security

MAX_FAILED = 5
LOCK_SECONDS = 300
_DUMMY = security.hash_password("dummy-password-for-timing")


class AuthError(Exception):
    pass


def secret() -> str:
    s = os.getenv("JWT_SECRET") or db.kv_get("jwt_secret")
    if not s:
        s = secrets.token_hex(32)
        db.kv_set("jwt_secret", s)
    return s


def login(username: str, password: str) -> dict:
    username = (username or "").strip().lower()
    u = db.q1("SELECT * FROM users WHERE username=?", (username,))
    if u and u["locked_until"] > time.time():
        mins = int((u["locked_until"] - time.time()) // 60) + 1
        raise AuthError(f"Too many failed attempts. Account locked for about {mins} more minute(s).")
    ok = security.verify_password(password or "", u["password_hash"] if u else _DUMMY)
    if not u or not ok:
        if u:
            fails = u["failed_attempts"] + 1
            if fails >= MAX_FAILED:
                db.x("UPDATE users SET failed_attempts=0, locked_until=? WHERE user_id=?", (time.time() + LOCK_SECONDS, u["user_id"]))
            else:
                db.x("UPDATE users SET failed_attempts=? WHERE user_id=?", (fails, u["user_id"]))
        raise AuthError("Invalid username or password.")
    db.x("UPDATE users SET failed_attempts=0, locked_until=0, last_login=datetime('now') WHERE user_id=?", (u["user_id"],))
    cust = db.get_customer(u["customer_id"])
    return {"token": security.make_token(secret(), u["username"], u["customer_id"]),
            "user": {"username": u["username"], "customer_id": u["customer_id"], "name": cust["name"]}}


def verify_token(token: str) -> dict:
    try:
        claims = security.read_token(secret(), token)
    except jwt.PyJWTError:
        raise AuthError("Session expired or invalid. Please sign in again.")
    u = db.q1("SELECT username, customer_id FROM users WHERE username=?", (claims["sub"],))
    if not u:
        raise AuthError("Unknown user.")
    return u


def check_password(username: str, password: str) -> bool:
    """Step-up authentication (e.g. to approve a transfer)."""
    u = db.q1("SELECT password_hash FROM users WHERE username=?", (username,))
    return bool(u) and security.verify_password(password or "", u["password_hash"])
