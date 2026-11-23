"""End-to-end API tests using FastAPI's TestClient."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _trip_payload(vehicle_class: str = "suv") -> dict:
    pickup_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "passenger_name": "Mr Whitfield",
        "passenger_phone": "+19045556789",
        "pickup_label": "JAX Terminal",
        "pickup_lat": 30.4941,
        "pickup_lon": -81.6878,
        "dropoff_label": "Cloister, Sea Island",
        "dropoff_lat": 31.1969,
        "dropoff_lon": -81.3438,
        "pickup_time": pickup_time,
        "vehicle_class": vehicle_class,
        "notes": "Inside-terminal meet-and-greet",
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_driver_create_list_status(client):
    r = client.post(
        "/drivers",
        json={"name": "Marcus", "phone": "+19125550101", "vehicle_classes": ["suv", "sprinter"]},
    )
    assert r.status_code == 201
    driver_id = r.json()["id"]

    r = client.patch(
        f"/drivers/{driver_id}/status",
        json={"status": "available", "lat": 31.15, "lon": -81.49},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "available"
    assert body["lat"] == 31.15
    assert body["lon"] == -81.49

    r = client.get("/drivers")
    assert r.status_code == 200
    assert any(d["id"] == driver_id for d in r.json())


def test_duplicate_driver_phone_409(client):
    p = {"name": "A", "phone": "+19125550999", "vehicle_classes": ["suv"]}
    assert client.post("/drivers", json=p).status_code == 201
    assert client.post("/drivers", json=p).status_code == 409


def test_full_dispatch_round_trip(client):
    # Two available SUV drivers near Brunswick
    d1 = client.post("/drivers", json={"name": "D1", "phone": "+19125551001", "vehicle_classes": ["suv"]}).json()
    d2 = client.post("/drivers", json={"name": "D2", "phone": "+19125551002", "vehicle_classes": ["suv"]}).json()
    client.patch(f"/drivers/{d1['id']}/status", json={"status": "available", "lat": 31.15, "lon": -81.49})
    client.patch(f"/drivers/{d2['id']}/status", json={"status": "available", "lat": 31.16, "lon": -81.50})

    trip = client.post("/trips", json=_trip_payload()).json()
    assert trip["status"] == "requested"

    r = client.post(f"/trips/{trip['id']}/broadcast")
    assert r.status_code == 200
    broadcasts = r.json()
    assert len(broadcasts) == 2

    r = client.post(f"/trips/{trip['id']}/accept", params={"driver_id": d1["id"]})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["assigned_driver_id"] == d1["id"]

    # Second accept should 409 — first wins
    r = client.post(f"/trips/{trip['id']}/accept", params={"driver_id": d2["id"]})
    assert r.status_code == 409

    # Status progression
    for status in ("en_route_pickup", "on_scene", "in_transit"):
        r = client.patch(f"/trips/{trip['id']}/status", json={"status": status})
        assert r.status_code == 200
        assert r.json()["status"] == status

    r = client.patch(f"/trips/{trip['id']}/status", json={"status": "completed", "fare_cents": 32500})
    assert r.status_code == 200
    assert r.json()["fare_cents"] == 32500

    # Driver returns to AVAILABLE
    r = client.get(f"/drivers/{d1['id']}")
    assert r.json()["status"] == "available"


def test_broadcast_with_no_drivers_409(client):
    # Sprinter-only trip but only an SUV driver registered
    d = client.post("/drivers", json={"name": "SUV-only", "phone": "+19125552001", "vehicle_classes": ["suv"]}).json()
    client.patch(f"/drivers/{d['id']}/status", json={"status": "available", "lat": 31.15, "lon": -81.49})

    trip = client.post("/trips", json=_trip_payload(vehicle_class="sprinter")).json()
    r = client.post(f"/trips/{trip['id']}/broadcast")
    assert r.status_code == 409
    # Trip should be marked NO_DRIVERS
    r = client.get(f"/trips/{trip['id']}")
    assert r.json()["status"] == "no_drivers"


def test_filter_trips_by_status(client):
    p = _trip_payload()
    a = client.post("/trips", json=p).json()
    b = client.post("/trips", json=p).json()
    assert a["id"] != b["id"]
    r = client.get("/trips", params={"status": "requested"})
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()]
    assert a["id"] in ids and b["id"] in ids


def test_create_trip_normalizes_passenger_phone(client):
    """Regression: passenger_phone must be E.164-normalized on POST /trips so the
    Twilio caller-ID match (Trip.passenger_phone == inbound From) fires for repeat
    callers whose number was entered with human separators."""
    payload = _trip_payload()
    payload["passenger_phone"] = "+1 (904) 555-6789"
    r = client.post("/trips", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["passenger_phone"] == "+19045556789"


def test_create_trip_rejects_non_e164_phone(client):
    payload = _trip_payload()
    payload["passenger_phone"] = "not a phone"
    r = client.post("/trips", json=payload)
    assert r.status_code == 422


def test_create_trip_coerces_bare_us_phone(client):
    """A guest who types a bare US number (no +1) should still be accepted —
    the corridor is US-only and the reserve form doesn't force E.164. Both the
    10-digit and 1-prefixed 11-digit shapes coerce to E.164."""
    for raw, expected in (
        ("(912) 555-0100", "+19125550100"),
        ("912-555-0100", "+19125550100"),
        ("1 904 555 6789", "+19045556789"),
    ):
        payload = _trip_payload()
        payload["passenger_phone"] = raw
        r = client.post("/trips", json=payload)
        assert r.status_code == 201, r.text
        assert r.json()["passenger_phone"] == expected
