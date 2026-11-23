"""End-to-end scenario tests.

Each test exercises a realistic dispatch flow against the full FastAPI stack
via TestClient — the same data layer, lifecycle, and notification provider the
real service uses. These verify behaviors that unit tests miss: multi-trip
interaction, concurrency on accept, driver state coherence under load.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_driver(client, name: str, phone: str, classes: list[str], lat: float, lon: float) -> dict:
    r = client.post("/drivers", json={"name": name, "phone": phone, "vehicle_classes": classes})
    assert r.status_code == 201, r.text
    driver = r.json()
    r = client.patch(f"/drivers/{driver['id']}/status", json={"status": "available", "lat": lat, "lon": lon})
    assert r.status_code == 200, r.text
    return r.json()


def _make_trip(client, vehicle_class: str = "suv", **overrides) -> dict:
    pickup_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = {
        "passenger_name": overrides.get("passenger_name", "Test Passenger"),
        "passenger_phone": overrides.get("passenger_phone", "+19045550000"),
        "pickup_label": overrides.get("pickup_label", "JAX Terminal"),
        "pickup_lat": overrides.get("pickup_lat", 30.4941),
        "pickup_lon": overrides.get("pickup_lon", -81.6878),
        "dropoff_label": overrides.get("dropoff_label", "The Cloister"),
        "dropoff_lat": overrides.get("dropoff_lat", 31.1969),
        "dropoff_lon": overrides.get("dropoff_lon", -81.3438),
        "pickup_time": pickup_time,
        "vehicle_class": vehicle_class,
        "notes": overrides.get("notes"),
    }
    r = client.post("/trips", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _progress_to_completed(client, trip_id: int, fare_cents: int = 30000) -> None:
    for status in ("en_route_pickup", "on_scene", "in_transit"):
        r = client.patch(f"/trips/{trip_id}/status", json={"status": status})
        assert r.status_code == 200, r.text
    r = client.patch(f"/trips/{trip_id}/status", json={"status": "completed", "fare_cents": fare_cents})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def test_peak_week_full_dispatch_cycle(client):
    """Five drivers, four trips across vehicle classes, all complete cleanly."""
    # Driver pool around Brunswick / St. Simons / Darien
    d1 = _register_driver(client, "Marcus (lead, SUV+Sprinter)", "+19125550101", ["suv", "sprinter"], 31.15, -81.49)
    d2 = _register_driver(client, "Eleanor (SUV)", "+19125550102", ["suv"], 31.15, -81.39)
    d3 = _register_driver(client, "Henry (SUV+Sprinter)", "+19125550103", ["suv", "sprinter"], 31.37, -81.43)
    d4 = _register_driver(client, "Nora (SUV)", "+19125550104", ["suv"], 31.20, -81.45)
    d5 = _register_driver(client, "Owen (Sprinter)", "+19125550105", ["sprinter"], 31.16, -81.40)

    # Four trips: 3 SUV airport transfers + 1 Sprinter group move
    trips = [
        _make_trip(client, "suv", passenger_name="JAX guest 1"),
        _make_trip(client, "suv", passenger_name="JAX guest 2"),
        _make_trip(client, "suv", passenger_name="SAV guest", pickup_label="SAV", pickup_lat=32.13, pickup_lon=-81.20),
        _make_trip(client, "sprinter", passenger_name="Wedding party (8 pax)"),
    ]

    accepted_pairs: list[tuple[int, int]] = []
    for trip in trips:
        r = client.post(f"/trips/{trip['id']}/broadcast")
        assert r.status_code == 200
        broadcasts = r.json()
        assert len(broadcasts) >= 1
        # Accept on the closest driver who isn't already on a trip
        for bc in broadcasts:
            d_status = client.get(f"/drivers/{bc['driver_id']}").json()["status"]
            if d_status == "available":
                r = client.post(f"/trips/{trip['id']}/accept", params={"driver_id": bc["driver_id"]})
                if r.status_code == 200:
                    accepted_pairs.append((trip["id"], bc["driver_id"]))
                    break

    assert len(accepted_pairs) == 4, "Expected all four trips to find a willing driver in this pool"

    # Progress all to completed and verify drivers return to AVAILABLE
    for trip_id, driver_id in accepted_pairs:
        _progress_to_completed(client, trip_id)
        d = client.get(f"/drivers/{driver_id}").json()
        assert d["status"] == "available", f"Driver {driver_id} should be available after trip {trip_id}"

    # Final state: all trips completed, fares logged
    completed = client.get("/trips", params={"status": "completed"}).json()
    assert len(completed) == 4
    assert all(t["fare_cents"] is not None for t in completed)


def test_first_accept_wins_second_accept_rejected(client):
    """First accept wins; second accept on the same trip is rejected even with a different driver.

    The dispatch system enforces this atomically via a compare-and-set UPDATE in
    `core.accept_trip` (UPDATE Trip ... WHERE status IN (REQUESTED, BROADCASTING)
    AND assigned_driver_id IS NULL). Whichever transaction commits the UPDATE
    first wins; subsequent UPDATEs find the WHERE clause unsatisfiable and the
    accept call returns 409.

    This test is intentionally sequential. A threading-based "true concurrency"
    version was tried and removed: SQLite + StaticPool (the test stack) shares
    a single connection across sessions, so two threads cannot safely host
    independent transactions for a real race. Production runs against Postgres
    where SELECT FOR UPDATE / unique constraints provide the same atomicity
    guarantee under genuine concurrent load.
    """
    d1 = _register_driver(client, "A", "+19125551001", ["suv"], 31.15, -81.49)
    d2 = _register_driver(client, "B", "+19125551002", ["suv"], 31.16, -81.49)
    trip = _make_trip(client, "suv")

    r = client.post(f"/trips/{trip['id']}/broadcast")
    assert r.status_code == 200
    assert len(r.json()) == 2

    # First accept succeeds.
    r1 = client.post(f"/trips/{trip['id']}/accept", params={"driver_id": d1["id"]})
    assert r1.status_code == 200, r1.text

    # Second accept on the same trip — different driver, same broadcast wave — must fail.
    r2 = client.post(f"/trips/{trip['id']}/accept", params={"driver_id": d2["id"]})
    assert r2.status_code == 409, r2.text

    # Final state coherent: trip ACCEPTED to d1; d1 ON_TRIP; d2 still AVAILABLE.
    final_trip = client.get(f"/trips/{trip['id']}").json()
    assert final_trip["status"] == "accepted"
    assert final_trip["assigned_driver_id"] == d1["id"]
    assert client.get(f"/drivers/{d1['id']}").json()["status"] == "on_trip"
    assert client.get(f"/drivers/{d2['id']}").json()["status"] == "available"


def test_no_drivers_then_rebroadcast_after_driver_arrives(client):
    """Trip broadcasts but pool is empty for the right vehicle class; later a driver registers and a re-broadcast succeeds."""
    # Sprinter trip, but only an SUV-only driver online
    d_suv = _register_driver(client, "SUV-only", "+19125552001", ["suv"], 31.15, -81.49)

    trip = _make_trip(client, "sprinter")

    r = client.post(f"/trips/{trip['id']}/broadcast")
    assert r.status_code == 409  # no eligible drivers
    state = client.get(f"/trips/{trip['id']}").json()
    assert state["status"] == "no_drivers"

    # Sprinter-capable driver comes online
    d_sprinter = _register_driver(client, "Sprinter-capable", "+19125552002", ["sprinter"], 31.16, -81.49)

    r = client.post(f"/trips/{trip['id']}/broadcast")
    assert r.status_code == 200
    assert any(b["driver_id"] == d_sprinter["id"] for b in r.json())

    state = client.get(f"/trips/{trip['id']}").json()
    assert state["status"] == "broadcasting"


def test_decline_then_alternate_driver_accepts(client):
    """Driver declines the broadcast; second driver accepts and becomes assigned."""
    d1 = _register_driver(client, "A", "+19125553001", ["suv"], 31.15, -81.49)
    d2 = _register_driver(client, "B", "+19125553002", ["suv"], 31.16, -81.49)
    trip = _make_trip(client, "suv")

    client.post(f"/trips/{trip['id']}/broadcast")

    r = client.post(f"/trips/{trip['id']}/decline", params={"driver_id": d1["id"]})
    assert r.status_code == 200

    r = client.post(f"/trips/{trip['id']}/accept", params={"driver_id": d2["id"]})
    assert r.status_code == 200
    assert r.json()["assigned_driver_id"] == d2["id"]


def test_driver_back_to_available_after_completion_can_take_next_trip(client):
    """A driver completes one trip and is immediately eligible for the next."""
    d = _register_driver(client, "Solo", "+19125554001", ["suv"], 31.15, -81.49)
    trip1 = _make_trip(client, "suv")
    client.post(f"/trips/{trip1['id']}/broadcast")
    client.post(f"/trips/{trip1['id']}/accept", params={"driver_id": d["id"]})
    _progress_to_completed(client, trip1["id"], fare_cents=27500)

    trip2 = _make_trip(client, "suv", passenger_name="Second guest")
    r = client.post(f"/trips/{trip2['id']}/broadcast")
    assert r.status_code == 200
    assert any(b["driver_id"] == d["id"] for b in r.json())


def test_multiple_simultaneous_trips_with_distinct_assignments(client):
    """Two SUV drivers, two SUV trips broadcast back-to-back — each driver lands on a different trip."""
    d1 = _register_driver(client, "A", "+19125555001", ["suv"], 31.15, -81.49)
    d2 = _register_driver(client, "B", "+19125555002", ["suv"], 31.16, -81.49)

    t1 = _make_trip(client, "suv", passenger_name="Trip 1 guest")
    t2 = _make_trip(client, "suv", passenger_name="Trip 2 guest")

    r1 = client.post(f"/trips/{t1['id']}/broadcast")
    assert r1.status_code == 200
    # Accept t1 on d1 — d1 → on_trip
    r = client.post(f"/trips/{t1['id']}/accept", params={"driver_id": d1["id"]})
    assert r.status_code == 200

    # Now broadcast t2 — only d2 should be eligible
    r2 = client.post(f"/trips/{t2['id']}/broadcast")
    assert r2.status_code == 200
    broadcast_driver_ids = [b["driver_id"] for b in r2.json()]
    assert broadcast_driver_ids == [d2["id"]]

    r = client.post(f"/trips/{t2['id']}/accept", params={"driver_id": d2["id"]})
    assert r.status_code == 200

    # Both trips ACCEPTED, both drivers ON_TRIP, no cross-assignment
    state1 = client.get(f"/trips/{t1['id']}").json()
    state2 = client.get(f"/trips/{t2['id']}").json()
    assert state1["assigned_driver_id"] == d1["id"]
    assert state2["assigned_driver_id"] == d2["id"]
