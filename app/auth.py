"""Admin password (dashboard) and team API key (translation endpoints)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from .db import Database


def env_admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "").strip()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_hash(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    candidate = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, stored) and bool(digest_hex)


def setup_required(db: Database) -> bool:
    return not env_admin_password() and not db.get_setting("admin_password_hash")


def check_password(db: Database, password: str) -> bool:
    env_pw = env_admin_password()
    if env_pw:
        return hmac.compare_digest(password.encode(), env_pw.encode())
    stored = db.get_setting("admin_password_hash")
    return bool(stored) and verify_hash(password, stored)  # type: ignore[arg-type]


def ensure_secrets(db: Database) -> None:
    if not db.get_setting("team_key"):
        db.set_setting("team_key", new_team_key())
    if not db.get_setting("session_secret"):
        db.set_setting("session_secret", secrets.token_hex(32))


def new_team_key() -> str:
    return "gls_" + secrets.token_urlsafe(24)


def extract_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (
        request.headers.get("x-api-key", "")
        or request.query_params.get("token", "")
        or request.query_params.get("key", "")
    ).strip()


def require_team_key(request: Request, db: Database) -> None:
    token = extract_token(request)
    expected = db.get_setting("team_key") or ""
    if not token or not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=401,
            detail="Missing or wrong team key. Copy it from the Glossa dashboard → Connect.",
        )


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(status_code=401, detail="Please sign in.")


class LoginLimiter:
    """At most `limit` failed attempts per client per `window` seconds."""

    def __init__(self, limit: int = 8, window: float = 300.0):
        self.limit = limit
        self.window = window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str) -> deque[float]:
        q = self._fails[key]
        cutoff = time.time() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._trim(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._trim(key).append(time.time())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


LIMITER = LoginLimiter()
