"""Tests for the dispatcher dashboard.

The dashboard is single-route HTML rendered server-side from dashboard.py.
We test both the route (returns 200 + HTML) and the underlying renderer (correct
content for a populated session). The renderer is unit-tested directly to avoid
having to round-trip every assertion through the HTTP layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import dashboard as _dashboard
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


# ---------------------------------------------------------------------------
# Route-level: dashboard reachable, HTML, auto-refresh wired
# ---------------------------------------------------------------------------


def test_dashboard_route_returns_html(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_dashboard_route_renders_with_empty_db(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.text
    assert "Livery" in body
    assert "Dispatch" in body
    # Empty-state copy is rendered when there's nothing in the DB.
    assert "No trips currently in motion" in body
    assert "No pending reservations to review" in body


def test_dashboard_includes_auto_refresh_meta_tag(client):
    response = client.get("/dashboard")
    assert 'http-equiv="refresh"' in response.text


def test_dashboard_marked_noindex(client):
    response = client.get("/dashboard")
    # Internal dispatcher tool — must NOT be indexed even if the route leaks out.
    assert 'name="robots"' in response.text
    assert "noindex" in response.text


# ---------------------------------------------------------------------------
# Renderer: populated DB surfaces the right rows + counts
# ---------------------------------------------------------------------------


def _seed_driver(session, name="Jane Driver", phone="+19045550001",
                 status=DriverStatus.AVAILABLE, classes="suv"):
    d = Driver(name=name, phone=phone, status=status, vehicle_classes=classes,
               lat=31.15, lon=-81.49)
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def _seed_trip(session, driver=None, sub=None, status=TripStatus.BROADCASTING,
               passenger="Henderson", fare_cents=None):
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
        fare_cents=fare_cents,
        assigned_driver_id=driver.id if driver else None,
        assigned_subcontractor_id=sub.id if sub else None,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _seed_reservation(session, name="Smith party"):
    r = Reservation(
        name=name,
        email="smith@example.com",
        phone="+19045551111",
        pickup_label="SAV Airport",
        dropoff_label="King and Prince, SSI",
        pickup_date="2026-05-20",
        pickup_time_str="14:30",
        pickup_at=datetime(2026, 5, 20, 14, 30),
        passengers="2",
        vehicle_pref="escalade-esv",
        vehicle_class=VehicleClass.SUV,
        source="site/sea-island.html#reserve",
        status=ReservationStatus.PENDING_REVIEW,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def _seed_inquiry(session, kind=InquiryKind.WEDDING, name="Sarah Planner"):
    i = Inquiry(
        kind=kind,
        name=name,
        email="sarah@plannerco.com",
        phone="+19045552222",
        details_json='{"weekend_date": "2026-10-15", "guests": 80, "resort": "Sea Island"}',
        status=InquiryStatus.PENDING_REVIEW,
    )
    session.add(i)
    session.commit()
    session.refresh(i)
    return i


def _seed_sub(session, name="Sample Northshore Cars", tier=SubcontractorTier.TIER_1,
              phone="+18435551111"):
    s = Subcontractor(
        name=name,
        dispatch_phone=phone,
        vehicle_classes="suv,sprinter",
        tier=tier,
        markup_pct=15.0,
        base_lat=32.1656, base_lon=-80.7510,
        service_radius_mi=120.0,
        active=True,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def test_renderer_lists_active_trip(session):
    driver = _seed_driver(session, name="Mike Chauffeur")
    _seed_trip(session, driver=driver, status=TripStatus.EN_ROUTE_PICKUP, passenger="Henderson")
    html = _dashboard.render_dashboard(session)
    assert "Henderson" in html
    assert "Mike Chauffeur" in html
    assert "En Route Pickup" in html  # status label rendered in Title Case


def test_renderer_lists_pending_reservation(session):
    _seed_reservation(session, name="Smith party")
    html = _dashboard.render_dashboard(session)
    assert "Smith party" in html
    assert "SAV Airport" in html
    assert "King and Prince" in html


def test_renderer_lists_pending_inquiry(session):
    _seed_inquiry(session)
    html = _dashboard.render_dashboard(session)
    assert "Sarah Planner" in html
    assert "Wedding" in html  # kind label rendered


def test_renderer_lists_drivers_and_subs(session):
    _seed_driver(session, name="Alice Available", status=DriverStatus.AVAILABLE)
    _seed_driver(session, name="Bob Offline", phone="+19045550002", status=DriverStatus.OFFLINE)
    _seed_sub(session, name="Sample Northshore Cars", tier=SubcontractorTier.TIER_1,
              phone="+18435551111")
    _seed_sub(session, name="Backup Operator", tier=SubcontractorTier.TIER_2,
              phone="+18435552222")
    html = _dashboard.render_dashboard(session)
    assert "Alice Available" in html
    assert "Bob Offline" in html
    assert "Sample Northshore Cars" in html
    assert "Backup Operator" in html
    assert "Tier 1" in html
    assert "Tier 2" in html


def test_renderer_summary_counts_match_db(session):
    # Two active trips, one completed (shouldn't count as active), 1 pending reservation
    driver = _seed_driver(session, name="A")
    _seed_trip(session, driver=driver, status=TripStatus.ACCEPTED)
    _seed_trip(session, status=TripStatus.BROADCASTING)
    _seed_trip(session, status=TripStatus.COMPLETED, fare_cents=42500)
    _seed_reservation(session)
    _seed_inquiry(session)

    html = _dashboard.render_dashboard(session)
    # Pull the chip-value cells out via crude string search; we look for the
    # value adjacent to its label. The summary order is fixed in dashboard.py.
    # We assert the values are present without coupling to the exact HTML shape.
    assert "Active trips" in html
    assert "Pending reservations" in html
    assert "Pending inquiries" in html
    # Two active trips: BROADCASTING + ACCEPTED. Completed isn't active.
    # The chip value markup includes ">2<" between the value div tags.
    assert ">2<" in html  # active trips
    # Trip created today total is 3 (completed/2 active).
    assert "1/3" in html or ">3<" in html


def test_renderer_does_not_include_spam_reservations(session):
    spam = Reservation(
        name="Bot",
        email="bot@example.com",
        phone="+19045550000",
        pickup_label="x",
        dropoff_label="y",
        status=ReservationStatus.SPAM,
    )
    session.add(spam)
    session.commit()
    html = _dashboard.render_dashboard(session)
    assert "Bot" not in html


def test_renderer_does_not_include_inactive_subs(session):
    inactive = Subcontractor(
        name="Dormant Sub",
        dispatch_phone="+19999999999",
        vehicle_classes="suv",
        tier=SubcontractorTier.TIER_3,
        markup_pct=10.0,
        active=False,
    )
    session.add(inactive)
    session.commit()
    html = _dashboard.render_dashboard(session)
    assert "Dormant Sub" not in html


def test_renderer_escapes_user_supplied_text(session):
    """A malicious passenger_name shouldn't break the page or inject HTML."""
    payload = "<script>alert('xss')</script>"
    _seed_reservation(session, name=payload)
    html = _dashboard.render_dashboard(session)
    assert "<script>alert" not in html  # raw script tag NOT in output
    assert "&lt;script&gt;alert" in html  # escaped form IS in output


def test_renderer_emits_complete_html_document(session):
    html = _dashboard.render_dashboard(session)
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "<head>" in html and "</head>" in html
    assert "<body>" in html and "</body>" in html
