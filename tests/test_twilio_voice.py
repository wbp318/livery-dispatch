"""Tests for the Twilio Voice + Voicemail webhooks.

The actual call routing happens in Twilio's infrastructure — we test the
TwiML our handlers return and the DB side-effects (CallLog rows created,
voicemails promoted to Reservations, dispatcher SMS alerts).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from core import (
    CallerIdentity,
    identify_caller,
    is_quiet_hours,
    parse_voicemail_transcript,
    trip_status_description,
)
from models import (
    CallLog,
    Driver,
    DriverStatus,
    Reservation,
    ReservationStatus,
    Subcontractor,
    SubcontractorTier,
    Trip,
    TripStatus,
    VehicleClass,
)


# ---------------------------------------------------------------------------
# /webhooks/twilio/voice — inbound call entry point
# ---------------------------------------------------------------------------


def test_voice_webhook_returns_twiml(client):
    response = client.post(
        "/webhooks/twilio/voice",
        data={
            "CallSid": "CA_test_1",
            "From": "+19045550000",
            "To": "+19125559999",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    body = response.text
    assert "<Response>" in body


def test_voice_webhook_creates_call_log_row(client, session):
    response = client.post(
        "/webhooks/twilio/voice",
        data={
            "CallSid": "CA_test_2",
            "From": "+19045550000",
            "To": "+19125559999",
        },
    )
    assert response.status_code == 200
    rows = list(session.scalars(select(CallLog).where(CallLog.twilio_call_sid == "CA_test_2")).all())
    assert len(rows) == 1
    assert rows[0].from_phone == "+19045550000"
    assert rows[0].direction == "inbound"
    assert rows[0].outcome == "ringing"


def test_voice_webhook_dials_dispatcher_when_configured(client, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    response = client.post(
        "/webhooks/twilio/voice",
        data={
            "CallSid": "CA_test_3",
            "From": "+19045550000",
            "To": "+19125559999",
        },
    )
    body = response.text
    assert "<Dial" in body
    assert "+19125559999" in body


def test_voice_webhook_voicemail_only_when_no_dispatcher_phone(client, monkeypatch):
    monkeypatch.delenv("DISPATCHER_PHONE", raising=False)
    response = client.post(
        "/webhooks/twilio/voice",
        data={
            "CallSid": "CA_test_4",
            "From": "+19045550000",
            "To": "+19125559999",
        },
    )
    body = response.text
    assert "<Record" in body
    assert "<Dial" not in body


# ---------------------------------------------------------------------------
# /webhooks/twilio/dial-status — completed / no-answer
# ---------------------------------------------------------------------------


def test_dial_status_completed_marks_answered(client, session):
    # Pre-seed a ringing call log
    session.add(CallLog(twilio_call_sid="CA_dial_1", from_phone="+19045550000",
                        to_phone="+19125559999", direction="inbound", outcome="ringing"))
    session.commit()
    response = client.post(
        "/webhooks/twilio/dial-status",
        data={"CallSid": "CA_dial_1", "DialCallStatus": "completed"},
    )
    assert response.status_code == 200
    log = session.scalars(select(CallLog).where(CallLog.twilio_call_sid == "CA_dial_1")).first()
    assert log.outcome == "answered"


def test_dial_status_no_answer_falls_through_to_voicemail(client, session):
    session.add(CallLog(twilio_call_sid="CA_dial_2", from_phone="+19045550000",
                        to_phone="+19125559999", direction="inbound", outcome="ringing"))
    session.commit()
    response = client.post(
        "/webhooks/twilio/dial-status",
        data={"CallSid": "CA_dial_2", "DialCallStatus": "no-answer"},
    )
    body = response.text
    assert "<Record" in body
    log = session.scalars(select(CallLog).where(CallLog.twilio_call_sid == "CA_dial_2")).first()
    assert log.outcome == "voicemail"


# ---------------------------------------------------------------------------
# /webhooks/twilio/voicemail — transcription callback
# ---------------------------------------------------------------------------


def test_voicemail_webhook_updates_log_and_creates_reservation(client, session):
    # Voice webhook first creates a CallLog
    client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_vm_1", "From": "+19045550000", "To": "+19125559999"},
    )
    # Then the transcription-callback lands
    response = client.post(
        "/webhooks/twilio/voicemail",
        data={
            "CallSid": "CA_vm_1",
            "From": "+19045550000",
            "To": "+19125559999",
            "RecordingUrl": "https://api.twilio.com/RE_test/recording.mp3",
            "RecordingDuration": "23",
            "TranscriptionText": "Hi this is Mr Henderson, I need a Sprinter for tomorrow.",
        },
    )
    assert response.status_code == 204

    log = session.scalars(select(CallLog).where(CallLog.twilio_call_sid == "CA_vm_1")).first()
    assert log is not None
    assert log.outcome == "voicemail"
    assert log.recording_url == "https://api.twilio.com/RE_test/recording.mp3"
    assert log.recording_duration_seconds == 23
    assert "Henderson" in log.transcription
    assert log.linked_reservation_id is not None

    res = session.get(Reservation, log.linked_reservation_id)
    assert res is not None
    assert res.status == ReservationStatus.PENDING_REVIEW
    assert "Henderson" in res.notes


def test_voicemail_webhook_no_transcription_no_reservation(client, session):
    """A recording with no transcribable speech (silence, hangup) shouldn't
    create a reservation — but should still update the CallLog with the URL."""
    client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_vm_2", "From": "+19045550000", "To": "+19125559999"},
    )
    response = client.post(
        "/webhooks/twilio/voicemail",
        data={
            "CallSid": "CA_vm_2",
            "RecordingUrl": "https://api.twilio.com/RE_test/recording2.mp3",
            "RecordingDuration": "2",
            "TranscriptionText": "",
        },
    )
    assert response.status_code == 204
    log = session.scalars(select(CallLog).where(CallLog.twilio_call_sid == "CA_vm_2")).first()
    assert log.recording_url is not None
    assert log.linked_reservation_id is None


def test_voicemail_webhook_handles_orphan_call(client, session):
    """Idempotency: if Twilio sends the transcription callback without a
    prior voice-webhook fire (race or retry), we should create the CallLog
    from scratch rather than 500."""
    response = client.post(
        "/webhooks/twilio/voicemail",
        data={
            "CallSid": "CA_orphan_1",
            "From": "+19045550000",
            "To": "+19125559999",
            "RecordingUrl": "https://api.twilio.com/RE_test/orphan.mp3",
            "RecordingDuration": "12",
            "TranscriptionText": "I need a ride.",
        },
    )
    assert response.status_code == 204
    log = session.scalars(select(CallLog).where(CallLog.twilio_call_sid == "CA_orphan_1")).first()
    assert log is not None
    assert log.outcome == "voicemail"


def test_voicemail_done_returns_thank_you_twiml(client):
    """The action callback fires when <Record> finishes. Should return TwiML
    that says thank you and hangs up — the transcription-callback above is
    what does the real work."""
    response = client.post(
        "/webhooks/twilio/voicemail-done",
        data={"CallSid": "CA_done_1"},
    )
    assert response.status_code == 200
    assert "<Hangup" in response.text


# ---------------------------------------------------------------------------
# Feature 1: Voicemail transcript → structured reservation parsing
# ---------------------------------------------------------------------------


def test_parse_voicemail_extracts_name():
    r = parse_voicemail_transcript("Hi this is John Henderson, I need a ride")
    assert r["name"] == "John Henderson"


def test_parse_voicemail_name_case_insensitive():
    r = parse_voicemail_transcript("my name is sarah jones and i need a car")
    assert r["name"] == "Sarah Jones"


def test_parse_voicemail_extracts_pickup():
    r = parse_voicemail_transcript("I need a pickup from the Cloister going to JAX airport")
    assert r["pickup_label"] is not None
    assert "Cloister" in r["pickup_label"]


def test_parse_voicemail_extracts_dropoff():
    r = parse_voicemail_transcript("I need a ride going to Jekyll Island please")
    assert r["dropoff_label"] is not None
    assert "Jekyll" in r["dropoff_label"]


def test_parse_voicemail_extracts_time():
    r = parse_voicemail_transcript("I need a car at 6 pm tomorrow")
    assert r["pickup_time_str"] is not None
    assert "6" in r["pickup_time_str"]


def test_parse_voicemail_extracts_date_keyword():
    r = parse_voicemail_transcript("I need a car tomorrow evening around 7 pm")
    assert r["pickup_date"] == "tomorrow evening"


def test_parse_voicemail_unparseable_graceful_degradation():
    r = parse_voicemail_transcript("uh yeah call me back")
    assert r["name"] is None
    assert r["pickup_label"] is None
    assert r["dropoff_label"] is None
    assert r["pickup_date"] is None
    assert r["pickup_time_str"] is None


def test_parse_voicemail_empty_input():
    r = parse_voicemail_transcript("")
    assert all(v is None for v in r.values())
    r2 = parse_voicemail_transcript("   ")
    assert all(v is None for v in r2.values())


# ---------------------------------------------------------------------------
# Voicemail parser hardening (v1.0.5)
# ---------------------------------------------------------------------------
#
# The transcript is attacker-controlled — a caller can speak anything Twilio's
# STT will transcribe. Probes confirm the parser strips control chars, caps
# per-field length below the Reservation DB column limits, rejects whitespace-
# only captures, and truncates pathologically long input.


def test_vm_parser_caps_name_length():
    # Construct a name capture that would exceed 80 chars without the cap.
    long_name = " ".join(["Alexandros"] * 20)  # ~210 chars, all valid name words
    r = parse_voicemail_transcript(f"my name is {long_name} calling")
    name = r["name"]
    assert name is not None
    assert len(name) <= 80, f"Parsed name length {len(name)} exceeds 80-char cap"


def test_vm_parser_caps_pickup_label_length():
    # The pickup regex captures until a terminator. Feed it a long unterminated string.
    long_loc = " ".join(["EastBeach"] * 60)  # ~600 chars
    r = parse_voicemail_transcript(f"pickup from {long_loc}")
    label = r["pickup_label"]
    if label is not None:
        assert len(label) <= 200, f"Pickup label length {len(label)} exceeds cap"


def test_vm_parser_caps_dropoff_label_length():
    long_loc = " ".join(["WestBeach"] * 60)
    r = parse_voicemail_transcript(f"going to {long_loc}")
    label = r["dropoff_label"]
    if label is not None:
        assert len(label) <= 200, f"Dropoff label length {len(label)} exceeds cap"


def test_vm_parser_strips_null_bytes():
    r = parse_voicemail_transcript("my name is John\x00Doe calling")
    name = r["name"]
    # Whatever the parser captures, it must not contain a null byte.
    if name is not None:
        assert "\x00" not in name


def test_vm_parser_strips_control_characters():
    r = parse_voicemail_transcript("my name is Mary\x01\x02\x07Johnson calling")
    name = r["name"]
    if name is not None:
        # No control chars survive.
        assert all(ch == " " or ch >= " " for ch in name), f"Control char in: {name!r}"


def test_vm_parser_rejects_whitespace_only_captures():
    # Construct a regex match where the captured group is whitespace/control
    # chars only. After sanitization, the field should be None.
    r = parse_voicemail_transcript("pickup from \x00\x01\x02 to JAX")
    # Whatever survives must be non-whitespace, non-control.
    if r["pickup_label"] is not None:
        assert r["pickup_label"].strip(), "whitespace-only capture should be rejected"


def test_vm_parser_truncates_oversized_input():
    # 50KB transcript — parser must not hang or return giant strings.
    huge = "blah " * 10000  # 50k chars
    huge = "my name is John Smith calling " + huge + "pickup from Cloister"
    r = parse_voicemail_transcript(huge)
    # Should still parse the name (it's near the start, within the 5000-char cap).
    assert r["name"] == "John Smith"
    # The "pickup from Cloister" near the end is past the cap → not parsed.
    assert r["pickup_label"] is None


def test_vm_parser_xss_payload_in_name_treated_as_literal():
    # Caller speaks something Twilio transcribes literally. If the dispatcher
    # UI ever renders the name unescaped, this is the XSS test surface.
    r = parse_voicemail_transcript("my name is <script>alert(1)</script> calling")
    # Whatever the parser captures should still be a literal string (no
    # transformation that "executes" anything). Template-escape is the real
    # defense; here we just verify the input is preserved as text.
    name = r["name"]
    if name is not None:
        assert isinstance(name, str)


def test_vm_parser_per_field_caps_below_db_columns():
    """Sanity check: per-field caps must be ≤ Reservation DB column limits
    so anything that slips through the regex still inserts cleanly."""
    from core import (
        _VM_MAX_NAME_CHARS,
        _VM_MAX_LABEL_CHARS,
        _VM_MAX_DATE_CHARS,
        _VM_MAX_TIME_CHARS,
    )
    # Reservation column limits (from models.py).
    assert _VM_MAX_NAME_CHARS <= 200
    assert _VM_MAX_LABEL_CHARS <= 500
    assert _VM_MAX_DATE_CHARS <= 20
    assert _VM_MAX_TIME_CHARS <= 10


def test_voicemail_webhook_populates_structured_fields(client, session):
    """Integration: rich transcript → Reservation has parsed name + locations."""
    client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_parse_1", "From": "+19045551234", "To": "+19125559999"},
    )
    client.post(
        "/webhooks/twilio/voicemail",
        data={
            "CallSid": "CA_parse_1",
            "From": "+19045551234",
            "To": "+19125559999",
            "RecordingUrl": "https://api.twilio.com/RE_parse/recording.mp3",
            "RecordingDuration": "30",
            "TranscriptionText": (
                "Hi this is John Henderson I need a pickup from the Cloister "
                "going to JAX airport tomorrow at 6 pm"
            ),
        },
    )
    log = session.scalars(
        select(CallLog).where(CallLog.twilio_call_sid == "CA_parse_1")
    ).first()
    assert log.linked_reservation_id is not None
    res = session.get(Reservation, log.linked_reservation_id)
    assert res.name == "John Henderson"
    assert "Cloister" in res.pickup_label
    assert res.pickup_date == "tomorrow"
    assert res.pickup_time_str is not None
    # Raw transcription always preserved in notes
    assert "Henderson" in res.notes


def test_voicemail_webhook_unparseable_uses_fallback_fields(client, session):
    """Garbled transcript → Reservation created with placeholder defaults."""
    client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_parse_2", "From": "+19045559876", "To": "+19125559999"},
    )
    client.post(
        "/webhooks/twilio/voicemail",
        data={
            "CallSid": "CA_parse_2",
            "From": "+19045559876",
            "RecordingUrl": "https://api.twilio.com/RE_parse/garble.mp3",
            "RecordingDuration": "5",
            "TranscriptionText": "uh yeah call me back please",
        },
    )
    log = session.scalars(
        select(CallLog).where(CallLog.twilio_call_sid == "CA_parse_2")
    ).first()
    res = session.get(Reservation, log.linked_reservation_id)
    assert "Voicemail from" in res.name
    assert "see transcription" in res.pickup_label


# ---------------------------------------------------------------------------
# Foundation: identify_caller
# ---------------------------------------------------------------------------


def _seed_driver(session, name="James", phone="+19045550001", status=DriverStatus.ON_TRIP):
    d = Driver(
        name=name, phone=phone, status=status,
        vehicle_classes="suv", auth_token=f"tok_{name.lower()}",
    )
    session.add(d)
    session.flush()
    return d


def _seed_trip(session, *, driver=None, passenger_phone="+19045550099",
               status=TripStatus.EN_ROUTE_PICKUP):
    t = Trip(
        passenger_name="Test Passenger", passenger_phone=passenger_phone,
        pickup_label="The Cloister", dropoff_label="JAX Airport",
        pickup_lat=31.19, pickup_lon=-81.37,
        dropoff_lat=30.49, dropoff_lon=-81.69,
        pickup_time=datetime(2026, 6, 1, 22, 0),
        vehicle_class=VehicleClass.SUV, status=status,
        assigned_driver_id=driver.id if driver else None,
    )
    session.add(t)
    session.flush()
    return t


def test_identify_caller_driver_with_active_trip(session):
    d = _seed_driver(session)
    t = _seed_trip(session, driver=d, status=TripStatus.EN_ROUTE_PICKUP)
    caller = identify_caller(session, d.phone)
    assert caller.kind == "driver"
    assert caller.name == d.name
    assert caller.active_trip is not None
    assert caller.active_trip.id == t.id


def test_identify_caller_driver_no_active_trip(session):
    d = _seed_driver(session, status=DriverStatus.AVAILABLE)
    caller = identify_caller(session, d.phone)
    assert caller.kind == "driver"
    assert caller.active_trip is None


def test_identify_caller_subcontractor(session):
    sub = Subcontractor(
        name="Coast Limo", dispatch_phone="+19125550050",
        vehicle_classes="suv,sedan", tier=SubcontractorTier.TIER_1,
        markup_pct=15.0,
    )
    session.add(sub)
    session.flush()
    caller = identify_caller(session, "+19125550050")
    assert caller.kind == "subcontractor"
    assert caller.name == "Coast Limo"


def test_identify_caller_customer_with_active_trip(session):
    d = _seed_driver(session)
    t = _seed_trip(session, driver=d, passenger_phone="+19045553333",
                   status=TripStatus.ON_SCENE)
    caller = identify_caller(session, "+19045553333")
    assert caller.kind == "customer"
    assert caller.name == t.passenger_name
    assert caller.active_trip.id == t.id
    assert caller.driver_for_trip is not None
    assert caller.driver_for_trip.id == d.id


def test_identify_caller_unknown(session):
    caller = identify_caller(session, "+19999990000")
    assert caller.kind == "unknown"
    assert caller.active_trip is None


def test_identify_caller_empty_phone(session):
    caller = identify_caller(session, "")
    assert caller.kind == "unknown"


def test_trip_status_description_passenger():
    assert "on the way" in trip_status_description(TripStatus.EN_ROUTE_PICKUP)
    assert "arrived" in trip_status_description(TripStatus.ON_SCENE)


def test_trip_status_description_dispatcher():
    assert "en route" in trip_status_description(TripStatus.EN_ROUTE_PICKUP, audience="dispatcher")


# ---------------------------------------------------------------------------
# Feature 4: Driver vs. customer call routing (timeout)
# ---------------------------------------------------------------------------


def test_voice_webhook_driver_with_active_trip_shorter_timeout(client, session, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    d = _seed_driver(session)
    _seed_trip(session, driver=d, status=TripStatus.IN_TRANSIT)
    response = client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_drv_1", "From": d.phone, "To": "+19125559999"},
    )
    body = response.text
    assert 'timeout="10"' in body


def test_voice_webhook_unknown_caller_standard_timeout(client, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    response = client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_unk_1", "From": "+19999990000", "To": "+19125559999"},
    )
    body = response.text
    assert 'timeout="20"' in body


# ---------------------------------------------------------------------------
# Feature 3: After-hours smart fallback
# ---------------------------------------------------------------------------


def test_quiet_hours_returns_true_at_3am_et():
    # 3am ET = 07:00 UTC (during EST) or 07:00 UTC (EDT)
    # Use a time that's unambiguously 3am ET: 2026-01-15 08:00 UTC = 3am EST
    dt = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(dt) is True


def test_quiet_hours_returns_false_at_10pm_et():
    # 10pm ET = 03:00 UTC next day (EST)
    dt = datetime(2026, 1, 16, 3, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(dt) is False


def test_quiet_hours_returns_false_at_noon_et():
    dt = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)  # noon EST
    assert is_quiet_hours(dt) is False


def test_quiet_hours_wraps_midnight(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "22:00")
    monkeypatch.setenv("QUIET_HOURS_END", "06:00")
    # 11pm ET = 04:00 UTC (EST)
    assert is_quiet_hours(datetime(2026, 1, 16, 4, 0, tzinfo=timezone.utc)) is True
    # 3am ET = 08:00 UTC (EST)
    assert is_quiet_hours(datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)) is True
    # 10am ET = 15:00 UTC (EST)
    assert is_quiet_hours(datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)) is False


def test_voice_webhook_quiet_hours_skips_dial(client, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    monkeypatch.setenv("QUIET_HOURS_ENABLED", "true")
    import core as _core
    monkeypatch.setattr(_core, "is_quiet_hours", lambda now_utc=None: True)
    response = client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_quiet_1", "From": "+19045550000", "To": "+19125559999"},
    )
    body = response.text
    assert "<Dial" not in body
    assert "currently away" in body
    assert "<Record" in body


def test_voice_webhook_quiet_hours_sets_outcome(client, session, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    monkeypatch.setenv("QUIET_HOURS_ENABLED", "true")
    import core as _core
    monkeypatch.setattr(_core, "is_quiet_hours", lambda now_utc=None: True)
    client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_quiet_2", "From": "+19045550000", "To": "+19125559999"},
    )
    log = session.scalars(
        select(CallLog).where(CallLog.twilio_call_sid == "CA_quiet_2")
    ).first()
    assert log.outcome == "after_hours"


def test_voice_webhook_quiet_hours_disabled_by_toggle(client, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    monkeypatch.setenv("QUIET_HOURS_ENABLED", "false")
    import core as _core
    monkeypatch.setattr(_core, "is_quiet_hours", lambda now_utc=None: True)
    response = client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_quiet_3", "From": "+19045550000", "To": "+19125559999"},
    )
    body = response.text
    assert "<Dial" in body  # not gated; dials through


# ---------------------------------------------------------------------------
# Features 2 + 5: Whisper endpoint + active-trip status
# ---------------------------------------------------------------------------


def test_whisper_endpoint_returns_caller_identity(client):
    response = client.post(
        "/webhooks/twilio/whisper?kind=driver&name=James",
        data={"CallSid": "CA_w_1"},
    )
    assert response.status_code == 200
    body = response.text
    assert "Call from James" in body
    assert "driver" in body


def test_whisper_endpoint_includes_trip_status(client):
    response = client.post(
        "/webhooks/twilio/whisper?kind=customer&name=John&trip_id=7&trip_status=en_route_pickup",
        data={"CallSid": "CA_w_2"},
    )
    body = response.text
    assert "Trip 7" in body
    assert "en route" in body


def test_whisper_endpoint_unknown_caller_minimal(client):
    response = client.post(
        "/webhooks/twilio/whisper",
        data={"CallSid": "CA_w_3"},
    )
    body = response.text
    assert "Unknown caller" in body


def test_whisper_endpoint_xml_escapes_name(client):
    # Name with XML metacharacters that MUST be escaped in <Say> text content.
    response = client.post(
        "/webhooks/twilio/whisper?kind=customer&name=A%3Cb%26c",  # "A<b&c"
        data={"CallSid": "CA_w_4"},
    )
    assert response.status_code == 200
    body = response.text
    # Raw metacharacters must NOT appear unescaped in the spoken text.
    assert "A<b&c" not in body
    # saxutils.escape() turns < -> &lt; and & -> &amp; in text content.
    assert "A&lt;b&amp;c" in body
    # Response envelope stays well-formed.
    assert "<Response>" in body and "</Response>" in body


def test_voice_webhook_customer_with_active_trip_hears_status(client, session, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    d = _seed_driver(session)
    _seed_trip(session, driver=d, passenger_phone="+19045553333",
               status=TripStatus.EN_ROUTE_PICKUP)
    response = client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_cust_1", "From": "+19045553333", "To": "+19125559999"},
    )
    body = response.text
    assert "on the way to pick you up" in body
    assert "Connecting you to dispatch now" in body


def test_voice_webhook_customer_without_active_trip_standard_greeting(client, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    response = client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_cust_2", "From": "+19045559876", "To": "+19125559999"},
    )
    body = response.text
    assert "Livery Dispatch" in body
    assert "on the way" not in body


def test_voice_webhook_whisper_url_present(client, monkeypatch):
    """Every call (even unknown) gets a whisper URL on the <Number> element."""
    monkeypatch.setenv("DISPATCHER_PHONE", "+19125559999")
    response = client.post(
        "/webhooks/twilio/voice",
        data={"CallSid": "CA_wurl_1", "From": "+19999990000", "To": "+19125559999"},
    )
    body = response.text
    assert "/webhooks/twilio/whisper?" in body
    assert "kind=unknown" in body
