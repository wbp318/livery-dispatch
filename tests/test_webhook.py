"""Tests for the inbound Twilio SMS webhook.

Drivers and subcontractors both reply to broadcast SMSes with `ACCEPT N` or
`DECLINE N`. The webhook parses the body, matches the From phone to a Driver
or Subcontractor, and routes to the correct accept/decline function.

Signature validation is off by default (matches the documented-gap pattern in
test_security.py); enable in production via TWILIO_VALIDATE_SIGNATURE=true.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trip_payload(vehicle_class: str = "suv") -> dict:
    pickup_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "passenger_name": "Mr Whitfield",
        "passenger_phone": "+19045556789",
        "pickup_label": "JAX Terminal",
        "pickup_lat": 30.4941,
        "pickup_lon": -81.6878,
        "dropoff_label": "Cloister",
        "dropoff_lat": 31.1969,
        "dropoff_lon": -81.3438,
        "pickup_time": pickup_time,
        "vehicle_class": vehicle_class,
    }


def _register_driver(client, name: str, phone: str, lat: float, lon: float) -> dict:
    r = client.post("/drivers", json={"name": name, "phone": phone, "vehicle_classes": ["suv"]})
    assert r.status_code == 201, r.text
    driver = r.json()
    client.patch(f"/drivers/{driver['id']}/status", json={"status": "available", "lat": lat, "lon": lon})
    return driver


def _register_sub(client, name: str, phone: str, base_lat: float, base_lon: float) -> dict:
    r = client.post(
        "/subcontractors",
        json={
            "name": name,
            "dispatch_phone": phone,
            "vehicle_classes": ["suv"],
            "tier": 1,
            "markup_pct": 15.0,
            "base_lat": base_lat,
            "base_lon": base_lon,
            "service_radius_mi": 200.0,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Driver SMS reply
# ---------------------------------------------------------------------------


def test_webhook_driver_accept(client):
    driver = _register_driver(client, "Marcus", "+19125550101", 31.15, -81.49)
    trip = client.post("/trips", json=_trip_payload()).json()
    client.post(f"/trips/{trip['id']}/broadcast")

    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": "+19125550101", "Body": f"ACCEPT {trip['id']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] is True
    assert body["recipient_kind"] == "driver"
    assert body["recipient_id"] == driver["id"]
    assert body["action"] == "accept"
    assert body["trip_id"] == trip["id"]

    # Trip is now accepted by this driver
    state = client.get(f"/trips/{trip['id']}").json()
    assert state["status"] == "accepted"
    assert state["assigned_driver_id"] == driver["id"]


def test_webhook_driver_decline(client):
    d1 = _register_driver(client, "A", "+19125551001", 31.15, -81.49)
    d2 = _register_driver(client, "B", "+19125551002", 31.16, -81.49)
    trip = client.post("/trips", json=_trip_payload()).json()
    client.post(f"/trips/{trip['id']}/broadcast")

    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": d1["phone"], "Body": f"DECLINE {trip['id']}"},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "decline"

    # Other driver can still accept
    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": d2["phone"], "Body": f"accept {trip['id']}"},  # lowercase
    )
    assert r.status_code == 200
    assert r.json()["matched"] is True
    state = client.get(f"/trips/{trip['id']}").json()
    assert state["assigned_driver_id"] == d2["id"]


def test_webhook_second_driver_accept_returns_soft_failure(client):
    """First-accept-wins surfaced over SMS: when two drivers both reply ACCEPT on
    the same broadcast wave, the loser gets a 200 soft-failure (matched=False) so
    Twilio doesn't retry, and the trip stays assigned to the winner."""
    d1 = _register_driver(client, "First", "+19125552001", 31.15, -81.49)
    d2 = _register_driver(client, "Second", "+19125552002", 31.16, -81.49)
    trip = client.post("/trips", json=_trip_payload()).json()
    client.post(f"/trips/{trip['id']}/broadcast")

    win = client.post("/webhooks/twilio/sms",
                      data={"From": d1["phone"], "Body": f"ACCEPT {trip['id']}"})
    assert win.status_code == 200
    assert win.json()["matched"] is True
    assert client.get(f"/trips/{trip['id']}").json()["assigned_driver_id"] == d1["id"]

    lose = client.post("/webhooks/twilio/sms",
                       data={"From": d2["phone"], "Body": f"ACCEPT {trip['id']}"})
    assert lose.status_code == 200, lose.text  # 200 so Twilio doesn't retry
    body = lose.json()
    assert body["matched"] is False
    assert body["recipient_kind"] == "driver"
    assert body["recipient_id"] == d2["id"]
    assert body["action"] == "accept"
    # No double-assign — winner keeps the trip.
    assert client.get(f"/trips/{trip['id']}").json()["assigned_driver_id"] == d1["id"]


def test_webhook_second_sub_accept_returns_soft_failure(client):
    """Same first-accept-wins guarantee on the overflow (subcontractor) path."""
    s1 = _register_sub(client, "Sub One", "+19125553001", 31.15, -81.49)
    s2 = _register_sub(client, "Sub Two", "+19125553002", 31.16, -81.49)
    trip = client.post("/trips", json=_trip_payload()).json()
    client.post(f"/trips/{trip['id']}/broadcast-overflow")

    win = client.post("/webhooks/twilio/sms",
                      data={"From": s1["dispatch_phone"], "Body": f"ACCEPT {trip['id']}"})
    assert win.status_code == 200
    assert win.json()["matched"] is True
    assert client.get(f"/trips/{trip['id']}").json()["assigned_subcontractor_id"] == s1["id"]

    lose = client.post("/webhooks/twilio/sms",
                       data={"From": s2["dispatch_phone"], "Body": f"ACCEPT {trip['id']}"})
    assert lose.status_code == 200, lose.text
    body = lose.json()
    assert body["matched"] is False
    assert body["recipient_kind"] == "subcontractor"
    assert body["recipient_id"] == s2["id"]
    state = client.get(f"/trips/{trip['id']}").json()
    assert state["assigned_subcontractor_id"] == s1["id"]
    assert state["assigned_driver_id"] is None


def test_webhook_handles_punctuated_and_padded_replies(client):
    driver = _register_driver(client, "Marcus", "+19125550201", 31.15, -81.49)
    trip = client.post("/trips", json=_trip_payload()).json()
    client.post(f"/trips/{trip['id']}/broadcast")

    # "Accept-1" with hyphen, leading whitespace, mixed case
    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": driver["phone"], "Body": f"  Accept-{trip['id']}  "},
    )
    assert r.status_code == 200
    assert r.json()["matched"] is True
    assert r.json()["action"] == "accept"


# ---------------------------------------------------------------------------
# Subcontractor SMS reply
# ---------------------------------------------------------------------------


def test_webhook_subcontractor_accept(client):
    sub = _register_sub(client, "Coast", "+18435553001", 31.15, -81.49)
    trip = client.post("/trips", json=_trip_payload()).json()
    client.post(f"/trips/{trip['id']}/broadcast-overflow")

    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": "+18435553001", "Body": f"ACCEPT {trip['id']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] is True
    assert body["recipient_kind"] == "subcontractor"
    assert body["recipient_id"] == sub["id"]
    assert body["action"] == "accept"

    # Trip is now assigned to the sub (no quote was provided via SMS, so fare stays null)
    state = client.get(f"/trips/{trip['id']}").json()
    assert state["status"] == "accepted"
    assert state["assigned_subcontractor_id"] == sub["id"]
    assert state["assigned_driver_id"] is None
    assert state["fare_cents"] is None


def test_webhook_subcontractor_decline(client):
    sub = _register_sub(client, "Coast", "+18435553002", 31.15, -81.49)
    trip = client.post("/trips", json=_trip_payload()).json()
    client.post(f"/trips/{trip['id']}/broadcast-overflow")

    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": sub["dispatch_phone"], "Body": f"DECLINE {trip['id']}"},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "decline"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_webhook_unknown_sender_returns_unmatched(client):
    trip = client.post("/trips", json=_trip_payload()).json()
    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": "+10000000000", "Body": f"ACCEPT {trip['id']}"},
    )
    assert r.status_code == 200
    assert r.json()["matched"] is False
    assert r.json()["detail"] == "Sender not recognized"


def test_webhook_unrecognized_body_returns_unmatched(client):
    driver = _register_driver(client, "Marcus", "+19125559900", 31.15, -81.49)
    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": driver["phone"], "Body": "what is going on"},
    )
    assert r.status_code == 200
    assert r.json()["matched"] is False
    assert "Unrecognized" in r.json()["detail"]


def test_webhook_accept_for_unbroadcast_trip_returns_soft_failure(client):
    driver = _register_driver(client, "Marcus", "+19125559901", 31.15, -81.49)
    # No broadcast issued for this trip
    trip = client.post("/trips", json=_trip_payload()).json()

    r = client.post(
        "/webhooks/twilio/sms",
        data={"From": driver["phone"], "Body": f"ACCEPT {trip['id']}"},
    )
    # Soft failure: 200 with matched=False, so Twilio doesn't retry
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] is False
    assert body["recipient_kind"] == "driver"
    assert body["recipient_id"] == driver["id"]


def test_webhook_form_encoded_required_fields(client):
    # No body / no From — should be a clean 422, not a 500
    r = client.post("/webhooks/twilio/sms", data={})
    assert r.status_code == 422


def test_webhook_does_not_affect_existing_endpoints(client):
    # Smoke test — the new webhook shouldn't have broken anything obvious.
    assert client.get("/health").status_code == 200
    assert client.get("/drivers").status_code == 200
    assert client.get("/subcontractors").status_code == 200
    assert client.get("/trips").status_code == 200
