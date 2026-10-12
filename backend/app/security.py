"""Password hashing (scrypt) and signed session tokens (JWT, HS256). Pure functions - no DB access."""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import jwt

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def make_token(secret: str, username: str, customer_id: str, ttl: int = 8 * 3600) -> str:
    now = int(time.time())
    return jwt.encode({"sub": username, "cid": customer_id, "iat": now, "exp": now + ttl}, secret, algorithm="HS256")


def read_token(secret: str, token: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])  # raises jwt.PyJWTError on bad/expired token
