"""Pydantic request/response schemas for the dispatch API."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models import (
    BroadcastResponse,
    DriverStatus,
    InquiryKind,
    InquiryStatus,
    ReservationStatus,
    SubcontractorTier,
    TripStatus,
    VehicleClass,
)


# E.164: a leading "+", then a country-code first digit 1–9, then up to 14 more
# digits. We strip common human separators (spaces, dashes, parens, dots) before
# validating, so "+1 (904) 555-1234" normalizes to "+19045551234" — the form
# Twilio uses on inbound webhook From-addresses.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
_PHONE_SEPS_RE = re.compile(r"[\s\-().]")


def _normalize_phone(value: str, field_name: str = "phone") -> str:
    """Strip common separators and coerce to E.164. Raises ValueError on bad input.

    The corridor is US-only (GA/FL), and the public reserve form's phone field is
    a free-text ``type="tel"`` with only a ``+1`` placeholder — no hard pattern —
    so most guests type a bare US number ("912-555-0100", "(904) 555-1234"). We
    coerce the two unambiguous US shapes to E.164 rather than 422-rejecting them
    (which previously dropped the reservation to the silent Netlify fallback):
      - 10 digits          → assume US, prepend "+1"
      - 11 digits, "1…"    → US with country code, prepend "+"
    Anything already E.164 passes through; anything else still raises.
    """
    cleaned = _PHONE_SEPS_RE.sub("", value)
    if not cleaned.startswith("+"):
        if len(cleaned) == 10 and cleaned.isdigit():
            cleaned = "+1" + cleaned
        elif len(cleaned) == 11 and cleaned.isdigit() and cleaned.startswith("1"):
            cleaned = "+" + cleaned
    if not _E164_RE.match(cleaned):
        raise ValueError(
            f"{field_name} must be a valid phone number — a US number "
            f"(e.g. (904) 555-1234) or E.164 with country code (e.g. +19045551234)"
        )
    return cleaned


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


class DriverCreate(BaseModel):
    name: str
    phone: str
    vehicle_classes: list[VehicleClass] = Field(default_factory=lambda: [VehicleClass.SUV])

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        return _normalize_phone(v, "phone")


class DriverStatusUpdate(BaseModel):
    status: DriverStatus
    # Optional coords: Field(ge=/le=) bounds apply only to provided non-None
    # values — an omitted or explicit-None field skips the range check by design.
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)


class DriverLocationUpdate(BaseModel):
    """Location-only update. Used by the Phase-4.5 driver PWA to push GPS coords
    every ~30s without touching the driver's availability state. Status changes
    go through `/drivers/{id}/status`; this is purely position reporting."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    status: DriverStatus
    vehicle_classes: str
    lat: float | None
    lon: float | None


# ---------------------------------------------------------------------------
# Subcontractors
# ---------------------------------------------------------------------------


class SubcontractorCreate(BaseModel):
    name: str
    dispatch_phone: str
    dispatch_email: str | None = None
    vehicle_classes: list[VehicleClass] = Field(default_factory=lambda: [VehicleClass.SUV])
    tier: SubcontractorTier = SubcontractorTier.TIER_1
    markup_pct: float = Field(default=15.0, ge=0.0)
    base_lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    base_lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    service_radius_mi: float | None = Field(default=None, ge=0.0)
    notes: str | None = None

    @field_validator("dispatch_phone")
    @classmethod
    def _validate_dispatch_phone(cls, v: str) -> str:
        return _normalize_phone(v, "dispatch_phone")


class SubcontractorUpdate(BaseModel):
    """Patch-style: any field may be omitted to keep its current value."""

    name: str | None = None
    dispatch_phone: str | None = None
    dispatch_email: str | None = None
    vehicle_classes: list[VehicleClass] | None = None
    tier: SubcontractorTier | None = None
    markup_pct: float | None = Field(default=None, ge=0.0)
    base_lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    base_lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    service_radius_mi: float | None = Field(default=None, ge=0.0)
    active: bool | None = None
    notes: str | None = None

    @field_validator("dispatch_phone")
    @classmethod
    def _validate_dispatch_phone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _normalize_phone(v, "dispatch_phone")


class SubcontractorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    dispatch_phone: str
    dispatch_email: str | None
    vehicle_classes: str
    tier: SubcontractorTier
    markup_pct: float
    base_lat: float | None
    base_lon: float | None
    service_radius_mi: float | None
    active: bool
    notes: str | None


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


class TripCreate(BaseModel):
    passenger_name: str
    passenger_phone: str
    pickup_label: str
    pickup_lat: float = Field(ge=-90.0, le=90.0)
    pickup_lon: float = Field(ge=-180.0, le=180.0)
    dropoff_label: str
    dropoff_lat: float = Field(ge=-90.0, le=90.0)
    dropoff_lon: float = Field(ge=-180.0, le=180.0)
    pickup_time: datetime
    vehicle_class: VehicleClass = VehicleClass.SUV
    notes: str | None = None

    @field_validator("passenger_phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        return _normalize_phone(v, "passenger_phone")


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    passenger_name: str
    passenger_phone: str
    pickup_label: str
    pickup_lat: float
    pickup_lon: float
    dropoff_label: str
    dropoff_lat: float
    dropoff_lon: float
    pickup_time: datetime
    vehicle_class: VehicleClass
    notes: str | None
    status: TripStatus
    assigned_driver_id: int | None
    assigned_subcontractor_id: int | None
    fare_cents: int | None
    markup_cents: int | None
    created_at: datetime
    completed_at: datetime | None = None  # v1.1.20 — settlement date


class TripBroadcastOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trip_id: int
    driver_id: int | None
    subcontractor_id: int | None
    response: BroadcastResponse
    distance_mi: float
    sent_at: datetime


class TripStatusUpdate(BaseModel):
    status: TripStatus
    fare_cents: int | None = Field(default=None, ge=0)


class SubAcceptRequest(BaseModel):
    """Optional cost quote at acceptance time. If provided, the trip's
    fare_cents = quoted_cost_cents + markup_cents (computed from sub.markup_pct).
    """

    quoted_cost_cents: int | None = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# Inbound webhooks
# ---------------------------------------------------------------------------


class WebhookAck(BaseModel):
    """Response shape for the inbound SMS webhook (also returned to the
    operator for visibility — Twilio itself doesn't use the body, only the
    HTTP status, but a structured ack is useful for testing and ops dashboards)."""

    matched: bool
    recipient_kind: str | None = None  # "driver" or "subcontractor"
    recipient_id: int | None = None
    trip_id: int | None = None
    action: str | None = None  # "accept" or "decline"
    detail: str


# ---------------------------------------------------------------------------
# Reservations (customer-side inquiries from the marketing site form)
# ---------------------------------------------------------------------------


class ReservationCreate(BaseModel):
    """Public POST body — accepts the site reserve form 1:1.

    Validation is intentionally permissive: text addresses pass through, the
    date and time fields can be either separate strings or a combined ISO
    `pickup_at`, and `bot_field` is the Netlify-style honeypot. Anything
    that arrives with a non-empty `bot_field` is recorded with status SPAM
    so the dispatcher's review queue stays clean.
    """

    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=32)
    pickup_label: str = Field(min_length=1, max_length=500)
    dropoff_label: str = Field(min_length=1, max_length=500)
    pickup_date: str | None = Field(default=None, max_length=20)
    pickup_time_str: str | None = Field(default=None, max_length=10)
    pickup_at: datetime | None = None
    passengers: str | None = Field(default=None, max_length=20)
    vehicle_pref: str | None = Field(default=None, max_length=40)
    flight: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(default=None, max_length=120)
    bot_field: str | None = Field(default=None, max_length=500)

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        return _normalize_phone(v, "phone")


class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    phone: str
    pickup_label: str
    dropoff_label: str
    pickup_date: str | None
    pickup_time_str: str | None
    pickup_at: datetime | None
    passengers: str | None
    vehicle_pref: str | None
    vehicle_class: VehicleClass | None
    flight: str | None
    notes: str | None
    source: str | None
    status: ReservationStatus
    linked_trip_id: int | None
    dispatcher_notes: str | None
    created_at: datetime


class ReservationUpdate(BaseModel):
    """Dispatcher-side patch: change the lifecycle status, attach notes, or
    record the Trip the reservation was promoted into."""

    status: ReservationStatus | None = None
    dispatcher_notes: str | None = Field(default=None, max_length=2000)
    linked_trip_id: int | None = None


# ---------------------------------------------------------------------------
# Inquiries (wedding-inquiry / vendor-onboarding / early-access)
# ---------------------------------------------------------------------------


class InquiryCreate(BaseModel):
    """Public POST body for the three non-reservation marketing-site forms.

    `kind` is the discriminator (wedding / vendor / early_access). Common
    fields are explicit columns; kind-specific overflow goes in `details` as
    a free-form dict that the API serializes to JSON server-side.
    """

    kind: InquiryKind
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=2000)
    details: dict | None = Field(default=None)
    source: str | None = Field(default=None, max_length=120)
    bot_field: str | None = Field(default=None, max_length=500)

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return _normalize_phone(v, "phone")


class InquiryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: InquiryKind
    name: str
    email: str
    phone: str | None
    notes: str | None
    details: dict | None  # parsed back from details_json
    source: str | None
    status: InquiryStatus
    dispatcher_notes: str | None
    created_at: datetime


class InquiryUpdate(BaseModel):
    """Dispatcher-side patch: status + private notes only."""

    status: InquiryStatus | None = None
    dispatcher_notes: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Quote (public — pre-submission estimate for the reserve form)
# ---------------------------------------------------------------------------


class QuoteRequest(BaseModel):
    """Public POST body — shaped like the reserve form's submission.

    Carries free-text labels (no coordinates), an optional vehicle preference,
    and an optional pickup time. The /quote endpoint resolves the text labels
    via `anchors.resolve_anchor`, falls back to a "dispatcher confirms"
    response when anything can't be placed, and is rate-limited per IP via
    the same bucket as /reservations + /inquiries.
    """

    pickup_label: str = Field(min_length=1, max_length=500)
    dropoff_label: str = Field(min_length=1, max_length=500)
    vehicle_pref: str | None = Field(default=None, max_length=40)
    pickup_at: datetime | None = None
    is_peak_week: bool = False


class QuoteLine(BaseModel):
    """One line item in the quote breakdown (base fare, distance, time, peak,
    surcharges, minimum-fare bump). Mirrors `fare.FareBreakdown.lines`."""

    label: str
    cents: int


class QuoteResponse(BaseModel):
    """Estimate response from /quote.

    `resolved=True` when both pickup + dropoff were placed against the anchor
    table AND an active rate card exists for the resolved vehicle class. In
    that case `total_cents` is the estimate and `message` is the customer-
    facing copy ("From $XXX — estimate, dispatcher confirms by phone").

    `resolved=False` is the soft-fail path — either we couldn't place one of
    the labels, the vehicle preference was multi-vehicle (no auto-class), or
    no active rate card matched. `total_cents` is null; `message` explains.
    The reserve form renders the message and lets the guest submit anyway.
    """

    resolved: bool
    pickup_resolved: str | None = None
    dropoff_resolved: str | None = None
    vehicle_class: VehicleClass | None = None
    total_cents: int | None = None
    total_dollars: float | None = None
    distance_miles: float | None = None
    duration_hours: float | None = None
    rate_card_id: int | None = None
    rate_card_name: str | None = None
    lines: list[QuoteLine] = Field(default_factory=list)
    message: str


# ---------------------------------------------------------------------------
# Map (live dispatcher view)
# ---------------------------------------------------------------------------
#
# The dispatcher map polls `GET /map/data` every ~12s for fresh positions. The
# payload is intentionally tight: enough to render every entity that belongs on
# the map (drivers, sub bases, active trip pickups + dropoffs) and nothing else.
# Full per-resource detail still flows through the existing typed endpoints.


class MapDriver(BaseModel):
    id: int
    name: str
    phone: str
    status: DriverStatus
    vehicle_classes: str
    lat: float | None
    lon: float | None


class MapSubcontractor(BaseModel):
    id: int
    name: str
    tier: SubcontractorTier
    vehicle_classes: str
    base_lat: float | None
    base_lon: float | None
    service_radius_mi: float | None


class MapTrip(BaseModel):
    id: int
    passenger_name: str
    status: TripStatus
    vehicle_class: VehicleClass
    pickup_label: str
    pickup_lat: float
    pickup_lon: float
    dropoff_label: str
    dropoff_lat: float
    dropoff_lon: float
    pickup_time: datetime
    assigned_driver_id: int | None
    assigned_subcontractor_id: int | None


class MapData(BaseModel):
    """Snapshot the live map renders. Generated on every poll."""

    generated_at: datetime
    drivers: list[MapDriver]
    subcontractors: list[MapSubcontractor]
    active_trips: list[MapTrip]


# ---------------------------------------------------------------------------
# Web push subscriptions (v1.1.0)
# ---------------------------------------------------------------------------


class PushSubscriptionKeys(BaseModel):
    """The `.keys` sub-object of a W3C PushSubscription. p256dh is the
    base64url ECDH public key (~88 chars); auth is the base64url 16-byte
    authentication secret (~22 chars). We accept a slightly larger range
    to absorb implementation variance + padding."""

    p256dh: str = Field(min_length=40, max_length=120)
    auth: str = Field(min_length=8, max_length=48)


class PushSubscriptionIn(BaseModel):
    """Subscription payload from `pushManager.subscribe().toJSON()`. Field
    names match the W3C spec so the client JS can POST the object verbatim.

    `endpoint` is the URL the push service gave the browser. Length cap
    matches the DB column. `user_agent` is captured server-side from the
    request header; clients don't send it."""

    endpoint: str = Field(min_length=1, max_length=500)
    keys: PushSubscriptionKeys


class PushUnsubscribeIn(BaseModel):
    """Identifies the device to unsubscribe — endpoint is the natural key."""

    endpoint: str = Field(min_length=1, max_length=500)


class PushSubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    driver_id: int
    endpoint: str
    created_at: datetime
    last_seen_at: datetime
    last_success_at: datetime | None


class PushVAPIDKeyOut(BaseModel):
    """Public VAPID key the browser uses in `pushManager.subscribe()`.
    `enabled=false` means the feature flag is off and the dashboard
    button stays disabled."""

    publicKey: str
    enabled: bool
