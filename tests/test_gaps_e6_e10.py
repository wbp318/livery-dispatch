"""Code gaps E6–E10 from the v1.1.16 documentation audit (fixed v1.1.18).

E6  Rate cards can be created in production (admin form + `cli.py rates seed-defaults`).
E7  Cancelling an accepted trip returns the driver to AVAILABLE (when idle).
E8  An empty broadcast doesn't strand still-pending offers by flipping to NO_DRIVERS.
E9  Sub SMS accepts can carry a price; a bare accept's cost can be recorded later.
E10 Hygiene: push off-switch in the driver PWA, CLI survives a cp1252 console,
    no unread ACCEPT_WINDOW_SECONDS in .env.example.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import core
from models import Driver, DriverStatus, RateCard, Trip, TripStatus

DISPATCH_DIR = pathlib.Path(__file__).resolve().parents[1]


def _trip(client, vehicle_class="suv") -> int:
    pickup = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    r = client.post("/trips", json={
        "passenger_name": "Gap Probe", "passenger_phone": "+19045550150",
        "pickup_label": "JAX", "pickup_lat": 30.4941, "pickup_lon": -81.6878,
        "dropoff_label": "Cloister", "dropoff_lat": 31.1969, "dropoff_lon": -81.3438,
        "pickup_time": pickup, "vehicle_class": vehicle_class,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _driver(client, phone="+19125550160") -> int:
    r = client.post("/drivers", json={"name": "Gap Driver", "phone": phone, "vehicle_classes": ["suv"]})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    r = client.patch(f"/drivers/{did}/status", json={"status": "available", "lat": 31.15, "lon": -81.49})
    assert r.status_code == 200, r.text
    return did


def _sub(client, phone="+19125550170", markup=15.0) -> int:
    r = client.post("/subcontractors", json={
        "name": "Gap Sub", "dispatch_phone": phone, "vehicle_classes": ["suv"],
        "tier": 1, "markup_pct": markup, "base_lat": 31.15, "base_lon": -81.49,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _sms(client, frm, body):
    return client.post("/webhooks/twilio/sms", data={"From": frm, "Body": body})


# ---------------------------------------------------------------------------
# E6 — create rate cards in production
# ---------------------------------------------------------------------------


def test_new_rate_card_form_renders(client):
    r = client.get("/admin/rates/new")
    assert r.status_code == 200
    assert 'action="/admin/rates/new"' in r.text
    assert "Create rate card" in r.text


def test_rates_list_offers_new_card_and_explains_empty_state(client):
    r = client.get("/admin/rates")
    assert "+ New rate card" in r.text
    assert "rates seed-defaults" in r.text


def test_create_rate_card_clamps_and_redirects(client, session):
    r = client.post("/admin/rates/new", data={
        "name": "  Prod SUV  ", "vehicle_class": "suv", "base_fare_cents": "-5",
        "per_mile_cents": "550", "per_hour_cents": "12500", "airport_meet_surcharge_cents": "2500",
        "after_hours_surcharge_cents": "2500", "peak_week_multiplier": "0.5",
        "minimum_fare_cents": "12500", "active": "1", "notes": "from admin",
    }, follow_redirects=False)
    assert r.status_code == 303
    from urllib.parse import unquote
    assert "Rate card created" in unquote(r.headers["location"])
    card = session.query(RateCard).filter_by(name="Prod SUV").one()
    assert card.base_fare_cents == 0            # clamped
    assert card.peak_week_multiplier == 1.0     # clamped
    assert card.active is True and card.per_mile_cents == 550


@pytest.mark.parametrize("data,needle", [
    ({"name": "", "vehicle_class": "suv"}, "name is required"),
    ({"name": "X", "vehicle_class": "limo"}, "unknown vehicle class"),
])
def test_create_rate_card_rejects_bad_input(client, session, data, needle):
    r = client.post("/admin/rates/new", data=data, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/rates/new?ok=Failed")
    from urllib.parse import unquote
    assert needle in unquote(r.headers["location"])
    assert session.query(RateCard).count() == 0


def test_create_rate_card_requires_csrf(client, session):
    client.headers.pop("X-CSRF-Token", None)
    r = client.post("/admin/rates/new", data={"name": "X", "vehicle_class": "suv"})
    assert r.status_code == 403
    assert session.query(RateCard).count() == 0


def test_quote_works_once_a_card_exists(client):
    client.post("/admin/rates/new", data={
        "name": "Prod SUV", "vehicle_class": "suv", "base_fare_cents": "15000",
        "per_mile_cents": "550", "minimum_fare_cents": "12500", "active": "1",
    })
    r = client.post("/quote", json={"pickup_label": "JAX", "dropoff_label": "The Cloister", "vehicle_pref": "escalade-esv"})
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is True
    assert r.json()["total_cents"] >= 15000


@pytest.fixture
def cli_on_test_db(engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    import cli
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(cli, "init_db", lambda: None)
    from typer.testing import CliRunner
    return cli, CliRunner()


def test_cli_rates_seed_defaults_is_idempotent(cli_on_test_db, session):
    cli, runner = cli_on_test_db
    first = runner.invoke(cli.app, ["rates", "seed-defaults"])
    assert first.exit_code == 0, first.output
    assert "Seeded 2" in first.output
    second = runner.invoke(cli.app, ["rates", "seed-defaults"])
    assert second.exit_code == 0 and "nothing seeded" in second.output
    assert session.query(RateCard).count() == 2
    listed = runner.invoke(cli.app, ["rates", "list"])
    assert "Standard SUV" in listed.output and "Sprinter" in listed.output


def test_seed_and_cli_share_one_definition():
    from rate_defaults import planning_rate_cards
    cards = planning_rate_cards()
    assert {c.vehicle_class.value for c in cards} == {"suv", "sprinter"}
    assert all(c.active for c in cards)


# ---------------------------------------------------------------------------
# E7 — cancelling frees the driver (when idle)
# ---------------------------------------------------------------------------


def _accepted_trip(client):
    did = _driver(client)
    tid = _trip(client)
    assert client.post(f"/trips/{tid}/broadcast").status_code == 200
    r = client.post(f"/trips/{tid}/accept", params={"driver_id": did})
    assert r.status_code == 200, r.text
    return did, tid


def test_cancel_accepted_trip_returns_driver_to_available(client, session):
    did, tid = _accepted_trip(client)
    assert session.get(Driver, did).status == DriverStatus.ON_TRIP
    r = client.patch(f"/trips/{tid}/status", json={"status": "cancelled"})
    assert r.status_code == 200, r.text
    session.expire_all()
    assert session.get(Driver, did).status == DriverStatus.AVAILABLE


def test_admin_cancel_action_frees_driver(client, session):
    did, tid = _accepted_trip(client)
    client.post(f"/admin/trips/{tid}/action/cancel", follow_redirects=False)
    session.expire_all()
    assert session.get(Driver, did).status == DriverStatus.AVAILABLE


def test_cancel_leaves_off_duty_driver_off_duty(client, session):
    did, tid = _accepted_trip(client)
    session.get(Driver, did).status = DriverStatus.OFF_DUTY
    session.commit()
    client.patch(f"/trips/{tid}/status", json={"status": "cancelled"})
    session.expire_all()
    assert session.get(Driver, did).status == DriverStatus.OFF_DUTY


def test_cancel_keeps_driver_busy_when_another_trip_is_active(client, session):
    did, tid = _accepted_trip(client)
    other = session.get(Trip, _trip(client))
    other.assigned_driver_id = did
    other.status = TripStatus.EN_ROUTE_PICKUP
    session.commit()
    client.patch(f"/trips/{tid}/status", json={"status": "cancelled"})
    session.expire_all()
    assert session.get(Driver, did).status == DriverStatus.ON_TRIP


def test_complete_still_frees_driver(client, session):
    did, tid = _accepted_trip(client)
    for st in ("en_route_pickup", "on_scene", "in_transit", "completed"):
        assert client.patch(f"/trips/{tid}/status", json={"status": st}).status_code == 200
    session.expire_all()
    assert session.get(Driver, did).status == DriverStatus.AVAILABLE


# ---------------------------------------------------------------------------
# E8 — an empty broadcast doesn't strand open offers
# ---------------------------------------------------------------------------


def test_empty_overflow_keeps_open_own_fleet_offers_acceptable(client, session):
    did = _driver(client)
    tid = _trip(client)
    assert client.post(f"/trips/{tid}/broadcast").status_code == 200   # driver offer pending
    r = client.post(f"/trips/{tid}/broadcast-overflow")                  # no subs exist
    assert r.status_code == 409
    assert "earlier offers are still open" in r.json()["detail"]
    session.expire_all()
    assert session.get(Trip, tid).status == TripStatus.BROADCASTING
    r = client.post(f"/trips/{tid}/accept", params={"driver_id": did})
    assert r.status_code == 200, r.text                                  # before E8: 409



def test_admin_own_fleet_flash_does_not_claim_no_drivers_while_offers_open(client, session):
    did = _driver(client)
    tid = _trip(client)
    client.post(f"/trips/{tid}/broadcast")
    session.get(Driver, did).status = DriverStatus.OFF_DUTY
    session.commit()
    r = client.post(f"/admin/trips/{tid}/action/broadcast", follow_redirects=False)
    from urllib.parse import unquote
    loc = unquote(r.headers["location"])
    assert "earlier offers are still open" in loc and "no_drivers" not in loc


def test_push_turn_off_checks_the_server_response(client, session):
    did = _driver(client)
    token = session.get(Driver, did).auth_token
    client.cookies.clear()
    html = client.get("/driver", headers={"Authorization": f"Bearer {token}"}).text
    assert "r && r.ok" in html

def test_empty_overflow_with_no_open_offers_is_no_drivers(client, session):
    did = _driver(client)
    tid = _trip(client)
    client.post(f"/trips/{tid}/broadcast")
    assert client.post(f"/trips/{tid}/decline", params={"driver_id": did}).status_code == 200
    r = client.post(f"/trips/{tid}/broadcast-overflow")
    assert r.status_code == 409
    session.expire_all()
    assert session.get(Trip, tid).status == TripStatus.NO_DRIVERS


def test_empty_own_fleet_rebroadcast_keeps_pending_offers(client, session):
    did = _driver(client)
    tid = _trip(client)
    client.post(f"/trips/{tid}/broadcast")
    # Take the only driver out of the candidate pool, then re-broadcast.
    session.get(Driver, did).status = DriverStatus.OFF_DUTY
    session.commit()
    assert client.post(f"/trips/{tid}/broadcast").status_code == 409
    session.expire_all()
    assert session.get(Trip, tid).status == TripStatus.BROADCASTING


# ---------------------------------------------------------------------------
# E9 — sub SMS accepts with a price; record cost later otherwise
# ---------------------------------------------------------------------------


def _overflowed_trip(client, markup=15.0):
    _sub(client, markup=markup)
    tid = _trip(client)
    r = client.post(f"/trips/{tid}/broadcast-overflow")
    assert r.status_code == 200, r.text
    return tid


@pytest.mark.parametrize("body_tmpl,cost", [
    ("ACCEPT {id} 450", 45000),
    ("accept {id} $450.50", 45050),
    ("Accept-{id} at 450", 45000),
    ("ACCEPT {id} $1,200", 120000),
])
def test_sub_sms_accept_with_price_records_cost_plus_fare(client, session, body_tmpl, cost):
    tid = _overflowed_trip(client)
    r = _sms(client, "+19125550170", body_tmpl.format(id=tid))
    assert r.status_code == 200 and r.json()["matched"] is True, r.text
    session.expire_all()
    trip = session.get(Trip, tid)
    markup = int(round(cost * 15.0 / 100.0))
    assert trip.status == TripStatus.ACCEPTED
    assert trip.markup_cents == markup
    assert trip.fare_cents == cost + markup
    assert "markup" in r.json()["detail"]


def test_sub_sms_bare_accept_then_admin_records_cost(client, session):
    tid = _overflowed_trip(client)
    r = _sms(client, "+19125550170", f"ACCEPT {tid}")
    assert "fare pending" in r.json()["detail"]
    session.expire_all()
    assert session.get(Trip, tid).fare_cents is None
    page = client.get(f"/admin/trips/{tid}").text
    assert f'action="/admin/trips/{tid}/action/sub-cost"' in page
    assert "Record sub cost (fare pending)" in page
    r = client.post(f"/admin/trips/{tid}/action/sub-cost", data={"quoted_cost_dollars": "$450"},
                    follow_redirects=False)
    assert r.status_code == 303 and "fare" in r.headers["location"]
    session.expire_all()
    assert session.get(Trip, tid).fare_cents == 45000 + 6750


def test_record_sub_cost_refusals(client, session):
    tid = _trip(client)  # no sub assigned
    with pytest.raises(ValueError, match="not assigned to a subcontractor"):
        core.record_subcontractor_cost(session, tid, 45000)
    r = client.post(f"/admin/trips/{tid}/action/sub-cost", data={"quoted_cost_dollars": "450"},
                    follow_redirects=False)
    assert "Failed" in r.headers["location"]
    tid2 = _overflowed_trip(client)
    _sms(client, "+19125550170", f"ACCEPT {tid2}")
    with pytest.raises(ValueError, match="positive"):
        core.record_subcontractor_cost(session, tid2, 0)
    r = client.post(f"/admin/trips/{tid2}/action/sub-cost", data={"quoted_cost_dollars": "abc"},
                    follow_redirects=False)
    assert "Failed" in r.headers["location"]


def test_driver_sms_price_is_ignored(client, session):
    did = _driver(client)
    tid = _trip(client)
    client.post(f"/trips/{tid}/broadcast")
    r = _sms(client, "+19125550160", f"ACCEPT {tid} 450")
    assert r.json()["matched"] is True
    session.expire_all()
    trip = session.get(Trip, tid)
    assert trip.assigned_driver_id == did and trip.fare_cents is None


@pytest.mark.parametrize("body,trip_id,price", [
    ("ACCEPT 12 $1,200", "12", "1,200"),
    ("accept 12 1,200.50", "12", "1,200.50"),
    ("ACCEPT 12450", "12450", None),
    ("ACCEPT 12 450", "12", "450"),
    ("decline 7", "7", None),
    ("accept 3 $99.95", "3", "99.95"),
])
def test_reply_grammar(body, trip_id, price):
    from app import _REPLY_RE
    m = _REPLY_RE.match(body)
    assert m and m.group(2) == trip_id and m.group(3) == price


def test_overflow_sms_tells_subs_how_to_quote(client, session):
    sent = []

    class Spy:
        def send_sms(self, to, body):
            sent.append(body)

    _sub(client)
    tid = _trip(client)
    core.broadcast_to_subcontractors(session, session.get(Trip, tid), provider=Spy())
    assert sent and f"ACCEPT {tid} 450" in sent[0]


def test_cli_sub_cost(cli_on_test_db, client, session):
    cli, runner = cli_on_test_db
    tid = _overflowed_trip(client)
    _sms(client, "+19125550170", f"ACCEPT {tid}")
    out = runner.invoke(cli.app, ["trips", "sub-cost", str(tid), "--cost-cents", "45000"])
    assert out.exit_code == 0, out.output
    session.expire_all()
    assert session.get(Trip, tid).fare_cents == 51750


# ---------------------------------------------------------------------------
# E10 — hygiene
# ---------------------------------------------------------------------------


def test_driver_dashboard_offers_push_off_switch(client, session):
    did = _driver(client)
    token = session.get(Driver, did).auth_token
    client.cookies.clear()
    r = client.get("/driver", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert 'id="push-disable-btn"' in r.text
    assert "method: 'DELETE'" in r.text and "/driver/push/subscribe" in r.text


def test_cli_survives_cp1252_console():
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    code = "import cli, sys; print(sys.stdout.errors); print('JAX → Cloister — ok')"
    proc = subprocess.run([sys.executable, "-c", code], cwd=DISPATCH_DIR, env=env,
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert b"replace" in proc.stdout and b"ok" in proc.stdout


def test_env_example_has_no_unread_accept_window():
    text = (DISPATCH_DIR / ".env.example").read_text(encoding="utf-8")
    assert not any(line.startswith("ACCEPT_WINDOW_SECONDS=") for line in text.splitlines())
