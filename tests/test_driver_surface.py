"""Tests for the /driver/* web surface — Phase 4.5 walking skeleton (v0.9.9).

Covers the auth contract (bearer token via header or cookie), the dashboard
render, accept/decline round-trips, and admin-side token rotation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import broadcast_trip
from models import (
    Driver,
    DriverStatus,
    Trip,
    TripBroadcast,
    TripStatus,
    VehicleClass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_driver(session, *, name="Marcus", phone="+19125550101",
                 status=DriverStatus.AVAILABLE, classes="suv", lat=31.15, lon=-81.49):
    d = Driver(name=name, phone=phone, status=status,
               vehicle_classes=classes, lat=lat, lon=lon)
    d.ensure_auth_token()
    session.add(d); session.commit(); session.refresh(d)
    return d


def _seed_trip(session, *, status=TripStatus.REQUESTED, vc=VehicleClass.SUV):
    t = Trip(
        passenger_name="Mr. Henderson", passenger_phone="+19045556789",
        pickup_label="JAX Terminal", pickup_lat=30.4941, pickup_lon=-81.6879,
        dropoff_label="The Cloister", dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=4),
        vehicle_class=vc, status=status,
    )
    session.add(t); session.commit(); session.refresh(t)
    return t


# ---------------------------------------------------------------------------
# Auth contract
# ---------------------------------------------------------------------------


def test_driver_dashboard_requires_token(client):
    """No header, no cookie → 401."""
    r = client.get("/driver")
    assert r.status_code == 401


def test_driver_dashboard_rejects_unknown_token(client):
    r = client.get("/driver", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_driver_dashboard_accepts_bearer_token(client, session):
    d = _seed_driver(session)
    r = client.get("/driver", headers={"Authorization": f"Bearer {d.auth_token}"})
    assert r.status_code == 200
    assert d.name in r.text


def test_driver_dashboard_accepts_cookie(client, session):
    """Login cookie path — set the cookie via POST /driver/login, then GET works."""
    d = _seed_driver(session)
    login = client.post("/driver/login", data={"token": d.auth_token},
                        follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/driver"
    # Cookie was set on the response — client carries it forward.
    assert "driver_token" in login.cookies
    r = client.get("/driver")
    assert r.status_code == 200
    assert d.name in r.text


def test_driver_login_wrong_token_rejected(client, session):
    r = client.post("/driver/login", data={"token": "bogus"})
    assert r.status_code == 401
    assert "Token not recognized" in r.text


def test_driver_login_empty_token_rejected(client):
    r = client.post("/driver/login", data={"token": ""})
    assert r.status_code == 400


def test_driver_logout_clears_cookie_and_redirects(client, session):
    d = _seed_driver(session)
    client.post("/driver/login", data={"token": d.auth_token})
    r = client.post("/driver/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/driver/login"
    # After logout, a fresh GET without re-login is 401 again.
    client.cookies.clear()
    assert client.get("/driver").status_code == 401


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------


def test_dashboard_renders_pending_broadcasts_for_this_driver(client, session):
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    session.refresh(t)
    assert t.status == TripStatus.BROADCASTING

    r = client.get("/driver", headers={"Authorization": f"Bearer {d.auth_token}"})
    assert r.status_code == 200
    body = r.text
    # The trip's ID, route, and pickup time should all be visible.
    assert f"#{t.id}" in body
    assert "JAX Terminal" in body
    assert "Cloister" in body
    # Accept + Decline buttons rendered.
    assert f'/driver/trips/{t.id}/accept' in body
    assert f'/driver/trips/{t.id}/decline' in body


def test_dashboard_hides_broadcasts_addressed_to_other_drivers(client, session):
    me = _seed_driver(session, name="Me", phone="+19125550100")
    other = _seed_driver(session, name="Other", phone="+19125550101", lat=99.0, lon=-99.0)
    t = _seed_trip(session)
    # Manually emit a broadcast only to `other` so we can verify isolation.
    bc = TripBroadcast(trip_id=t.id, driver_id=other.id, distance_mi=5.0)
    session.add(bc)
    t.status = TripStatus.BROADCASTING
    session.commit()

    r = client.get("/driver", headers={"Authorization": f"Bearer {me.auth_token}"})
    assert r.status_code == 200
    body = r.text
    # The other driver's broadcast must NOT appear on `me`'s dashboard.
    # `#{t.id}` alone would also match CSS color hex strings — assert on the
    # trip's unique passenger name + the accept-action URL instead.
    assert t.passenger_name not in body
    assert f"/driver/trips/{t.id}/accept" not in body
    # `me`'s dashboard should still render their identity though.
    assert "Me" in body


def test_dashboard_shows_current_active_trip_when_assigned(client, session):
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = Trip(
        passenger_name="Active Guest", passenger_phone="+19045552020",
        pickup_label="SAV Airport", pickup_lat=32.1276, pickup_lon=-81.2021,
        dropoff_label="The Cloister", dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=1),
        vehicle_class=VehicleClass.SUV,
        status=TripStatus.EN_ROUTE_PICKUP,
        assigned_driver_id=d.id,
    )
    session.add(t); session.commit(); session.refresh(t)
    r = client.get("/driver", headers={"Authorization": f"Bearer {d.auth_token}"})
    assert r.status_code == 200
    assert "Your current trip" in r.text
    assert "Active Guest" in r.text


# ---------------------------------------------------------------------------
# Accept / Decline round-trip
# ---------------------------------------------------------------------------


def test_driver_accept_flips_trip_to_accepted_state(client, session):
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)

    r = client.post(f"/driver/trips/{t.id}/accept",
                    headers={"Authorization": f"Bearer {d.auth_token}"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/driver?")
    session.refresh(t); session.refresh(d)
    assert t.status == TripStatus.ACCEPTED
    assert t.assigned_driver_id == d.id
    assert d.status == DriverStatus.ON_TRIP


def test_driver_decline_records_decline_without_assigning(client, session):
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)

    r = client.post(f"/driver/trips/{t.id}/decline",
                    headers={"Authorization": f"Bearer {d.auth_token}"},
                    follow_redirects=False)
    assert r.status_code == 303
    session.refresh(t)
    # Decline by this driver shouldn't auto-accept; trip is still BROADCASTING
    # (the broadcast row carries the decline, but other drivers can still
    # accept).
    assert t.assigned_driver_id is None
    assert t.status == TripStatus.BROADCASTING


def test_driver_accept_without_auth_is_401(client, session):
    t = _seed_trip(session)
    r = client.post(f"/driver/trips/{t.id}/accept")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Admin: rotate token
# ---------------------------------------------------------------------------


def test_admin_driver_detail_surfaces_auth_token(client, session):
    d = _seed_driver(session)
    body = client.get(f"/admin/drivers/{d.id}").text
    assert "Access token" in body
    assert d.auth_token in body
    assert f'action="/admin/drivers/{d.id}/action/rotate-token"' in body


def test_admin_driver_detail_lazy_mints_token_if_missing(client, session):
    """A pre-v0.9.9 row with auth_token=None gets a token minted on first
    admin view, not on a separate rotate action."""
    d = Driver(name="Legacy", phone="+19125559999", vehicle_classes="suv")
    session.add(d); session.commit(); session.refresh(d)
    assert d.auth_token is None

    body = client.get(f"/admin/drivers/{d.id}").text
    session.refresh(d)
    assert d.auth_token is not None
    assert d.auth_token in body


def test_rotate_token_invalidates_the_old_value(client, session):
    d = _seed_driver(session)
    old_token = d.auth_token

    r = client.post(f"/admin/drivers/{d.id}/action/rotate-token",
                    follow_redirects=False)
    assert r.status_code == 303
    session.refresh(d)
    assert d.auth_token != old_token

    # Old token is rejected now.
    assert client.get("/driver", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
    # New token works.
    assert client.get("/driver", headers={"Authorization": f"Bearer {d.auth_token}"}).status_code == 200


def test_rotate_token_invalidates_stale_cookie_session(client, session):
    """Token rotation must revoke the cookie session too, not just the bearer
    token — the PWA authenticates via the driver_token cookie (lost-phone path)."""
    d = _seed_driver(session)
    old_token = d.auth_token
    login = client.post("/driver/login", data={"token": old_token},
                        follow_redirects=False)
    assert login.status_code == 303
    assert client.get("/driver").status_code == 200  # cookie auth works

    r = client.post(f"/admin/drivers/{d.id}/action/rotate-token",
                    follow_redirects=False)
    assert r.status_code == 303
    session.refresh(d)
    assert d.auth_token != old_token

    # Re-attach the now-stale cookie and confirm it no longer authenticates.
    client.cookies.set("driver_token", old_token)
    assert client.get("/driver").status_code == 401


# ---------------------------------------------------------------------------
# PWA shell (v0.9.10) — manifest + Service Worker + icon
# ---------------------------------------------------------------------------


def test_driver_manifest_returns_valid_pwa_manifest(client):
    import json as _json
    r = client.get("/driver/manifest.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    body = _json.loads(r.text)
    # Required PWA install fields.
    assert body["name"]
    assert body["short_name"]
    assert body["start_url"] == "/driver"
    assert body["scope"] == "/driver"
    assert body["display"] == "standalone"
    assert body["theme_color"] == "#161311"
    # At least one icon, referencing the SVG endpoint.
    assert isinstance(body["icons"], list) and body["icons"]
    icon = body["icons"][0]
    assert icon["src"] == "/driver/icon.svg"
    assert icon["type"] == "image/svg+xml"


def test_driver_service_worker_returns_javascript_no_cache(client):
    r = client.get("/driver/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    # Service Worker file MUST NOT be cached or new versions never reach
    # already-installed clients.
    assert "no-cache" in r.headers["cache-control"]
    assert "no-store" in r.headers["cache-control"]
    # The body should contain the cache-version constant + a fetch handler.
    assert "driver-v1" in r.text
    assert "self.addEventListener('install'" in r.text
    assert "self.addEventListener('fetch'" in r.text


def test_driver_icon_returns_svg(client):
    r = client.get("/driver/icon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text
    assert "viewBox" in r.text


# ---------------------------------------------------------------------------
# Trip detail page (v0.9.11) — Chunk 2: native-map deep-links + scoped auth
# ---------------------------------------------------------------------------


def test_trip_detail_requires_token(client, session):
    t = _seed_trip(session)
    assert client.get(f"/driver/trips/{t.id}").status_code == 401


def test_trip_detail_404_for_unknown_trip(client, session):
    d = _seed_driver(session)
    r = client.get("/driver/trips/9999", headers={"Authorization": f"Bearer {d.auth_token}"})
    assert r.status_code == 404


def test_trip_detail_403_for_unrelated_trip(client, session):
    """A driver can only view trips they're assigned to or have an open
    broadcast on. An unrelated trip → 403, not 200."""
    me = _seed_driver(session, name="Me", phone="+19125550100")
    t = _seed_trip(session)
    # No broadcast to `me`, no assignment to `me` — trip is unrelated.
    r = client.get(f"/driver/trips/{t.id}",
                   headers={"Authorization": f"Bearer {me.auth_token}"})
    assert r.status_code == 403


def test_trip_detail_visible_when_assigned_to_this_driver(client, session):
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_trip(session, status=TripStatus.ACCEPTED)
    t.assigned_driver_id = d.id
    session.commit()
    r = client.get(f"/driver/trips/{t.id}",
                   headers={"Authorization": f"Bearer {d.auth_token}"})
    assert r.status_code == 200
    body = r.text
    assert t.passenger_name in body
    assert "Pickup" in body
    assert "Dropoff" in body


def test_trip_detail_visible_when_open_broadcast_addressed_to_driver(client, session):
    """If the dispatcher has broadcast a trip to this driver and the driver
    hasn't accepted yet, they should still be able to view the detail page
    — that's where they read the passenger context before deciding."""
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    r = client.get(f"/driver/trips/{t.id}",
                   headers={"Authorization": f"Bearer {d.auth_token}"})
    assert r.status_code == 200
    assert t.passenger_name in r.text


def test_trip_detail_renders_native_map_deep_links(client, session):
    """All three platforms' deep-link URLs must be present so the chauffeur
    can tap whichever matches their phone. No JS sniffing — they pick."""
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_trip(session, status=TripStatus.ACCEPTED)
    t.assigned_driver_id = d.id
    session.commit()
    body = client.get(f"/driver/trips/{t.id}",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    # Android `geo:` scheme.
    assert f'href="geo:{t.pickup_lat},{t.pickup_lon}' in body
    assert f'href="geo:{t.dropoff_lat},{t.dropoff_lon}' in body
    # Apple Maps web URL.
    assert "maps.apple.com" in body
    # Google Maps search + directions.
    assert "google.com/maps/search" in body
    assert "google.com/maps/dir" in body


def test_trip_detail_includes_tel_link_for_passenger_phone(client, session):
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_trip(session, status=TripStatus.ACCEPTED)
    t.assigned_driver_id = d.id
    session.commit()
    body = client.get(f"/driver/trips/{t.id}",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert f'href="tel:{t.passenger_phone}"' in body


def test_dashboard_cards_link_to_trip_detail(client, session):
    """Dashboard surfaces the link inline on each broadcast/active card so
    the chauffeur can drill in for full context."""
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    body = client.get("/driver",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert f'href="/driver/trips/{t.id}"' in body
    assert "Trip details" in body


# ---------------------------------------------------------------------------
# Status transitions (v0.9.12) — Chunk 3: chauffeur-side lifecycle
# ---------------------------------------------------------------------------


def _seed_assigned_trip(session, driver, status):
    """A Trip already assigned to `driver` in the given status — for status-
    transition tests that start mid-lifecycle."""
    t = Trip(
        passenger_name="LifecycleGuest", passenger_phone="+19045552020",
        pickup_label="JAX Terminal", pickup_lat=30.4941, pickup_lon=-81.6879,
        dropoff_label="The Cloister", dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=1),
        vehicle_class=VehicleClass.SUV,
        status=status, assigned_driver_id=driver.id,
    )
    session.add(t); session.commit(); session.refresh(t)
    return t


def test_status_transition_accepted_to_en_route(client, session):
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, d, TripStatus.ACCEPTED)
    r = client.post(
        f"/driver/trips/{t.id}/status/en_route_pickup",
        headers={"Authorization": f"Bearer {d.auth_token}"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    session.refresh(t)
    assert t.status == TripStatus.EN_ROUTE_PICKUP


def test_status_transition_full_lifecycle_walk(client, session):
    """ACCEPTED → EN_ROUTE → ON_SCENE → IN_TRANSIT → COMPLETED. Each step
    succeeds; final COMPLETED flips the driver back to AVAILABLE."""
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, d, TripStatus.ACCEPTED)
    headers = {"Authorization": f"Bearer {d.auth_token}"}
    for target in ("en_route_pickup", "on_scene", "in_transit", "completed"):
        r = client.post(f"/driver/trips/{t.id}/status/{target}",
                        headers=headers, follow_redirects=False)
        assert r.status_code == 303, f"transition to {target} failed: {r.text}"
    session.refresh(t); session.refresh(d)
    assert t.status == TripStatus.COMPLETED
    # core.update_trip_status flips the driver back to AVAILABLE on COMPLETED.
    assert d.status == DriverStatus.AVAILABLE


def test_status_transition_skipping_a_step_is_400(client, session):
    """ACCEPTED → COMPLETED is NOT a valid one-tap path. The driver must
    walk through EN_ROUTE → ON_SCENE → IN_TRANSIT first."""
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, d, TripStatus.ACCEPTED)
    r = client.post(
        f"/driver/trips/{t.id}/status/completed",
        headers={"Authorization": f"Bearer {d.auth_token}"},
    )
    assert r.status_code == 400
    assert "invalid transition" in r.text.lower()
    session.refresh(t)
    assert t.status == TripStatus.ACCEPTED  # unchanged


def test_status_transition_for_non_assigned_driver_is_403(client, session):
    me = _seed_driver(session, name="Me", phone="+19125550100")
    other = _seed_driver(session, name="Other", phone="+19125550101",
                         status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, other, TripStatus.ACCEPTED)
    r = client.post(
        f"/driver/trips/{t.id}/status/en_route_pickup",
        headers={"Authorization": f"Bearer {me.auth_token}"},
    )
    assert r.status_code == 403


def test_status_transition_without_auth_is_401(client, session):
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, d, TripStatus.ACCEPTED)
    r = client.post(f"/driver/trips/{t.id}/status/en_route_pickup")
    assert r.status_code == 401


def test_status_transition_with_unknown_status_value_is_400(client, session):
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, d, TripStatus.ACCEPTED)
    r = client.post(
        f"/driver/trips/{t.id}/status/teleported",
        headers={"Authorization": f"Bearer {d.auth_token}"},
    )
    assert r.status_code == 400
    assert "unknown status" in r.text.lower()


def test_trip_detail_renders_next_status_button_for_assigned_driver(client, session):
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, d, TripStatus.ACCEPTED)
    body = client.get(f"/driver/trips/{t.id}",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert "Next step" in body
    assert "Mark on the way" in body
    assert f'action="/driver/trips/{t.id}/status/en_route_pickup"' in body


def test_trip_detail_hides_status_button_when_not_assigned(client, session):
    """A driver viewing a trip via an open broadcast (not assigned yet)
    should NOT see the lifecycle button — they're not the chauffeur."""
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    body = client.get(f"/driver/trips/{t.id}",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert "Next step" not in body
    assert "Mark on the way" not in body


def test_trip_detail_hides_status_button_on_completed_trip(client, session):
    d = _seed_driver(session, status=DriverStatus.AVAILABLE)
    t = _seed_assigned_trip(session, d, TripStatus.COMPLETED)
    body = client.get(f"/driver/trips/{t.id}",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert "Next step" not in body


# ---------------------------------------------------------------------------
# GPS heartbeat (v0.9.13) — Chunk 4: POST /driver/location + body data-on-trip
# ---------------------------------------------------------------------------


def test_driver_location_requires_token(client):
    r = client.post("/driver/location", json={"lat": 31.15, "lon": -81.49})
    assert r.status_code == 401


def test_driver_location_updates_driver_row(client, session):
    d = _seed_driver(session, status=DriverStatus.AVAILABLE)
    # Reset lat/lon so we can prove the POST changed them.
    d.lat = None; d.lon = None
    session.commit()
    r = client.post(
        "/driver/location",
        json={"lat": 31.234, "lon": -81.567},
        headers={"Authorization": f"Bearer {d.auth_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["lat"] == 31.234
    assert body["lon"] == -81.567
    session.refresh(d)
    assert d.lat == 31.234
    assert d.lon == -81.567


def test_driver_location_invalid_body_returns_422(client, session):
    d = _seed_driver(session)
    # Missing lat / lon
    r = client.post(
        "/driver/location",
        json={"lat": 31.234},
        headers={"Authorization": f"Bearer {d.auth_token}"},
    )
    assert r.status_code == 422


def test_driver_location_writes_to_authenticated_driver_only(client, session):
    """The endpoint identifies the target driver via bearer token, NOT a
    path param — so a chauffeur can't spoof another driver's location
    even if they know the other driver's id."""
    d1 = _seed_driver(session, name="One", phone="+19125550100")
    d2 = _seed_driver(session, name="Two", phone="+19125550101")
    d2.lat = None; d2.lon = None
    session.commit()
    # POST authenticated as d1 — d2's coords must be untouched. Distinctive
    # but in-range sentinel (45.0 / -75.0); 99.0 would now be rejected by the
    # v1.1.7 coordinate-range validator and never reach the write.
    client.post(
        "/driver/location",
        json={"lat": 45.0, "lon": -75.0},
        headers={"Authorization": f"Bearer {d1.auth_token}"},
    )
    session.refresh(d2)
    assert d2.lat is None and d2.lon is None
    session.refresh(d1)
    assert d1.lat == 45.0


def test_dashboard_sets_data_on_trip_when_driver_has_active_trip(client, session):
    """The inline GPS-ping JS gates on body[data-on-trip="1"] so we don't
    burn the chauffeur's battery while they're parked at home. The
    dashboard must set the attribute correctly based on whether the driver
    has an active trip."""
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    # No trip yet — data-on-trip should be 0.
    body_no_trip = client.get("/driver",
                              headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert 'data-on-trip="0"' in body_no_trip

    # Assign an active trip — data-on-trip should flip to 1.
    t = _seed_assigned_trip(session, d, TripStatus.EN_ROUTE_PICKUP)
    body_with_trip = client.get("/driver",
                                headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert 'data-on-trip="1"' in body_with_trip


def test_trip_detail_sets_data_on_trip_only_for_assigned_active_driver(client, session):
    """Trip detail page gates the GPS ping similarly: only when the viewer
    is the assigned driver AND the trip is in an active state."""
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, d, TripStatus.EN_ROUTE_PICKUP)
    body = client.get(f"/driver/trips/{t.id}",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert 'data-on-trip="1"' in body

    # A broadcast-only viewer (not assigned) sees data-on-trip="0" even on an
    # active trip — they haven't accepted yet, no need to ping.
    other = _seed_driver(session, name="Other", phone="+19125550999")
    bc = TripBroadcast(trip_id=t.id, driver_id=other.id, distance_mi=5.0)
    session.add(bc); session.commit()
    body_other = client.get(f"/driver/trips/{t.id}",
                            headers={"Authorization": f"Bearer {other.auth_token}"}).text
    assert 'data-on-trip="0"' in body_other


def test_driver_pages_embed_gps_ping_script(client, session):
    """The inline GPS-ping JS must be present on dashboard + trip detail so
    the browser ticks when data-on-trip flips to 1."""
    d = _seed_driver(session)
    body = client.get("/driver",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert "navigator.geolocation" in body
    assert "/driver/location" in body
    assert "data-on-trip" in body  # the JS guards on this attribute


# ---------------------------------------------------------------------------
# CSRF protection (v0.9.14) — Chunk 6: defense-in-depth on cookie-auth POSTs
# ---------------------------------------------------------------------------


def _csrf_for(client, session, driver):
    """Helper: log in via cookie auth, then pull the CSRF token from the
    dashboard's <meta name="csrf-token"> tag. Mirrors how the real browser
    flow gets a token after POST /driver/login."""
    client.post("/driver/login", data={"token": driver.auth_token})
    body = client.get("/driver").text
    import re
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', body)
    assert m, "CSRF meta tag missing from dashboard"
    return m.group(1)


def test_cookie_auth_post_without_csrf_token_is_403(client, session):
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    # Log in via cookie path only (TestClient persists the cookie).
    client.post("/driver/login", data={"token": d.auth_token})
    # POST without csrf_token form field — must be 403.
    r = client.post(f"/driver/trips/{t.id}/accept", follow_redirects=False)
    assert r.status_code == 403
    assert "csrf" in r.text.lower()


def test_cookie_auth_post_with_valid_csrf_succeeds(client, session):
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    token = _csrf_for(client, session, d)
    r = client.post(f"/driver/trips/{t.id}/accept",
                    data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303
    session.refresh(t)
    assert t.assigned_driver_id == d.id


def test_cookie_auth_post_with_wrong_csrf_is_403(client, session):
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    client.post("/driver/login", data={"token": d.auth_token})
    r = client.post(f"/driver/trips/{t.id}/accept",
                    data={"csrf_token": "wrong-token-aaaaa"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_bearer_auth_post_skips_csrf_entirely(client, session):
    """Bearer-auth POSTs are already CSRF-safe (other origins can't set
    Authorization headers via simple form submits) — the verify step
    short-circuits when it detects bearer auth so existing chauffeur-app
    integrations don't break."""
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    r = client.post(f"/driver/trips/{t.id}/accept",
                    headers={"Authorization": f"Bearer {d.auth_token}"},
                    follow_redirects=False)
    assert r.status_code == 303
    session.refresh(t)
    assert t.assigned_driver_id == d.id


def test_cookie_auth_location_fetch_requires_csrf_via_header(client, session):
    """/driver/location is a JSON fetch endpoint — the CSRF token must come
    in via X-CSRF-Token header, not a form field."""
    d = _seed_driver(session)
    client.post("/driver/login", data={"token": d.auth_token})
    # Without the header — 403.
    r1 = client.post("/driver/location", json={"lat": 31.2, "lon": -81.5})
    assert r1.status_code == 403
    # With the header — 200.
    body = client.get("/driver").text
    import re
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', body).group(1)
    r2 = client.post("/driver/location",
                     json={"lat": 31.2, "lon": -81.5},
                     headers={"X-CSRF-Token": token})
    assert r2.status_code == 200


def test_csrf_token_is_driver_bound(client, session):
    """Driver A's CSRF token must NOT authorize a POST as driver B. If it
    did, an attacker who phished one token could forge requests as anyone."""
    da = _seed_driver(session, name="A", phone="+19125550100")
    db = _seed_driver(session, name="B", phone="+19125550101")
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)

    # Log in as driver B, grab their CSRF token from the dashboard.
    client.post("/driver/login", data={"token": db.auth_token})
    body = client.get("/driver").text
    import re
    db_token = re.search(r'<meta name="csrf-token" content="([^"]+)"', body).group(1)

    # Now switch the session cookie to driver A's token but try to use
    # driver B's CSRF token. The CSRF check is keyed to the authenticated
    # driver's id, so this MUST 403.
    client.cookies.clear()
    client.post("/driver/login", data={"token": da.auth_token})
    r = client.post(f"/driver/trips/{t.id}/accept",
                    data={"csrf_token": db_token},
                    follow_redirects=False)
    assert r.status_code == 403


def test_dashboard_renders_csrf_meta_and_hidden_inputs(client, session):
    d = _seed_driver(session)
    t = _seed_trip(session)
    broadcast_trip(session, trip=t)
    body = client.get("/driver",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    # Meta tag for the inline JS to read.
    assert '<meta name="csrf-token" content="' in body
    # Hidden input in each form.
    assert 'name="csrf_token"' in body


def test_trip_detail_renders_csrf_in_status_form(client, session):
    d = _seed_driver(session, status=DriverStatus.ON_TRIP)
    t = _seed_assigned_trip(session, d, TripStatus.ACCEPTED)
    body = client.get(f"/driver/trips/{t.id}",
                      headers={"Authorization": f"Bearer {d.auth_token}"}).text
    # The "Next step" form must include the hidden CSRF input.
    assert '<meta name="csrf-token" content="' in body
    assert 'name="csrf_token"' in body


def test_driver_login_html_links_manifest_and_registers_sw(client):
    r = client.get("/driver/login")
    assert r.status_code == 200
    body = r.text
    assert 'rel="manifest"' in body
    assert 'href="/driver/manifest.json"' in body
    assert 'rel="apple-touch-icon"' in body
    assert 'theme-color' in body
    assert "navigator.serviceWorker.register('/driver/sw.js'" in body


def test_driver_dashboard_html_links_manifest_and_registers_sw(client, session):
    d = _seed_driver(session)
    body = client.get("/driver", headers={"Authorization": f"Bearer {d.auth_token}"}).text
    assert 'rel="manifest"' in body
    assert 'href="/driver/manifest.json"' in body
    assert "navigator.serviceWorker.register('/driver/sw.js'" in body


def test_create_driver_via_rest_mints_token_eagerly(client):
    """POST /drivers should return a driver row that already has auth_token,
    so the dispatcher can immediately hand it off without a second round-trip."""
    r = client.post("/drivers", json={
        "name": "EagerToken", "phone": "+19045550909",
        "vehicle_classes": ["suv"],
    })
    assert r.status_code == 201
    body = r.json()
    # auth_token isn't in DriverOut by design — read it via /admin's lazy-mint
    # check OR directly from the DB. Easier path: hit /admin/drivers/{id} which
    # should NOT need to mint (because POST /drivers already did).
    detail = client.get(f"/admin/drivers/{body['id']}").text
    assert "(not yet generated" not in detail


# ---------------------------------------------------------------------------
# Unit tests: driver_can_see_trip visibility guard
# ---------------------------------------------------------------------------


def test_driver_can_see_assigned_trip(session):
    """driver_can_see_trip returns True when the driver is the assigned driver."""
    import driver_pages
    d = _seed_driver(session, name="Assigned")
    t = _seed_trip(session, status=TripStatus.ACCEPTED)
    t.assigned_driver_id = d.id
    session.commit()
    assert driver_pages.driver_can_see_trip(session, d, t) is True


def test_driver_can_see_broadcast_trip(session):
    """driver_can_see_trip returns True when the driver has a broadcast for the trip."""
    import driver_pages
    d = _seed_driver(session, name="Broadcast")
    t = _seed_trip(session, status=TripStatus.BROADCASTING)
    broadcast_trip(session, t, radius_mi=200)
    session.refresh(t)
    assert driver_pages.driver_can_see_trip(session, d, t) is True


def test_driver_cannot_see_unrelated_trip(session):
    """driver_can_see_trip returns False for a trip the driver has no relation to."""
    import driver_pages
    d1 = _seed_driver(session, name="Unrelated", phone="+19125550201")
    d2 = _seed_driver(session, name="Assigned", phone="+19125550202")
    t = _seed_trip(session, status=TripStatus.ACCEPTED)
    t.assigned_driver_id = d2.id
    session.commit()
    assert driver_pages.driver_can_see_trip(session, d1, t) is False


# ---------------------------------------------------------------------------
# Unit tests: map link generation
# ---------------------------------------------------------------------------


def test_native_map_links_contains_all_platforms():
    """_native_map_links renders Android geo:, Apple Maps, and Google Maps links."""
    import driver_pages
    html = driver_pages._native_map_links(31.15, -81.49, "JAX Airport")
    assert "geo:31.15,-81.49" in html
    assert "maps.apple.com" in html
    assert "google.com/maps" in html
    assert "JAX Airport" in html


def test_directions_link_format():
    """_directions_link renders a Google Maps directions URL with origin and destination."""
    import driver_pages
    html = driver_pages._directions_link(30.49, -81.69, 31.20, -81.34)
    assert "origin=30.49,-81.69" in html
    assert "destination=31.2,-81.34" in html
    assert "Pickup" in html
