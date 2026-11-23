"""Tests for the daily-recap CLI helper.

We exercise the underlying _build_recap helper directly. Driving the typer
CLI through subprocess is slower and harder to assert on; the helper carries
the actual aggregation logic and is what we want pinned by tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from recap import build_recap as _build_recap
from models import (
    Driver,
    DriverStatus,
    Inquiry,
    InquiryKind,
    InquiryStatus,
    Reservation,
    ReservationStatus,
    Subcontractor,
    SubcontractorTier,
    Trip,
    TripStatus,
    VehicleClass,
)


def _add_trip(session, *, status=TripStatus.COMPLETED, fare_cents=30000,
              markup_cents=None, assigned_driver_id=None,
              assigned_subcontractor_id=None,
              vehicle_class=VehicleClass.SUV, created_at=None):
    t = Trip(
        passenger_name="Test passenger",
        passenger_phone="+19045550000",
        pickup_label="x", pickup_lat=0.0, pickup_lon=0.0,
        dropoff_label="y", dropoff_lat=0.0, dropoff_lon=0.0,
        pickup_time=datetime.now(timezone.utc),
        vehicle_class=vehicle_class,
        status=status,
        fare_cents=fare_cents,
        markup_cents=markup_cents,
        assigned_driver_id=assigned_driver_id,
        assigned_subcontractor_id=assigned_subcontractor_id,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    if created_at:
        t.created_at = created_at
        session.commit()
    return t


def test_recap_for_empty_day(session):
    r = _build_recap(session, datetime(2026, 5, 16))
    assert r["date"] == "2026-05-16"
    assert r["total_trips"] == 0
    assert r["completed"] == 0
    assert r["fare_total_cents"] == 0
    assert r["markup_total_cents"] == 0
    assert r["net_revenue_cents"] == 0
    assert r["fare_missing_count"] == 0
    assert r["fare_missing_trip_ids"] == []
    assert r["reservations_received"] == 0
    assert r["inquiries_received"] == 0


def test_recap_flags_completed_trips_with_missing_fare(session):
    """A trip can be marked COMPLETED before its fare is entered (manual
    fare entry lag, invoice in dispute). Such trips should be counted and
    their IDs surfaced so end-of-shift revenue is honest, not silently
    understated."""
    today = datetime.now(timezone.utc)
    paid = _add_trip(session, status=TripStatus.COMPLETED, fare_cents=30000)
    unpaid = _add_trip(session, status=TripStatus.COMPLETED, fare_cents=None)
    # A CANCELLED trip with no fare should NOT count toward missing — it's
    # not a completed trip, so there's nothing missing.
    _add_trip(session, status=TripStatus.CANCELLED, fare_cents=None)
    r = _build_recap(session, today)
    assert r["completed"] == 2
    assert r["fare_total_cents"] == 30000  # unaffected by the missing fare
    assert r["fare_missing_count"] == 1
    assert r["fare_missing_trip_ids"] == [unpaid.id]
    assert paid.id not in r["fare_missing_trip_ids"]


def test_recap_aggregates_completed_trip_revenue(session):
    today = datetime.now(timezone.utc)
    _add_trip(session, status=TripStatus.COMPLETED, fare_cents=30000)
    _add_trip(session, status=TripStatus.COMPLETED, fare_cents=45000)
    _add_trip(session, status=TripStatus.CANCELLED, fare_cents=None)
    r = _build_recap(session, today)
    assert r["total_trips"] == 3
    assert r["completed"] == 2
    assert r["fare_total_cents"] == 75000  # only completed trips count toward revenue
    assert r["by_status"]["completed"] == 2
    assert r["by_status"]["cancelled"] == 1


def test_recap_separates_own_fleet_and_sub_assignments(session):
    driver = Driver(name="Mike", phone="+19045551234", vehicle_classes="suv")
    session.add(driver)
    sub = Subcontractor(name="Coast", dispatch_phone="+18435551111",
                        vehicle_classes="suv", tier=SubcontractorTier.TIER_1,
                        markup_pct=15.0)
    session.add(sub)
    session.commit()
    session.refresh(driver)
    session.refresh(sub)

    today = datetime.now(timezone.utc)
    _add_trip(session, status=TripStatus.COMPLETED, fare_cents=30000,
              assigned_driver_id=driver.id)
    _add_trip(session, status=TripStatus.COMPLETED, fare_cents=46000,
              markup_cents=6000, assigned_subcontractor_id=sub.id)
    r = _build_recap(session, today)
    assert r["own_fleet"] == 1
    assert r["sub_fleet"] == 1
    assert r["fare_total_cents"] == 76000
    assert r["markup_total_cents"] == 6000
    assert r["net_revenue_cents"] == 70000  # fare - markup paid to sub


def test_recap_breaks_down_by_vehicle_class(session):
    today = datetime.now(timezone.utc)
    _add_trip(session, vehicle_class=VehicleClass.SUV)
    _add_trip(session, vehicle_class=VehicleClass.SUV)
    _add_trip(session, vehicle_class=VehicleClass.SPRINTER)
    r = _build_recap(session, today)
    assert r["by_vehicle"]["suv"] == 2
    assert r["by_vehicle"]["sprinter"] == 1


def test_recap_includes_reservations_and_inquiries(session):
    today = datetime.now(timezone.utc)
    r1 = Reservation(name="A", email="a@x.com", phone="+19040000001",
                     pickup_label="x", dropoff_label="y",
                     status=ReservationStatus.PENDING_REVIEW)
    r2 = Reservation(name="B", email="b@x.com", phone="+19040000002",
                     pickup_label="x", dropoff_label="y",
                     status=ReservationStatus.CONVERTED)
    i1 = Inquiry(kind=InquiryKind.WEDDING, name="W", email="w@x.com",
                 status=InquiryStatus.PENDING_REVIEW)
    session.add_all([r1, r2, i1])
    session.commit()
    r = _build_recap(session, today)
    assert r["reservations_received"] == 2
    assert r["reservations_converted"] == 1
    assert r["inquiries_received"] == 1


def test_recap_filters_to_the_requested_day(session):
    # Yesterday's trip shouldn't appear in today's recap.
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    _add_trip(session, created_at=yesterday)
    _add_trip(session)  # today
    r_today = _build_recap(session, datetime.now(timezone.utc))
    r_yesterday = _build_recap(session, yesterday)
    assert r_today["total_trips"] == 1
    assert r_yesterday["total_trips"] == 1
