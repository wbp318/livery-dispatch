"""Tests for the admin_auth module — password hashing, session cookies, env config, login/logout routes."""

from __future__ import annotations

import os
import time

import pytest


def test_hash_round_trip():
    from admin_auth import hash_password, verify_password

    hashed = hash_password("coastal-dispatch-2026")
    assert hashed.startswith("$2b$")
    assert verify_password("coastal-dispatch-2026", hashed)


def test_hash_mismatch_rejects():
    from admin_auth import hash_password, verify_password

    hashed = hash_password("correct-password")
    assert not verify_password("wrong-password", hashed)


def test_session_cookie_round_trip():
    from admin_auth import create_session_cookie, verify_session_cookie

    cookie = create_session_cookie("dispatch")
    username = verify_session_cookie(cookie)
    assert username == "dispatch"


def test_session_cookie_rejects_tampered():
    from admin_auth import create_session_cookie, verify_session_cookie

    cookie = create_session_cookie("dispatch")
    parts = cookie.split(":")
    parts[2] = "a" * 64  # bogus signature
    assert verify_session_cookie(":".join(parts)) is None


def test_session_cookie_rejects_expired(monkeypatch):
    from admin_auth import create_session_cookie, verify_session_cookie

    cookie = create_session_cookie("dispatch")
    real_time = time.time()
    monkeypatch.setattr(time, "time", lambda: real_time + 90_000)
    assert verify_session_cookie(cookie) is None


def test_session_cookie_rejects_garbage():
    from admin_auth import verify_session_cookie

    assert verify_session_cookie("not-a-cookie") is None
    assert verify_session_cookie("") is None
    assert verify_session_cookie("a:b:c:d") is None


def test_authenticate_checks_env(monkeypatch):
    from admin_auth import authenticate, hash_password

    pw_hash = hash_password("secret123")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", pw_hash)
    assert authenticate("admin", "secret123")
    assert not authenticate("admin", "wrong")
    assert not authenticate("other", "secret123")


def test_authenticate_returns_false_when_env_missing(monkeypatch):
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    from admin_auth import authenticate

    assert not authenticate("admin", "anything")


def test_get_admin_credentials_raises_when_env_missing(monkeypatch):
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    from admin_auth import _get_admin_credentials

    with pytest.raises(RuntimeError, match="ADMIN_USERNAME"):
        _get_admin_credentials()


def test_csrf_token_round_trip():
    from admin_auth import (
        create_session_cookie,
        make_admin_csrf_token,
    )

    cookie = create_session_cookie("dispatch")
    token = make_admin_csrf_token(cookie)
    assert len(token) == 64  # SHA-256 hex


def test_csrf_token_differs_across_sessions(monkeypatch):
    from admin_auth import create_session_cookie, make_admin_csrf_token

    t = time.time()
    monkeypatch.setattr(time, "time", lambda: t)
    c1 = create_session_cookie("dispatch")
    monkeypatch.setattr(time, "time", lambda: t + 1)
    c2 = create_session_cookie("dispatch")
    assert make_admin_csrf_token(c1) != make_admin_csrf_token(c2)


# ---------------------------------------------------------------------------
# Login / logout route tests
# ---------------------------------------------------------------------------

_TEST_PASSWORD = "test-dispatch-pw"


@pytest.fixture()
def _admin_env(monkeypatch):
    from admin_auth import hash_password
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(_TEST_PASSWORD))


def test_login_page_renders(client):
    client.cookies.clear()
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


def test_login_happy_path(client, _admin_env):
    r = client.post(
        "/admin/login",
        data={"username": "admin", "password": _TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert "admin_session" in r.cookies


def test_login_bad_password(client, _admin_env):
    r = client.post(
        "/admin/login",
        data={"username": "admin", "password": "wrong"},
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


def test_login_missing_fields(client, _admin_env):
    r = client.post("/admin/login", data={"username": "", "password": ""})
    assert r.status_code == 400
    assert "required" in r.text.lower()


def test_logout_clears_cookie(client, _admin_env):
    client.post(
        "/admin/login",
        data={"username": "admin", "password": _TEST_PASSWORD},
    )
    r = client.post("/admin/logout", follow_redirects=False)
    assert r.status_code == 303
    assert "/admin/login" in r.headers["location"]


def test_login_redirects_if_already_authenticated(client, _admin_env):
    client.post(
        "/admin/login",
        data={"username": "admin", "password": _TEST_PASSWORD},
    )
    r = client.get("/admin/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"


# ---------------------------------------------------------------------------
# Auth middleware — unauthenticated access blocked
# ---------------------------------------------------------------------------


def test_unauthenticated_admin_html_redirects_to_login(client):
    client.cookies.clear()
    for path in ["/dashboard", "/admin/trips", "/admin/drivers", "/map"]:
        r = client.get(path, headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 307, f"{path} should redirect, got {r.status_code}"
        assert r.headers["location"] == "/admin/login"


def test_unauthenticated_api_returns_401(client):
    client.cookies.clear()
    for path in ["/trips", "/drivers", "/subcontractors"]:
        r = client.get(path)
        assert r.status_code == 401, f"GET {path} should 401, got {r.status_code}"


def test_public_endpoints_remain_open(client):
    client.cookies.clear()
    assert client.get("/health").status_code == 200
    r = client.post("/reservations", json={
        "name": "Test", "email": "t@t.com", "phone": "+19045550001",
        "pickup_label": "JAX", "dropoff_label": "Cloister",
    })
    assert r.status_code == 201


def test_driver_surface_unaffected_by_admin_auth(client):
    client.cookies.clear()
    r = client.get("/driver/login")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Admin CSRF protection
# ---------------------------------------------------------------------------


def test_csrf_token_rendered_in_admin_page(client):
    r = client.get("/admin/trips")
    assert r.status_code == 200
    assert 'name="csrf-token"' in r.text


def test_admin_post_without_csrf_returns_403(client):
    saved = client.headers.pop("X-CSRF-Token", None)
    try:
        r = client.post(
            "/admin/trips/999/action/cancel",
            follow_redirects=False,
        )
        assert r.status_code == 403
    finally:
        if saved:
            client.headers["X-CSRF-Token"] = saved


def test_admin_post_with_wrong_csrf_returns_403(client):
    saved = client.headers.pop("X-CSRF-Token", None)
    client.headers["X-CSRF-Token"] = "bogus" * 8
    try:
        r = client.post(
            "/admin/trips/999/action/cancel",
            follow_redirects=False,
        )
        assert r.status_code == 403
    finally:
        if saved:
            client.headers["X-CSRF-Token"] = saved


def test_admin_post_with_valid_csrf_passes(client, session):
    from models import Trip, TripStatus, VehicleClass
    from datetime import datetime, timezone
    t = Trip(
        passenger_name="CSRF Test",
        passenger_phone="+19045550099",
        pickup_label="JAX", pickup_lat=30.49, pickup_lon=-81.69,
        dropoff_label="SSI", dropoff_lat=31.15, dropoff_lon=-81.39,
        pickup_time=datetime.now(timezone.utc),
        vehicle_class=VehicleClass.SUV,
        status=TripStatus.REQUESTED,
    )
    session.add(t)
    session.commit()
    r = client.post(
        f"/admin/trips/{t.id}/action/cancel",
        follow_redirects=False,
    )
    assert r.status_code == 303
