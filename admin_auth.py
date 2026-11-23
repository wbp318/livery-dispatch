"""Admin authentication — session-based, single-user, env-var credentials.

Shape: signed cookie holding ``{username}:{expiry_unix}:{hmac_signature}``.
No DB table, no itsdangerous dep — stdlib ``hmac`` + ``hashlib`` only.

Credential flow:
  1. ``ADMIN_USERNAME`` + ``ADMIN_PASSWORD_HASH`` (bcrypt) in env.
  2. ``POST /admin/login`` verifies password → sets signed session cookie.
  3. ``require_admin`` FastAPI dependency reads + verifies cookie on every
     admin route; returns 401 (redirect to login for HTML) on failure.
  4. ``POST /admin/logout`` clears the cookie.

Secret resolution mirrors the driver CSRF pattern: ``ADMIN_SESSION_SECRET``
env var in production; per-process random fallback in dev/test (tokens
don't survive restarts — acceptable for local work, not for production).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Optional

import bcrypt
from fastapi import HTTPException, Request

COOKIE_NAME = "admin_session"
SESSION_MAX_AGE = 60 * 60 * 24  # 24 hours


def _resolve_session_secret() -> bytes:
    val = os.getenv("ADMIN_SESSION_SECRET")
    if val:
        return val.encode("utf-8")
    import secrets as _secrets
    return _secrets.token_urlsafe(32).encode("utf-8")


_SESSION_SECRET = _resolve_session_secret()


_BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8")[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8")[:_BCRYPT_MAX_BYTES], hashed.encode("utf-8"))


def _get_admin_credentials() -> tuple[str, str]:
    username = os.getenv("ADMIN_USERNAME")
    pw_hash = os.getenv("ADMIN_PASSWORD_HASH")
    if not username or not pw_hash:
        raise RuntimeError(
            "ADMIN_USERNAME and ADMIN_PASSWORD_HASH must be set. "
            "Generate a hash with: python -c \"from admin_auth import hash_password; print(hash_password('yourpass'))\""
        )
    return username, pw_hash


def authenticate(username: str, password: str) -> bool:
    try:
        expected_user, pw_hash = _get_admin_credentials()
    except RuntimeError:
        return False
    if not hmac.compare_digest(username, expected_user):
        return False
    return verify_password(password, pw_hash)


def _sign(payload: str) -> str:
    return hmac.new(
        _SESSION_SECRET,
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_session_cookie(username: str) -> str:
    expires = int(time.time()) + SESSION_MAX_AGE
    payload = f"{username}:{expires}"
    sig = _sign(payload)
    return f"{payload}:{sig}"


def verify_session_cookie(cookie_value: str) -> Optional[str]:
    parts = cookie_value.split(":")
    if len(parts) != 3:
        return None
    username, expires_str, sig = parts
    try:
        expires = int(expires_str)
    except ValueError:
        return None
    if time.time() > expires:
        return None
    expected_sig = _sign(f"{username}:{expires_str}")
    if not hmac.compare_digest(sig, expected_sig):
        return None
    return username


def get_admin_user(request: Request) -> Optional[str]:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    return verify_session_cookie(cookie)


def require_admin(request: Request) -> str:
    user = get_admin_user(request)
    if user is None:
        if _wants_html(request):
            from fastapi.responses import RedirectResponse
            raise HTTPException(
                status_code=307,
                headers={"Location": "/admin/login"},
            )
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def make_admin_csrf_token(session_cookie: str) -> str:
    return hmac.new(
        _SESSION_SECRET,
        session_cookie.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_admin_csrf_token(request: Request, form_token: str | None) -> None:
    cookie = request.cookies.get(COOKIE_NAME) or ""
    header_token = request.headers.get("x-csrf-token") or request.headers.get("X-CSRF-Token")
    presented = (form_token or header_token or "").strip()
    if not presented:
        raise HTTPException(status_code=403, detail="Missing CSRF token")
    expected = make_admin_csrf_token(cookie)
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept
