"""SQLAlchemy ORM models for the dispatch domain.

Five core entities:
    - Driver: a chauffeur with a current status, location, and licensed vehicle classes.
    - Subcontractor: an outside operator reachable for overflow handoff. We don't model the sub's internal
      drivers; the sub handles its own dispatch. We track a single dispatch
      contact phone, the vehicle classes they can serve, a tier (1=preferred,
      2=backup, 3=last-resort), the markup applied to their cost-plus, and the
      base location + service radius used for candidate filtering.
    - Trip: a passenger ride request with pickup/dropoff and lifecycle status.
      Can be assigned to either a Driver (own fleet) or a Subcontractor
      (overflow), but not both. The atomic compare-and-set in core.accept_trip*
      enforces this.
    - TripBroadcast: per-recipient record of a broadcast offer for a given trip.
      Recipient is exactly one of driver_id / subcontractor_id (XOR enforced by
      table check constraint). This polymorphism keeps the broadcast/accept
      logic uniform across own-fleet and overflow handoff.
    - Reservation: a customer-side booking inquiry from the marketing site
      reserve form. Carries text addresses (no geocoding required) and the
      raw form fields. A dispatcher later promotes a Reservation into a Trip
      after confirming details with the guest — Year-1 reality is the human
      dispatcher in the loop, not auto-conversion.

Vehicle is intentionally folded into Driver/Subcontractor (vehicle_classes is a
comma-separated list of classes the recipient can serve). Real fleet management
would split this out; for MVP it keeps the model surface tight.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VehicleClass(str, Enum):
    SUV = "suv"          # Cadillac Escalade ESV / Suburban-class
    SPRINTER = "sprinter"  # Mercedes Sprinter Limited / executive trim
    SEDAN = "sedan"      # included for future flex; not Year-1 fleet


class DriverStatus(str, Enum):
    OFFLINE = "offline"
    AVAILABLE = "available"
    ON_TRIP = "on_trip"
    OFF_DUTY = "off_duty"


class TripStatus(str, Enum):
    REQUESTED = "requested"           # created but not yet broadcast
    BROADCASTING = "broadcasting"     # offered to drivers/subs, awaiting accept
    ACCEPTED = "accepted"             # driver or subcontractor assigned
    EN_ROUTE_PICKUP = "en_route_pickup"
    ON_SCENE = "on_scene"             # driver at pickup
    IN_TRANSIT = "in_transit"         # passenger on board
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_DRIVERS = "no_drivers"         # no driver/sub accepted within window


class BroadcastResponse(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class SubcontractorTier(int, Enum):
    """Tier informs broadcast priority. Tier-1 = preferred; Tier-2 = backup; Tier-3 = last-resort."""

    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3


class ReservationStatus(str, Enum):
    """Lifecycle for a customer-side booking inquiry from the marketing site.

    Inquiries land as PENDING_REVIEW. The dispatcher confirms details with
    the guest by phone or email, then either CONFIRMED (intent to run) or
    DECLINED (date we can't cover, etc). When a dispatcher promotes the
    reservation into a Trip, status flips to CONVERTED and `linked_trip_id`
    points at the Trip row. SPAM is set automatically when the honeypot
    field is non-empty on submission.
    """

    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    CONVERTED = "converted"
    SPAM = "spam"


class InquiryKind(str, Enum):
    """Kinds of non-reservation inquiries from the marketing site.

    These are NOT ride bookings — they're B2B / lead-capture conversations.
    Kept separate from Reservation so the dispatcher's review queue stays
    sharp: a guest asking for a JAX → Cloister run is a different mental
    model than a wedding planner asking for a multi-day movement plan.
    """

    WEDDING = "wedding"            # concierge.html wedding-inquiry form
    VENDOR = "vendor"              # concierge.html vendor-onboarding form
    EARLY_ACCESS = "early_access"  # account.html early-access form


class InquiryStatus(str, Enum):
    """Lifecycle for an Inquiry. Lighter than Reservation — no conversion
    into a Trip; these get a human reply and then close."""

    PENDING_REVIEW = "pending_review"
    RESPONDED = "responded"
    CLOSED = "closed"
    SPAM = "spam"


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[DriverStatus] = mapped_column(SAEnum(DriverStatus), default=DriverStatus.OFFLINE)
    # Comma-separated VehicleClass values; e.g. "suv,sprinter".
    vehicle_classes: Mapped[str] = mapped_column(String(64), default="suv")
    # Last known location. Null when offline / never reported.
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-driver opaque bearer token used by the Phase 4.5 driver PWA / web
    # surface. Generated lazily on first read via `ensure_auth_token()` so
    # existing rows from pre-v0.9.9 DBs don't break, and rotatable from the
    # admin surface via /admin/drivers/{id}/action/rotate-token.
    auth_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    broadcasts: Mapped[list["TripBroadcast"]] = relationship(back_populates="driver")

    def serves(self, vehicle_class: VehicleClass) -> bool:
        return vehicle_class.value in {c.strip() for c in self.vehicle_classes.split(",")}

    def ensure_auth_token(self) -> str:
        """Return the driver's bearer token, generating one if missing.

        Callers MUST commit the session after invoking this on a row read
        from the DB — `secrets.token_urlsafe(32)` mutates the row's
        `auth_token` field but doesn't flush by itself.
        """
        if not self.auth_token:
            import secrets as _secrets
            self.auth_token = _secrets.token_urlsafe(32)
        return self.auth_token


class Subcontractor(Base):
    __tablename__ = "subcontractors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    # Single dispatch-desk phone — the sub handles internal driver routing.
    dispatch_phone: Mapped[str] = mapped_column(String(32), unique=True)
    dispatch_email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    vehicle_classes: Mapped[str] = mapped_column(String(64), default="suv")
    tier: Mapped[SubcontractorTier] = mapped_column(SAEnum(SubcontractorTier), default=SubcontractorTier.TIER_1)
    # Cost-plus markup percentage applied to the sub's quoted rate. Default is
    # the 15% Tier-1 markup.
    markup_pct: Mapped[float] = mapped_column(Float, default=15.0)
    # Sub's home base — used to compute distance to a trip pickup.
    base_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Sub will only quote inside this radius from their base. Nullable means
    # "no advertised limit" and falls back to the broadcast radius.
    service_radius_mi: Mapped[float | None] = mapped_column(Float, nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    broadcasts: Mapped[list["TripBroadcast"]] = relationship(back_populates="subcontractor")

    def serves(self, vehicle_class: VehicleClass) -> bool:
        return vehicle_class.value in {c.strip() for c in self.vehicle_classes.split(",")}


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    passenger_name: Mapped[str] = mapped_column(String(120))
    passenger_phone: Mapped[str] = mapped_column(String(32))
    pickup_label: Mapped[str] = mapped_column(String(255))
    pickup_lat: Mapped[float] = mapped_column(Float)
    pickup_lon: Mapped[float] = mapped_column(Float)
    dropoff_label: Mapped[str] = mapped_column(String(255))
    dropoff_lat: Mapped[float] = mapped_column(Float)
    dropoff_lon: Mapped[float] = mapped_column(Float)
    pickup_time: Mapped[datetime] = mapped_column(DateTime)
    vehicle_class: Mapped[VehicleClass] = mapped_column(SAEnum(VehicleClass))
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[TripStatus] = mapped_column(SAEnum(TripStatus), default=TripStatus.REQUESTED)
    # Exactly one of assigned_driver_id / assigned_subcontractor_id is set
    # once the trip is ACCEPTED. Both stay NULL until acceptance. Enforced by
    # the atomic compare-and-set in core.accept_trip / core.accept_trip_subcontractor.
    assigned_driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"), nullable=True)
    assigned_subcontractor_id: Mapped[int | None] = mapped_column(ForeignKey("subcontractors.id"), nullable=True)
    fare_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Markup applied when handed to a sub (cost-plus). Logged for accounting;
    # null for own-fleet trips.
    markup_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    # v1.1.20: stamped once by core.update_trip_status on entering COMPLETED.
    # Settlement reads this, not updated_at — any later edit (fare fix,
    # record_subcontractor_cost, notes) used to move a finished trip into the
    # current settlement period. Null for unfinished trips; legacy rows were
    # backfilled from updated_at by migration 5c1e7a9b3d42.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    broadcasts: Mapped[list["TripBroadcast"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "assigned_driver_id IS NULL OR assigned_subcontractor_id IS NULL",
            name="ck_trip_one_assignee",
        ),
    )


class TripBroadcast(Base):
    __tablename__ = "trip_broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"))
    # Polymorphic recipient: exactly one of driver_id / subcontractor_id is set.
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"), nullable=True)
    subcontractor_id: Mapped[int | None] = mapped_column(ForeignKey("subcontractors.id"), nullable=True)
    response: Mapped[BroadcastResponse] = mapped_column(
        SAEnum(BroadcastResponse), default=BroadcastResponse.PENDING
    )
    # Distance from recipient (driver location or sub base) to trip pickup.
    distance_mi: Mapped[float] = mapped_column(Float)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    trip: Mapped["Trip"] = relationship(back_populates="broadcasts")
    driver: Mapped["Driver | None"] = relationship(back_populates="broadcasts")
    subcontractor: Mapped["Subcontractor | None"] = relationship(back_populates="broadcasts")

    __table_args__ = (
        CheckConstraint(
            "(driver_id IS NOT NULL AND subcontractor_id IS NULL) "
            "OR (driver_id IS NULL AND subcontractor_id IS NOT NULL)",
            name="ck_broadcast_recipient_xor",
        ),
    )

    @property
    def recipient_kind(self) -> str:
        return "driver" if self.driver_id is not None else "subcontractor"


class Reservation(Base):
    """A customer-side booking inquiry from the marketing site reserve form.

    Carries text-only addresses (no geocoding) plus the raw form fields. A
    dispatcher reviews the inquiry, calls the guest back, and — if it's a
    real ride — promotes it to a Trip via `linked_trip_id`. The form maps
    1:1 to columns here so a future ops UI can render the original submission
    without joining anything.
    """

    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(32))
    pickup_label: Mapped[str] = mapped_column(String(500))
    dropoff_label: Mapped[str] = mapped_column(String(500))
    # The site form collects date and time as separate fields; we store both
    # the raw strings and (when JS combines them) the parsed datetime.
    pickup_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pickup_time_str: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pickup_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    passengers: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Raw form value: "escalade-esv" / "sprinter-limited" / "multi-vehicle" / "".
    vehicle_pref: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Normalized to a VehicleClass for downstream dispatch. Null when the
    # guest selects multi-vehicle or no preference — the dispatcher resolves.
    vehicle_class: Mapped[VehicleClass | None] = mapped_column(SAEnum(VehicleClass), nullable=True)
    flight: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Tracks which page on the site originated the submission; useful when
    # per-resort landing pages each get their own form.
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[ReservationStatus] = mapped_column(
        SAEnum(ReservationStatus), default=ReservationStatus.PENDING_REVIEW
    )
    # When promoted to a Trip, points at the new Trip row.
    linked_trip_id: Mapped[int | None] = mapped_column(ForeignKey("trips.id"), nullable=True)
    dispatcher_notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class RateCard(Base):
    """Pricing reference per vehicle class.

    Year-1 reality: every premium concierge channel negotiates rates per trip,
    so the rate card is a *quoting reference* not a strict tariff. The fare
    calculator (`fare.py`) computes a suggested fare from the card and the
    trip's distance/duration; the dispatcher accepts or overrides on the trip
    detail page. Surcharges stack additively; the peak_week_multiplier applies
    to the (base + per-mile + per-hour) subtotal before surcharges.
    """

    __tablename__ = "rate_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    vehicle_class: Mapped[VehicleClass] = mapped_column(SAEnum(VehicleClass))
    # All money in cents to keep math clean and DB integer-only.
    base_fare_cents: Mapped[int] = mapped_column(Integer, default=0)
    per_mile_cents: Mapped[int] = mapped_column(Integer, default=0)
    per_hour_cents: Mapped[int] = mapped_column(Integer, default=0)
    # Common surcharges. Set to 0 to disable.
    airport_meet_surcharge_cents: Mapped[int] = mapped_column(Integer, default=0)
    after_hours_surcharge_cents: Mapped[int] = mapped_column(Integer, default=0)
    # Multiplier applied to the (base + miles + hours) subtotal during peak
    # weeks (RSM, Masters week traffic, etc.). 1.0 = no peak; 1.25 = +25%.
    peak_week_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    # Minimum trip charge — applied if computed fare falls below this.
    minimum_fare_cents: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class CallLog(Base):
    """Inbound + outbound call audit trail.

    Populated by Twilio Voice webhooks on every call hitting the dispatch
    number. For inbound: row is created when the call connects; updated when
    it ends with duration and outcome. If unanswered → recorded voicemail →
    transcription. If the voicemail looks like a ride request, the webhook
    also creates a Reservation row and back-references it via
    `linked_reservation_id`. The dispatcher reviews the call list at
    /admin/calls and can replay the audio.
    """

    __tablename__ = "call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Twilio call SID — useful for cross-referencing with Twilio's console
    # and for idempotency on webhook retries.
    twilio_call_sid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    from_phone: Mapped[str] = mapped_column(String(32))
    to_phone: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16), default="inbound")  # inbound / outbound
    # answered / voicemail / missed / no_answer / failed
    outcome: Mapped[str] = mapped_column(String(20), default="answered")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Voicemail recording (if captured)
    recording_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recording_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcription: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    # When the voicemail was promoted to a reservation
    linked_reservation_id: Mapped[int | None] = mapped_column(ForeignKey("reservations.id"), nullable=True)
    dispatcher_notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Inquiry(Base):
    """Non-reservation B2B / lead-capture inquiry from the marketing site.

    Three kinds — wedding planner inquiry, vendor / concierge-desk onboarding,
    and Year-2-portal early access — that share the same dispatcher review
    workflow but carry different kind-specific fields. Rather than build
    three near-identical tables, we keep common columns (name/email/phone/
    notes) at the top level and stash kind-specific fields in `details_json`
    as a JSON-encoded string. Dispatcher UI / CLI can render the parsed
    payload back; the API surface returns it as a dict.
    """

    __tablename__ = "inquiries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[InquiryKind] = mapped_column(SAEnum(InquiryKind))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # JSON-encoded dict of kind-specific fields (e.g. wedding `weekend_date`,
    # `guests`, `resort`; vendor `organization`, `title`, `needed`;
    # early-access `account_type`). Empty / null for inquiries with no extras.
    details_json: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[InquiryStatus] = mapped_column(
        SAEnum(InquiryStatus), default=InquiryStatus.PENDING_REVIEW
    )
    dispatcher_notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class AdminAuditLog(Base):
    """Append-only log of admin write actions (POST/PATCH/DELETE on /admin/*).

    user_id is the authenticated admin username (admin auth landed v1.0.0).
    It's null only for writes to /admin/login itself. The audit middleware
    sits inside the auth middleware, so writes the auth gate rejects never
    reach it and aren't logged. payload_hash is a SHA-256
    hex digest of the raw request body; the full body is NOT stored to avoid
    PII bloat.
    """

    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(500))
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer)


class DriverPushSubscription(Base):
    """Web Push (RFC 8030 / VAPID) subscription owned by a driver (v1.1.0).

    Each device the chauffeur subscribes from is its own row — a phone + a
    tablet from the same driver are two rows. Endpoint URLs from
    FCM/Mozilla/Apple Push are long (often 200+ chars), hence String(500).
    p256dh and auth are the base64-encoded keying material the W3C Push
    spec returns from PushManager.subscribe().

    last_seen_at updates every time the dispatcher successfully delivers a
    push to this subscription. When the push service returns 410/404
    (subscription gone), the row is deleted on the spot — no soft-delete,
    no expiry sweep job. Token rotation on the parent Driver cascades
    a delete here (admin "rotate token" action implies "this device is no
    longer trusted").
    """

    __tablename__ = "driver_push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    driver_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("drivers.id", ondelete="CASCADE"), index=True
    )
    endpoint: Mapped[str] = mapped_column(String(500))
    p256dh: Mapped[str] = mapped_column(String(120))
    auth: Mapped[str] = mapped_column(String(48))
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # One row per (driver, endpoint). Re-subscribing from the same device
        # upserts — see /driver/push/subscribe.
        UniqueConstraint("driver_id", "endpoint", name="uq_push_driver_endpoint"),
    )
