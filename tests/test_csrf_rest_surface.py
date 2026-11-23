"""CSRF on the admin-gated REST surface (v1.1.13, follow-up F1).

Before v1.1.13 the admin CSRF check only fired on ``/admin/*`` paths, so the
REST mutations — ``/trips``, ``/drivers``, ``/subcontractors``, PATCH
``/reservations/{id}``, PATCH ``/inquiries/{id}`` — were session-gated but
token-free, leaning on SameSite=Lax + the JSON content-type preflight alone.
Admin auth is cookie-only, so every admin-gated mutation is a cookie-auth
mutation and now gets the same contract as the ``/admin/*`` forms: a token
bound to the session cookie, as ``X-CSRF-Token`` or a ``csrf_token`` field.

Public form POSTs (``/reservations``, ``/inquiries``, ``/quote``) are not
admin-gated and must stay token-free — the marketing site can't hold one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from admin_auth import COOKIE_NAME, create_session_cookie, make_admin_csrf_token


def _trip_payload() -> dict:
    pickup_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "passenger_name": "CSRF Probe",
        "passenger_phone": "+19045550177",
        "pickup_label": "JAX Terminal",
        "pickup_lat": 30.4941,
        "pickup_lon": -81.6878,
        "dropoff_label": "Cloister, Sea Island",
        "dropoff_lat": 31.1969,
        "dropoff_lon": -81.3438,
        "pickup_time": pickup_time,
        "vehicle_class": "suv",
    }


@pytest.fixture
def ids(client):
    """Create one of each record (with the conftest CSRF header), return ids."""
    driver = client.post(
        "/drivers", json={"name": "CSRF Driver", "phone": "+19125550170", "vehicle_classes": ["suv"]},
    )
    sub = client.post(
        "/subcontractors",
        json={"name": "CSRF Sub", "dispatch_phone": "+19125550171", "vehicle_classes": ["suv"]},
    )
    trip = client.post("/trips", json=_trip_payload())
    res = client.post("/reservations", json={
        "name": "CSRF Res", "email": "r@example.com", "phone": "+19045550172",
        "pickup_label": "JAX", "dropoff_label": "Cloister",
    })
    inq = client.post("/inquiries", json={
        "kind": "wedding", "name": "CSRF Inq", "email": "i@example.com", "phone": "+19045550173",
    })
    for r in (driver, sub, trip, res, inq):
        assert r.status_code == 201, r.text
    return {
        "driver": driver.json()["id"], "sub": sub.json()["id"], "trip": trip.json()["id"],
        "res": res.json()["id"], "inq": inq.json()["id"],
    }


# (method, path template, json body). Bodies are valid enough that a request
# which clears CSRF reaches the handler; the probes only care about 403 vs not.
_MUTATIONS = [
    ("POST", "/drivers", {"name": "New", "phone": "+19125550180", "vehicle_classes": ["suv"]}),
    ("PATCH", "/drivers/{driver}/status", {"status": "available", "lat": 31.15, "lon": -81.49}),
    ("POST", "/drivers/{driver}/location", {"lat": 31.15, "lon": -81.49}),
    ("POST", "/subcontractors", {"name": "Sub2", "dispatch_phone": "+19125550181", "vehicle_classes": ["suv"]}),
    ("PATCH", "/subcontractors/{sub}", {"active": False}),
    ("POST", "/trips", None),  # filled with _trip_payload() at call time
    ("POST", "/trips/{trip}/broadcast", None),
    ("POST", "/trips/{trip}/broadcast-overflow", None),
    ("POST", "/trips/{trip}/accept?driver_id={driver}", None),
    ("POST", "/trips/{trip}/decline?driver_id={driver}", None),
    ("POST", "/trips/{trip}/sub-accept?subcontractor_id={sub}", None),
    ("POST", "/trips/{trip}/sub-decline?subcontractor_id={sub}", None),
    ("PATCH", "/trips/{trip}/status", {"status": "cancelled"}),
    ("PATCH", "/reservations/{res}", {"status": "spam"}),
    ("PATCH", "/inquiries/{inq}", {"dispatcher_notes": "probe"}),
]
_IDS = [f"{m} {p.split('?')[0]}" for m, p, _ in _MUTATIONS]


def _send(client, method, path_tmpl, body, ids, headers=None):
    path = path_tmpl.format(**ids)
    if path == "/trips" and body is None:
        body = _trip_payload()
    return client.request(method, path, json=body, headers=headers or {})


@pytest.mark.parametrize("method,path,body", _MUTATIONS, ids=_IDS)
def test_rest_mutation_without_csrf_token_is_403(client, ids, method, path, body):
    client.headers.pop("X-CSRF-Token", None)
    r = _send(client, method, path, body, ids)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Missing CSRF token"


@pytest.mark.parametrize("method,path,body", _MUTATIONS, ids=_IDS)
def test_rest_mutation_with_wrong_csrf_token_is_403(client, ids, method, path, body):
    client.headers.pop("X-CSRF-Token", None)
    r = _send(client, method, path, body, ids, headers={"X-CSRF-Token": "0" * 64})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Invalid CSRF token"


@pytest.mark.parametrize("method,path,body", _MUTATIONS, ids=_IDS)
def test_rest_mutation_with_valid_csrf_token_clears_the_check(client, ids, method, path, body):
    # conftest's client already carries the session-bound header.
    r = _send(client, method, path, body, ids)
    assert r.status_code != 403, r.text


def test_token_minted_for_another_session_is_rejected(client, ids):
    """The token is bound to the session cookie value, so a token lifted from
    (or minted for) a different session can't be replayed against this one."""
    client.headers["X-CSRF-Token"] = make_admin_csrf_token("admin:9999999999:not-this-session")
    r = client.post("/trips", json=_trip_payload())
    assert r.status_code == 403
    assert r.json()["detail"] == "Invalid CSRF token"


def test_header_name_is_case_insensitive(client, ids):
    token = client.headers.pop("X-CSRF-Token")
    r = client.post("/trips", json=_trip_payload(), headers={"x-csrf-token": token})
    assert r.status_code == 201, r.text


def test_form_body_csrf_token_clears_the_check_on_rest_routes(client, ids):
    """Parity with /admin/* forms: a csrf_token form field is accepted in lieu
    of the header. The handler itself wants JSON, so it 422s — but past CSRF."""
    token = client.headers.pop("X-CSRF-Token")
    r = client.patch(f"/trips/{ids['trip']}/status", data={"csrf_token": token, "status": "cancelled"})
    assert r.status_code not in (401, 403), r.text
    r = client.patch(f"/trips/{ids['trip']}/status", data={"csrf_token": "bogus", "status": "cancelled"})
    assert r.status_code == 403


def test_rejected_request_has_no_side_effect(client, ids):
    before = len(client.get("/trips").json())
    client.headers.pop("X-CSRF-Token", None)
    assert client.post("/trips", json=_trip_payload()).status_code == 403
    assert len(client.get("/trips").json()) == before


def test_reads_stay_token_free(client, ids):
    client.headers.pop("X-CSRF-Token", None)
    for path in ("/trips", "/drivers", "/subcontractors", f"/reservations/{ids['res']}",
                 f"/inquiries/{ids['inq']}", "/reservations", "/inquiries"):
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("with_admin_cookie", [True, False])
def test_public_form_posts_stay_token_free(client, with_admin_cookie):
    """The marketing site posts these cross-origin with no token. A dispatcher
    who also happens to hold an admin cookie must not break them either."""
    client.headers.pop("X-CSRF-Token", None)
    if not with_admin_cookie:
        client.cookies.clear()
    r = client.post("/reservations", json={
        "name": "Public", "email": "p@example.com", "phone": "+19045550174",
        "pickup_label": "JAX", "dropoff_label": "Cloister",
    })
    assert r.status_code == 201, r.text
    r = client.post("/inquiries", json={
        "kind": "wedding", "name": "Public", "email": "p@example.com", "phone": "+19045550175",
    })
    assert r.status_code == 201, r.text
    r = client.post("/quote", json={"pickup_label": "JAX", "dropoff_label": "The Cloister",
                                    "vehicle_class": "suv"})
    assert r.status_code not in (401, 403), r.text


def test_unauthenticated_mutation_is_401_not_403(client):
    """Auth runs before CSRF: no session → 401, never a CSRF message that
    would confirm the route exists behind a token check."""
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    r = client.post("/trips", json=_trip_payload())
    assert r.status_code == 401


def test_map_page_embeds_session_bound_csrf_meta(client):
    html = client.get("/map").text
    cookie = client.cookies.get(COOKIE_NAME)
    expected = make_admin_csrf_token(cookie)
    assert f'<meta name="csrf-token" content="{expected}" />' in html
    # Both write fetches echo it back.
    assert "'X-CSRF-Token': csrfToken()" in html
    assert html.count("csrfToken()") >= 2
    assert "__CSRF_META__" not in html


def test_map_page_without_token_renders_no_meta():
    import map_view
    html = map_view.render_map_page()
    assert '<meta name="csrf-token"' not in html
    assert "__CSRF_META__" not in html


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("POST", "/trips", True),
        ("PATCH", "/trips/1/status", True),
        ("PUT", "/drivers/1", True),
        ("DELETE", "/subcontractors/1", True),
        ("PATCH", "/reservations/1", True),
        ("PATCH", "/inquiries/1", True),
        ("POST", "/admin/trips/1/edit", True),
        ("GET", "/trips", False),
        ("GET", "/map", False),
        ("POST", "/reservations", False),
        ("POST", "/inquiries", False),
        ("POST", "/quote", False),
        ("POST", "/admin/login", False),
        ("POST", "/admin/logout", False),
        ("POST", "/driver/location", False),
        ("POST", "/webhooks/twilio/sms", False),
        ("POST", "/health", False),
    ],
)
def test_csrf_required_predicate(method, path, expected):
    from app import _csrf_required
    assert _csrf_required(method, path) is expected


def test_fresh_session_mints_fresh_token(client):
    """Logging in again (new cookie) invalidates tokens from the old cookie."""
    old_token = client.headers["X-CSRF-Token"]
    import time
    time.sleep(1.01)  # cookie embeds expiry seconds → force a distinct value
    new_cookie = create_session_cookie("admin")
    client.cookies.set(COOKIE_NAME, new_cookie)
    r = client.post("/trips", json=_trip_payload())
    assert r.status_code == 403
    client.headers["X-CSRF-Token"] = make_admin_csrf_token(new_cookie)
    assert client.post("/trips", json=_trip_payload()).status_code == 201
    assert old_token != client.headers["X-CSRF-Token"]
