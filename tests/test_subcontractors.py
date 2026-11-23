"""Tests for subcontractor (overflow) dispatch.

Covers:
    - Subcontractor model + candidate filtering (active, vehicle class, service radius, tier)
    - broadcast_to_subcontractors creates broadcast rows + sends SMS + flips trip
    - accept_trip_subcontractor enforces atomic claim against own-fleet acceptance
    - decline_trip_subcontractor leaves the trip open for siblings
    - Markup math: fare = quoted_cost + (quoted_cost * markup_pct / 100)
    - REST endpoints: CRUD subcontractors, broadcast-overflow, sub-accept, sub-decline
    - Cross-table: a sub cannot accept a trip already taken by an own-fleet driver, and vice versa
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import core
from models import (
    BroadcastResponse,
    Driver,
    DriverStatus,
    Subcontractor,
    SubcontractorTier,
    Trip,
    TripStatus,
    VehicleClass,
)
from notifications import ConsoleProvider


# ---------------------------------------------------------------------------
# Fixtures / helpers (private to this file)
# ---------------------------------------------------------------------------


def _make_trip(pickup_lat: float = 30.4941, pickup_lon: float = -81.6878, vc: VehicleClass = VehicleClass.SUV) -> Trip:
    return Trip(
        passenger_name="Test",
        passenger_phone="+10000000000",
        pickup_label="JAX Terminal",
        pickup_lat=pickup_lat,
        pickup_lon=pickup_lon,
        dropoff_label="Cloister",
        dropoff_lat=31.1969,
        dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=2),
        vehicle_class=vc,
    )


def _make_sub(
    name: str,
    phone: str,
    *,
    classes: str = "suv",
    tier: SubcontractorTier = SubcontractorTier.TIER_1,
    markup_pct: float = 15.0,
    base_lat: float | None = 31.1499,
    base_lon: float | None = -81.4915,
    service_radius_mi: float | None = 100.0,
    active: bool = True,
) -> Subcontractor:
    return Subcontractor(
        name=name,
        dispatch_phone=phone,
        vehicle_classes=classes,
        tier=tier,
        markup_pct=markup_pct,
        base_lat=base_lat,
        base_lon=base_lon,
        service_radius_mi=service_radius_mi,
        active=active,
    )


# ---------------------------------------------------------------------------
# Core: candidate filter
# ---------------------------------------------------------------------------


def test_candidate_subcontractors_filters_active_class_and_radius(session):
    s_active_match = _make_sub("Coast", "+18435550100", classes="suv,sedan")
    s_inactive = _make_sub("Inactive Co", "+18435550101", active=False)
    s_wrong_class = _make_sub("Sprinter Only", "+18435550102", classes="sprinter")
    s_too_far = _make_sub("Far Away", "+18435550103", base_lat=33.7, base_lon=-84.4, service_radius_mi=10.0)
    s_no_base = _make_sub("Nowhere", "+18435550104", base_lat=None, base_lon=None, service_radius_mi=None)
    session.add_all([s_active_match, s_inactive, s_wrong_class, s_too_far, s_no_base])
    session.commit()

    trip = _make_trip(31.1499, -81.4915, VehicleClass.SUV)
    session.add(trip)
    session.commit()

    results = core.candidate_subcontractors(session, trip, radius_mi=200)
    names = [s.name for s, _ in results]
    # active SUV-serving subs within radius (or with no base — no_base falls in as last-resort)
    assert "Coast" in names
    assert "Nowhere" in names
    # filtered out:
    assert "Inactive Co" not in names
    assert "Sprinter Only" not in names
    assert "Far Away" not in names


def test_candidate_subcontractors_sorted_by_tier_then_distance(session):
    near_t2 = _make_sub("near-T2", "+1801", base_lat=31.149, base_lon=-81.49, tier=SubcontractorTier.TIER_2)
    near_t1 = _make_sub("near-T1", "+1802", base_lat=31.150, base_lon=-81.49, tier=SubcontractorTier.TIER_1)
    far_t1 = _make_sub("far-T1", "+1803", base_lat=31.40, base_lon=-81.20, tier=SubcontractorTier.TIER_1)
    near_t3 = _make_sub("near-T3", "+1804", base_lat=31.151, base_lon=-81.49, tier=SubcontractorTier.TIER_3)
    session.add_all([near_t2, near_t1, far_t1, near_t3])
    session.commit()

    trip = _make_trip(31.149, -81.491)
    session.add(trip)
    session.commit()

    results = core.candidate_subcontractors(session, trip, radius_mi=200)
    names = [s.name for s, _ in results]
    # tier 1 first (sorted by distance), then tier 2, then tier 3
    assert names == ["near-T1", "far-T1", "near-T2", "near-T3"]


def test_candidate_subcontractors_can_filter_by_tier(session):
    s_t1 = _make_sub("T1", "+1901", tier=SubcontractorTier.TIER_1)
    s_t2 = _make_sub("T2", "+1902", tier=SubcontractorTier.TIER_2)
    session.add_all([s_t1, s_t2])
    session.commit()

    trip = _make_trip()
    session.add(trip)
    session.commit()

    only_t1 = core.candidate_subcontractors(session, trip, radius_mi=500, tiers=[SubcontractorTier.TIER_1])
    assert [s.name for s, _ in only_t1] == ["T1"]


def test_subcontractor_uses_its_own_service_radius_over_default(session):
    # Sub with a 30-mile service radius; trip pickup is 50 miles from base.
    sub = _make_sub("Tight Radius", "+1011", service_radius_mi=30.0, base_lat=31.149, base_lon=-81.491)
    session.add(sub)
    session.commit()

    # Trip pickup ~63 mi away (Brunswick → JAX)
    trip = _make_trip(30.4941, -81.6878)
    session.add(trip)
    session.commit()

    # Even with a generous default radius, sub's own narrower limit excludes it.
    results = core.candidate_subcontractors(session, trip, radius_mi=500)
    assert results == []


# ---------------------------------------------------------------------------
# Core: broadcast + accept + decline
# ---------------------------------------------------------------------------


def test_broadcast_to_subs_creates_records_and_marks_trip(session):
    subs = [
        _make_sub("Coast", "+1100", base_lat=31.15 + i * 0.1, base_lon=-81.49)
        for i in range(4)
    ]
    # tweak phones to be distinct
    for i, s in enumerate(subs):
        s.dispatch_phone = f"+1110{i}"
    session.add_all(subs)
    session.commit()

    trip = _make_trip(31.1499, -81.4915)
    session.add(trip)
    session.commit()

    broadcasts = core.broadcast_to_subcontractors(session, trip, provider=ConsoleProvider(), fanout=3, radius_mi=200)
    session.refresh(trip)

    assert len(broadcasts) == 3
    assert trip.status == TripStatus.BROADCASTING
    assert all(b.subcontractor_id is not None and b.driver_id is None for b in broadcasts)


def test_broadcast_to_subs_no_candidates_marks_no_drivers(session):
    # Sprinter-only trip but no Sprinter-capable sub
    sub = _make_sub("SUV Only", "+12001", classes="suv")
    session.add(sub)
    session.commit()

    trip = _make_trip(vc=VehicleClass.SPRINTER)
    session.add(trip)
    session.commit()

    broadcasts = core.broadcast_to_subcontractors(session, trip, provider=ConsoleProvider(), radius_mi=200)
    session.refresh(trip)

    assert broadcasts == []
    assert trip.status == TripStatus.NO_DRIVERS


def test_subcontractor_accepts_trip_records_markup_when_quoted(session):
    sub = _make_sub("Coast", "+13001", markup_pct=15.0)
    session.add(sub)
    session.commit()

    trip = _make_trip()
    session.add(trip)
    session.commit()

    core.broadcast_to_subcontractors(session, trip, provider=ConsoleProvider(), fanout=1, radius_mi=200)

    # Sub quotes $250.00 ($25000 cents) for the ride.
    accepted = core.accept_trip_subcontractor(session, trip.id, sub.id, quoted_cost_cents=25000)

    assert accepted.status == TripStatus.ACCEPTED
    assert accepted.assigned_subcontractor_id == sub.id
    assert accepted.assigned_driver_id is None
    # 15% markup on $250 = $37.50 → fare to customer = $287.50 ($28750 cents)
    assert accepted.markup_cents == 3750
    assert accepted.fare_cents == 28750


def test_subcontractor_accept_without_quote_leaves_fare_null(session):
    sub = _make_sub("Coast", "+14001")
    session.add(sub)
    session.commit()
    trip = _make_trip()
    session.add(trip)
    session.commit()

    core.broadcast_to_subcontractors(session, trip, provider=ConsoleProvider(), fanout=1, radius_mi=200)
    accepted = core.accept_trip_subcontractor(session, trip.id, sub.id)

    assert accepted.fare_cents is None
    assert accepted.markup_cents is None


def test_second_sub_accept_after_assignment_raises(session):
    s1 = _make_sub("A", "+15001")
    s2 = _make_sub("B", "+15002", base_lat=31.20, base_lon=-81.50)
    session.add_all([s1, s2])
    session.commit()
    trip = _make_trip()
    session.add(trip)
    session.commit()

    core.broadcast_to_subcontractors(session, trip, provider=ConsoleProvider(), fanout=2, radius_mi=200)
    core.accept_trip_subcontractor(session, trip.id, s1.id)

    with pytest.raises(ValueError):
        core.accept_trip_subcontractor(session, trip.id, s2.id)


def test_sub_decline_leaves_trip_open_for_siblings(session):
    s1 = _make_sub("A", "+16001")
    s2 = _make_sub("B", "+16002", base_lat=31.20, base_lon=-81.50)
    session.add_all([s1, s2])
    session.commit()
    trip = _make_trip()
    session.add(trip)
    session.commit()

    core.broadcast_to_subcontractors(session, trip, provider=ConsoleProvider(), fanout=2, radius_mi=200)
    core.decline_trip_subcontractor(session, trip.id, s1.id)

    session.refresh(trip)
    assert trip.status == TripStatus.BROADCASTING

    accepted = core.accept_trip_subcontractor(session, trip.id, s2.id)
    assert accepted.assigned_subcontractor_id == s2.id


# ---------------------------------------------------------------------------
# Cross-table: own-fleet vs subcontractor mutual exclusion
# ---------------------------------------------------------------------------


def test_driver_accept_blocks_subsequent_sub_accept(session):
    """A driver wins the trip; a parallel sub broadcast cannot then accept."""
    driver = Driver(
        name="D", phone="+17001", vehicle_classes="suv",
        status=DriverStatus.AVAILABLE, lat=31.149, lon=-81.491,
    )
    sub = _make_sub("S", "+17002")
    session.add_all([driver, sub])
    session.commit()
    trip = _make_trip()
    session.add(trip)
    session.commit()

    core.broadcast_trip(session, trip, provider=ConsoleProvider(), fanout=1, radius_mi=200)
    core.broadcast_to_subcontractors(session, trip, provider=ConsoleProvider(), fanout=1, radius_mi=200)

    # Driver wins
    core.accept_trip(session, trip.id, driver.id)

    # Sub cannot now accept the same trip
    with pytest.raises(ValueError):
        core.accept_trip_subcontractor(session, trip.id, sub.id)


def test_sub_accept_blocks_subsequent_driver_accept(session):
    driver = Driver(
        name="D", phone="+18001", vehicle_classes="suv",
        status=DriverStatus.AVAILABLE, lat=31.149, lon=-81.491,
    )
    sub = _make_sub("S", "+18002")
    session.add_all([driver, sub])
    session.commit()
    trip = _make_trip()
    session.add(trip)
    session.commit()

    core.broadcast_trip(session, trip, provider=ConsoleProvider(), fanout=1, radius_mi=200)
    core.broadcast_to_subcontractors(session, trip, provider=ConsoleProvider(), fanout=1, radius_mi=200)

    core.accept_trip_subcontractor(session, trip.id, sub.id)

    with pytest.raises(ValueError):
        core.accept_trip(session, trip.id, driver.id)


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


def test_subcontractor_crud_via_api(client):
    payload = {
        "name": "Sample Northshore Cars",
        "dispatch_phone": "+18435550100",
        "vehicle_classes": ["suv", "sedan"],
        "tier": 1,
        "markup_pct": 15.0,
        "base_lat": 32.2371,
        "base_lon": -80.8607,
        "service_radius_mi": 80.0,
        "notes": "Bluffton SC; mid-tier coverage",
    }
    r = client.post("/subcontractors", json=payload)
    assert r.status_code == 201, r.text
    sub_id = r.json()["id"]
    assert r.json()["active"] is True

    # Duplicate phone → 409
    r = client.post("/subcontractors", json=payload)
    assert r.status_code == 409

    # Get
    r = client.get(f"/subcontractors/{sub_id}")
    assert r.status_code == 200

    # List + active filter
    assert any(s["id"] == sub_id for s in client.get("/subcontractors").json())
    assert any(s["id"] == sub_id for s in client.get("/subcontractors", params={"active_only": True}).json())

    # Patch — deactivate + bump markup
    r = client.patch(f"/subcontractors/{sub_id}", json={"active": False, "markup_pct": 18.0})
    assert r.status_code == 200
    assert r.json()["active"] is False
    assert r.json()["markup_pct"] == 18.0

    # Active filter now excludes
    assert all(s["id"] != sub_id for s in client.get("/subcontractors", params={"active_only": True}).json())


def test_broadcast_overflow_no_active_subs_returns_409(client):
    trip = client.post(
        "/trips",
        json={
            "passenger_name": "X",
            "passenger_phone": "+19000",
            "pickup_label": "JAX",
            "pickup_lat": 30.4941,
            "pickup_lon": -81.6878,
            "dropoff_label": "Cloister",
            "dropoff_lat": 31.1969,
            "dropoff_lon": -81.3438,
            "pickup_time": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
            "vehicle_class": "suv",
        },
    ).json()

    r = client.post(f"/trips/{trip['id']}/broadcast-overflow")
    assert r.status_code == 409


def test_broadcast_overflow_then_sub_accept_via_api(client):
    sub = client.post(
        "/subcontractors",
        json={
            "name": "Coast",
            "dispatch_phone": "+18435551111",
            "vehicle_classes": ["suv"],
            "tier": 1,
            "markup_pct": 15.0,
            "base_lat": 31.15,
            "base_lon": -81.49,
            "service_radius_mi": 200.0,
        },
    ).json()

    trip = client.post(
        "/trips",
        json={
            "passenger_name": "Mr Whitfield",
            "passenger_phone": "+19045556789",
            "pickup_label": "JAX",
            "pickup_lat": 30.4941,
            "pickup_lon": -81.6878,
            "dropoff_label": "Cloister",
            "dropoff_lat": 31.1969,
            "dropoff_lon": -81.3438,
            "pickup_time": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
            "vehicle_class": "suv",
        },
    ).json()

    r = client.post(f"/trips/{trip['id']}/broadcast-overflow")
    assert r.status_code == 200
    broadcasts = r.json()
    assert len(broadcasts) == 1
    assert broadcasts[0]["subcontractor_id"] == sub["id"]
    assert broadcasts[0]["driver_id"] is None

    r = client.post(
        f"/trips/{trip['id']}/sub-accept",
        params={"subcontractor_id": sub["id"]},
        json={"quoted_cost_cents": 30000},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["assigned_subcontractor_id"] == sub["id"]
    assert body["assigned_driver_id"] is None
    # 15% markup on $300 = $45 → fare $345 ($34500 cents)
    assert body["markup_cents"] == 4500
    assert body["fare_cents"] == 34500


def test_sub_decline_via_api(client):
    sub = client.post(
        "/subcontractors",
        json={
            "name": "Coast",
            "dispatch_phone": "+18435552222",
            "vehicle_classes": ["suv"],
            "tier": 1,
            "base_lat": 31.15,
            "base_lon": -81.49,
            "service_radius_mi": 200.0,
        },
    ).json()
    trip = client.post(
        "/trips",
        json={
            "passenger_name": "X",
            "passenger_phone": "+1900",
            "pickup_label": "JAX",
            "pickup_lat": 30.4941,
            "pickup_lon": -81.6878,
            "dropoff_label": "Cloister",
            "dropoff_lat": 31.1969,
            "dropoff_lon": -81.3438,
            "pickup_time": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
            "vehicle_class": "suv",
        },
    ).json()

    client.post(f"/trips/{trip['id']}/broadcast-overflow")
    r = client.post(f"/trips/{trip['id']}/sub-decline", params={"subcontractor_id": sub["id"]})
    assert r.status_code == 200
    assert r.json()["response"] == "declined"

    # Trip stays in BROADCASTING (no other sibling, but the trip wasn't cancelled)
    state = client.get(f"/trips/{trip['id']}").json()
    assert state["status"] == "broadcasting"
