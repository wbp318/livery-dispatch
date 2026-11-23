"""Trip.completed_at — the settlement date (v1.1.20).

Settlement used to treat ``updated_at`` as a trip's completion date, so any
edit after completion moved a finished trip into the current settlement
period. Recording a sub's cost late (``record_subcontractor_cost``, v1.1.18),
a fare fix or a note all did it. ``completed_at`` is stamped once, on
entering COMPLETED, and settlement reads it.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core
from models import Subcontractor, Trip, TripStatus

DISPATCH_ROOT = Path(__file__).resolve().parent.parent


def _trip(client) -> int:
    pickup = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    r = client.post("/trips", json={
        "passenger_name": "Settle Probe", "passenger_phone": "+19045550161",
        "pickup_label": "JAX", "pickup_lat": 30.4941, "pickup_lon": -81.6878,
        "dropoff_label": "Cloister", "dropoff_lat": 31.1969, "dropoff_lon": -81.3438,
        "pickup_time": pickup, "vehicle_class": "suv",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _walk_to_completed(session, tid):
    for st in (TripStatus.ACCEPTED, TripStatus.EN_ROUTE_PICKUP, TripStatus.ON_SCENE,
               TripStatus.IN_TRANSIT, TripStatus.COMPLETED):
        core.update_trip_status(session, tid, st, force=(st == TripStatus.ACCEPTED))


def _naive(dt):
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def test_completing_stamps_completed_at(client, session):
    tid = _trip(client)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    _walk_to_completed(session, tid)
    trip = session.get(Trip, tid)
    assert trip.completed_at is not None
    assert _naive(trip.completed_at) >= before - timedelta(seconds=1)


def test_unfinished_and_cancelled_trips_have_no_completed_at(client, session):
    tid = _trip(client)
    assert session.get(Trip, tid).completed_at is None
    core.update_trip_status(session, tid, TripStatus.CANCELLED)
    assert session.get(Trip, tid).completed_at is None


def test_forced_move_out_of_completed_clears_it(client, session):
    tid = _trip(client)
    _walk_to_completed(session, tid)
    core.update_trip_status(session, tid, TripStatus.IN_TRANSIT, force=True)
    assert session.get(Trip, tid).completed_at is None


def test_trip_out_exposes_completed_at(client, session):
    tid = _trip(client)
    _walk_to_completed(session, tid)
    body = client.get(f"/trips/{tid}").json()
    assert body["completed_at"] is not None


def _sub_trip_completed_days_ago(client, session, days: int) -> tuple[int, int]:
    sub = Subcontractor(name="Settle Sub", dispatch_phone="+19125550181", vehicle_classes="suv",
                        tier=1, markup_pct=15.0, base_lat=31.15, base_lon=-81.49, active=True)
    session.add(sub)
    session.commit()
    tid = _trip(client)
    trip = session.get(Trip, tid)
    trip.assigned_subcontractor_id = sub.id
    session.commit()
    _walk_to_completed(session, tid)
    long_ago = datetime.now(timezone.utc) - timedelta(days=days)
    trip = session.get(Trip, tid)
    trip.completed_at = long_ago
    session.commit()
    # Force updated_at back too (onupdate would otherwise pin it to "now").
    session.execute(Trip.__table__.update().where(Trip.id == tid).values(updated_at=long_ago))
    session.commit()
    return sub.id, tid


def test_late_cost_recording_keeps_trip_in_its_settlement_period(client, session):
    """The quirk this release fixes. A trip finished 40 days ago, then its
    cost is recorded today. It must stay out of the "last 30 days" view and
    stay in the date range covering its real completion date."""
    from admin_pages import _settlement_row, _settlement_trips
    sub_id, tid = _sub_trip_completed_days_ago(client, session, days=40)
    core.record_subcontractor_cost(session, tid, 45000)          # bumps updated_at to now
    session.expire_all()
    sub = session.get(Subcontractor, sub_id)
    assert tid not in [t.id for t in _settlement_trips(session, sub, days=30)]
    then = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
    in_range = _settlement_trips(session, sub, days=0, date_from=then, date_to=then)
    assert tid in [t.id for t in in_range]
    row = _settlement_row(session.get(Trip, tid))
    assert row["age_days"] >= 39
    assert row["fare_cents"] == 45000 + 6750


def test_legacy_row_without_completed_at_falls_back_to_updated_at(client, session):
    from admin_pages import _settlement_row, _settlement_trips
    sub_id, tid = _sub_trip_completed_days_ago(client, session, days=5)
    session.execute(Trip.__table__.update().where(Trip.id == tid).values(completed_at=None))
    session.commit()
    session.expire_all()
    sub = session.get(Subcontractor, sub_id)
    assert tid in [t.id for t in _settlement_trips(session, sub, days=30)]
    assert _settlement_row(session.get(Trip, tid))["completed_at"] is not None


def test_migration_backfills_completed_trips_from_updated_at():
    """Upgrade a DB that is at the v1.1.0 revision and holds a completed and an
    accepted trip. Only the completed one gets completed_at = updated_at."""
    from alembic import command
    from alembic.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        cfg = Config(str(DISPATCH_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(DISPATCH_ROOT / "alembic"))
        os.environ["DATABASE_URL"] = f"sqlite:///{db}"
        try:
            command.upgrade(cfg, "47830a2466c9")                  # the revision before this one
            con = sqlite3.connect(db)
            insert = (
                "INSERT INTO trips (passenger_name, passenger_phone, pickup_label, pickup_lat, "
                "pickup_lon, dropoff_label, dropoff_lat, dropoff_lon, pickup_time, vehicle_class, "
                "status, created_at, updated_at) "
                "VALUES ('P', '+19045550000', 'A', 30.5, -81.6, 'B', 31.2, -81.3, "
                "'2026-01-01 10:00:00', 'SUV', ?, '2026-01-01 09:00:00', '2026-01-02 12:34:56')"
            )
            for status in ("COMPLETED", "ACCEPTED"):
                con.execute(insert, (status,))
            con.commit()
            con.close()
            command.upgrade(cfg, "head")
            con = sqlite3.connect(db)
            rows = dict(con.execute("SELECT status, completed_at FROM trips").fetchall())
            con.close()
        finally:
            os.environ.pop("DATABASE_URL", None)
        assert rows["COMPLETED"] == "2026-01-02 12:34:56"
        assert rows["ACCEPTED"] is None
