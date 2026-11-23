"""Tests for the live dispatcher map at /map and its data feed at /map/data.

Two surfaces under test:

    1. The HTML page: served by `/map`, must include Leaflet, a noindex meta
       tag, a `#map` container, the side panel scaffolding, and the new-trip
       drawer. Renders without database access (it's a static template).

    2. The JSON data feed: served by `/map/data` and used by the page's
       12-second poll loop. Must surface drivers (with locations),
       subcontractors (with bases), and active trips (with pickup +
       dropoff). Inactive subs and non-active trip statuses are filtered out.

Plus the new POST /drivers/{id}/location endpoint that the Phase-4.5 driver
PWA will use to push GPS coords without touching the driver's status.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import map_view as _map_view
from models import (
    Driver,
    DriverStatus,
    Subcontractor,
    SubcontractorTier,
    Trip,
    TripStatus,
    VehicleClass,
)


# ---------------------------------------------------------------------------
# Helpers (lighter than test_dashboard's — only what the map tests need)
# ---------------------------------------------------------------------------


def _seed_driver(session, *, name="Jane Driver", phone="+19045550001",
                 status=DriverStatus.AVAILABLE, classes="suv",
                 lat=31.15, lon=-81.49):
    d = Driver(
        name=name, phone=phone, status=status,
        vehicle_classes=classes, lat=lat, lon=lon,
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def _seed_sub(session, *, name="Sample Northshore Cars", tier=SubcontractorTier.TIER_1,
              phone="+18435551111", active=True, base_lat=32.16, base_lon=-80.75,
              radius=80.0):
    s = Subcontractor(
        name=name, dispatch_phone=phone, vehicle_classes="suv,sprinter",
        tier=tier, markup_pct=15.0,
        base_lat=base_lat, base_lon=base_lon, service_radius_mi=radius,
        active=active,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _seed_trip(session, *, status=TripStatus.BROADCASTING, passenger="Henderson",
               assigned_driver=None):
    t = Trip(
        passenger_name=passenger,
        passenger_phone="+19045559999",
        pickup_label="JAX Airport, baggage claim",
        pickup_lat=30.4941, pickup_lon=-81.6879,
        dropoff_label="The Cloister, Sea Island",
        dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=2),
        vehicle_class=VehicleClass.SUV,
        status=status,
        assigned_driver_id=assigned_driver.id if assigned_driver else None,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


# ---------------------------------------------------------------------------
# Route-level: /map returns the HTML page
# ---------------------------------------------------------------------------


def test_map_route_returns_html(client):
    response = client.get("/map")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_map_page_includes_leaflet(client):
    response = client.get("/map")
    body = response.text
    # Page must pull Leaflet (CSS + JS) from the CDN — these scripts are the
    # difference between "page renders" and "page actually does anything".
    assert "leaflet@1.9.4/dist/leaflet.css" in body
    assert "leaflet@1.9.4/dist/leaflet.js" in body


def test_map_page_is_noindex(client):
    response = client.get("/map")
    body = response.text
    # Internal dispatcher tool — must NOT be indexed even if the URL leaks.
    assert 'name="robots"' in body
    assert "noindex" in body


def test_map_page_has_required_dom(client):
    response = client.get("/map")
    body = response.text
    # The map container, side panel, dispatch banner, and new-trip drawer
    # are all required for the JS to function — assert each scaffold is
    # present so a regression in the renderer is caught immediately.
    assert 'id="map"' in body
    assert 'id="sidepanel"' in body
    assert 'id="dispatchBanner"' in body
    assert 'id="newTripDrawer"' in body
    assert 'id="newTripBtn"' in body
    # The four sidebar tabs scaffold:
    assert 'data-tab="trips"' in body
    assert 'data-tab="drivers"' in body
    assert 'data-tab="subs"' in body
    assert 'data-tab="legend"' in body


def test_map_page_links_back_to_dashboard(client):
    response = client.get("/map")
    body = response.text
    # Cross-link between dashboard and map is load-bearing for the
    # dispatcher's everyday flow — neither page should be a dead end.
    assert 'href="/dashboard"' in body


def test_dashboard_page_links_to_map(client):
    response = client.get("/dashboard")
    body = response.text
    assert 'href="/map"' in body


def test_map_page_carries_trip_status_pin_classes(client):
    """The /map page must render the per-status pin CSS classes so the JS
    `tripIcon(role, status)` factory and the legend can both reference
    them. Without these classes the status overlay regression-fails silently
    (icons render but all in the same brass color)."""
    response = client.get("/map")
    body = response.text
    # Each active TripStatus that can appear on the map must have a CSS
    # selector colored for it.
    for status in (
        "pin-status-requested",
        "pin-status-broadcasting",
        "pin-status-accepted",
        "pin-status-en_route_pickup",
        "pin-status-on_scene",
        "pin-status-in_transit",
    ):
        assert status in body, f"missing CSS for {status}"
    # The JS factory now takes status and emits the class.
    assert "tripIcon('pickup', t.status)" in body
    assert "tripIcon('dropoff', t.status)" in body
    assert "function tripIcon(role, status)" in body


def test_map_page_legend_includes_trip_status_rows(client):
    """The bottom-left map legend must explain the status colors so the
    dispatcher can decode the pin palette at a glance."""
    response = client.get("/map")
    body = response.text
    assert 'legend-status' in body
    # The five informative trip lifecycle states show up by name in the legend.
    assert 'Broadcasting' in body
    assert 'Accepted' in body
    assert 'En route pickup' in body
    assert 'On scene' in body
    assert 'In transit' in body


# ---------------------------------------------------------------------------
# /map/data — JSON shape + filtering
# ---------------------------------------------------------------------------


def test_map_data_empty_db_returns_empty_lists(client):
    response = client.get("/map/data")
    assert response.status_code == 200
    body = response.json()
    assert body["drivers"] == []
    assert body["subcontractors"] == []
    assert body["active_trips"] == []
    assert "generated_at" in body


def test_map_data_includes_drivers_with_locations(client, session):
    _seed_driver(session, name="Marcus", phone="+19125550101",
                 lat=31.15, lon=-81.49)
    _seed_driver(session, name="Eleanor", phone="+19125550102",
                 status=DriverStatus.ON_TRIP, lat=31.15, lon=-81.39)
    response = client.get("/map/data")
    body = response.json()
    names = [d["name"] for d in body["drivers"]]
    assert "Marcus" in names
    assert "Eleanor" in names
    # Coords + status round-trip correctly.
    eleanor = [d for d in body["drivers"] if d["name"] == "Eleanor"][0]
    assert eleanor["lat"] == 31.15
    assert eleanor["lon"] == -81.39
    assert eleanor["status"] == "on_trip"


def test_map_data_includes_active_subs_only(client, session):
    _seed_sub(session, name="Coast", phone="+18435551111", active=True)
    _seed_sub(session, name="Dormant", phone="+18435551112", active=False)
    response = client.get("/map/data")
    body = response.json()
    names = [s["name"] for s in body["subcontractors"]]
    assert "Coast" in names
    assert "Dormant" not in names


def test_map_data_only_returns_active_trips(client, session):
    _seed_trip(session, status=TripStatus.BROADCASTING, passenger="Live one")
    _seed_trip(session, status=TripStatus.COMPLETED, passenger="Old completed")
    _seed_trip(session, status=TripStatus.ACCEPTED, passenger="In motion")
    _seed_trip(session, status=TripStatus.CANCELLED, passenger="Cancelled")
    response = client.get("/map/data")
    body = response.json()
    names = [t["passenger_name"] for t in body["active_trips"]]
    assert "Live one" in names
    assert "In motion" in names
    assert "Old completed" not in names
    assert "Cancelled" not in names


def test_map_data_trip_carries_assignment_and_coordinates(client, session):
    driver = _seed_driver(session, name="Marcus")
    trip = _seed_trip(session, status=TripStatus.ACCEPTED, assigned_driver=driver)
    response = client.get("/map/data")
    body = response.json()
    t = [x for x in body["active_trips"] if x["id"] == trip.id][0]
    assert t["assigned_driver_id"] == driver.id
    assert t["assigned_subcontractor_id"] is None
    assert t["pickup_lat"] == 30.4941
    assert t["dropoff_lat"] == 31.1969


# ---------------------------------------------------------------------------
# Renderer-level: build_map_data directly (no HTTP round-trip)
# ---------------------------------------------------------------------------


def test_build_map_data_returns_expected_top_level_keys(session):
    data = _map_view.build_map_data(session)
    assert set(data.keys()) == {"generated_at", "drivers", "subcontractors", "active_trips"}
    assert isinstance(data["generated_at"], datetime)


def test_build_map_data_drivers_with_no_fix_still_included(session):
    """Drivers with no lat/lon should still appear in the payload so the
    sidebar can list them with a 'no fix' marker. The JS handles whether to
    plot a pin."""
    d = Driver(name="No GPS", phone="+19125550103",
               status=DriverStatus.AVAILABLE, vehicle_classes="suv",
               lat=None, lon=None)
    session.add(d)
    session.commit()
    data = _map_view.build_map_data(session)
    assert any(x["name"] == "No GPS" for x in data["drivers"])
    no_gps = next(x for x in data["drivers"] if x["name"] == "No GPS")
    assert no_gps["lat"] is None and no_gps["lon"] is None


# ---------------------------------------------------------------------------
# POST /drivers/{id}/location — PWA-facing location endpoint
# ---------------------------------------------------------------------------


def test_update_driver_location_updates_coords(client, session):
    d = _seed_driver(session, name="Marcus", phone="+19125550101",
                     lat=31.15, lon=-81.49)
    response = client.post(
        f"/drivers/{d.id}/location",
        json={"lat": 31.20, "lon": -81.42},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lat"] == 31.20
    assert body["lon"] == -81.42


def test_update_driver_location_does_not_change_status(client, session):
    d = _seed_driver(session, name="Marcus", phone="+19125550101",
                     status=DriverStatus.AVAILABLE)
    response = client.post(
        f"/drivers/{d.id}/location",
        json={"lat": 31.20, "lon": -81.42},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "available"


def test_update_driver_location_404_for_unknown_driver(client):
    response = client.post(
        "/drivers/9999/location",
        json={"lat": 31.20, "lon": -81.42},
    )
    assert response.status_code == 404


def test_update_driver_location_requires_both_lat_and_lon(client, session):
    d = _seed_driver(session, name="Marcus", phone="+19125550101")
    # Missing lon — pydantic should 422 us.
    response = client.post(f"/drivers/{d.id}/location", json={"lat": 31.0})
    assert response.status_code == 422
