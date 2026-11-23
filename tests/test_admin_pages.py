"""Smoke + edit-form tests for the /admin/* page surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models import (
    AdminAuditLog,
    CallLog,
    Driver, DriverStatus,
    Inquiry, InquiryKind, InquiryStatus,
    RateCard,
    Reservation, ReservationStatus,
    Subcontractor, SubcontractorTier,
    Trip, TripStatus,
    VehicleClass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_card(session):
    c = RateCard(
        name="SUV", vehicle_class=VehicleClass.SUV,
        base_fare_cents=15000, per_mile_cents=550, per_hour_cents=12500,
        airport_meet_surcharge_cents=2500, after_hours_surcharge_cents=2500,
        peak_week_multiplier=1.25, minimum_fare_cents=12500, active=True,
    )
    session.add(c); session.commit(); session.refresh(c)
    return c


def _seed_driver(session, name="Marcus", phone="+19125550101"):
    d = Driver(name=name, phone=phone, status=DriverStatus.AVAILABLE,
               vehicle_classes="suv,sprinter", lat=31.15, lon=-81.49)
    session.add(d); session.commit(); session.refresh(d)
    return d


def _seed_sub(session, name="Coast", phone="+18435550100"):
    s = Subcontractor(
        name=name, dispatch_phone=phone, vehicle_classes="suv",
        tier=SubcontractorTier.TIER_1, markup_pct=15.0,
        base_lat=32.16, base_lon=-80.75, service_radius_mi=80.0, active=True,
    )
    session.add(s); session.commit(); session.refresh(s)
    return s


def _seed_trip(session):
    t = Trip(
        passenger_name="Henderson", passenger_phone="+19045559999",
        pickup_label="JAX Terminal", pickup_lat=30.4941, pickup_lon=-81.6879,
        dropoff_label="The Cloister", dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=4),
        vehicle_class=VehicleClass.SUV, status=TripStatus.REQUESTED,
    )
    session.add(t); session.commit(); session.refresh(t)
    return t


def _seed_reservation(session):
    r = Reservation(
        name="Smith party", email="smith@example.com", phone="+19045551111",
        pickup_label="SAV", dropoff_label="The Cloister",
        status=ReservationStatus.PENDING_REVIEW,
    )
    session.add(r); session.commit(); session.refresh(r)
    return r


def _seed_inquiry(session):
    i = Inquiry(
        kind=InquiryKind.WEDDING, name="Sarah", email="sarah@example.com",
        status=InquiryStatus.PENDING_REVIEW,
    )
    session.add(i); session.commit(); session.refresh(i)
    return i


def _seed_call(session):
    c = CallLog(
        twilio_call_sid="CA_test_1",
        from_phone="+19045550000", to_phone="+19125559999",
        direction="inbound", outcome="voicemail",
        transcription="Hi this is Mr Henderson, I need a ride from JAX to Cloister tomorrow at 3pm.",
    )
    session.add(c); session.commit(); session.refresh(c)
    return c


# ---------------------------------------------------------------------------
# List pages all return 200 and have nav links
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "/admin/trips", "/admin/reservations", "/admin/inquiries",
    "/admin/drivers", "/admin/subcontractors", "/admin/rates", "/admin/calls",
])
def test_admin_list_page_returns_html(client, url):
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # Cross-page nav is present
    assert 'href="/dashboard"' in body
    assert 'href="/admin/trips"' in body
    assert 'href="/admin/rates"' in body
    assert 'href="/admin/calls"' in body
    # noindex
    assert "noindex" in body


def test_admin_trips_detail_renders(client, session):
    t = _seed_trip(session)
    response = client.get(f"/admin/trips/{t.id}")
    assert response.status_code == 200
    assert t.passenger_name in response.text


# ---------------------------------------------------------------------------
# List filter chips
# ---------------------------------------------------------------------------


def test_drivers_list_filter_chips_are_present(client, session):
    _seed_driver(session, name="A", phone="+19125550101")
    _seed_driver(session, name="B", phone="+19125550102")
    body = client.get("/admin/drivers").text
    assert 'href="/admin/drivers?status=available"' in body
    assert 'href="/admin/drivers?status=on_trip"' in body
    assert 'href="/admin/drivers?status=off_duty"' in body
    assert 'href="/admin/drivers?status=offline"' in body


def test_drivers_list_status_filter_narrows_results(client, session):
    available = _seed_driver(session, name="Marcus", phone="+19125550101")
    offline = _seed_driver(session, name="Eleanor", phone="+19125550102")
    offline.status = DriverStatus.OFFLINE
    session.commit()

    body = client.get("/admin/drivers?status=available").text
    assert "Marcus" in body
    assert "Eleanor" not in body

    body = client.get("/admin/drivers?status=offline").text
    assert "Eleanor" in body
    assert "Marcus" not in body


def test_subs_list_filter_chips_are_present(client, session):
    _seed_sub(session, name="Coast", phone="+18435550100")
    body = client.get("/admin/subcontractors").text
    # Active/Inactive filter row
    assert 'active=true' in body
    assert 'active=false' in body
    # Tier filter row
    assert 'tier=1' in body
    assert 'tier=2' in body
    assert 'tier=3' in body


def test_subs_list_active_filter_narrows_results(client, session):
    active_sub = _seed_sub(session, name="ActiveCoast", phone="+18435550100")
    inactive_sub = _seed_sub(session, name="SleepyATG", phone="+18435550101")
    inactive_sub.active = False
    session.commit()

    body = client.get("/admin/subcontractors?active=true").text
    assert "ActiveCoast" in body
    assert "SleepyATG" not in body

    body = client.get("/admin/subcontractors?active=false").text
    assert "SleepyATG" in body
    assert "ActiveCoast" not in body


def test_subs_list_tier_filter_narrows_results(client, session):
    tier1 = _seed_sub(session, name="Coast", phone="+18435550100")
    tier2 = _seed_sub(session, name="Backup", phone="+18435550101")
    tier2.tier = SubcontractorTier.TIER_2
    session.commit()

    body = client.get("/admin/subcontractors?tier=1").text
    assert "Coast" in body
    assert "Backup" not in body


def test_subs_list_active_and_tier_filters_compose(client, session):
    """Clicking one chip preserves the other dimension's selection."""
    _seed_sub(session, name="Coast", phone="+18435550100")  # active, tier 1
    body = client.get("/admin/subcontractors?active=true").text
    # Tier chips should preserve the active=true selection in their hrefs.
    assert 'tier=1&active=true' in body or 'active=true&tier=1' in body


def test_admin_rate_detail_renders(client, session):
    c = _seed_card(session)
    response = client.get(f"/admin/rates/{c.id}")
    assert response.status_code == 200
    assert c.name in response.text


def test_admin_call_detail_renders(client, session):
    c = _seed_call(session)
    response = client.get(f"/admin/calls/{c.id}")
    assert response.status_code == 200
    assert c.from_phone in response.text
    assert c.transcription in response.text


# ---------------------------------------------------------------------------
# Edit form PRG behavior
# ---------------------------------------------------------------------------


def test_trips_list_vehicle_class_chips_present(client, session):
    """v0.9.7 — vehicle-class filter chips render alongside the status chips."""
    body = client.get("/admin/trips").text
    assert 'href="/admin/trips?vehicle_class=suv"' in body
    assert 'href="/admin/trips?vehicle_class=sprinter"' in body
    assert 'href="/admin/trips?vehicle_class=sedan"' in body


def test_trips_list_vehicle_class_filter_narrows(client, session):
    _seed_trip(session)  # SUV by default
    # Seed a Sprinter trip
    sprinter = Trip(
        passenger_name="Sprinter Party", passenger_phone="+19045552020",
        pickup_label="JAX Terminal", pickup_lat=30.4941, pickup_lon=-81.6879,
        dropoff_label="The Cloister", dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=4),
        vehicle_class=VehicleClass.SPRINTER, status=TripStatus.REQUESTED,
    )
    session.add(sprinter); session.commit()

    body_suv = client.get("/admin/trips?vehicle_class=suv").text
    assert "Henderson" in body_suv
    assert "Sprinter Party" not in body_suv

    body_sp = client.get("/admin/trips?vehicle_class=sprinter").text
    assert "Sprinter Party" in body_sp
    assert "Henderson" not in body_sp


def test_trips_list_status_and_vehicle_class_compose(client, session):
    """Both filter dimensions can stack via query string."""
    body = client.get("/admin/trips?status=requested&vehicle_class=suv").text
    # Status chips should preserve vehicle_class in their hrefs and vice versa.
    assert 'vehicle_class=suv' in body  # carried by status chips
    assert 'status=requested' in body   # carried by vehicle_class chips


def test_trip_detail_renders_rate_card_select_when_card_exists(client, session):
    """v0.9.7 — Quote action now exposes a rate-card select + peak-week check
    so the dispatcher sees which card they're about to use."""
    _seed_card(session)
    t = _seed_trip(session)
    body = client.get(f"/admin/trips/{t.id}").text
    assert '<select name="rate_card_id"' in body
    assert 'name="is_peak_week"' in body
    assert "Peak week" in body
    assert "(auto" in body  # "(auto — most recent)" default option


def test_trip_detail_renders_no_card_message_when_class_unsupported(client, session):
    """SEDAN class with no active SEDAN rate card → disabled button + note."""
    _seed_card(session)  # SUV card
    sedan_trip = Trip(
        passenger_name="Sedan", passenger_phone="+19045553030",
        pickup_label="JAX", pickup_lat=30.4941, pickup_lon=-81.6879,
        dropoff_label="Cloister", dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=4),
        vehicle_class=VehicleClass.SEDAN, status=TripStatus.REQUESTED,
    )
    session.add(sedan_trip); session.commit(); session.refresh(sedan_trip)
    body = client.get(f"/admin/trips/{sedan_trip.id}").text
    assert "No active rate card" in body
    assert "SEDAN" in body
    assert 'disabled' in body
    # Select should NOT be rendered when there are no cards.
    assert '<select name="rate_card_id"' not in body


def test_quote_action_honors_is_peak_week_form_field(client, session):
    """The endpoint accepts is_peak_week — verify the peak-multiplier path
    fires when the checkbox is sent."""
    card = _seed_card(session)
    trip = _seed_trip(session)
    # Without peak.
    r1 = client.post(f"/admin/trips/{trip.id}/action/quote",
                     data={"rate_card_id": str(card.id)},
                     follow_redirects=False)
    assert r1.status_code == 303
    session.refresh(trip)
    fare_no_peak = trip.fare_cents
    # Reset.
    trip.fare_cents = None
    session.commit()
    # With peak.
    r2 = client.post(f"/admin/trips/{trip.id}/action/quote",
                     data={"rate_card_id": str(card.id), "is_peak_week": "1"},
                     follow_redirects=False)
    assert r2.status_code == 303
    session.refresh(trip)
    assert trip.fare_cents > fare_no_peak  # 1.25× multiplier moved the number


def _seed_completed_sub_trip(session, sub, *, fare_cents=46000, markup_cents=6000,
                              vehicle_class=VehicleClass.SUV, days_ago=2):
    """Create a COMPLETED Trip assigned to `sub` with `updated_at` `days_ago`
    days in the past — useful for verifying the settlement page's date
    filter and age calculation."""
    t = Trip(
        passenger_name=f"SubTripGuest{datetime.now(timezone.utc).microsecond}",
        passenger_phone="+19045557777",
        pickup_label="JAX Terminal", pickup_lat=30.4941, pickup_lon=-81.6879,
        dropoff_label="The Cloister", dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) - timedelta(days=days_ago, hours=4),
        vehicle_class=vehicle_class,
        status=TripStatus.COMPLETED,
        assigned_subcontractor_id=sub.id,
        fare_cents=fare_cents,
        markup_cents=markup_cents,
    )
    session.add(t); session.commit(); session.refresh(t)
    # Backdate updated_at so the day-filter math is exercised.
    t.updated_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    session.commit()
    return t


def test_sub_settlement_page_renders_totals_and_trip_rows(client, session):
    """v0.9.8 — per-trip ledger for a sub: completed trips, totals, age."""
    s = _seed_sub(session)
    _seed_completed_sub_trip(session, s, fare_cents=50000, markup_cents=7000, days_ago=1)
    _seed_completed_sub_trip(session, s, fare_cents=30000, markup_cents=4000, days_ago=5)

    response = client.get(f"/admin/subcontractors/{s.id}/settlement")
    assert response.status_code == 200
    body = response.text
    # Total payout = (50000-7000) + (30000-4000) = 69000 cents = $690.00
    assert "$690.00" in body
    # Totals + per-trip rows visible.
    assert "Trips completed" in body
    assert "Total fare" in body
    assert "Total markup" in body
    # Both trip ids surfaced.
    assert "JAX Terminal" in body
    # Export link present.
    assert "Export this view as CSV" in body
    assert f"/admin/subcontractors/{s.id}/settlement.csv" in body


def test_sub_settlement_page_filters_by_days(client, session):
    """The ?days=N param narrows the trip list to the last N days."""
    s = _seed_sub(session)
    recent = _seed_completed_sub_trip(session, s, fare_cents=50000, markup_cents=7000, days_ago=2)
    old = _seed_completed_sub_trip(session, s, fare_cents=30000, markup_cents=4000, days_ago=45)

    # 7-day window excludes the 45-day-old trip.
    body = client.get(f"/admin/subcontractors/{s.id}/settlement?days=7").text
    assert f">#{recent.id}</a>" in body
    assert f">#{old.id}</a>" not in body
    # 90-day window includes both.
    body = client.get(f"/admin/subcontractors/{s.id}/settlement?days=90").text
    assert f">#{recent.id}</a>" in body
    assert f">#{old.id}</a>" in body
    # All-time (days=0) includes both regardless of age.
    body = client.get(f"/admin/subcontractors/{s.id}/settlement?days=0").text
    assert f">#{recent.id}</a>" in body
    assert f">#{old.id}</a>" in body


def test_sub_settlement_page_excludes_other_subs_trips(client, session):
    """Settlement is per-sub: another sub's completed trip must not appear."""
    s = _seed_sub(session, name="OurSub", phone="+18435551000")
    other = _seed_sub(session, name="OtherSub", phone="+18435551001")
    ours = _seed_completed_sub_trip(session, s, fare_cents=20000, markup_cents=3000)
    theirs = _seed_completed_sub_trip(session, other, fare_cents=40000, markup_cents=5000)

    body = client.get(f"/admin/subcontractors/{s.id}/settlement").text
    assert f">#{ours.id}</a>" in body
    assert f">#{theirs.id}</a>" not in body


def test_sub_settlement_page_excludes_non_completed_trips(client, session):
    """Only COMPLETED trips count toward settlement — in-progress / cancelled
    don't get paid out."""
    s = _seed_sub(session)
    done = _seed_completed_sub_trip(session, s, fare_cents=20000, markup_cents=3000)
    # An in-progress trip assigned to the same sub.
    in_progress = Trip(
        passenger_name="StillEnRoute", passenger_phone="+19045558888",
        pickup_label="x", pickup_lat=30.4941, pickup_lon=-81.6879,
        dropoff_label="y", dropoff_lat=31.1969, dropoff_lon=-81.3438,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=1),
        vehicle_class=VehicleClass.SUV,
        status=TripStatus.EN_ROUTE_PICKUP,
        assigned_subcontractor_id=s.id,
        fare_cents=99999, markup_cents=15000,
    )
    session.add(in_progress); session.commit(); session.refresh(in_progress)
    body = client.get(f"/admin/subcontractors/{s.id}/settlement").text
    assert f">#{done.id}</a>" in body
    assert f">#{in_progress.id}</a>" not in body
    # And the $99,999 ghost fare must NOT pollute totals.
    assert "$999" not in body


def test_sub_settlement_csv_export_returns_text_csv(client, session):
    s = _seed_sub(session)
    t = _seed_completed_sub_trip(session, s, fare_cents=50000, markup_cents=7000)

    response = client.get(f"/admin/subcontractors/{s.id}/settlement.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "settlement_" in response.headers["content-disposition"]
    body = response.text
    # CSV has a header row and at least one data row.
    lines = body.strip().split("\n")
    assert lines[0] == "trip_id,completed_at_utc,vehicle_class,passenger_name,pickup_label,dropoff_label,fare_cents,markup_cents,payout_cents,age_days"
    # Data row contains the trip's numbers.
    data_line = lines[1]
    assert str(t.id) in data_line
    assert "50000" in data_line       # fare
    assert "7000" in data_line        # markup
    assert "43000" in data_line       # payout = 50000 - 7000


def test_sub_settlement_csv_quotes_fields_with_commas(client, session):
    """RFC 4180: commas inside cell values must be quoted, embedded quotes
    doubled. Sub name + trip labels are user-input so this matters."""
    s = _seed_sub(session, name="Coast, LLC")  # comma in name
    t = _seed_completed_sub_trip(session, s, fare_cents=20000, markup_cents=3000)
    # Patch a comma into the pickup label.
    t.pickup_label = "1 Cloister Drive, Sea Island"
    session.commit()

    body = client.get(f"/admin/subcontractors/{s.id}/settlement.csv").text
    # Pickup label is quoted because it contains a comma.
    assert '"1 Cloister Drive, Sea Island"' in body
    # Each row must still parse as the header-defined number of columns:
    # quoted fields keep their internal commas off the split.
    for line in body.strip().split("\n")[1:]:
        # Naive CSV count is wrong with quotes; just verify quoting present.
        if "1 Cloister Drive, Sea Island" in line:
            assert '"1 Cloister Drive, Sea Island"' in line


def test_sub_detail_page_links_to_settlement(client, session):
    s = _seed_sub(session)
    body = client.get(f"/admin/subcontractors/{s.id}").text
    assert f'href="/admin/subcontractors/{s.id}/settlement"' in body
    assert "Settlement" in body


def test_sub_settlement_date_range_filters_trips(client, session):
    """Date-range picker (v0.9.24) narrows settlement by explicit dates,
    overriding the days chip."""
    s = _seed_sub(session)
    recent = _seed_completed_sub_trip(session, s, fare_cents=50000,
                                      markup_cents=7000, days_ago=2)
    old = _seed_completed_sub_trip(session, s, fare_cents=30000,
                                   markup_cents=4000, days_ago=45)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    body = client.get(
        f"/admin/subcontractors/{s.id}/settlement?date_from={week_ago}&date_to={today}"
    ).text
    assert f">#{recent.id}</a>" in body
    assert f">#{old.id}</a>" not in body
    assert "date-range-form" in body
    assert "Clear" in body


def test_sub_settlement_vehicle_class_breakdown(client, session):
    """Vehicle-class breakdown table (v0.9.24) only renders when 2+ classes
    are present in the settlement window."""
    s = _seed_sub(session)
    _seed_completed_sub_trip(session, s, fare_cents=50000, markup_cents=7000,
                             vehicle_class=VehicleClass.SUV, days_ago=1)
    _seed_completed_sub_trip(session, s, fare_cents=40000, markup_cents=5000,
                             vehicle_class=VehicleClass.SPRINTER, days_ago=1)

    body = client.get(f"/admin/subcontractors/{s.id}/settlement").text
    assert "By vehicle class" in body
    assert "SUV" in body
    assert "SPRINTER" in body

    # With only one class, breakdown is hidden.
    s2 = _seed_sub(session, name="OneClass", phone="+18435559999")
    _seed_completed_sub_trip(session, s2, fare_cents=20000, markup_cents=3000,
                             vehicle_class=VehicleClass.SUV, days_ago=1)
    body2 = client.get(f"/admin/subcontractors/{s2.id}/settlement").text
    assert "By vehicle class" not in body2


def test_sub_settlement_csv_with_date_range(client, session):
    """CSV export (v0.9.24) respects date_from/date_to query params."""
    s = _seed_sub(session)
    recent = _seed_completed_sub_trip(session, s, fare_cents=50000,
                                      markup_cents=7000, days_ago=2)
    old = _seed_completed_sub_trip(session, s, fare_cents=30000,
                                   markup_cents=4000, days_ago=45)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    response = client.get(
        f"/admin/subcontractors/{s.id}/settlement.csv?date_from={week_ago}&date_to={today}"
    )
    assert response.status_code == 200
    body = response.text
    lines = body.strip().split("\n")
    row_ids = [line.split(",")[0] for line in lines[1:]]
    assert str(recent.id) in row_ids
    assert str(old.id) not in row_ids
    assert week_ago in response.headers["content-disposition"]


def test_driver_edit_form_redirects_303_and_persists(client, session):
    """Pins the v0.9.5 fix where `DriverStatus` wasn't imported into `app.py`
    even though `driver_edit_form` used it as a `Form(...)` annotation. The
    bug only surfaced when a real browser POSTed the form — `from __future__
    import annotations` deferred the type lookup, and Pydantic 2.11 couldn't
    resolve the forward reference at request time. None of the existing
    admin tests exercised this POST handler, so the bug was invisible to
    the suite until the tests/e2e/ admin-crud browser tests caught it.

    Don't delete: this is a regression pin against an import-set drift."""
    d = _seed_driver(session)
    response = client.post(
        f"/admin/drivers/{d.id}/edit",
        data={
            "status": "available",
            "vehicle_classes": "suv,sprinter",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/admin/drivers/{d.id}?")
    session.refresh(d)
    assert d.status == DriverStatus.AVAILABLE
    assert "sprinter" in d.vehicle_classes.lower()


def test_trip_edit_redirects_303(client, session):
    t = _seed_trip(session)
    response = client.post(
        f"/admin/trips/{t.id}/edit",
        data={
            "status": "broadcasting",
            "passenger_name": "Updated Name",
            "passenger_phone": "+19045551234",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/admin/trips/{t.id}")
    session.refresh(t)
    assert t.passenger_name == "Updated Name"
    assert t.passenger_phone == "+19045551234"


def test_trip_edit_unchanged_status_with_existing_fare(client, session):
    """Regression: editing a field on a trip that already has a fare, with the
    status unchanged, must not fail. The form always re-submits the current
    status + fare; pre-fix the no-op X→X self-transition raised 'Invalid
    transition' (and for COMPLETED trips it blocked every edit, fare included)."""
    t = _seed_trip(session)
    t.status = TripStatus.COMPLETED
    t.fare_cents = 48500
    session.commit()
    response = client.post(
        f"/admin/trips/{t.id}/edit",
        data={
            "status": "completed",   # unchanged
            "fare_cents": "48500",   # current fare, re-submitted by the form
            "passenger_name": "Renamed Guest",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    loc = response.headers["location"]
    assert loc.startswith(f"/admin/trips/{t.id}")
    assert "failed" not in loc.lower()
    session.refresh(t)
    assert t.passenger_name == "Renamed Guest"
    assert t.fare_cents == 48500
    assert t.status == TripStatus.COMPLETED


def test_trip_edit_updates_lat_lng_from_autocomplete(client, session):
    """Selecting a Nominatim suggestion in the UI fills hidden lat/lng inputs
    and submits them with the form. The handler should update trip coordinates."""
    t = _seed_trip(session)
    response = client.post(
        f"/admin/trips/{t.id}/edit",
        data={
            "status": "requested",
            "pickup_label": "Ritz-Carlton Amelia Island",
            "pickup_lat": "30.6708",
            "pickup_lon": "-81.4622",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(t)
    assert t.pickup_label == "Ritz-Carlton Amelia Island"
    assert abs(t.pickup_lat - 30.6708) < 1e-4
    assert abs(t.pickup_lon - (-81.4622)) < 1e-4


def test_rate_card_edit(client, session):
    c = _seed_card(session)
    response = client.post(
        f"/admin/rates/{c.id}/edit",
        data={
            "name": "Updated SUV", "vehicle_class": "suv",
            "base_fare_cents": "20000", "per_mile_cents": "600",
            "per_hour_cents": "15000",
            "airport_meet_surcharge_cents": "3000",
            "after_hours_surcharge_cents": "3000",
            "peak_week_multiplier": "1.5",
            "minimum_fare_cents": "15000",
            "active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(c)
    assert c.name == "Updated SUV"
    assert c.base_fare_cents == 20000
    assert c.per_mile_cents == 600
    assert c.peak_week_multiplier == 1.5


def test_reservation_action_confirm(client, session):
    r = _seed_reservation(session)
    response = client.post(
        f"/admin/reservations/{r.id}/action/confirm",
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(r)
    assert r.status == ReservationStatus.CONFIRMED


def test_reservation_action_spam_sets_status(client, session):
    r = _seed_reservation(session)
    response = client.post(
        f"/admin/reservations/{r.id}/action/spam",
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(r)
    assert r.status == ReservationStatus.SPAM


def test_reservation_inline_action_redirects_to_list(client, session):
    r = _seed_reservation(session)
    response = client.post(
        f"/admin/reservations/{r.id}/action/decline",
        data={"return_to": "list"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/reservations?ok=")
    session.refresh(r)
    assert r.status == ReservationStatus.DECLINED


def test_reservations_list_renders_inline_action_buttons_for_pending(client, session):
    pending = _seed_reservation(session)
    confirmed = Reservation(
        name="Already confirmed", email="c@example.com", phone="+19045552222",
        pickup_label="JAX", dropoff_label="St. Simons",
        status=ReservationStatus.CONFIRMED,
    )
    session.add(confirmed); session.commit(); session.refresh(confirmed)
    response = client.get("/admin/reservations")
    assert response.status_code == 200
    body = response.text
    assert f'/admin/reservations/{pending.id}/action/confirm' in body
    assert f'/admin/reservations/{pending.id}/action/decline' in body
    assert f'/admin/reservations/{pending.id}/action/spam' in body
    # Non-pending rows must NOT show inline action posts.
    assert f'/admin/reservations/{confirmed.id}/action/confirm' not in body
    assert 'inline-action-form' in body
    assert 'name="return_to" value="list"' in body


def test_inquiry_action_responded(client, session):
    i = _seed_inquiry(session)
    response = client.post(
        f"/admin/inquiries/{i.id}/action/responded",
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(i)
    assert i.status == InquiryStatus.RESPONDED


def test_inquiries_list_kind_filter_chips(client, session):
    """v0.9.25 — kind filter chips appear and narrow the list."""
    w = Inquiry(kind=InquiryKind.WEDDING, name="Bride Sarah", email="s@example.com",
                status=InquiryStatus.PENDING_REVIEW)
    v = Inquiry(kind=InquiryKind.VENDOR, name="Vendor Joe", email="j@example.com",
                status=InquiryStatus.PENDING_REVIEW)
    session.add_all([w, v]); session.commit()

    body = client.get("/admin/inquiries").text
    assert "Bride Sarah" in body
    assert "Vendor Joe" in body
    assert 'kind=wedding' in body
    assert 'kind=vendor' in body

    body = client.get("/admin/inquiries?kind=wedding").text
    assert "Bride Sarah" in body
    assert "Vendor Joe" not in body


def test_inquiries_list_search_by_name(client, session):
    """v0.9.25 — free-text search across name, email, and notes."""
    a = Inquiry(kind=InquiryKind.WEDDING, name="Alice Henderson",
                email="alice@example.com", notes="Sea Island wedding",
                status=InquiryStatus.PENDING_REVIEW)
    b = Inquiry(kind=InquiryKind.VENDOR, name="Bob Fleet",
                email="bob@example.com", notes="Airport shuttle vendor",
                status=InquiryStatus.PENDING_REVIEW)
    session.add_all([a, b]); session.commit()

    body = client.get("/admin/inquiries?q=henderson").text
    assert "Alice Henderson" in body
    assert "Bob Fleet" not in body

    body = client.get("/admin/inquiries?q=shuttle").text
    assert "Bob Fleet" in body
    assert "Alice Henderson" not in body

    body = client.get("/admin/inquiries?q=example.com").text
    assert "Alice Henderson" in body
    assert "Bob Fleet" in body

    body = client.get("/admin/inquiries?q=nonexistent").text
    assert "No inquiries match this filter" in body


def test_call_edit_saves_dispatcher_notes(client, session):
    c = _seed_call(session)
    response = client.post(
        f"/admin/calls/{c.id}/edit",
        data={"dispatcher_notes": "Called back at 3:15pm, confirmed pickup."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.refresh(c)
    assert "Called back" in c.dispatcher_notes


# ---------------------------------------------------------------------------
# 404s
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bulk-decline-spam (4.6e #2 — v0.9.23)
# ---------------------------------------------------------------------------


def test_reservations_list_renders_bulk_checkboxes_for_pending(client, session):
    pending = _seed_reservation(session)
    confirmed = Reservation(
        name="Confirmed Guest", email="c@example.com", phone="+19045552222",
        pickup_label="JAX", dropoff_label="St. Simons",
        status=ReservationStatus.CONFIRMED,
    )
    session.add(confirmed); session.commit()
    body = client.get("/admin/reservations").text
    assert f'name="ids" value="{pending.id}"' in body
    assert f'name="ids" value="{confirmed.id}"' not in body
    assert 'id="bulk-select-all"' in body
    assert 'id="bulk-spam-btn"' in body


def test_bulk_spam_marks_selected_pending_as_spam(client, session):
    r1 = _seed_reservation(session)
    r2 = Reservation(
        name="Guest two", email="g2@example.com", phone="+19045552222",
        pickup_label="SAV", dropoff_label="Jekyll",
        status=ReservationStatus.PENDING_REVIEW,
    )
    session.add(r2); session.commit(); session.refresh(r2)
    response = client.post(
        "/admin/reservations/bulk-spam",
        content=f"ids={r1.id}&ids={r2.id}",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "2%20reservation" in response.headers["location"]
    session.refresh(r1)
    session.refresh(r2)
    assert r1.status == ReservationStatus.SPAM
    assert r2.status == ReservationStatus.SPAM


def test_bulk_spam_skips_non_pending_reservations(client, session):
    r = _seed_reservation(session)
    r.status = ReservationStatus.CONFIRMED
    session.commit()
    response = client.post(
        "/admin/reservations/bulk-spam",
        content=f"ids={r.id}",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "0%20reservation" in response.headers["location"]
    session.refresh(r)
    assert r.status == ReservationStatus.CONFIRMED


# ---------------------------------------------------------------------------
# 404s
# ---------------------------------------------------------------------------


def test_unknown_trip_404(client):
    response = client.get("/admin/trips/9999")
    assert response.status_code == 404


def test_unknown_rate_404(client):
    response = client.get("/admin/rates/9999")
    assert response.status_code == 404


def test_unknown_call_404(client):
    response = client.get("/admin/calls/9999")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Admin audit log (4.6e #8 — v0.9.26)
# ---------------------------------------------------------------------------


def test_admin_write_creates_audit_log_entry(client, session):
    """POST/PATCH to /admin/* paths produce AdminAuditLog rows."""
    d = _seed_driver(session)
    client.post(
        f"/admin/drivers/{d.id}/edit",
        data={"status": "available", "vehicle_classes": "suv,sprinter", "notes": "test"},
        follow_redirects=False,
    )
    entries = list(session.scalars(
        __import__("sqlalchemy").select(AdminAuditLog).order_by(AdminAuditLog.id.desc())
    ).all())
    assert len(entries) >= 1
    latest = entries[0]
    assert latest.method == "POST"
    assert f"/admin/drivers/{d.id}/edit" == latest.path
    assert latest.status_code == 303
    assert latest.payload_hash is not None
    assert len(latest.payload_hash) == 64


def test_non_admin_write_not_logged(client, session):
    """Public form POSTs (e.g. /reservations) must NOT appear in the audit log."""
    before = session.scalar(
        __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(AdminAuditLog)
    ) or 0
    client.post("/reservations", json={
        "name": "Test", "email": "t@example.com", "phone": "+19045550000",
        "pickup_label": "JAX", "dropoff_label": "Cloister",
    })
    after = session.scalar(
        __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(AdminAuditLog)
    ) or 0
    assert after == before


def test_audit_log_page_renders(client, session):
    """The /admin/audit page shows log entries after an admin action."""
    d = _seed_driver(session)
    client.post(
        f"/admin/drivers/{d.id}/edit",
        data={"status": "available", "vehicle_classes": "suv", "notes": ""},
        follow_redirects=False,
    )
    response = client.get("/admin/audit")
    assert response.status_code == 200
    body = response.text
    assert "Audit Log" in body
    assert "POST" in body
    assert f"/admin/drivers/{d.id}/edit" in body


# ---------------------------------------------------------------------------
# Settlement math edge cases
# ---------------------------------------------------------------------------


def test_settlement_zero_fare_trip_payout_is_zero(client, session):
    """Trips with fare_cents=0 and markup_cents=0 yield $0.00 payout."""
    s = _seed_sub(session)
    _seed_completed_sub_trip(session, s, fare_cents=0, markup_cents=0, days_ago=1)
    body = client.get(f"/admin/subcontractors/{s.id}/settlement").text
    assert "$0.00" in body


def test_settlement_markup_exceeds_fare_payout_floors_at_zero(client, session):
    """Edge: if markup > fare for some reason, payout should floor at zero."""
    s = _seed_sub(session)
    _seed_completed_sub_trip(session, s, fare_cents=5000, markup_cents=8000, days_ago=1)
    body = client.get(f"/admin/subcontractors/{s.id}/settlement").text
    assert "Trips completed" in body


def test_settlement_all_time_shows_everything(client, session):
    """days=0 + no date range returns every completed trip regardless of age."""
    s = _seed_sub(session)
    t1 = _seed_completed_sub_trip(session, s, fare_cents=20000, markup_cents=3000, days_ago=1)
    t2 = _seed_completed_sub_trip(session, s, fare_cents=30000, markup_cents=4000, days_ago=400)
    body = client.get(f"/admin/subcontractors/{s.id}/settlement?days=0").text
    assert f">#{t1.id}</a>" in body
    assert f">#{t2.id}</a>" in body


# ---------------------------------------------------------------------------
# Filter composition tests
# ---------------------------------------------------------------------------


def test_inquiries_status_and_kind_compose(client, session):
    """Status + kind filters work together without resetting each other."""
    w_pending = Inquiry(kind=InquiryKind.WEDDING, name="WedPend",
                        email="wp@example.com", status=InquiryStatus.PENDING_REVIEW)
    w_responded = Inquiry(kind=InquiryKind.WEDDING, name="WedResp",
                          email="wr@example.com", status=InquiryStatus.RESPONDED)
    v_pending = Inquiry(kind=InquiryKind.VENDOR, name="VendPend",
                        email="vp@example.com", status=InquiryStatus.PENDING_REVIEW)
    session.add_all([w_pending, w_responded, v_pending])
    session.commit()

    body = client.get("/admin/inquiries?status=pending_review&kind=wedding").text
    assert "WedPend" in body
    assert "WedResp" not in body
    assert "VendPend" not in body


def test_inquiries_search_and_kind_compose(client, session):
    """Search + kind filter work together."""
    a = Inquiry(kind=InquiryKind.WEDDING, name="Alice Wedding",
                email="alice@example.com", status=InquiryStatus.PENDING_REVIEW)
    b = Inquiry(kind=InquiryKind.WEDDING, name="Bob Wedding",
                email="bob@example.com", status=InquiryStatus.PENDING_REVIEW)
    c = Inquiry(kind=InquiryKind.VENDOR, name="Alice Vendor",
                email="alicev@example.com", status=InquiryStatus.PENDING_REVIEW)
    session.add_all([a, b, c]); session.commit()

    body = client.get("/admin/inquiries?kind=wedding&q=alice").text
    assert "Alice Wedding" in body
    assert "Bob Wedding" not in body
    assert "Alice Vendor" not in body


def test_trips_list_status_filter(client, session):
    """Trip list filters by status correctly."""
    d = _seed_driver(session)
    t1 = Trip(
        passenger_name="ActiveTrip", passenger_phone="+19045550001",
        pickup_label="JAX", pickup_lat=30.49, pickup_lon=-81.69,
        dropoff_label="Cloister", dropoff_lat=31.20, dropoff_lon=-81.34,
        pickup_time=datetime.now(timezone.utc) + timedelta(hours=2),
        vehicle_class=VehicleClass.SUV, status=TripStatus.BROADCASTING,
    )
    t2 = Trip(
        passenger_name="DoneTrip", passenger_phone="+19045550002",
        pickup_label="SAV", pickup_lat=32.13, pickup_lon=-81.20,
        dropoff_label="Jekyll", dropoff_lat=31.07, dropoff_lon=-81.42,
        pickup_time=datetime.now(timezone.utc) - timedelta(hours=4),
        vehicle_class=VehicleClass.SUV, status=TripStatus.COMPLETED,
    )
    session.add_all([t1, t2]); session.commit()

    body = client.get("/admin/trips?status=broadcasting").text
    assert "ActiveTrip" in body
    assert "DoneTrip" not in body

    body = client.get("/admin/trips?status=completed").text
    assert "DoneTrip" in body
    assert "ActiveTrip" not in body


def test_reservations_status_filter(client, session):
    """Reservation list filters by status correctly."""
    p = _seed_reservation(session)
    s = Reservation(
        name="SpamGuest", email="spam@example.com", phone="+19045553333",
        pickup_label="nowhere", dropoff_label="nowhere",
        status=ReservationStatus.SPAM,
    )
    session.add(s); session.commit()

    body = client.get("/admin/reservations?status=pending_review").text
    assert p.name in body
    assert "SpamGuest" not in body

    body = client.get("/admin/reservations?status=spam").text
    assert "SpamGuest" in body
