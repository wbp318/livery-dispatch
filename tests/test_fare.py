"""Tests for the fare calculator + the /admin/trips/{id}/action/quote endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import fare
from models import (
    Driver, DriverStatus,
    RateCard,
    Trip, TripStatus,
    VehicleClass,
)


# Sea Island corridor anchor points (matches seed.py).
BRUNSWICK = (31.1499, -81.4915)
JAX_TERMINAL = (30.4941, -81.6878)
CLOISTER = (31.1969, -81.3438)


# ---------------------------------------------------------------------------
# Pure calculator unit tests
# ---------------------------------------------------------------------------


def test_quote_trip_with_zero_card_returns_zero():
    b = fare.quote_trip(
        pickup_lat=BRUNSWICK[0], pickup_lon=BRUNSWICK[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
    )
    assert b.total_cents == 0
    assert b.lines == []


def test_quote_trip_per_mile_only():
    # ~9.3 mile leg Brunswick -> Cloister; at 500 c/mi that's ~$46.50.
    b = fare.quote_trip(
        pickup_lat=BRUNSWICK[0], pickup_lon=BRUNSWICK[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        per_mile_cents=500,
    )
    assert 4000 < b.total_cents < 5500
    assert any("Distance" in l for l, _ in b.lines)


def test_quote_trip_jax_to_cloister_long_leg():
    # ~53 mile haversine leg JAX -> Cloister; at 500 c/mi that's ~$263.
    # (Road mileage is ~65 mi but haversine is what the calculator uses
    # until the OSRM swap-in.)
    b = fare.quote_trip(
        pickup_lat=JAX_TERMINAL[0], pickup_lon=JAX_TERMINAL[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        per_mile_cents=500,
    )
    assert 25000 < b.total_cents < 30000
    assert b.distance_miles > 50


def test_quote_trip_base_plus_mile_plus_hour():
    b = fare.quote_trip(
        pickup_lat=BRUNSWICK[0], pickup_lon=BRUNSWICK[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        base_fare_cents=15000,
        per_mile_cents=550,
        per_hour_cents=12500,
    )
    # Base $150 + ~13mi*$5.50 ≈ $71.50 + ~0.5hr*$125 ≈ $62.50 = ~$284
    assert 25000 < b.total_cents < 35000
    labels = [l for l, _ in b.lines]
    assert any("Base fare" in l for l in labels)
    assert any("Distance" in l for l in labels)
    assert any("Time" in l for l in labels)


def test_quote_trip_minimum_fare_bumps_total():
    b = fare.quote_trip(
        pickup_lat=BRUNSWICK[0], pickup_lon=BRUNSWICK[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        per_mile_cents=100,            # tiny per-mile
        minimum_fare_cents=12500,
    )
    assert b.total_cents == 12500
    assert any("Minimum" in l for l, _ in b.lines)


def test_quote_trip_peak_week_multiplier_applies_only_when_flagged():
    base_kwargs = dict(
        pickup_lat=BRUNSWICK[0], pickup_lon=BRUNSWICK[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        base_fare_cents=10000, per_mile_cents=500,
        peak_week_multiplier=1.25,
    )
    no_peak = fare.quote_trip(**base_kwargs, is_peak_week=False)
    yes_peak = fare.quote_trip(**base_kwargs, is_peak_week=True)
    assert yes_peak.total_cents > no_peak.total_cents
    assert any("Peak" in l for l, _ in yes_peak.lines)
    assert all("Peak" not in l for l, _ in no_peak.lines)


def test_quote_trip_airport_meet_surcharge():
    b = fare.quote_trip(
        pickup_lat=JAX_TERMINAL[0], pickup_lon=JAX_TERMINAL[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        base_fare_cents=15000, per_mile_cents=550,
        airport_meet_surcharge_cents=2500,
        is_airport_meet=True,
    )
    assert any("Airport meet" in l for l, _ in b.lines)


def test_quote_trip_after_hours_surcharge_only_in_window():
    common = dict(
        pickup_lat=BRUNSWICK[0], pickup_lon=BRUNSWICK[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        base_fare_cents=10000, per_mile_cents=500,
        after_hours_surcharge_cents=2500,
    )
    morning = fare.quote_trip(**common, pickup_time=datetime(2026, 11, 13, 10, 0))
    late_night = fare.quote_trip(**common, pickup_time=datetime(2026, 11, 13, 23, 30))
    early_am = fare.quote_trip(**common, pickup_time=datetime(2026, 11, 14, 4, 0))

    assert all("After-hours" not in l for l, _ in morning.lines)
    assert any("After-hours" in l for l, _ in late_night.lines)
    assert any("After-hours" in l for l, _ in early_am.lines)


def test_breakdown_serializes_to_dict():
    b = fare.quote_trip(
        pickup_lat=BRUNSWICK[0], pickup_lon=BRUNSWICK[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        base_fare_cents=15000, per_mile_cents=550,
    )
    d = b.to_dict()
    assert "total_cents" in d
    assert "lines" in d
    assert all("label" in line and "cents" in line for line in d["lines"])


def test_breakdown_text_summary_includes_total():
    b = fare.quote_trip(
        pickup_lat=BRUNSWICK[0], pickup_lon=BRUNSWICK[1],
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        base_fare_cents=15000, per_mile_cents=550,
    )
    txt = b.as_text()
    assert "$" in txt
    assert "=" in txt


# ---------------------------------------------------------------------------
# OSRM real-road distance (v0.9.7)
# ---------------------------------------------------------------------------


class _FakeURLOpenResponse:
    """Minimal context-manager response stub matching urlopen's contract."""
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_osrm_distance_miles_parses_routes_distance(monkeypatch):
    """OSRM returns meters; we convert to miles."""
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        # 75 mi in meters → 120700.8
        body = b'{"routes":[{"distance":120700.8}]}'
        return _FakeURLOpenResponse(body)

    monkeypatch.setattr(fare._urlrequest, "urlopen", fake_urlopen)
    miles = fare.osrm_distance_miles(*JAX_TERMINAL, *CLOISTER)
    assert miles is not None
    assert 74 < miles < 76
    # OSRM URL must be in lon,lat order — easy to swap, easy to test.
    assert captured["url"].endswith(
        f"/route/v1/driving/{JAX_TERMINAL[1]},{JAX_TERMINAL[0]};{CLOISTER[1]},{CLOISTER[0]}?overview=false"
    )


def test_osrm_distance_miles_returns_none_on_http_error(monkeypatch):
    from urllib.error import URLError

    def fake_urlopen(url, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr(fare._urlrequest, "urlopen", fake_urlopen)
    assert fare.osrm_distance_miles(*JAX_TERMINAL, *CLOISTER) is None


def test_osrm_distance_miles_returns_none_on_empty_routes(monkeypatch):
    def fake_urlopen(url, timeout=None):
        return _FakeURLOpenResponse(b'{"routes":[]}')

    monkeypatch.setattr(fare._urlrequest, "urlopen", fake_urlopen)
    assert fare.osrm_distance_miles(*JAX_TERMINAL, *CLOISTER) is None


def test_osrm_distance_miles_returns_none_on_malformed_json(monkeypatch):
    def fake_urlopen(url, timeout=None):
        return _FakeURLOpenResponse(b"not json at all")

    monkeypatch.setattr(fare._urlrequest, "urlopen", fake_urlopen)
    assert fare.osrm_distance_miles(*JAX_TERMINAL, *CLOISTER) is None


def test_compute_distance_falls_back_to_haversine_when_osrm_unreachable(monkeypatch):
    """The whole point of having OSRM be optional: when it fails (rate-limit,
    network blip, demo server down), the fare calculator keeps working."""
    from urllib.error import URLError

    monkeypatch.setenv("FARE_DISTANCE_PROVIDER", "osrm")
    monkeypatch.setattr(fare._urlrequest, "urlopen",
                        lambda url, timeout=None: (_ for _ in ()).throw(URLError("down")))

    miles = fare._compute_distance_miles(*JAX_TERMINAL, *CLOISTER)
    # Falls back to haversine — JAX → Cloister haversine is ~53 mi.
    haversine = fare.haversine_miles(*JAX_TERMINAL, *CLOISTER)
    assert abs(miles - haversine) < 0.001


def test_compute_distance_uses_osrm_when_enabled(monkeypatch):
    monkeypatch.setenv("FARE_DISTANCE_PROVIDER", "osrm")
    monkeypatch.setattr(fare._urlrequest, "urlopen",
                        lambda url, timeout=None: _FakeURLOpenResponse(b'{"routes":[{"distance":120700.8}]}'))

    miles = fare._compute_distance_miles(*JAX_TERMINAL, *CLOISTER)
    # OSRM value (~75 mi) wins over haversine (~53 mi).
    assert 74 < miles < 76


def test_compute_distance_haversine_provider_skips_osrm(monkeypatch):
    """FARE_DISTANCE_PROVIDER=haversine never calls OSRM, even if reachable."""
    called = {"n": 0}

    def fake_urlopen(url, timeout=None):
        called["n"] += 1
        return _FakeURLOpenResponse(b'{"routes":[{"distance":120700.8}]}')

    monkeypatch.setenv("FARE_DISTANCE_PROVIDER", "haversine")
    monkeypatch.setattr(fare._urlrequest, "urlopen", fake_urlopen)

    miles = fare._compute_distance_miles(*JAX_TERMINAL, *CLOISTER)
    haversine = fare.haversine_miles(*JAX_TERMINAL, *CLOISTER)
    assert abs(miles - haversine) < 0.001
    assert called["n"] == 0  # OSRM was NOT called


# ---------------------------------------------------------------------------
# /admin/trips/{id}/action/quote endpoint
# ---------------------------------------------------------------------------


def _seed_card(session, vc=VehicleClass.SUV, active=True):
    c = RateCard(
        name=f"Test {vc.value}", vehicle_class=vc,
        base_fare_cents=15000, per_mile_cents=550, per_hour_cents=12500,
        airport_meet_surcharge_cents=2500, after_hours_surcharge_cents=2500,
        peak_week_multiplier=1.25, minimum_fare_cents=12500,
        active=active,
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _seed_trip(session, vc=VehicleClass.SUV):
    t = Trip(
        passenger_name="Mr. Whitfield",
        passenger_phone="+19045556789",
        pickup_label="JAX Terminal",
        pickup_lat=JAX_TERMINAL[0], pickup_lon=JAX_TERMINAL[1],
        dropoff_label="The Cloister, Sea Island",
        dropoff_lat=CLOISTER[0], dropoff_lon=CLOISTER[1],
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=4),
        vehicle_class=vc,
        status=TripStatus.REQUESTED,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def test_quote_endpoint_fills_fare_and_redirects(client, session):
    card = _seed_card(session)
    trip = _seed_trip(session)
    response = client.post(
        f"/admin/trips/{trip.id}/action/quote",
        data={"rate_card_id": str(card.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/admin/trips/{trip.id}")
    session.refresh(trip)
    assert trip.fare_cents is not None
    assert trip.fare_cents > 0


def test_quote_endpoint_auto_picks_card_for_vehicle_class(client, session):
    _seed_card(session, vc=VehicleClass.SUV)
    _seed_card(session, vc=VehicleClass.SPRINTER)
    trip = _seed_trip(session, vc=VehicleClass.SPRINTER)
    response = client.post(
        f"/admin/trips/{trip.id}/action/quote",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(trip)
    assert trip.fare_cents is not None


def test_quote_endpoint_no_matching_card_returns_flash(client, session):
    # Only SUV card; trip is Sprinter.
    _seed_card(session, vc=VehicleClass.SUV)
    trip = _seed_trip(session, vc=VehicleClass.SPRINTER)
    response = client.post(
        f"/admin/trips/{trip.id}/action/quote",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Flash should mention the missing card.
    assert "No+active+rate+card" in response.headers["location"] \
        or "No%20active%20rate%20card" in response.headers["location"]
    session.refresh(trip)
    assert trip.fare_cents is None  # unchanged


def test_quote_endpoint_inactive_card_skipped(client, session):
    _seed_card(session, vc=VehicleClass.SUV, active=False)
    active = _seed_card(session, vc=VehicleClass.SUV, active=True)
    trip = _seed_trip(session, vc=VehicleClass.SUV)
    response = client.post(
        f"/admin/trips/{trip.id}/action/quote",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(trip)
    assert trip.fare_cents is not None
