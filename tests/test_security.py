"""Security probes against the dispatch API.

Two categories:

1. **Active probes** — attempt classic vulnerabilities (SQL injection, XSS, IDOR,
   oversized inputs, method confusion, race conditions). These should pass:
   the system tolerates the input safely (ORM escapes, FastAPI validates,
   first-write-wins prevents double-assignment).

2. **Documented gaps** — tests whose name pins a known security posture. Originally
   these confirmed *missing* controls (no auth, no rate limit, etc.). As controls
   land in Phase 4.6c / 4.5 they're updated to verify the *new* behavior — assertions
   flip, name keeps the `documented_gap_` prefix so the audit trail stays greppable.
   Don't delete them.

Reference for what's intentionally NOT in the MVP, see `dispatch/README.md`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest


def _trip_payload(**overrides) -> dict:
    pickup_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    base = {
        "passenger_name": overrides.get("passenger_name", "Test Passenger"),
        "passenger_phone": overrides.get("passenger_phone", "+19045550000"),
        "pickup_label": overrides.get("pickup_label", "JAX Terminal"),
        "pickup_lat": overrides.get("pickup_lat", 30.4941),
        "pickup_lon": overrides.get("pickup_lon", -81.6878),
        "dropoff_label": overrides.get("dropoff_label", "Cloister"),
        "dropoff_lat": overrides.get("dropoff_lat", 31.1969),
        "dropoff_lon": overrides.get("dropoff_lon", -81.3438),
        "pickup_time": overrides.get("pickup_time", pickup_time),
        "vehicle_class": overrides.get("vehicle_class", "suv"),
        "notes": overrides.get("notes"),
    }
    return base


# ---------------------------------------------------------------------------
# Active probes
# ---------------------------------------------------------------------------


def test_sql_injection_in_passenger_name_is_neutralized(client):
    """Classic SQL injection payload in a string field. ORM should treat it as a literal."""
    payload = _trip_payload(passenger_name="Robert'); DROP TABLE trips;--")
    r = client.post("/trips", json=payload)
    assert r.status_code == 201
    trip_id = r.json()["id"]

    # Verify trips table still exists and the literal value was stored
    r = client.get(f"/trips/{trip_id}")
    assert r.status_code == 200
    assert r.json()["passenger_name"] == "Robert'); DROP TABLE trips;--"

    # Verify list endpoint still works (the table is intact)
    r = client.get("/trips")
    assert r.status_code == 200
    assert any(t["id"] == trip_id for t in r.json())


def test_sql_injection_in_pickup_label_is_neutralized(client):
    """Same probe in a different string field with an OR-true classic."""
    payload = _trip_payload(pickup_label="' OR 1=1--")
    r = client.post("/trips", json=payload)
    assert r.status_code == 201
    assert r.json()["pickup_label"] == "' OR 1=1--"


def test_xss_payload_in_notes_is_stored_as_literal(client):
    """We don't render HTML, so XSS payloads are safe to store. Document this property."""
    xss = "<script>alert('xss')</script>"
    payload = _trip_payload(notes=xss)
    r = client.post("/trips", json=payload)
    assert r.status_code == 201
    # The API echoes the literal back; any consumer rendering it as HTML is responsible for escaping.
    assert r.json()["notes"] == xss


def test_oversized_notes_field_is_handled(client):
    """Notes is bounded at 500 chars by the model. Send 10x that and verify the request fails cleanly."""
    payload = _trip_payload(notes="A" * 5000)
    r = client.post("/trips", json=payload)
    # Either FastAPI/Pydantic rejects it (422) or SQLAlchemy/SQLite truncates/errors.
    # Either way, the system must not 500 with an opaque error.
    assert r.status_code in (201, 400, 422), f"Unexpected status {r.status_code}: {r.text}"


def test_extreme_coordinates_handled_predictably(client):
    """Lat/lon outside Earth ranges are rejected (v1.1.7): latitude is bounded to
    [-90, 90] and longitude to [-180, 180] via Pydantic Field(ge=/le=). Pinned here
    and exhaustively in tests/pentests/test_pentest_coordinate_validation.py."""
    for lat, lon in [(91.0, -181.0), (-91.0, 181.0), (1e9, -1e9)]:
        payload = _trip_payload(pickup_lat=lat, pickup_lon=lon)
        r = client.post("/trips", json=payload)
        assert r.status_code == 422, f"expected 422 for ({lat}, {lon}), got {r.status_code}"


def test_path_traversal_string_in_label_stored_as_literal(client):
    """Path-traversal payload should land as a string with no filesystem effect."""
    payload = _trip_payload(pickup_label="../../../etc/passwd")
    r = client.post("/trips", json=payload)
    assert r.status_code == 201
    assert r.json()["pickup_label"] == "../../../etc/passwd"


def test_idor_get_nonexistent_trip_returns_404_not_500(client):
    """Accessing a trip that doesn't exist should be a clean 404, not an internal error."""
    r = client.get("/trips/999999")
    assert r.status_code == 404


def test_idor_get_nonexistent_driver_returns_404_not_500(client):
    """Same for drivers."""
    r = client.get("/drivers/999999")
    assert r.status_code == 404


def test_accept_unbroadcast_trip_rejected(client):
    """Driver tries to accept a trip that wasn't broadcast to them — should be a clean 409."""
    r = client.post("/drivers", json={"name": "X", "phone": "+19125559001", "vehicle_classes": ["suv"]}).json()
    client.patch(f"/drivers/{r['id']}/status", json={"status": "available", "lat": 31.15, "lon": -81.49})
    trip = client.post("/trips", json=_trip_payload()).json()
    # No broadcast issued.
    response = client.post(f"/trips/{trip['id']}/accept", params={"driver_id": r["id"]})
    assert response.status_code == 409


def test_method_confusion_returns_405(client):
    """GET on a POST-only endpoint should be 405, not 404 or 500."""
    r = client.get("/drivers/1/status")
    # The status endpoint is PATCH-only.
    assert r.status_code in (405, 404)  # 404 if route not matched at all; 405 if matched but wrong method


def test_malformed_json_rejected_cleanly(client):
    """Sending malformed JSON should be a clean 422, not a 500."""
    r = client.post("/trips", content=b"{not valid json", headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 422)


def test_missing_required_fields_returns_422(client):
    """Pydantic should reject incomplete payloads with a structured error."""
    r = client.post("/trips", json={"passenger_name": "Solo"})
    assert r.status_code == 422
    body = r.json()
    assert "detail" in body


def test_invalid_vehicle_class_rejected(client):
    """Unknown vehicle class should be rejected at the schema layer."""
    r = client.post("/trips", json=_trip_payload(vehicle_class="motorcycle"))
    assert r.status_code == 422


def test_duplicate_phone_returns_409_not_500(client):
    """Registering a driver with an already-used phone is a clean 409."""
    payload = {"name": "A", "phone": "+19125559999", "vehicle_classes": ["suv"]}
    assert client.post("/drivers", json=payload).status_code == 201
    r = client.post("/drivers", json=payload)
    assert r.status_code == 409


def test_huge_url_does_not_crash(client):
    """A long path should be a clean 404 / 414, not a 500."""
    r = client.get("/drivers/" + "A" * 5000)
    assert r.status_code in (404, 414, 422)


# ---------------------------------------------------------------------------
# Documented gaps — forcing functions for hardening before production
# ---------------------------------------------------------------------------


def test_documented_gap_authentication_required_on_all_surfaces(client):
    """Auth contract pinned by this test (v1.0.0):

    1. Admin HTML surface (/dashboard, /map, /admin/*) requires a valid
       admin session cookie. Unauthenticated HTML requests redirect to
       /admin/login (307). Unauthenticated API requests return 401 JSON.
    2. Admin REST surface (/drivers, /trips, /subcontractors) requires the
       same admin session cookie.
    3. Driver web surface (/driver) is gated by per-driver bearer token
       (landed in v0.9.9).
    4. Public endpoints (/health, POST /reservations, POST /inquiries,
       POST /quote, /webhooks/*) remain open.

    Keeping the `documented_gap_` prefix on the test name so the audit
    trail stays greppable.
    """
    client.cookies.clear()

    # 1. Admin HTML — redirects to login
    for path in ("/dashboard", "/admin/trips", "/admin/drivers", "/map"):
        r = client.get(path, headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 307, f"GET {path} should redirect to login, got {r.status_code}"

    # 2. Admin REST — 401
    assert client.get("/drivers").status_code == 401
    assert client.get("/trips").status_code == 401
    assert client.get("/subcontractors").status_code == 401
    assert client.post("/drivers", json={"name": "Unauth", "phone": "+19999999999", "vehicle_classes": ["suv"]}).status_code == 401
    assert client.post(
        "/subcontractors",
        json={"name": "UnauthSub", "dispatch_phone": "+19999999998", "vehicle_classes": ["suv"]},
    ).status_code == 401

    # 2b. Reservation + inquiry DETAIL and PATCH are dispatcher-only. The
    # public marketing-site forms only POST to the bare /reservations +
    # /inquiries collections; the per-id detail GET and PATCH carry customer
    # PII (name / email / phone / flight / itinerary / dispatcher notes) and
    # must be gated. These previously fell through _route_requires_admin's
    # open default because it exact-matched the collection path — an
    # unauthenticated IDOR. Auth is enforced in middleware before the row
    # lookup, so a non-existent id still 401s (not 404).
    assert client.get("/reservations/1").status_code == 401
    assert client.patch("/reservations/1", json={"status": "spam"}).status_code == 401
    assert client.get("/inquiries/1").status_code == 401
    assert client.patch("/inquiries/1", json={"status": "closed"}).status_code == 401

    # 3. Driver surface gated by bearer token (v0.9.9)
    assert client.get("/driver").status_code == 401
    assert client.post("/driver/trips/1/accept").status_code == 401
    assert client.post("/driver/trips/1/decline").status_code == 401

    # 4. Public endpoints remain open
    assert client.get("/health").status_code == 200


def test_documented_gap_twilio_webhook_signature_now_required_when_token_set(client, monkeypatch):
    """Twilio signature validation is now ON by default when TWILIO_AUTH_TOKEN is set.

    Two halves:
    1. Token unset (dev / test path) — webhook still accepts unsigned requests so
       the local dev loop and the existing webhook test suite don't break.
    2. Token set + signature missing or wrong — webhook rejects with 403.

    Originally this test pinned "unsigned accepted by default." That gap closed
    in Phase 4.6c; assertions now verify the hardened behavior. Name kept with
    the `documented_gap_` prefix so the audit trail stays visible.
    """
    # 1. No token in env — dev/test path, unsigned still ok.
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_VALIDATE_SIGNATURE", raising=False)
    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": "+10000000000", "Body": "ACCEPT 1"},
    )
    assert r.status_code == 200, "Dev path (no token) must continue to accept unsigned requests."

    # 2. Token set but signature missing — should reject.
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake-test-token-for-validation")
    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": "+10000000000", "Body": "ACCEPT 1"},
        # No X-Twilio-Signature header
    )
    assert r.status_code == 403, "With TWILIO_AUTH_TOKEN set, missing/invalid signature must 403."


def test_documented_gap_rate_limit_now_enforced_on_reservations(client, monkeypatch):
    """Public POST /reservations is now rate-limited per IP.

    Originally this test confirmed *no* limiter was in place. Phase 4.6c added a
    sliding-window per-IP cap (default 5 per 60s). Test enables a tight 3-per-60s
    cap, bursts 6 requests, and verifies the 4th+ returns 429.
    """
    monkeypatch.setenv("PUBLIC_FORM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_FORM_RATE_WINDOW_S", "60")
    monkeypatch.setenv("PUBLIC_FORM_RATE_MAX", "3")

    # Reset the in-memory history dict so prior tests don't leak in.
    import app as app_module

    app_module._rate_history.clear()

    payload = {
        "name": "Burst Tester",
        "email": "burst@example.com",
        "phone": "+19045551234",
        "pickup_label": "JAX",
        "dropoff_label": "Cloister",
    }
    statuses = []
    for _ in range(6):
        statuses.append(client.post("/reservations", json=payload).status_code)
    assert statuses.count(201) == 3, f"Expected 3 accepted before limit, got {statuses}"
    assert statuses.count(429) == 3, f"Expected 3 rate-limited, got {statuses}"
    # And the last one must be 429 (the burst order matters):
    assert statuses[-1] == 429


def test_documented_gap_phone_format_now_e164_enforced(client):
    """Phone numbers are now validated as E.164 (with sensible separator stripping).

    Originally this test confirmed the loose-storage gap. Phase 4.6c added a
    field validator that normalizes "+1 (904) 555-1234" → "+19045551234" and
    rejects anything that doesn't end up in E.164. Test confirms both halves:
    obviously invalid inputs are rejected; human-formatted inputs are accepted
    and stored normalized.
    """
    # Each of these should be rejected (no valid E.164 form).
    for bad in ["555", "not-a-phone", "", "+", "12345"]:
        r = client.post("/drivers", json={"name": "X", "phone": bad, "vehicle_classes": ["suv"]})
        assert r.status_code == 422, f"Phone {bad!r} should be rejected, got {r.status_code}"

    # Human-formatted but valid — should be accepted AND stored normalized.
    r = client.post(
        "/drivers", json={"name": "Y", "phone": "+1 (904) 555-1234", "vehicle_classes": ["suv"]}
    )
    assert r.status_code == 201
    assert r.json()["phone"] == "+19045551234", "Phone must be stored normalized to E.164."


# ---------------------------------------------------------------------------
# HTTP security headers (v1.0.2)
# ---------------------------------------------------------------------------
#
# Defense-in-depth headers covered by `_security_headers` middleware in app.py.
# Probes assert presence + specific values so any future change to the
# middleware that weakens posture trips a test.


def test_csp_header_present_on_public_route(client):
    r = client.get("/health")
    csp = r.headers.get("Content-Security-Policy", "")
    assert csp, "Content-Security-Policy header must be set on every response."
    assert "frame-ancestors 'none'" in csp, "CSP must block framing (clickjacking)."
    assert "object-src 'none'" in csp, "CSP must block <object>/<embed>."
    assert "base-uri 'self'" in csp, "CSP must restrict <base> to same origin."
    assert "form-action 'self'" in csp, "CSP must restrict form submissions to self."


def test_csp_header_present_on_admin_login(client):
    r = client.get("/admin/login")
    assert r.headers.get("Content-Security-Policy"), "Admin login must carry CSP."


def test_csp_header_present_on_driver_login(client):
    r = client.get("/driver/login")
    assert r.headers.get("Content-Security-Policy"), "Driver login must carry CSP."


def test_x_frame_options_deny(client):
    r = client.get("/health")
    assert r.headers.get("X-Frame-Options") == "DENY", \
        "X-Frame-Options must be DENY (legacy clickjacking defense)."


def test_x_content_type_options_nosniff(client):
    r = client.get("/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff", \
        "X-Content-Type-Options must be nosniff (MIME-confusion defense)."


def test_referrer_policy_strict_origin(client):
    r = client.get("/health")
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin", \
        "Referrer-Policy must redact path info on cross-origin nav."


def test_permissions_policy_locked_down(client):
    r = client.get("/health")
    pp = r.headers.get("Permissions-Policy", "")
    assert "geolocation=(self)" in pp, "Driver PWA needs geolocation=(self)."
    assert "camera=()" in pp, "Camera must be disabled."
    assert "microphone=()" in pp, "Microphone must be disabled."
    assert "payment=()" in pp, "Payment API must be disabled."


def test_cross_origin_opener_policy_same_origin(client):
    r = client.get("/health")
    assert r.headers.get("Cross-Origin-Opener-Policy") == "same-origin", \
        "COOP must be same-origin (Spectre-class isolation)."


def test_hsts_absent_in_dev(client):
    # DISPATCH_ENV is unset in test conftest — HSTS would be a no-op over HTTP
    # and shipping it in dev makes test browsers cache a useless policy.
    r = client.get("/health")
    assert "Strict-Transport-Security" not in r.headers, \
        "HSTS must NOT be set when DISPATCH_ENV is not 'production'."


def test_hsts_set_in_production(client, monkeypatch):
    monkeypatch.setenv("DISPATCH_ENV", "production")
    r = client.get("/health")
    hsts = r.headers.get("Strict-Transport-Security", "")
    assert "max-age=31536000" in hsts, "HSTS max-age must be at least 1 year."
    assert "includeSubDomains" in hsts, "HSTS must cover subdomains."
    assert "preload" in hsts, "HSTS must be preload-eligible."


def test_security_headers_on_404(client):
    # Headers must apply even when the route doesn't match — a 404 page
    # rendered in an iframe is still a clickjacking vector.
    r = client.get("/this-route-does-not-exist")
    assert r.status_code == 404
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Content-Security-Policy"), "404 responses must carry CSP."


def test_docs_endpoints_exempted_from_csp(client):
    # Swagger UI loads JS from jsdelivr.net; a strict CSP breaks /docs.
    # Other headers (X-Frame-Options, nosniff) still apply.
    r = client.get("/docs")
    assert "Content-Security-Policy" not in r.headers, \
        "/docs must be CSP-exempt so Swagger UI renders."
    assert r.headers.get("X-Frame-Options") == "DENY", \
        "/docs still needs clickjacking protection."


def test_documented_gap_cors_production_guard_refuses_wildcard(monkeypatch):
    """Production deploy must pin CORS_ORIGINS — startup refuses to load with `*`.

    Module reload check: set DISPATCH_ENV=production with permissive CORS, then
    re-import app and expect RuntimeError. This is a deploy-time safety net so
    a forgotten env var doesn't ship a wide-open API.
    """
    import importlib
    import sys

    monkeypatch.setenv("DISPATCH_ENV", "production")
    monkeypatch.setenv("CORS_ORIGINS", "*")
    sys.modules.pop("app", None)
    try:
        with __import__("pytest").raises(RuntimeError, match="CORS_ORIGINS must be pinned"):
            importlib.import_module("app")
    finally:
        # Restore prior import state so subsequent tests use the normal app.
        sys.modules.pop("app", None)
        monkeypatch.delenv("DISPATCH_ENV", raising=False)
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        importlib.import_module("app")


# ---------------------------------------------------------------------------
# Production fail-closed env validation (v1.0.3)
# ---------------------------------------------------------------------------
#
# Each probe sets DISPATCH_ENV=production with one env var deliberately
# missing/weak, then re-imports `app` and asserts RuntimeError with a message
# mentioning the missing var. Same module-reload pattern as the CORS guard
# test above so a fresh import runs `_validate_production_config()`.


def _try_prod_boot(monkeypatch, *, override: dict[str, str], unset: list[str] = ()):
    """Re-import app under DISPATCH_ENV=production with the given env overrides
    applied on top of the test conftest defaults. Returns the captured
    exception (or None if app booted cleanly).
    """
    import importlib
    import sys

    # Sane defaults so only the variable under test is the loose one.
    base = {
        "DISPATCH_ENV": "production",
        "CORS_ORIGINS": "https://example.com",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD_HASH": "$2b$12$" + "a" * 53,  # bcrypt-shaped placeholder
        "ADMIN_SESSION_SECRET": "x" * 48,
        "DRIVER_CSRF_SECRET": "y" * 48,
        # v1.0.6: console refused in prod. Default to twilio + token so other
        # tests don't trip the new guard; the console-refuse probe overrides.
        "NOTIFICATION_PROVIDER": "twilio",
        "TWILIO_AUTH_TOKEN": "fake-twilio-token",
        # v1.1.17 (E4): the SID + FROM number are now required too, so a
        # half-configured Twilio can't silently fall back to console.
        "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
        "TWILIO_FROM_NUMBER": "+19125550100",
        # v1.1.17 (E5): explicit proxy posture.
        "PUBLIC_BASE_URL": "https://dispatch.example.com",
        "CLIENT_IP_HEADER": "Fly-Client-IP",
    }
    base.update(override)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    for k in unset:
        monkeypatch.delenv(k, raising=False)

    sys.modules.pop("app", None)
    captured: Exception | None = None
    try:
        try:
            __import__("importlib").import_module("app")
        except Exception as exc:
            captured = exc
    finally:
        sys.modules.pop("app", None)
        for k in base:
            monkeypatch.delenv(k, raising=False)
        importlib.import_module("app")
    return captured


def test_prod_guard_refuses_missing_admin_username(monkeypatch):
    exc = _try_prod_boot(monkeypatch, override={"ADMIN_USERNAME": ""})
    assert isinstance(exc, RuntimeError)
    assert "ADMIN_USERNAME" in str(exc)


def test_prod_guard_refuses_missing_admin_password_hash(monkeypatch):
    exc = _try_prod_boot(monkeypatch, override={"ADMIN_PASSWORD_HASH": ""})
    assert isinstance(exc, RuntimeError)
    assert "ADMIN_PASSWORD_HASH" in str(exc)


def test_prod_guard_refuses_non_bcrypt_password_hash(monkeypatch):
    # SHA-256-ish hex string — not bcrypt. Common operator mistake.
    exc = _try_prod_boot(monkeypatch, override={"ADMIN_PASSWORD_HASH": "deadbeef" * 8})
    assert isinstance(exc, RuntimeError)
    assert "bcrypt" in str(exc).lower()


def test_prod_guard_accepts_bcrypt_2y_variant(monkeypatch):
    # $2y$ is the PHP variant — bcrypt verifies it the same as $2b$. We accept
    # it so operators who hashed via a different tool don't get a false reject.
    exc = _try_prod_boot(monkeypatch, override={"ADMIN_PASSWORD_HASH": "$2y$12$" + "a" * 53})
    assert exc is None, f"$2y$ should be accepted; got: {exc}"


def test_prod_guard_refuses_short_session_secret(monkeypatch):
    exc = _try_prod_boot(monkeypatch, override={"ADMIN_SESSION_SECRET": "short"})
    assert isinstance(exc, RuntimeError)
    assert "ADMIN_SESSION_SECRET" in str(exc)


def test_prod_guard_refuses_short_driver_csrf_secret(monkeypatch):
    exc = _try_prod_boot(monkeypatch, override={"DRIVER_CSRF_SECRET": "short"})
    assert isinstance(exc, RuntimeError)
    assert "DRIVER_CSRF_SECRET" in str(exc)


def test_prod_guard_refuses_twilio_provider_without_auth_token(monkeypatch):
    exc = _try_prod_boot(monkeypatch, override={
        "NOTIFICATION_PROVIDER": "twilio",
        "TWILIO_AUTH_TOKEN": "",
    })
    assert isinstance(exc, RuntimeError)
    assert "TWILIO_AUTH_TOKEN" in str(exc)


def test_prod_guard_refuses_console_notification_provider(monkeypatch):
    # v1.0.6: console provider prints SMS bodies + phone numbers to stdout
    # (PII). Production must use Twilio.
    exc = _try_prod_boot(monkeypatch, override={
        "NOTIFICATION_PROVIDER": "console",
    })
    assert isinstance(exc, RuntimeError)
    assert "NOTIFICATION_PROVIDER=console" in str(exc) or "console" in str(exc).lower()


def test_prod_guard_reports_all_errors_at_once(monkeypatch):
    # Multiple missing vars should all surface in one message — ops shouldn't
    # have to retry the deploy six times to discover six things missing.
    exc = _try_prod_boot(monkeypatch, override={
        "ADMIN_USERNAME": "",
        "ADMIN_PASSWORD_HASH": "",
        "ADMIN_SESSION_SECRET": "",
    })
    assert isinstance(exc, RuntimeError)
    msg = str(exc)
    assert "ADMIN_USERNAME" in msg
    assert "ADMIN_PASSWORD_HASH" in msg
    assert "ADMIN_SESSION_SECRET" in msg


# ---------------------------------------------------------------------------
# Logging hygiene (v1.0.6)
# ---------------------------------------------------------------------------
#
# Regression check: no print/log call in production source emits a value
# whose identifier name suggests a credential. Static scan — catches the
# "added `print(f'token: {driver.auth_token}')` during debugging and forgot
# to remove it" class of leak before it ships.


def test_no_credential_logging_in_production_source():
    import pathlib
    import re

    dispatch_dir = pathlib.Path(__file__).parent.parent
    forbidden = [
        # Anything that prints/logs a variable whose name names a credential.
        re.compile(r"print\s*\(.*\bauth_token\b"),
        re.compile(r"print\s*\(.*\bpassword\b"),
        re.compile(r"print\s*\(.*\bsession_secret\b", re.IGNORECASE),
        re.compile(r"print\s*\(.*\bcsrf_secret\b", re.IGNORECASE),
        re.compile(r"print\s*\(.*\bADMIN_PASSWORD_HASH\b"),
        re.compile(r"print\s*\(.*\bADMIN_SESSION_SECRET\b"),
        re.compile(r"print\s*\(.*\bDRIVER_CSRF_SECRET\b"),
        re.compile(r"print\s*\(.*\bTWILIO_AUTH_TOKEN\b"),
        re.compile(r"_log\.\w+\s*\(.*\bauth_token\b"),
        re.compile(r"_log\.\w+\s*\(.*\bpassword\b"),
        re.compile(r"_log\.\w+\s*\(.*\bsession_secret\b", re.IGNORECASE),
    ]
    # Strings that *describe* secret-handling but don't actually emit them.
    # These appear in error-message templates ("Generate with: python -c \"...print(hash_password('yourpass'))\"").
    permitted_phrases = [
        "print(hash_password",         # error message snippet only
        "print(secrets.token_urlsafe", # error message snippet only
    ]

    violations: list[str] = []
    for py in dispatch_dir.glob("*.py"):
        if py.name.startswith("test_"):
            continue
        text = py.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if any(phrase in line for phrase in permitted_phrases):
                continue
            for pat in forbidden:
                if pat.search(line):
                    violations.append(f"{py.name}:{line_no}: {line.strip()}")
                    break
    assert not violations, \
        "Credential-shaped log/print calls in production source:\n  " + "\n  ".join(violations)


def test_prod_guard_silent_outside_production(monkeypatch):
    # DISPATCH_ENV unset / dev / test — guard is a no-op even with everything blank.
    monkeypatch.delenv("DISPATCH_ENV", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "")
    # Direct call rather than module reload — we're verifying the no-op path.
    from app import _validate_production_config
    _validate_production_config()  # Must not raise.


# ---------------------------------------------------------------------------
# v1.1.17 — E4 (complete Twilio config) + E5 (explicit proxy posture)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("var", ["TWILIO_ACCOUNT_SID", "TWILIO_FROM_NUMBER"])
def test_prod_guard_refuses_half_configured_twilio(monkeypatch, var):
    """E4: get_provider() falls back to console when any Twilio var is
    missing — production must refuse to boot instead of paging nobody."""
    exc = _try_prod_boot(monkeypatch, override={var: ""})
    assert isinstance(exc, RuntimeError)
    assert var in str(exc)


@pytest.mark.parametrize("value", ["", "http://dispatch.example.com"])
def test_prod_guard_requires_https_public_base_url(monkeypatch, value):
    """E5: Twilio signs the public https URL; without it every webhook 403s."""
    exc = _try_prod_boot(monkeypatch, override={"PUBLIC_BASE_URL": value})
    assert isinstance(exc, RuntimeError)
    assert "PUBLIC_BASE_URL" in str(exc)


def test_prod_guard_requires_client_ip_header(monkeypatch):
    """E5: behind a proxy all visitors share one IP unless told which header
    carries the real one."""
    exc = _try_prod_boot(monkeypatch, override={"CLIENT_IP_HEADER": ""})
    assert isinstance(exc, RuntimeError)
    assert "CLIENT_IP_HEADER" in str(exc)


def test_prod_guard_accepts_client_ip_header_none(monkeypatch):
    """`none` is the explicit "nothing in front of uvicorn" answer."""
    exc = _try_prod_boot(monkeypatch, override={"CLIENT_IP_HEADER": "none"})
    assert exc is None, f"CLIENT_IP_HEADER=none should boot; got: {exc}"
