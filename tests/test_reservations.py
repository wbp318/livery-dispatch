"""Tests for the customer-side reservation flow.

Covers the public POST /reservations endpoint (called from the marketing
site reserve form), dispatcher-side review and lifecycle transitions, and
the honeypot path that quarantines obvious bot submissions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _form_payload(**overrides) -> dict:
    """Mirrors what the site reserve form sends — separate date + time, text
    addresses, no coordinates."""
    pickup_dt = datetime.now(timezone.utc) + timedelta(days=14)
    base = {
        "name": "Mr. Whitfield",
        "email": "whitfield@example.com",
        "phone": "+19045556789",
        "pickup_label": "SAV Airport, baggage claim",
        "dropoff_label": "The Cloister, Sea Island",
        "pickup_date": pickup_dt.strftime("%Y-%m-%d"),
        "pickup_time_str": pickup_dt.strftime("%H:%M"),
        "pickup_at": pickup_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "passengers": "4",
        "vehicle_pref": "escalade-esv",
        "flight": "DL2473",
        "notes": "Inside-terminal meet-and-greet",
        "source": "site/index.html#reserve",
    }
    base.update(overrides)
    return base


def test_create_reservation_minimum_fields(client):
    payload = {
        "name": "Mrs Cole",
        "email": "cole@example.com",
        "phone": "+19045550100",
        "pickup_label": "JAX",
        "dropoff_label": "Amelia Island",
    }
    r = client.post("/reservations", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending_review"
    assert body["name"] == "Mrs Cole"
    assert body["pickup_label"] == "JAX"
    assert body["dropoff_label"] == "Amelia Island"
    assert body["vehicle_class"] is None
    assert body["linked_trip_id"] is None


def test_create_reservation_full_form_payload(client):
    r = client.post("/reservations", json=_form_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending_review"
    assert body["passengers"] == "4"
    assert body["vehicle_pref"] == "escalade-esv"
    # Vehicle pref is normalized to the dispatch enum for downstream filters.
    assert body["vehicle_class"] == "suv"
    assert body["flight"] == "DL2473"
    assert body["pickup_at"] is not None


def test_vehicle_pref_sprinter_maps_to_sprinter_class(client):
    r = client.post(
        "/reservations",
        json=_form_payload(vehicle_pref="sprinter-limited", passengers="10–13"),
    )
    assert r.status_code == 201
    assert r.json()["vehicle_class"] == "sprinter"


def test_vehicle_pref_multi_vehicle_leaves_class_null(client):
    """Multi-vehicle / wedding inquiries can't be auto-classified — the
    dispatcher resolves whether it's two Escalades, a Sprinter, or both."""
    r = client.post(
        "/reservations",
        json=_form_payload(vehicle_pref="multi-vehicle", passengers="14+"),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["vehicle_pref"] == "multi-vehicle"
    assert body["vehicle_class"] is None


def test_honeypot_quarantines_as_spam(client):
    """A non-empty bot_field marks the row as SPAM and 201s silently — telling
    a bot it tripped the trap just trains it. The reservation is still
    persisted so the dispatcher can audit the spam stream if curious."""
    r = client.post("/reservations", json=_form_payload(bot_field="https://buyviagra.example"))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "spam"


def test_create_rejects_missing_required_fields(client):
    r = client.post(
        "/reservations",
        json={"name": "X", "email": "x@example.com", "phone": "+1"},  # no pickup/dropoff
    )
    assert r.status_code == 422


def test_list_reservations_orders_newest_first(client):
    a = client.post("/reservations", json=_form_payload(name="Alpha")).json()
    b = client.post("/reservations", json=_form_payload(name="Bravo")).json()
    r = client.get("/reservations")
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["id"] == b["id"]
    assert rows[1]["id"] == a["id"]


def test_list_reservations_filter_by_status(client):
    a = client.post("/reservations", json=_form_payload(name="Alpha")).json()
    client.post("/reservations", json=_form_payload(name="Bravo", bot_field="x"))  # spam

    r = client.get("/reservations", params={"status": "pending_review"})
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert a["id"] in ids
    assert all(row["status"] == "pending_review" for row in r.json())

    r = client.get("/reservations", params={"status": "spam"})
    assert all(row["status"] == "spam" for row in r.json())


def test_get_reservation_404(client):
    r = client.get("/reservations/9999")
    assert r.status_code == 404


def test_dispatcher_confirms_reservation(client):
    res = client.post("/reservations", json=_form_payload()).json()
    r = client.patch(
        f"/reservations/{res['id']}",
        json={"status": "confirmed", "dispatcher_notes": "Phoned guest, locked Escalade."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["dispatcher_notes"] == "Phoned guest, locked Escalade."


def test_dispatcher_declines_reservation(client):
    res = client.post("/reservations", json=_form_payload()).json()
    r = client.patch(
        f"/reservations/{res['id']}",
        json={"status": "declined", "dispatcher_notes": "Fleet committed to RSM Sunday."},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "declined"


def test_promote_reservation_to_trip(client):
    """Dispatcher promotes a reservation to a real Trip after geocoding the
    addresses and confirming the guest details. The reservation gets
    linked_trip_id and status=converted; the Trip is independent and goes
    through the normal dispatch flow."""
    res = client.post("/reservations", json=_form_payload()).json()

    pickup_time = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
    trip = client.post(
        "/trips",
        json={
            "passenger_name": res["name"],
            "passenger_phone": res["phone"],
            "pickup_label": res["pickup_label"],
            "pickup_lat": 32.1276,
            "pickup_lon": -81.2021,
            "dropoff_label": res["dropoff_label"],
            "dropoff_lat": 31.1969,
            "dropoff_lon": -81.3438,
            "pickup_time": pickup_time,
            "vehicle_class": res["vehicle_class"] or "suv",
            "notes": res["notes"],
        },
    ).json()

    r = client.patch(
        f"/reservations/{res['id']}",
        json={"status": "converted", "linked_trip_id": trip["id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "converted"
    assert body["linked_trip_id"] == trip["id"]


def test_link_trip_id_must_exist(client):
    res = client.post("/reservations", json=_form_payload()).json()
    r = client.patch(
        f"/reservations/{res['id']}",
        json={"linked_trip_id": 9999},
    )
    assert r.status_code == 409


def test_patch_404_for_unknown_reservation(client):
    r = client.patch("/reservations/9999", json={"status": "confirmed"})
    assert r.status_code == 404


def test_oversized_input_rejected_by_field_limits(client):
    """Field-length caps are the single line of defense between the public
    form and the database — an unbounded notes field would let anyone fill
    SQLite with megabytes per request."""
    r = client.post(
        "/reservations",
        json=_form_payload(notes="x" * 5000),
    )
    assert r.status_code == 422


def test_cors_preflight_allowed_for_browser_post(client):
    """The site posts to /reservations from a browser — CORS preflight must
    return 200 with permissive headers under the default `*` origin."""
    r = client.options(
        "/reservations",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in {k.lower() for k in r.headers.keys()}


# ---------------------------------------------------------------------------
# Promote-to-Trip shortcut (4.6e #3 — landed v0.9.22)
# ---------------------------------------------------------------------------


def test_promote_form_renders_prefilled_from_reservation(client):
    res = client.post("/reservations", json=_form_payload()).json()
    r = client.get(f"/admin/reservations/{res['id']}/promote")
    assert r.status_code == 200
    body = r.text
    # Passenger details + addresses + notes carry over.
    assert _form_payload()["name"] in body
    assert _form_payload()["phone"] in body
    assert _form_payload()["pickup_label"] in body
    assert _form_payload()["dropoff_label"] in body
    # Coords must be empty (text-only reservations don't have them).
    assert 'name="pickup_lat" required' in body
    assert 'name="dropoff_lon" required' in body
    # Vehicle pref pre-selects the normalized class.
    assert '<option value="suv" selected>SUV</option>' in body


def test_promote_submit_creates_trip_links_and_converts(client):
    res = client.post("/reservations", json=_form_payload()).json()
    when = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M")
    r = client.post(
        f"/admin/reservations/{res['id']}/promote",
        data={
            "passenger_name": "Mr. Whitfield",
            "passenger_phone": "+19045556789",
            "pickup_label": "SAV Airport, baggage claim",
            "pickup_lat": "32.1276",
            "pickup_lon": "-81.2021",
            "dropoff_label": "The Cloister, Sea Island",
            "dropoff_lat": "31.1969",
            "dropoff_lon": "-81.3438",
            "pickup_time": when,
            "vehicle_class": "suv",
            "notes": "Inside-terminal meet-and-greet",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/trips/")
    # Reservation should now be linked + converted.
    after = client.get(f"/reservations/{res['id']}").json()
    assert after["status"] == "converted"
    assert after["linked_trip_id"] is not None
    # The linked trip is a real row with the submitted coords.
    trip = client.get(f"/trips/{after['linked_trip_id']}").json()
    assert trip["pickup_lat"] == 32.1276
    assert trip["dropoff_lat"] == 31.1969
    assert trip["passenger_name"] == "Mr. Whitfield"
    assert trip["status"] == "requested"


def test_promote_blocked_when_already_converted(client):
    """Once a reservation has been promoted, the form must reject re-promotion
    so we don't fork two trips from one inquiry."""
    res = client.post("/reservations", json=_form_payload()).json()
    when = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M")
    first = client.post(
        f"/admin/reservations/{res['id']}/promote",
        data={
            "passenger_name": "Mr. Whitfield", "passenger_phone": "+19045556789",
            "pickup_label": "SAV", "pickup_lat": "32.13", "pickup_lon": "-81.20",
            "dropoff_label": "Cloister", "dropoff_lat": "31.20", "dropoff_lon": "-81.34",
            "pickup_time": when, "vehicle_class": "suv",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303
    again = client.get(f"/admin/reservations/{res['id']}/promote")
    assert again.status_code == 409


def test_promote_button_appears_on_reservation_detail_page(client):
    res = client.post("/reservations", json=_form_payload()).json()
    r = client.get(f"/admin/reservations/{res['id']}")
    assert r.status_code == 200
    assert f'/admin/reservations/{res["id"]}/promote' in r.text
    assert 'Promote to Trip' in r.text


def test_reservation_patch_to_converted_without_trip_rejected(client):
    """A reservation reaches CONVERTED only via the promote action (one
    reservation → at most one trip). A bare PATCH to CONVERTED with no backing
    trip is rejected so the row can't be stranded (shows converted, has no trip,
    can never be promoted)."""
    res = client.post("/reservations", json=_form_payload()).json()
    resp = client.patch(f"/reservations/{res['id']}", json={"status": "converted"})
    assert resp.status_code == 409
    assert client.get(f"/reservations/{res['id']}").json()["status"] == "pending_review"


def test_reservation_edit_form_to_converted_without_trip_rejected(client):
    """Same guard on the admin edit-form path."""
    res = client.post("/reservations", json=_form_payload()).json()
    resp = client.post(
        f"/admin/reservations/{res['id']}/edit",
        data={"status": "converted"},
        follow_redirects=False,
    )
    assert resp.status_code == 409
