"""Standalone IDOR regression (the v1.1.4 fix), greppable by name.

Before v1.1.4, `_route_requires_admin` only exact-matched the `/reservations`
and `/inquiries` collection paths, so the per-id detail/PATCH routes
(`/reservations/{id}`, `/inquiries/{id}`) fell through UNAUTHENTICATED — leaking
customer PII (name/email/phone/flight/itinerary/dispatcher notes) and allowing
status tampering by id enumeration. v1.1.4 prefix-gates the sub-paths.

This pins the create->read-back attack scenario explicitly: an authenticated
admin creates a record (capturing its id), then an anonymous client attempts to
read/tamper it by id and is denied. The auth gate runs in middleware before the
row lookup, so denial is 401 (not 404) and no PII appears in the body. The
broader symmetric-auth contract is also held by
test_security.py::test_documented_gap_authentication_required_on_all_surfaces;
keep both.
"""

from __future__ import annotations

_VICTIM_EMAIL = "victim@example.com"


def _deauth(client):
    """Drop the conftest admin session so subsequent requests are anonymous."""
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)


def _make_reservation(client):
    r = client.post(
        "/reservations",
        json={
            "name": "IDOR Victim",
            "email": _VICTIM_EMAIL,
            "phone": "+19045559999",
            "pickup_label": "A",
            "dropoff_label": "B",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_inquiry(client):
    r = client.post(
        "/inquiries",
        json={
            "kind": "wedding",
            "name": "IDOR Victim",
            "email": _VICTIM_EMAIL,
            "phone": "+19045559999",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_idor_anonymous_cannot_read_reservation_detail(client):
    rid = _make_reservation(client)
    _deauth(client)
    r = client.get(f"/reservations/{rid}")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    assert _VICTIM_EMAIL not in r.text  # no PII leak in the denial body


def test_idor_anonymous_cannot_patch_reservation(client):
    rid = _make_reservation(client)
    _deauth(client)
    r = client.patch(f"/reservations/{rid}", json={"status": "declined"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


def test_idor_anonymous_cannot_read_inquiry_detail(client):
    iid = _make_inquiry(client)
    _deauth(client)
    r = client.get(f"/inquiries/{iid}")
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
    assert _VICTIM_EMAIL not in r.text


def test_idor_anonymous_cannot_patch_inquiry(client):
    iid = _make_inquiry(client)
    _deauth(client)
    r = client.patch(f"/inquiries/{iid}", json={"status": "closed"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


def test_idor_admin_can_read_back(client):
    """Positive control: the legitimate admin (conftest auth) CAN read the record."""
    rid = _make_reservation(client)
    r = client.get(f"/reservations/{rid}")
    assert r.status_code == 200, r.text
    assert r.json()["email"] == _VICTIM_EMAIL
