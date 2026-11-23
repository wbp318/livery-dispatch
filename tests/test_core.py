"""Tests for core dispatch logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import core
from models import (
    BroadcastResponse,
    Driver,
    DriverStatus,
    Trip,
    TripStatus,
    VehicleClass,
)
from notifications import ConsoleProvider


def _make_driver(name: str, phone: str, classes: str, status: DriverStatus, lat: float | None, lon: float | None) -> Driver:
    return Driver(name=name, phone=phone, vehicle_classes=classes, status=status, lat=lat, lon=lon)


def _make_trip(
    pickup_lat: float,
    pickup_lon: float,
    vehicle_class: VehicleClass = VehicleClass.SUV,
) -> Trip:
    return Trip(
        passenger_name="Test Passenger",
        passenger_phone="+10000000000",
        pickup_label="Pickup",
        pickup_lat=pickup_lat,
        pickup_lon=pickup_lon,
        dropoff_label="Dropoff",
        dropoff_lat=pickup_lat + 0.5,
        dropoff_lon=pickup_lon + 0.5,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=1),
        vehicle_class=vehicle_class,
    )


def test_haversine_zero_distance():
    assert core.haversine_miles(31.0, -81.0, 31.0, -81.0) == pytest.approx(0.0)


def test_haversine_known_pair():
    # Brunswick GA → Jacksonville FL is ~63 miles great-circle
    miles = core.haversine_miles(31.1499, -81.4915, 30.3322, -81.6557)
    assert 55 < miles < 75


def test_candidate_drivers_filters_by_status_class_and_radius(session):
    available_close = _make_driver("close", "+1", "suv", DriverStatus.AVAILABLE, 31.15, -81.49)
    available_far = _make_driver("far", "+2", "suv", DriverStatus.AVAILABLE, 33.7, -84.4)  # Atlanta
    on_trip = _make_driver("busy", "+3", "suv", DriverStatus.ON_TRIP, 31.15, -81.49)
    wrong_class = _make_driver("sprinter-only", "+4", "sprinter", DriverStatus.AVAILABLE, 31.15, -81.49)
    no_location = _make_driver("nowhere", "+5", "suv", DriverStatus.AVAILABLE, None, None)
    session.add_all([available_close, available_far, on_trip, wrong_class, no_location])
    session.commit()

    trip = _make_trip(31.1499, -81.4915, VehicleClass.SUV)
    session.add(trip)
    session.commit()

    results = core.candidate_drivers(session, trip, radius_mi=50)
    names = [d.name for d, _ in results]
    assert names == ["close"]  # only one within radius matching class & status


def test_candidate_drivers_sorted_by_distance(session):
    near = _make_driver("near", "+10", "suv", DriverStatus.AVAILABLE, 31.15, -81.49)
    mid = _make_driver("mid", "+11", "suv", DriverStatus.AVAILABLE, 31.30, -81.50)
    far = _make_driver("far", "+12", "suv", DriverStatus.AVAILABLE, 31.50, -81.60)
    session.add_all([near, mid, far])
    session.commit()

    trip = _make_trip(31.1499, -81.4915)
    session.add(trip)
    session.commit()

    results = core.candidate_drivers(session, trip, radius_mi=100)
    names = [d.name for d, _ in results]
    assert names == ["near", "mid", "far"]


def test_broadcast_creates_records_and_marks_trip(session):
    drivers = [
        _make_driver(f"d{i}", f"+200{i:02d}", "suv", DriverStatus.AVAILABLE, 31.15 + i * 0.01, -81.49)
        for i in range(5)
    ]
    session.add_all(drivers)
    session.commit()

    trip = _make_trip(31.1499, -81.4915)
    session.add(trip)
    session.commit()

    broadcasts = core.broadcast_trip(session, trip, provider=ConsoleProvider(), fanout=3, radius_mi=100)
    session.refresh(trip)

    assert len(broadcasts) == 3
    assert trip.status == TripStatus.BROADCASTING
    # Broadcast distances are non-decreasing (sorted ascending).
    distances = [b.distance_mi for b in broadcasts]
    assert distances == sorted(distances)


def test_broadcast_with_no_candidates_marks_no_drivers(session):
    far = _make_driver("atl", "+1", "suv", DriverStatus.AVAILABLE, 33.7, -84.4)
    session.add(far)
    session.commit()

    trip = _make_trip(31.1499, -81.4915)
    session.add(trip)
    session.commit()

    broadcasts = core.broadcast_trip(session, trip, provider=ConsoleProvider(), fanout=3, radius_mi=10)
    session.refresh(trip)

    assert broadcasts == []
    assert trip.status == TripStatus.NO_DRIVERS


def test_first_accept_wins_others_expire(session):
    drivers = [
        _make_driver(f"d{i}", f"+30{i:02d}", "suv", DriverStatus.AVAILABLE, 31.15 + i * 0.01, -81.49)
        for i in range(3)
    ]
    session.add_all(drivers)
    session.commit()

    trip = _make_trip(31.1499, -81.4915)
    session.add(trip)
    session.commit()

    core.broadcast_trip(session, trip, provider=ConsoleProvider(), fanout=3, radius_mi=100)
    session.refresh(trip)

    winner_id = drivers[1].id
    accepted_trip = core.accept_trip(session, trip.id, winner_id)

    assert accepted_trip.status == TripStatus.ACCEPTED
    assert accepted_trip.assigned_driver_id == winner_id
    winner = session.get(Driver, winner_id)
    assert winner.status == DriverStatus.ON_TRIP

    for bc in accepted_trip.broadcasts:
        if bc.driver_id == winner_id:
            assert bc.response == BroadcastResponse.ACCEPTED
        else:
            assert bc.response == BroadcastResponse.EXPIRED


def test_second_accept_after_assignment_raises(session):
    drivers = [
        _make_driver("a", "+401", "suv", DriverStatus.AVAILABLE, 31.15, -81.49),
        _make_driver("b", "+402", "suv", DriverStatus.AVAILABLE, 31.16, -81.49),
    ]
    session.add_all(drivers)
    session.commit()

    trip = _make_trip(31.1499, -81.4915)
    session.add(trip)
    session.commit()

    core.broadcast_trip(session, trip, provider=ConsoleProvider(), fanout=2, radius_mi=100)
    core.accept_trip(session, trip.id, drivers[0].id)

    with pytest.raises(ValueError):
        core.accept_trip(session, trip.id, drivers[1].id)


def test_decline_does_not_affect_other_drivers(session):
    drivers = [
        _make_driver("a", "+501", "suv", DriverStatus.AVAILABLE, 31.15, -81.49),
        _make_driver("b", "+502", "suv", DriverStatus.AVAILABLE, 31.16, -81.49),
    ]
    session.add_all(drivers)
    session.commit()

    trip = _make_trip(31.1499, -81.4915)
    session.add(trip)
    session.commit()

    core.broadcast_trip(session, trip, provider=ConsoleProvider(), fanout=2, radius_mi=100)
    core.decline_trip(session, trip.id, drivers[0].id)

    session.refresh(trip)
    assert trip.status == TripStatus.BROADCASTING

    accepted = core.accept_trip(session, trip.id, drivers[1].id)
    assert accepted.status == TripStatus.ACCEPTED
    assert accepted.assigned_driver_id == drivers[1].id


def test_completed_status_returns_driver_to_available(session):
    driver = _make_driver("d", "+601", "suv", DriverStatus.AVAILABLE, 31.15, -81.49)
    session.add(driver)
    session.commit()

    trip = _make_trip(31.1499, -81.4915)
    session.add(trip)
    session.commit()

    core.broadcast_trip(session, trip, provider=ConsoleProvider(), fanout=1, radius_mi=100)
    core.accept_trip(session, trip.id, driver.id)
    core.update_trip_status(session, trip.id, TripStatus.EN_ROUTE_PICKUP)
    core.update_trip_status(session, trip.id, TripStatus.ON_SCENE)
    core.update_trip_status(session, trip.id, TripStatus.IN_TRANSIT)
    completed = core.update_trip_status(session, trip.id, TripStatus.COMPLETED, fare_cents=32500)

    session.refresh(driver)
    assert completed.status == TripStatus.COMPLETED
    assert completed.fare_cents == 32500
    assert driver.status == DriverStatus.AVAILABLE
