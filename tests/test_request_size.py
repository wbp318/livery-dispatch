"""Request-size cap (v1.4.1): `_RequestSizeLimitMiddleware` in app.py.

SECURITY.md asked for request-size limits at a reverse proxy; Fly's proxy
documents none, so the app enforces MAX_REQUEST_BYTES itself (default 32 KiB)
before anything reads or parses a body. These pin the contract: both ways a
body can arrive (with a Content-Length, or chunked without one), the security
headers on the 413, the order in the middleware stack, the env knob, and the
production boot guard.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app as app_module
from models import Reservation
from tests.test_security import _try_prod_boot

LIMIT = app_module.MAX_REQUEST_BYTES_DEFAULT


def _reservation(**overrides) -> dict:
    pickup = datetime.now(timezone.utc) + timedelta(days=14)
    body = {
        "name": "Mr. Whitfield",
        "email": "whitfield@example.com",
        "phone": "+19045556789",
        "pickup_label": "SAV Airport, baggage claim",
        "dropoff_label": "The Cloister, Sea Island",
        "pickup_date": pickup.strftime("%Y-%m-%d"),
        "pickup_time_str": pickup.strftime("%H:%M"),
        "pickup_at": pickup.strftime("%Y-%m-%dT%H:%M:%S"),
        "passengers": "4",
        "vehicle_pref": "escalade-esv",
        "notes": "Inside-terminal meet-and-greet",
        "source": "site/index.html#reserve",
    }
    body.update(overrides)
    return body


def _chunks(data: bytes, size: int = 1000):
    """A generator body: httpx sends it chunked, with no Content-Length."""
    for i in range(0, len(data), size):
        yield data[i:i + size]


def _count(session) -> int:
    return session.execute(select(func.count()).select_from(Reservation)).scalar_one()


def _assert_refused(r) -> None:
    assert r.status_code == 413, (r.status_code, r.text[:200])
    assert r.json() == {"detail": f"Request body too large (limit {LIMIT} bytes)"}
    assert r.headers["connection"] == "close"
    # Inside _security_headers, so the 413 carries the same headers as any page.
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


# ---------------------------------------------------------------------------
# The two ways a body arrives
# ---------------------------------------------------------------------------

def test_declared_length_over_the_cap_is_refused_unread(client, session):
    padded = json.dumps(_reservation(notes="x" * (LIMIT + 10))).encode()
    r = client.post("/reservations", content=padded, headers={"Content-Type": "application/json"})
    _assert_refused(r)
    assert _count(session) == 0


def test_chunked_body_over_the_cap_is_refused(client, session):
    padded = json.dumps(_reservation(notes="x" * (LIMIT + 10))).encode()
    r = client.post("/reservations", content=_chunks(padded),
                    headers={"Content-Type": "application/json"})
    _assert_refused(r)
    assert _count(session) == 0


def test_chunked_body_under_the_cap_reaches_the_route_intact(client, session):
    """The middleware reads a chunked body itself, then replays it: the route
    must see every byte, in order."""
    body = json.dumps(_reservation(name="Chunked Caller")).encode()
    r = client.post("/reservations", content=_chunks(body, size=7),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 201, r.text
    assert session.execute(select(Reservation.name)).scalar_one() == "Chunked Caller"


def test_a_body_exactly_at_the_cap_is_allowed(client):
    body = b"x" * LIMIT
    r = client.post("/reservations", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code != 413
    r = client.post("/reservations", content=_chunks(body), headers={"Content-Type": "application/json"})
    assert r.status_code != 413


def test_a_normal_booking_with_long_notes_is_well_under_the_cap(client, session):
    """The longest field a customer can fill (notes, 2,000 chars) leaves the
    request far below 32 KiB."""
    r = client.post("/reservations", json=_reservation(notes="n" * 2000))
    assert r.status_code == 201, r.text
    assert _count(session) == 1


def test_bodyless_requests_pass_through(client):
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Every surface, ahead of the auth and CSRF layers
# ---------------------------------------------------------------------------

def test_admin_form_over_the_cap_is_refused_before_csrf_parsing(client):
    big = "ids=1&csrf_token=" + "x" * (LIMIT + 10)
    r = client.post("/admin/reservations/bulk-spam", content=big.encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    _assert_refused(r)


def test_twilio_webhook_over_the_cap_is_refused(client):
    big = "From=%2B19045550000&Body=" + "x" * (LIMIT + 10)
    r = client.post("/webhooks/twilio/sms", content=big.encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    _assert_refused(r)


def test_cap_sits_just_inside_security_headers():
    stack = app_module.app.user_middleware
    assert stack[0].kwargs.get("dispatch") is app_module._security_headers
    assert stack[1].cls is app_module._RequestSizeLimitMiddleware, (
        "the cap must be second-outermost: inside _security_headers (so the 413 "
        "gets CSP) and outside everything that reads a body"
    )


# ---------------------------------------------------------------------------
# MAX_REQUEST_BYTES
# ---------------------------------------------------------------------------

def test_env_raises_the_cap_per_request(client, monkeypatch):
    body = b"x" * (LIMIT + 10)
    monkeypatch.setenv("MAX_REQUEST_BYTES", str(LIMIT * 2))
    r = client.post("/reservations", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 422          # reached the route; just isn't JSON
    monkeypatch.setenv("MAX_REQUEST_BYTES", "2048")
    r = client.post("/reservations", content=b"x" * 2049, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert "limit 2048 bytes" in r.text


def test_parse_max_request_bytes():
    parse = app_module._parse_max_request_bytes
    assert parse("65536") == 65536
    assert parse(" 64_000 ") == 64000
    assert parse("1024") == 1024
    for bad in ("1023", "0", "-5", "32k", "", "lots"):
        assert parse(bad) is None, bad


def test_an_invalid_value_falls_back_to_the_default_outside_production(monkeypatch):
    monkeypatch.setenv("MAX_REQUEST_BYTES", "32k")
    assert app_module._max_request_bytes() == LIMIT
    monkeypatch.delenv("MAX_REQUEST_BYTES")
    assert app_module._max_request_bytes() == LIMIT


def test_prod_guard_refuses_an_invalid_cap(monkeypatch):
    exc = _try_prod_boot(monkeypatch, override={"MAX_REQUEST_BYTES": "32k"})
    assert isinstance(exc, RuntimeError)
    assert "MAX_REQUEST_BYTES" in str(exc)


def test_prod_guard_accepts_a_valid_cap(monkeypatch):
    assert _try_prod_boot(monkeypatch, override={"MAX_REQUEST_BYTES": "65536"}) is None
