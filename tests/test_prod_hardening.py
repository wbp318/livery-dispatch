"""Pre-production hardening (v1.1.17) — the E1–E5 gaps found by the v1.1.16
documentation audit.

E1  Security headers on short-circuit responses (middleware order).
E2  No interactive API docs in production.
E3  Broadcast refuses trips past the offer stage.
E4  Half-configured Twilio can't silently fall back to console  → test_security.py
E5  Proxy posture: PUBLIC_BASE_URL for Twilio signatures, CLIENT_IP_HEADER
    for rate-limit keys (guard probes for E4/E5 live in test_security.py
    next to the other fail-closed boot probes).
"""

from __future__ import annotations

import importlib
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import core
from models import Trip, TripBroadcast, TripStatus

_SECURITY_HEADERS = {
    "content-security-policy": "frame-ancestors 'none'",
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "cross-origin-opener-policy": "same-origin",
}


def _assert_security_headers(r):
    for name, needle in _SECURITY_HEADERS.items():
        assert name in r.headers, f"{name} missing on {r.status_code} {r.request.url}"
        assert needle in r.headers[name], (name, r.headers[name])


def _trip_payload() -> dict:
    pickup = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "passenger_name": "Hardening Probe", "passenger_phone": "+19045550199",
        "pickup_label": "JAX", "pickup_lat": 30.4941, "pickup_lon": -81.6878,
        "dropoff_label": "Cloister", "dropoff_lat": 31.1969, "dropoff_lon": -81.3438,
        "pickup_time": pickup, "vehicle_class": "suv",
    }


# ---------------------------------------------------------------------------
# E1 — security headers on every response, including middleware short-circuits
# ---------------------------------------------------------------------------


def test_security_headers_middleware_is_outermost():
    import app as app_module
    outermost = app_module.app.user_middleware[0]
    assert outermost.kwargs.get("dispatch") is app_module._security_headers, (
        "_security_headers must be registered last (outermost) so short-circuit "
        "responses from the auth/CSRF middlewares still get CSP / X-Frame-Options"
    )


def test_headers_on_401_from_auth_middleware(client):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    r = client.get("/trips")
    assert r.status_code == 401
    _assert_security_headers(r)


def test_headers_on_307_login_redirect(client):
    client.cookies.clear()
    r = client.get("/dashboard", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 307
    _assert_security_headers(r)


def test_headers_on_csrf_403_from_header_check(client):
    client.headers["X-CSRF-Token"] = "0" * 64
    r = client.post("/trips", json=_trip_payload())
    assert r.status_code == 403
    _assert_security_headers(r)


def test_headers_on_csrf_403_from_body_check(client):
    client.headers.pop("X-CSRF-Token", None)
    r = client.post("/trips", json=_trip_payload())
    assert r.status_code == 403 and r.json()["detail"] == "Missing CSRF token"
    _assert_security_headers(r)


def test_headers_on_429_rate_limit(client, monkeypatch):
    import app as app_module
    monkeypatch.setenv("OSRM_PROXY_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("OSRM_PROXY_RATE_MAX", "0")
    monkeypatch.setattr(app_module, "_osrm_rate_history", {})
    r = client.get("/osrm/route/v1/driving/-81.6879,30.4941;-81.3438,31.1969")
    assert r.status_code == 429
    _assert_security_headers(r)


def test_hsts_on_short_circuit_401_in_production(client, monkeypatch):
    monkeypatch.setenv("DISPATCH_ENV", "production")
    client.cookies.clear()
    r = client.get("/trips")
    assert r.status_code == 401
    assert "max-age=31536000" in r.headers["strict-transport-security"]


def test_csp_nonce_still_reaches_rendered_pages(client):
    """Moving the middleware outward must not break nonce propagation: the
    nonce in the CSP header must be the one stamped on the page's inline tags."""
    r = client.get("/map")
    assert r.status_code == 200
    nonce = re.search(r"'nonce-([^']+)'", r.headers["content-security-policy"]).group(1)
    assert f'nonce="{nonce}"' in r.text


# ---------------------------------------------------------------------------
# E2 — no /docs, /redoc, /openapi.json in production
# ---------------------------------------------------------------------------


def test_docs_available_outside_production(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


@pytest.fixture
def prod_app(monkeypatch):
    """Import a fresh `app` module under DISPATCH_ENV=production with a
    complete, guard-passing environment; restore the normal module after."""
    env = {
        "DISPATCH_ENV": "production",
        "CORS_ORIGINS": "https://example.com",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD_HASH": "$2b$12$" + "a" * 53,
        "ADMIN_SESSION_SECRET": "x" * 48,
        "DRIVER_CSRF_SECRET": "y" * 48,
        "NOTIFICATION_PROVIDER": "twilio",
        "TWILIO_AUTH_TOKEN": "fake-twilio-token",
        "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
        "TWILIO_FROM_NUMBER": "+19125550100",
        "PUBLIC_BASE_URL": "https://dispatch.example.com",
        "CLIENT_IP_HEADER": "Fly-Client-IP",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("app", None)
    try:
        yield importlib.import_module("app")
    finally:
        sys.modules.pop("app", None)
        for k in env:
            monkeypatch.delenv(k, raising=False)
        importlib.import_module("app")


def test_docs_disabled_in_production(prod_app):
    assert prod_app.app.docs_url is None
    assert prod_app.app.redoc_url is None
    assert prod_app.app.openapi_url is None
    c = TestClient(prod_app.app)  # no `with` → no lifespan / DB needed
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = c.get(path)
        assert r.status_code == 404, (path, r.status_code)
        _assert_security_headers(r)


# ---------------------------------------------------------------------------
# E3 — broadcast only from requested / broadcasting / no_drivers
# ---------------------------------------------------------------------------

_PAST_OFFER = [
    TripStatus.ACCEPTED, TripStatus.EN_ROUTE_PICKUP, TripStatus.ON_SCENE,
    TripStatus.IN_TRANSIT, TripStatus.COMPLETED, TripStatus.CANCELLED,
]


def test_broadcastable_set_matches_the_state_machine():
    """Broadcastable = may sit in or move to BROADCASTING per VALID_TRANSITIONS."""
    derived = {
        s for s, nxt in core.VALID_TRANSITIONS.items()
        if s == TripStatus.BROADCASTING or TripStatus.BROADCASTING in nxt
    }
    assert core.BROADCASTABLE_STATUSES == derived


def _make_trip_in(client, session, status: TripStatus) -> int:
    trip_id = client.post("/trips", json=_trip_payload()).json()["id"]
    trip = session.get(Trip, trip_id)
    trip.status = status
    session.commit()
    return trip_id


@pytest.mark.parametrize("status", _PAST_OFFER, ids=lambda s: s.value)
@pytest.mark.parametrize("route", ["broadcast", "broadcast-overflow"])
def test_rest_broadcast_refuses_trip_past_offer_stage(client, session, status, route):
    client.post("/drivers", json={"name": "Ready", "phone": "+19125550191", "vehicle_classes": ["suv"]})
    trip_id = _make_trip_in(client, session, status)
    r = client.post(f"/trips/{trip_id}/{route}")
    assert r.status_code == 409, r.text
    assert status.value in r.json()["detail"]
    session.expire_all()
    assert session.get(Trip, trip_id).status == status, "status must be untouched"
    assert session.query(TripBroadcast).filter_by(trip_id=trip_id).count() == 0


@pytest.mark.parametrize("route", ["broadcast", "broadcast-overflow"])
def test_admin_action_broadcast_refuses_with_flash(client, session, route):
    trip_id = _make_trip_in(client, session, TripStatus.ACCEPTED)
    r = client.post(f"/admin/trips/{trip_id}/action/{route}", follow_redirects=False)
    assert r.status_code == 303
    assert "Failed" in r.headers["location"]
    session.expire_all()
    assert session.get(Trip, trip_id).status == TripStatus.ACCEPTED


@pytest.mark.parametrize("status", [TripStatus.REQUESTED, TripStatus.BROADCASTING, TripStatus.NO_DRIVERS],
                         ids=lambda s: s.value)
def test_broadcast_still_allowed_from_offer_states(client, session, status):
    trip_id = _make_trip_in(client, session, status)
    trip = session.get(Trip, trip_id)
    core.broadcast_trip(session, trip)  # must not raise (may find no drivers)
    core.broadcast_to_subcontractors(session, trip)


def test_core_raises_before_sending_anything(client, session):
    sent = []

    class Spy:
        def send_sms(self, to, body):
            sent.append((to, body))

    trip_id = _make_trip_in(client, session, TripStatus.IN_TRANSIT)
    with pytest.raises(core.TripNotBroadcastable):
        core.broadcast_trip(session, session.get(Trip, trip_id), provider=Spy())
    assert sent == []


# ---------------------------------------------------------------------------
# E5 — client IP for rate limiting, public URL for Twilio signatures
# ---------------------------------------------------------------------------


def _req(headers=None, peer="10.0.0.9"):
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (peer, 1234),
    }
    return Request(scope)


@pytest.mark.parametrize("env,headers,expected", [
    (None, {"Fly-Client-IP": "203.0.113.7"}, "10.0.0.9"),            # unset → socket peer
    ("none", {"Fly-Client-IP": "203.0.113.7"}, "10.0.0.9"),          # explicit no-proxy
    ("Fly-Client-IP", {"Fly-Client-IP": "203.0.113.7"}, "203.0.113.7"),
    ("Fly-Client-IP", {}, "10.0.0.9"),                               # header absent → peer
    ("Fly-Client-IP", {"X-Forwarded-For": "1.2.3.4"}, "10.0.0.9"),   # spoof ignored
    ("X-Forwarded-For", {"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}, "203.0.113.7"),  # last hop wins
])
def test_client_ip(monkeypatch, env, headers, expected):
    from app import _client_ip
    if env is None:
        monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    else:
        monkeypatch.setenv("CLIENT_IP_HEADER", env)
    assert _client_ip(_req(headers)) == expected


def test_rate_limit_buckets_follow_client_ip_header(client, monkeypatch):
    """Behind the proxy every request comes from the same socket peer; with
    CLIENT_IP_HEADER set, two visitors get two buckets."""
    import app as app_module
    monkeypatch.setenv("CLIENT_IP_HEADER", "Fly-Client-IP")
    monkeypatch.setenv("OSRM_PROXY_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("OSRM_PROXY_RATE_MAX", "1")
    monkeypatch.setattr(app_module, "_osrm_rate_history", {})
    path = "/osrm/route/v1/driving/-81.6879,30.4941;-81.3438,31.1969?overview=false"
    a1 = client.get(path, headers={"Fly-Client-IP": "203.0.113.1"}).status_code
    a2 = client.get(path, headers={"Fly-Client-IP": "203.0.113.1"}).status_code
    b1 = client.get(path, headers={"Fly-Client-IP": "203.0.113.2"}).status_code
    assert a2 == 429 and a1 != 429 and b1 != 429


_TOKEN = "test-twilio-token-for-public-url-check"


def _sig(url, params):
    from twilio.request_validator import RequestValidator
    return RequestValidator(_TOKEN).compute_signature(url, params)


def _anon(client):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)


def test_twilio_signature_verified_against_public_base_url(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://dispatch.example.com/")
    _anon(client)
    params = {"From": "+19045550000", "Body": "hello"}
    good = _sig("https://dispatch.example.com/webhooks/twilio/sms", params)
    r = client.post("/webhooks/twilio/sms", data=params, headers={"X-Twilio-Signature": good})
    assert r.status_code == 200, r.text


def test_twilio_signature_for_internal_url_rejected_when_public_base_set(client, monkeypatch):
    """A signature over the proxy-internal http:// URL is not what Twilio
    sends, and must not validate once PUBLIC_BASE_URL pins the origin."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://dispatch.example.com")
    _anon(client)
    params = {"From": "+19045550000", "Body": "hello"}
    internal = _sig("http://testserver/webhooks/twilio/sms", params)
    r = client.post("/webhooks/twilio/sms", data=params, headers={"X-Twilio-Signature": internal})
    assert r.status_code == 403


def test_twilio_public_base_keeps_query_string(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://dispatch.example.com")
    _anon(client)
    params = {"CallSid": "CA_q_001"}
    url = "https://dispatch.example.com/webhooks/twilio/whisper?kind=driver&name=Ann"
    r = client.post("/webhooks/twilio/whisper?kind=driver&name=Ann", data=params,
                    headers={"X-Twilio-Signature": _sig(url, params)})
    assert r.status_code == 200, r.text
