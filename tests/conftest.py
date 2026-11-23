"""Shared pytest fixtures: in-memory SQLite engine, isolated session per test, FastAPI TestClient."""

from __future__ import annotations

import os
import sys

# Ensure the dispatch/ source files are importable when pytest is run from this directory.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Test environment: widen the broadcast radius so realistic corridor distances
# (Brunswick → SAV ≈ 65 mi) fit within the candidate filter. Production keeps
# the .env default of 50 mi.
os.environ.setdefault("BROADCAST_RADIUS_MI", "200")
os.environ.setdefault("NOTIFICATION_PROVIDER", "console")
# Public-form rate limiter (shared bucket for /reservations + /inquiries) is
# enforced in production but disabled by default in tests so the broader
# suite isn't blocked by global limiter state shared across tests. The
# rate-limit test in test_security.py enables it explicitly.
os.environ.setdefault("PUBLIC_FORM_RATE_LIMIT_ENABLED", "false")
# Login rate limiter (separate bucket, see app.py:_login_rate_limited) likewise
# off by default in tests — same state-bleed concern. Pentest probes that need
# to verify rate-limit behavior re-enable it via monkeypatch.
os.environ.setdefault("LOGIN_RATE_LIMIT_ENABLED", "false")
# OSRM route-proxy limiter (v1.1.15, own bucket) — same state-bleed concern;
# test_osrm_proxy.py re-enables it where it verifies the limit.
os.environ.setdefault("OSRM_PROXY_RATE_LIMIT_ENABLED", "false")
# Force the fare calculator to use haversine in tests — OSRM would add an
# external HTTP dependency to every fare-touching test, and the math we're
# verifying is rate-card arithmetic, not road geometry. The OSRM path is
# exercised explicitly in test_fare's OSRM tests via env override.
os.environ.setdefault("FARE_DISTANCE_PROVIDER", "haversine")
# Admin auth — set default credentials for the test suite so the admin
# auto-fixture can mint a valid session cookie. Individual tests that
# need "no auth" (e.g. test_security.py) monkeypatch these away.
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD_HASH",
    "$2b$12$6hLWwI/KHQRoDHIOFvDnzuYotzLKwFpiKxIr6gzjlG5ga6xD64ZUe")
# ↑ bcrypt hash of "test-admin-pw" — only used in tests.

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import database as db_module
from database import Base


@pytest.fixture(scope="function")
def engine():
    """Per-test in-memory SQLite engine; fully isolated.

    StaticPool is required for in-memory SQLite (`sqlite://`) — without it, every
    connection from the pool gets its own fresh empty database, so tables created
    in one connection are invisible to the next. StaticPool reuses a single
    connection across the whole engine, so the in-memory DB persists for the test.
    """
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="function")
def session(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(scope="function")
def client(engine, monkeypatch):
    """FastAPI TestClient bound to the per-test engine via dependency override."""
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    # Patch the module-level engine and SessionLocal so app.lifespan's init_db hits this engine.
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", Session)

    from app import app
    from database import get_session

    def override_get_session():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override_get_session
    try:
        with TestClient(app) as c:
            # Auto-authenticate as admin — most tests exercise admin routes.
            # Tests that explicitly verify unauthenticated behavior clear the
            # cookie themselves.
            from admin_auth import create_session_cookie, make_admin_csrf_token, COOKIE_NAME
            session_cookie = create_session_cookie("admin")
            c.cookies.set(COOKIE_NAME, session_cookie)
            # Set CSRF header so admin POST/PATCH/DELETE requests pass the
            # CSRF middleware without needing form-body tokens.
            c.headers["X-CSRF-Token"] = make_admin_csrf_token(session_cookie)
            yield c
    finally:
        app.dependency_overrides.clear()
