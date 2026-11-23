"""Tests for the public /quote endpoint (Phase 4.6b scaffold).

The endpoint is the reserve form's pre-submission estimate. It resolves
text labels via the corridor anchor table, runs the same pure fare
calculator the admin uses, and soft-fails with a "dispatcher will confirm"
message when an input can't be placed. No auth, shared per-IP rate limit
with /reservations + /inquiries.
"""

from __future__ import annotations

from datetime import datetime

import anchors
from models import RateCard, VehicleClass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_cards(session) -> tuple[RateCard, RateCard]:
    """Two active rate cards (SUV + Sprinter) mirroring seed.py's defaults.

    Numbers are planning-grade — the suite asserts ranges, not exact dollars,
    so this stays robust if the real rate-card numbers move.
    """
    suv = RateCard(
        name="Test SUV",
        vehicle_class=VehicleClass.SUV,
        base_fare_cents=15000,
        per_mile_cents=550,
        per_hour_cents=12500,
        airport_meet_surcharge_cents=2500,
        after_hours_surcharge_cents=2500,
        peak_week_multiplier=1.25,
        minimum_fare_cents=12500,
        active=True,
    )
    spr = RateCard(
        name="Test Sprinter",
        vehicle_class=VehicleClass.SPRINTER,
        base_fare_cents=25000,
        per_mile_cents=800,
        per_hour_cents=20000,
        airport_meet_surcharge_cents=3500,
        after_hours_surcharge_cents=3500,
        peak_week_multiplier=1.30,
        minimum_fare_cents=25000,
        active=True,
    )
    session.add_all([suv, spr])
    session.commit()
    session.refresh(suv)
    session.refresh(spr)
    return suv, spr


def _payload(**overrides) -> dict:
    base = {
        "pickup_label": "JAX",
        "dropoff_label": "The Cloister, Sea Island",
        "vehicle_pref": "escalade-esv",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Anchor resolver unit tests
# ---------------------------------------------------------------------------


def test_resolve_anchor_exact_alias():
    got = anchors.resolve_anchor("JAX")
    assert got is not None
    canonical, lat, lon = got
    assert canonical == "JAX"
    assert 30 < lat < 31
    assert -82 < lon < -81


def test_resolve_anchor_case_insensitive():
    assert anchors.resolve_anchor("jax")[0] == "JAX"
    assert anchors.resolve_anchor("JAX Airport")[0] == "JAX"


def test_resolve_anchor_substring_match():
    """Free-text addresses should still hit the canonical when the alias
    appears somewhere in the input."""
    assert anchors.resolve_anchor("baggage claim at JAX terminal")[0] == "JAX"
    assert anchors.resolve_anchor("The Cloister Resort, Sea Island GA")[0] == "SI"


def test_resolve_anchor_longest_alias_wins():
    """If both 'sea island' and 'the lodge at sea island' could match the
    input, the longer alias should win. Both currently map to SI, so we use
    'cumberland ferry' vs 'cumberland' to distinguish — both resolve to
    CUM, so test the precedence with overlapping airport phrases."""
    # "jacksonville international" is a longer phrase than "jax" — should
    # still hit JAX (both alias to JAX, so the assertion is the same).
    got = anchors.resolve_anchor("Jacksonville International Airport pickup curbside")
    assert got is not None
    assert got[0] == "JAX"


def test_resolve_anchor_sea_island_variants():
    """All of these should map to SI — the Cloister, the Lodge, and various
    'sea island' spellings."""
    assert anchors.resolve_anchor("The Cloister")[0] == "SI"
    assert anchors.resolve_anchor("the lodge at sea island")[0] == "SI"
    assert anchors.resolve_anchor("Sea Island")[0] == "SI"
    assert anchors.resolve_anchor("Sea Island Resort, GA")[0] == "SI"


def test_resolve_anchor_jekyll_variants():
    assert anchors.resolve_anchor("Jekyll Island Club Resort")[0] == "JKL"
    assert anchors.resolve_anchor("Westin Jekyll")[0] == "JKL"
    assert anchors.resolve_anchor("Jekyll")[0] == "JKL"


def test_resolve_anchor_amelia_island_variants():
    assert anchors.resolve_anchor("Ritz-Carlton Amelia Island")[0] == "RIT"
    assert anchors.resolve_anchor("Omni Amelia Island Resort")[0] == "OMI"
    # Bare "Amelia Island" hits the generic AMELIA anchor.
    assert anchors.resolve_anchor("Amelia Island")[0] == "AMELIA"


def test_resolve_anchor_miss_returns_none():
    """Anything outside the canonical anchor set is None. The /quote endpoint
    then soft-fails with the dispatcher-confirms message."""
    assert anchors.resolve_anchor("my hotel in atlanta") is None
    assert anchors.resolve_anchor("12345 Random Street, Macon GA") is None
    assert anchors.resolve_anchor("") is None
    assert anchors.resolve_anchor("   ") is None


def test_is_airport_recognizes_airports():
    assert anchors.is_airport("JAX") is True
    assert anchors.is_airport("SAV") is True
    assert anchors.is_airport("BQK") is True
    assert anchors.is_airport("SI") is False
    assert anchors.is_airport(None) is False


# ---------------------------------------------------------------------------
# /quote endpoint
# ---------------------------------------------------------------------------


def test_quote_jax_to_sea_island_suv_returns_resolved_estimate(client, session):
    _seed_cards(session)
    r = client.post("/quote", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] is True
    assert body["pickup_resolved"] == "JAX"
    assert body["dropoff_resolved"] == "SI"
    assert body["vehicle_class"] == "suv"
    # JAX → Sea Island haversine is ~53 mi. With base $150 + $5.50/mi + time
    # + airport-meet surcharge, total lands in the ~$450–$650 range for SUV.
    assert 40000 < body["total_cents"] < 70000
    assert body["distance_miles"] > 40
    assert body["rate_card_name"] == "Test SUV"
    assert "From $" in body["message"]
    assert "dispatcher confirms" in body["message"].lower()


def test_quote_sav_to_sea_island_sprinter(client, session):
    _seed_cards(session)
    r = client.post(
        "/quote",
        json=_payload(
            pickup_label="Savannah Airport",
            dropoff_label="The Lodge at Sea Island",
            vehicle_pref="sprinter-limited",
        ),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] is True
    assert body["pickup_resolved"] == "SAV"
    assert body["dropoff_resolved"] == "SI"
    assert body["vehicle_class"] == "sprinter"
    # Sprinter rates are higher — expect at least the SUV-equivalent ceiling.
    assert body["total_cents"] > 30000
    assert body["rate_card_name"] == "Test Sprinter"


def test_quote_breakdown_lines_populated(client, session):
    _seed_cards(session)
    r = client.post("/quote", json=_payload())
    body = r.json()
    labels = [line["label"] for line in body["lines"]]
    assert any("Base fare" in l for l in labels)
    assert any("Distance" in l for l in labels)
    # Airport meet should fire since pickup is JAX.
    assert any("Airport meet" in l for l in labels)


def test_quote_after_hours_surcharge_applies_when_pickup_late(client, session):
    _seed_cards(session)
    # 23:30 — inside the after-hours window (>=22 or <6).
    late = datetime(2026, 11, 13, 23, 30).isoformat()
    r = client.post("/quote", json=_payload(pickup_at=late))
    body = r.json()
    assert any("After-hours" in line["label"] for line in body["lines"])


def test_quote_peak_week_applies_only_when_flagged(client, session):
    _seed_cards(session)
    not_peak = client.post("/quote", json=_payload(is_peak_week=False)).json()
    peak = client.post("/quote", json=_payload(is_peak_week=True)).json()
    assert peak["total_cents"] > not_peak["total_cents"]
    assert any("Peak" in line["label"] for line in peak["lines"])
    assert all("Peak" not in line["label"] for line in not_peak["lines"])


def test_quote_unrecognized_pickup_soft_fails(client, session):
    _seed_cards(session)
    r = client.post(
        "/quote",
        json=_payload(pickup_label="123 Random Street, Macon GA"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] is False
    assert body["pickup_resolved"] is None
    assert body["dropoff_resolved"] == "SI"  # this one did resolve
    assert body["total_cents"] is None
    assert "dispatcher will confirm" in body["message"].lower()


def test_quote_unrecognized_dropoff_soft_fails(client, session):
    _seed_cards(session)
    r = client.post(
        "/quote",
        json=_payload(dropoff_label="grandma's cabin in north georgia"),
    )
    body = r.json()
    assert body["resolved"] is False
    assert body["pickup_resolved"] == "JAX"
    assert body["dropoff_resolved"] is None
    assert body["total_cents"] is None


def test_quote_multi_vehicle_returns_no_class_soft_fail(client, session):
    """The reserve form's 'Multi-vehicle (wedding party)' choice can't be
    auto-classed — the dispatcher resolves whether it's 2× Escalade or a
    Sprinter or both. Quote soft-fails with a message that says so."""
    _seed_cards(session)
    r = client.post("/quote", json=_payload(vehicle_pref="multi-vehicle"))
    body = r.json()
    assert body["resolved"] is False
    assert body["vehicle_class"] is None
    # Still resolved the labels — that's useful info even when the class isn't picked.
    assert body["pickup_resolved"] == "JAX"
    assert body["dropoff_resolved"] == "SI"
    assert "single vehicle class" in body["message"].lower()


def test_quote_no_vehicle_pref_returns_no_class_soft_fail(client, session):
    _seed_cards(session)
    r = client.post(
        "/quote",
        json={"pickup_label": "JAX", "dropoff_label": "Sea Island"},
    )
    body = r.json()
    assert body["resolved"] is False
    assert body["vehicle_class"] is None
    assert body["total_cents"] is None


def test_quote_no_active_rate_card_for_class_soft_fails(client, session):
    """If the SUV card is somehow missing or all inactive, /quote soft-fails
    rather than 500. This is the safety net for an admin who accidentally
    deactivates every card."""
    spr_only = RateCard(
        name="Sprinter only",
        vehicle_class=VehicleClass.SPRINTER,
        base_fare_cents=25000, per_mile_cents=800, per_hour_cents=20000,
        active=True,
    )
    session.add(spr_only)
    session.commit()
    r = client.post("/quote", json=_payload(vehicle_pref="escalade-esv"))
    body = r.json()
    assert body["resolved"] is False
    assert body["vehicle_class"] == "suv"
    assert "suv" in body["message"].lower()


def test_quote_inactive_card_skipped(client, session):
    """An inactive card shouldn't be picked. Add an inactive SUV card and an
    active one — the response should match the active one."""
    inactive = RateCard(
        name="Inactive SUV",
        vehicle_class=VehicleClass.SUV,
        base_fare_cents=99999, per_mile_cents=99999,
        active=False,
    )
    active = RateCard(
        name="Active SUV",
        vehicle_class=VehicleClass.SUV,
        base_fare_cents=15000, per_mile_cents=550, per_hour_cents=12500,
        active=True,
    )
    session.add_all([inactive, active])
    session.commit()
    r = client.post("/quote", json=_payload())
    body = r.json()
    assert body["resolved"] is True
    assert body["rate_card_name"] == "Active SUV"


def test_quote_response_shape_contract(client, session):
    """Pin the response shape — the front-end JS reads these exact field
    names, so unannounced renames would silently break the reserve form."""
    _seed_cards(session)
    r = client.post("/quote", json=_payload())
    body = r.json()
    expected_keys = {
        "resolved", "pickup_resolved", "dropoff_resolved", "vehicle_class",
        "total_cents", "total_dollars", "distance_miles", "duration_hours",
        "rate_card_id", "rate_card_name", "lines", "message",
    }
    assert expected_keys.issubset(body.keys())
    for line in body["lines"]:
        assert "label" in line and "cents" in line


def test_quote_rate_limited_shares_public_form_bucket(client, session, monkeypatch):
    """The /quote endpoint shares the same per-IP rate limiter as
    /reservations and /inquiries. Bursting >limit calls should 429."""
    _seed_cards(session)
    monkeypatch.setenv("PUBLIC_FORM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_FORM_RATE_WINDOW_S", "60")
    monkeypatch.setenv("PUBLIC_FORM_RATE_MAX", "3")

    # Clear shared bucket state so other tests' hits don't pollute this one.
    from app import _rate_history
    _rate_history.clear()

    # 3 should succeed, 4th should 429.
    for _ in range(3):
        r = client.post("/quote", json=_payload())
        assert r.status_code == 200
    r = client.post("/quote", json=_payload())
    assert r.status_code == 429


def test_quote_validates_required_fields(client, session):
    """Empty or missing pickup/dropoff should 422 — Pydantic does the
    validation, but pin the contract."""
    _seed_cards(session)
    r = client.post("/quote", json={"pickup_label": "", "dropoff_label": "Sea Island"})
    assert r.status_code == 422
    r = client.post("/quote", json={"dropoff_label": "Sea Island"})
    assert r.status_code == 422
    r = client.post("/quote", json={"pickup_label": "JAX"})
    assert r.status_code == 422


def test_quote_oversized_input_rejected(client, session):
    """Match the /reservations posture — text labels capped at 500 chars."""
    _seed_cards(session)
    huge = "x" * 600
    r = client.post(
        "/quote",
        json={"pickup_label": huge, "dropoff_label": "Sea Island", "vehicle_pref": "escalade-esv"},
    )
    assert r.status_code == 422


def test_resolve_anchor_no_substring_false_positives():
    """Short alias codes (smi/sav/jia/oaks/...) must not match inside unrelated
    words — a bare-substring match there would defeat the None soft-fail and emit
    a confident, wrong-corridor fare estimate. Word-boundary matching fixes it."""
    assert anchors.resolve_anchor("transmission repair shop") is None  # tran[smi]ssion
    assert anchors.resolve_anchor("savory bistro downtown") is None     # [sav]ory
    assert anchors.resolve_anchor("my place in fujian") is None         # fu[jia]n
    assert anchors.resolve_anchor("oakshire apartments") is None        # [oaks]hire
    # Legitimate corridor phrases still resolve.
    assert anchors.resolve_anchor("baggage claim at JAX terminal")[0] == "JAX"
    assert anchors.resolve_anchor("the lodge at sea island")[0] == "SI"
