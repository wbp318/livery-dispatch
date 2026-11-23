"""Core dispatch logic.

The dispatch loop:
    1. A trip is created (REQUESTED).
    2. Find candidate drivers: status=available, vehicle_class match, within radius.
    3. Rank by haversine distance to pickup.
    4. Broadcast to top N (BROADCAST_FANOUT) — sends SMS, creates TripBroadcast rows.
    5. First driver to call `accept_trip` wins; trip → ACCEPTED, others → EXPIRED.
    6. If no driver accepts (or none was eligible), the dispatcher can engage
       overflow: `broadcast_to_subcontractors` blasts to active subs in tier
       order, matched by vehicle class and service-radius.
    7. First sub to call `accept_trip_subcontractor` wins; trip is assigned to
       that sub with the agreed cost-plus markup recorded on the trip.

Distance is haversine (great-circle), not road network. For Year-1 production
this should be replaced with a real routing distance from Google Distance Matrix
or Mapbox; haversine is good-enough for MVP and the corridor is short.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models import (
    BroadcastResponse,
    Driver,
    DriverStatus,
    Subcontractor,
    SubcontractorTier,
    Trip,
    TripBroadcast,
    TripStatus,
    VehicleClass,
)
from notifications import (
    NotificationProvider,
    WebPushProvider,
    get_provider,
    get_push_provider,
    push_notifications_enabled,
)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    R_mi = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R_mi * math.asin(math.sqrt(a))


def coords_in_range(lat: float, lon: float) -> bool:
    """True iff lat in [-90, 90] and lon in [-180, 180], inclusive. Rejects
    NaN and Infinity too (comparisons against them are always False). Pure;
    raises nothing so each caller chooses its error surface (Pydantic 422 vs
    HTTPException)."""
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


# ---------------------------------------------------------------------------
# Own-fleet (Driver) dispatch
# ---------------------------------------------------------------------------


def candidate_drivers(
    session: Session,
    trip: Trip,
    radius_mi: float,
) -> list[tuple[Driver, float]]:
    """Return (driver, distance_mi) sorted ascending. Filters by status, class, location, radius."""
    drivers = session.scalars(select(Driver).where(Driver.status == DriverStatus.AVAILABLE)).all()

    out: list[tuple[Driver, float]] = []
    for d in drivers:
        if d.lat is None or d.lon is None:
            continue
        if not d.serves(trip.vehicle_class):
            continue
        dist = haversine_miles(d.lat, d.lon, trip.pickup_lat, trip.pickup_lon)
        if dist <= radius_mi:
            out.append((d, dist))

    out.sort(key=lambda pair: pair[1])
    return out


# E3 (v1.1.17): states a trip may be (re)broadcast from. Broadcasting sets
# trip.status = BROADCASTING, so without this guard POST /trips/{id}/broadcast
# on an ACCEPTED (or EN_ROUTE / COMPLETED / CANCELLED) trip silently rewound it
# and re-paged drivers, bypassing VALID_TRANSITIONS — only the UI hid the
# button. BROADCASTING is included on purpose: a re-broadcast (wider radius)
# and an own-fleet → overflow hand-off both happen while offers are open.
BROADCASTABLE_STATUSES: frozenset[TripStatus] = frozenset(
    {TripStatus.REQUESTED, TripStatus.BROADCASTING, TripStatus.NO_DRIVERS}
)


class TripNotBroadcastable(ValueError):
    """Raised when a broadcast is attempted on a trip past the offer stage."""


def has_pending_offers(trip: Trip) -> bool:
    """True while any broadcast on the trip still awaits a response (E8)."""
    return any(bc.response == BroadcastResponse.PENDING for bc in trip.broadcasts)


def _ensure_broadcastable(trip: Trip) -> None:
    if trip.status not in BROADCASTABLE_STATUSES:
        raise TripNotBroadcastable(
            f"Trip {trip.id} is {trip.status.value}; only requested, broadcasting "
            "or no_drivers trips can be broadcast"
        )


def broadcast_trip(
    session: Session,
    trip: Trip,
    *,
    provider: NotificationProvider | None = None,
    fanout: int | None = None,
    radius_mi: float | None = None,
) -> list[TripBroadcast]:
    """Broadcast `trip` to up to `fanout` nearest available drivers within `radius_mi`.

    Side-effects: creates TripBroadcast rows, sends SMS to each, sets trip.status.
    Returns the list of broadcasts created. Raises TripNotBroadcastable (a
    ValueError) when the trip is past the offer stage — nothing is sent.
    """
    _ensure_broadcastable(trip)
    provider = provider or get_provider()
    fanout = fanout if fanout is not None else int(os.getenv("BROADCAST_FANOUT", "3"))
    radius_mi = radius_mi if radius_mi is not None else float(os.getenv("BROADCAST_RADIUS_MI", "50"))

    candidates = candidate_drivers(session, trip, radius_mi)[:fanout]

    if not candidates:
        # E8 (v1.1.18): only NO_DRIVERS when nothing is still on offer —
        # NO_DRIVERS blocks accept_trip*, which would strand open offers.
        if not has_pending_offers(trip):
            trip.status = TripStatus.NO_DRIVERS
        session.commit()
        return []

    # Web Push integration (v1.1.0, flag-gated). When PUSH_NOTIFICATIONS_ENABLED
    # is on, drivers with an active push subscription receive push instead of
    # SMS; drivers without subscriptions get SMS as before. Conditional per-
    # driver — no double-paging. Push provider expired-subscription cleanup
    # happens inline: if the push service returns 404/410 (subscription gone)
    # we delete the stale row in the same transaction and SMS as fallback.
    push = get_push_provider() if push_notifications_enabled() else None

    broadcasts: list[TripBroadcast] = []
    for driver, distance in candidates:
        bc = TripBroadcast(trip_id=trip.id, driver_id=driver.id, distance_mi=distance)
        session.add(bc)
        broadcasts.append(bc)

        time_str = trip.pickup_time.strftime("%Y-%m-%d %H:%M") if trip.pickup_time else "(time TBD)"
        sms_body = (
            f"Trip #{trip.id} — {trip.vehicle_class.value.upper()} — "
            f"{trip.pickup_label} → {trip.dropoff_label} at "
            f"{time_str}. "
            f"Reply ACCEPT {trip.id} or DECLINE {trip.id}. ({distance:.1f} mi from you)"
        )

        pushed = False
        if push is not None:
            from models import DriverPushSubscription  # local import — keeps core.py minimal
            subs = list(session.scalars(
                select(DriverPushSubscription).where(
                    DriverPushSubscription.driver_id == driver.id
                )
            ).all())
            for sub in subs:
                result = push.send_push(
                    endpoint=sub.endpoint,
                    p256dh=sub.p256dh,
                    auth=sub.auth,
                    payload={
                        "trip_id": trip.id,
                        "vehicle_class": trip.vehicle_class.value,
                        "pickup_label": trip.pickup_label,
                        "dropoff_label": trip.dropoff_label,
                        "pickup_time": time_str,
                        "distance_mi": round(distance, 1),
                        "title": f"Trip #{trip.id} — {trip.vehicle_class.value.upper()}",
                        "body": (
                            f"{trip.pickup_label} → {trip.dropoff_label} "
                            f"({distance:.1f} mi). Tap to review."
                        ),
                    },
                )
                if result.delivered:
                    sub.last_success_at = datetime.now(timezone.utc)
                    pushed = True
                elif result.expired:
                    # Subscription gone — clean up the stale row so future
                    # broadcasts don't waste time on it.
                    session.delete(sub)

        # If push delivered to at least one device for this driver, skip
        # SMS — push is the primary channel. Otherwise fall back to SMS so
        # the driver always sees the broadcast on at least one channel.
        if not pushed:
            provider.send_sms(driver.phone, sms_body)

    trip.status = TripStatus.BROADCASTING
    session.commit()
    for bc in broadcasts:
        session.refresh(bc)
    return broadcasts


def _raise_if_unacceptable(session: Session, trip_id: int, rowcount: int) -> None:
    """Roll back and raise a status-aware error when the atomic claim lost
    (rowcount==0). Shared by accept_trip and accept_trip_subcontractor so the
    two first-write-wins paths can't drift apart."""
    if rowcount == 0:
        session.rollback()
        trip_now = session.get(Trip, trip_id)
        if trip_now is None:
            raise ValueError(f"Trip {trip_id} not found")
        raise ValueError(f"Trip {trip_id} is in status {trip_now.status.value}; cannot accept")


def _expire_sibling_broadcasts(trip: Trip, winning_bc_id: int, now: datetime) -> None:
    """Mark every still-PENDING sibling broadcast on `trip` as EXPIRED, stamped
    with the winning broadcast's timestamp."""
    for sibling in trip.broadcasts:
        if sibling.id != winning_bc_id and sibling.response == BroadcastResponse.PENDING:
            sibling.response = BroadcastResponse.EXPIRED
            sibling.responded_at = now


def accept_trip(session: Session, trip_id: int, driver_id: int) -> Trip:
    """Driver accepts a trip. First-write-wins enforced atomically.

    The atomic claim is a single UPDATE with a WHERE clause that requires the
    trip to still be in REQUESTED or BROADCASTING with no assigned driver and no
    assigned subcontractor. The DB serializes concurrent UPDATEs; whichever
    transaction commits first wins, and the loser sees rowcount=0 and raises.
    This prevents the lost-update TOCTOU race that a Python-side check-then-write
    pattern would have, and it also prevents a driver from accepting a trip that
    has already been handed to a subcontractor.
    """
    bc = session.scalars(
        select(TripBroadcast).where(
            TripBroadcast.trip_id == trip_id,
            TripBroadcast.driver_id == driver_id,
        )
    ).first()
    if bc is None:
        raise ValueError(f"No broadcast to driver {driver_id} for trip {trip_id}")
    if bc.response != BroadcastResponse.PENDING:
        raise ValueError(f"Broadcast already {bc.response.value}")

    driver = session.get(Driver, driver_id)
    if driver is None:
        raise ValueError(f"Driver {driver_id} not found")
    if driver.status != DriverStatus.AVAILABLE:
        raise ValueError(f"Driver {driver_id} is not available")

    # Atomic claim. Only the first concurrent caller sees rowcount==1; later
    # callers find the trip already ACCEPTED (by a driver or by a sub) and get
    # rowcount==0.
    result = session.execute(
        update(Trip)
        .where(
            Trip.id == trip_id,
            Trip.status.in_([TripStatus.REQUESTED, TripStatus.BROADCASTING]),
            Trip.assigned_driver_id.is_(None),
            Trip.assigned_subcontractor_id.is_(None),
        )
        .values(status=TripStatus.ACCEPTED, assigned_driver_id=driver_id)
    )
    _raise_if_unacceptable(session, trip_id, result.rowcount)

    trip = session.get(Trip, trip_id)
    now = datetime.now(timezone.utc)
    bc.response = BroadcastResponse.ACCEPTED
    bc.responded_at = now
    _expire_sibling_broadcasts(trip, bc.id, now)
    driver.status = DriverStatus.ON_TRIP
    session.commit()
    session.refresh(trip)
    return trip


def decline_trip(session: Session, trip_id: int, driver_id: int) -> TripBroadcast:
    """Driver declines a broadcast; trip stays in BROADCASTING for the others."""
    bc = session.scalars(
        select(TripBroadcast).where(
            TripBroadcast.trip_id == trip_id,
            TripBroadcast.driver_id == driver_id,
        )
    ).first()
    if bc is None:
        raise ValueError(f"No broadcast to driver {driver_id} for trip {trip_id}")
    if bc.response != BroadcastResponse.PENDING:
        raise ValueError(f"Broadcast already {bc.response.value}")

    bc.response = BroadcastResponse.DECLINED
    bc.responded_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(bc)
    return bc


_DRIVER_ACTIVE_TRIP_STATUSES = frozenset({
    TripStatus.ACCEPTED, TripStatus.EN_ROUTE_PICKUP, TripStatus.ON_SCENE, TripStatus.IN_TRANSIT,
})


def _release_driver_if_idle(session: Session, trip: Trip) -> None:
    """Return the trip's driver to AVAILABLE when the trip ends (E7).

    Only flips a driver who is ON_TRIP: one who went OFF_DUTY or OFFLINE stays
    that way, so no one is paged who signed off. And only when no *other*
    trip still holds them, which guards against a dispatcher-assigned second
    trip."""
    if trip.assigned_driver_id is None:
        return
    driver = session.get(Driver, trip.assigned_driver_id)
    if driver is None or driver.status != DriverStatus.ON_TRIP:
        return
    still_busy = session.scalars(
        select(Trip.id).where(
            Trip.assigned_driver_id == driver.id,
            Trip.id != trip.id,
            Trip.status.in_(_DRIVER_ACTIVE_TRIP_STATUSES),
        )
    ).first()
    if still_busy is None:
        driver.status = DriverStatus.AVAILABLE


VALID_TRANSITIONS: dict[TripStatus, frozenset[TripStatus]] = {
    TripStatus.REQUESTED: frozenset({TripStatus.BROADCASTING, TripStatus.ACCEPTED, TripStatus.CANCELLED, TripStatus.NO_DRIVERS}),
    TripStatus.BROADCASTING: frozenset({TripStatus.ACCEPTED, TripStatus.NO_DRIVERS, TripStatus.CANCELLED}),
    TripStatus.NO_DRIVERS: frozenset({TripStatus.BROADCASTING, TripStatus.CANCELLED}),
    TripStatus.ACCEPTED: frozenset({TripStatus.EN_ROUTE_PICKUP, TripStatus.CANCELLED}),
    TripStatus.EN_ROUTE_PICKUP: frozenset({TripStatus.ON_SCENE, TripStatus.CANCELLED}),
    TripStatus.ON_SCENE: frozenset({TripStatus.IN_TRANSIT, TripStatus.CANCELLED}),
    TripStatus.IN_TRANSIT: frozenset({TripStatus.COMPLETED, TripStatus.CANCELLED}),
    TripStatus.COMPLETED: frozenset(),
    TripStatus.CANCELLED: frozenset(),
}


def update_trip_status(
    session: Session,
    trip_id: int,
    new_status: TripStatus,
    *,
    fare_cents: int | None = None,
    force: bool = False,
) -> Trip:
    """Lifecycle progression: ACCEPTED → EN_ROUTE_PICKUP → ON_SCENE → IN_TRANSIT → COMPLETED.

    On COMPLETED **or CANCELLED** (E7, v1.1.18), returns the assigned
    own-fleet driver to AVAILABLE — but only if they are still ON_TRIP and
    have no other active trip (see `_release_driver_if_idle`). Before v1.1.18
    a cancellation left the driver ON_TRIP, silently dropping them out of
    every future broadcast. Subcontractor-handled trips have no internal
    driver state to flip — the sub's own dispatch handles their roster.

    Pass ``force=True`` to override the transition guard (dispatcher override).
    """
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise ValueError(f"Trip {trip_id} not found")
    if not force and new_status not in VALID_TRANSITIONS.get(trip.status, frozenset()):
        raise ValueError(
            f"Invalid transition: {trip.status.value} → {new_status.value}"
        )
    was_completed = trip.status == TripStatus.COMPLETED
    trip.status = new_status
    if fare_cents is not None:
        trip.fare_cents = fare_cents
    # v1.1.20: the settlement date. Stamped on entering COMPLETED; cleared if
    # a force=True override ever moves a trip back out of COMPLETED.
    if new_status == TripStatus.COMPLETED and not was_completed:
        trip.completed_at = datetime.now(timezone.utc)
    elif was_completed and new_status != TripStatus.COMPLETED:
        trip.completed_at = None
    if new_status in (TripStatus.COMPLETED, TripStatus.CANCELLED):
        _release_driver_if_idle(session, trip)
    session.commit()
    session.refresh(trip)
    return trip


# ---------------------------------------------------------------------------
# Subcontractor (overflow) dispatch
# ---------------------------------------------------------------------------


def candidate_subcontractors(
    session: Session,
    trip: Trip,
    radius_mi: float,
    *,
    tiers: list[SubcontractorTier] | None = None,
) -> list[tuple[Subcontractor, float]]:
    """Return (sub, distance_mi) sorted by tier ascending then distance ascending.

    Filters by active=True, vehicle class match, and (if the sub has a base
    location) the trip pickup falling inside max(sub.service_radius_mi, radius_mi).
    Subs without a base location are included with distance=inf — used as a
    last-resort blast.
    """
    stmt = select(Subcontractor).where(Subcontractor.active.is_(True))
    if tiers is not None:
        stmt = stmt.where(Subcontractor.tier.in_(tiers))
    subs = session.scalars(stmt).all()

    out: list[tuple[Subcontractor, float]] = []
    for sub in subs:
        if not sub.serves(trip.vehicle_class):
            continue

        if sub.base_lat is None or sub.base_lon is None:
            # No base location → unknown distance; include as last-resort.
            out.append((sub, float("inf")))
            continue

        dist = haversine_miles(sub.base_lat, sub.base_lon, trip.pickup_lat, trip.pickup_lon)
        # Sub-specific service radius takes precedence over the broadcast default.
        effective_radius = sub.service_radius_mi if sub.service_radius_mi is not None else radius_mi
        if dist <= effective_radius:
            out.append((sub, dist))

    # Tier first (1 < 2 < 3), then nearer first.
    out.sort(key=lambda pair: (pair[0].tier.value, pair[1]))
    return out


def broadcast_to_subcontractors(
    session: Session,
    trip: Trip,
    *,
    provider: NotificationProvider | None = None,
    fanout: int | None = None,
    radius_mi: float | None = None,
    tiers: list[SubcontractorTier] | None = None,
) -> list[TripBroadcast]:
    """Hand off `trip` to up to `fanout` subcontractors matched by class, tier, radius.

    This is an explicit dispatcher action — typically taken after own-fleet
    broadcast yields no acceptance, or when the dispatcher knows in advance
    that own-fleet capacity won't cover (RSM Sunday departure window, large
    same-week wedding cluster, etc.).

    Side-effects: creates TripBroadcast rows for each sub, sends SMS to each
    sub's dispatch phone, sets trip.status back to BROADCASTING (overrides
    NO_DRIVERS if previously set — overflow is a re-broadcast). Raises
    TripNotBroadcastable when the trip is past the offer stage.
    """
    _ensure_broadcastable(trip)
    provider = provider or get_provider()
    fanout = fanout if fanout is not None else int(os.getenv("OVERFLOW_FANOUT", "5"))
    radius_mi = radius_mi if radius_mi is not None else float(os.getenv("BROADCAST_RADIUS_MI", "50"))

    candidates = candidate_subcontractors(session, trip, radius_mi, tiers=tiers)[:fanout]

    if not candidates:
        # E8 (v1.1.18): an overflow that finds no sub used to flip the trip
        # to NO_DRIVERS even while own-fleet offers were still open, which
        # made every pending driver's ACCEPT fail. Leave it BROADCASTING then.
        if not has_pending_offers(trip):
            trip.status = TripStatus.NO_DRIVERS
        session.commit()
        return []

    broadcasts: list[TripBroadcast] = []
    for sub, distance in candidates:
        bc = TripBroadcast(
            trip_id=trip.id,
            subcontractor_id=sub.id,
            distance_mi=distance if distance != float("inf") else 0.0,
        )
        session.add(bc)
        broadcasts.append(bc)

        distance_phrase = (
            f" ({distance:.1f} mi from your base)" if distance != float("inf") else ""
        )
        time_str = trip.pickup_time.strftime("%Y-%m-%d %H:%M") if trip.pickup_time else "(time TBD)"
        body = (
            f"OVERFLOW Trip #{trip.id} — {trip.vehicle_class.value.upper()} — "
            f"{trip.pickup_label} → {trip.dropoff_label} at "
            f"{time_str}. "
            f"Cost-plus {sub.markup_pct:.0f}%. "
            f"Reply ACCEPT {trip.id} <your cost $> (e.g. ACCEPT {trip.id} 450) "
            f"or DECLINE {trip.id}.{distance_phrase}"
        )
        provider.send_sms(sub.dispatch_phone, body)

    trip.status = TripStatus.BROADCASTING
    session.commit()
    for bc in broadcasts:
        session.refresh(bc)
    return broadcasts


def accept_trip_subcontractor(
    session: Session,
    trip_id: int,
    subcontractor_id: int,
    *,
    quoted_cost_cents: int | None = None,
) -> Trip:
    """Subcontractor accepts an overflow trip. Atomic compare-and-set, same
    pattern as `accept_trip` for own-fleet drivers.

    If `quoted_cost_cents` is provided, the trip records both the quoted cost
    (as fare_cents) and the markup (markup_cents = quoted_cost * markup_pct).
    The fare-on-trip reflects what we charge the customer (cost + markup);
    accounting can derive the sub's payout from the difference.
    """
    bc = session.scalars(
        select(TripBroadcast).where(
            TripBroadcast.trip_id == trip_id,
            TripBroadcast.subcontractor_id == subcontractor_id,
        )
    ).first()
    if bc is None:
        raise ValueError(f"No broadcast to subcontractor {subcontractor_id} for trip {trip_id}")
    if bc.response != BroadcastResponse.PENDING:
        raise ValueError(f"Broadcast already {bc.response.value}")

    sub = session.get(Subcontractor, subcontractor_id)
    if sub is None:
        raise ValueError(f"Subcontractor {subcontractor_id} not found")
    if not sub.active:
        raise ValueError(f"Subcontractor {subcontractor_id} is inactive")

    values: dict = {
        "status": TripStatus.ACCEPTED,
        "assigned_subcontractor_id": subcontractor_id,
    }
    if quoted_cost_cents is not None:
        markup_cents = int(round(quoted_cost_cents * sub.markup_pct / 100.0))
        values["fare_cents"] = quoted_cost_cents + markup_cents
        values["markup_cents"] = markup_cents

    result = session.execute(
        update(Trip)
        .where(
            Trip.id == trip_id,
            Trip.status.in_([TripStatus.REQUESTED, TripStatus.BROADCASTING]),
            Trip.assigned_driver_id.is_(None),
            Trip.assigned_subcontractor_id.is_(None),
        )
        .values(**values)
    )
    _raise_if_unacceptable(session, trip_id, result.rowcount)

    trip = session.get(Trip, trip_id)
    now = datetime.now(timezone.utc)
    bc.response = BroadcastResponse.ACCEPTED
    bc.responded_at = now
    _expire_sibling_broadcasts(trip, bc.id, now)
    session.commit()
    session.refresh(trip)
    return trip


def record_subcontractor_cost(session: Session, trip_id: int, quoted_cost_cents: int) -> Trip:
    """Record an overflow sub's quoted cost after the fact (E9, v1.1.18).

    A sub that accepts by SMS with a bare `ACCEPT N` leaves the trip ACCEPTED
    with no fare. A later `sub-accept` can't fix that because the broadcast
    is no longer PENDING. This sets the cost-plus fare the same way
    `accept_trip_subcontractor` does: fare = cost + round(cost * markup_pct),
    using the assigned sub's markup. Re-recording is allowed so a typo can be
    corrected. It refuses trips with no assigned sub, cancelled trips, and
    non-positive costs. Since v1.1.20 this no longer moves a completed trip in
    sub settlement, because settlement reads ``completed_at``, not
    ``updated_at``.
    """
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise ValueError(f"Trip {trip_id} not found")
    if trip.assigned_subcontractor_id is None:
        raise ValueError(f"Trip {trip_id} is not assigned to a subcontractor")
    if trip.status == TripStatus.CANCELLED:
        raise ValueError(f"Trip {trip_id} is cancelled")
    if quoted_cost_cents <= 0:
        raise ValueError("Quoted cost must be positive")
    sub = session.get(Subcontractor, trip.assigned_subcontractor_id)
    if sub is None:
        raise ValueError(f"Subcontractor {trip.assigned_subcontractor_id} not found")
    markup_cents = int(round(quoted_cost_cents * sub.markup_pct / 100.0))
    trip.fare_cents = quoted_cost_cents + markup_cents
    trip.markup_cents = markup_cents
    session.commit()
    session.refresh(trip)
    return trip

def decline_trip_subcontractor(
    session: Session,
    trip_id: int,
    subcontractor_id: int,
) -> TripBroadcast:
    """Subcontractor declines an overflow broadcast; trip stays open for the others."""
    bc = session.scalars(
        select(TripBroadcast).where(
            TripBroadcast.trip_id == trip_id,
            TripBroadcast.subcontractor_id == subcontractor_id,
        )
    ).first()
    if bc is None:
        raise ValueError(f"No broadcast to subcontractor {subcontractor_id} for trip {trip_id}")
    if bc.response != BroadcastResponse.PENDING:
        raise ValueError(f"Broadcast already {bc.response.value}")

    bc.response = BroadcastResponse.DECLINED
    bc.responded_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(bc)
    return bc


# ---------------------------------------------------------------------------
# Call-in automation helpers
# ---------------------------------------------------------------------------

ACTIVE_TRIP_STATES = (
    TripStatus.ACCEPTED,
    TripStatus.EN_ROUTE_PICKUP,
    TripStatus.ON_SCENE,
    TripStatus.IN_TRANSIT,
)

_PASSENGER_STATUS = {
    TripStatus.ACCEPTED: "has been assigned and is preparing for your ride",
    TripStatus.EN_ROUTE_PICKUP: "is on the way to pick you up",
    TripStatus.ON_SCENE: "has arrived at the pickup location",
    TripStatus.IN_TRANSIT: "is currently driving you",
}

_DISPATCHER_STATUS = {
    TripStatus.ACCEPTED: "accepted, preparing",
    TripStatus.EN_ROUTE_PICKUP: "driver en route to pickup",
    TripStatus.ON_SCENE: "driver on scene",
    TripStatus.IN_TRANSIT: "passenger in transit",
}


def trip_status_description(status: TripStatus, *, audience: str = "passenger") -> str:
    table = _DISPATCHER_STATUS if audience == "dispatcher" else _PASSENGER_STATUS
    return table.get(status, status.value.replace("_", " "))


@dataclass
class CallerIdentity:
    kind: str          # "driver" | "subcontractor" | "customer" | "unknown"
    name: str | None
    entity_id: int | None
    active_trip: Trip | None
    driver_for_trip: Driver | None


def identify_caller(session: Session, phone: str) -> CallerIdentity:
    if not phone or not phone.startswith("+"):
        return CallerIdentity("unknown", None, None, None, None)

    driver = session.scalars(
        select(Driver).where(Driver.phone == phone)
    ).first()
    if driver is not None:
        active = session.scalars(
            select(Trip).where(
                Trip.assigned_driver_id == driver.id,
                Trip.status.in_(ACTIVE_TRIP_STATES),
            )
        ).first()
        return CallerIdentity("driver", driver.name, driver.id, active, driver if active else None)

    sub = session.scalars(
        select(Subcontractor).where(Subcontractor.dispatch_phone == phone)
    ).first()
    if sub is not None:
        return CallerIdentity("subcontractor", sub.name, sub.id, None, None)

    trip = session.scalars(
        select(Trip).where(
            Trip.passenger_phone == phone,
            Trip.status.in_(ACTIVE_TRIP_STATES),
        ).order_by(Trip.updated_at.desc())
    ).first()
    if trip is not None:
        assigned_driver = None
        if trip.assigned_driver_id is not None:
            assigned_driver = session.get(Driver, trip.assigned_driver_id)
        return CallerIdentity("customer", trip.passenger_name, None, trip, assigned_driver)

    return CallerIdentity("unknown", None, None, None, None)


# --- Quiet-hours detection ---

def is_quiet_hours(now_utc: datetime | None = None) -> bool:
    tz_name = os.getenv("QUIET_HOURS_TIMEZONE", "America/New_York")
    start_str = os.getenv("QUIET_HOURS_START", "02:00")
    end_str = os.getenv("QUIET_HOURS_END", "08:00")

    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name))

    start_h, start_m = map(int, start_str.split(":"))
    end_h, end_m = map(int, end_str.split(":"))
    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m
    cur_min = now_local.hour * 60 + now_local.minute

    # start == end means an empty (zero-length) quiet window, not 24h. Operators
    # who want no quiet hours should set QUIET_HOURS_ENABLED=false; this guard
    # makes equal start/end behave sanely (no quiet window) either way.
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


# --- Voicemail transcript parsing (regex heuristic, no LLM) ---

_VM_NAME_RE = re.compile(
    r"(?:my name is|this is|i'm|i am)\s+"
    r"([a-z]{2,}(?:\s+[a-z]{2,}){0,2})",
    re.IGNORECASE,
)

_COMMON_WORDS = frozenset({
    "i", "a", "an", "the", "and", "or", "but", "for", "at", "from", "to",
    "in", "on", "we", "my", "me", "he", "she", "it", "is", "am", "was",
    "need", "want", "would", "like", "looking", "calling", "please", "can",
    "could", "will", "just", "with", "about", "have", "had", "do", "did",
})

_VM_PICKUP_RE = re.compile(
    r"(?:pick\s*up\s+(?:at|from)\s+|from\s+|coming\s+from\s+)"
    r"(.+?)(?:\s+(?:to|going|drop|heading)|[.,;!?]|$)",
    re.IGNORECASE,
)

_VM_DROPOFF_RE = re.compile(
    r"(?:going\s+to\s+|drop\s*off\s+(?:at\s+)?|heading\s+to\s+|"
    r"destination\s+(?:is\s+)?)"
    r"(.+?)(?:\s+(?:at\s+\d|around\s+\d|for\s+\d|tomorrow|tonight)|[.,;!?]|$)",
    re.IGNORECASE,
)

_VM_TIME_RE = re.compile(
    r"(?:at|around|for)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.?m\.?|p\.?m\.?)?)\b",
    re.IGNORECASE,
)

_VM_DATE_RE = re.compile(
    r"\b(today|tonight|this evening|tomorrow(?:\s+(?:morning|evening|night))?)\b",
    re.IGNORECASE,
)


# Per-field output length caps for parsed voicemail (v1.0.5). All below the
# Reservation DB column limits so anything that slips through can still
# insert cleanly. Voicemail transcripts are untrusted attacker-controlled
# input — a caller can speak anything Twilio's STT will transcribe.
_VM_MAX_INPUT_CHARS = 5000          # Twilio caps voicemails at ~5 min ≈ < 2000 chars
_VM_MAX_NAME_CHARS = 80             # vs. Reservation.name = String(200)
_VM_MAX_LABEL_CHARS = 200           # vs. Reservation.{pickup,dropoff}_label = String(500)
_VM_MAX_DATE_CHARS = 20             # matches Reservation.pickup_date column
_VM_MAX_TIME_CHARS = 10             # matches Reservation.pickup_time_str column


def _vm_sanitize(value: str, max_len: int) -> str | None:
    """Strip control characters (anything below ASCII space except actual
    space), cap length, and reject when nothing meaningful survives. Returns
    None when the value is empty/control-only after cleaning so the caller
    can fall back to its default placeholder."""
    if not value:
        return None
    cleaned = "".join(ch for ch in value if ch == " " or ch >= " ")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned or None


def parse_voicemail_transcript(text: str) -> dict:
    result: dict = {
        "name": None,
        "pickup_label": None,
        "dropoff_label": None,
        "pickup_date": None,
        "pickup_time_str": None,
    }
    if not text or not text.strip():
        return result
    # Input cap — defense in depth. Twilio already truncates but a future
    # transcription provider might not.
    if len(text) > _VM_MAX_INPUT_CHARS:
        text = text[:_VM_MAX_INPUT_CHARS]

    m = _VM_NAME_RE.search(text)
    if m:
        words = m.group(1).strip().split()
        while words and words[-1].lower() in _COMMON_WORDS:
            words.pop()
        if words:
            name = " ".join(words)
            if name.lower() not in ("calling", "looking", "trying", "wondering", "hoping"):
                result["name"] = _vm_sanitize(name.title(), _VM_MAX_NAME_CHARS)

    m = _VM_PICKUP_RE.search(text)
    if m:
        label = m.group(1).strip()
        if len(label) >= 3:
            result["pickup_label"] = _vm_sanitize(label, _VM_MAX_LABEL_CHARS)

    m = _VM_DROPOFF_RE.search(text)
    if m:
        label = m.group(1).strip()
        if len(label) >= 3:
            result["dropoff_label"] = _vm_sanitize(label, _VM_MAX_LABEL_CHARS)

    m = _VM_DATE_RE.search(text)
    if m:
        result["pickup_date"] = _vm_sanitize(m.group(1).strip().lower(), _VM_MAX_DATE_CHARS)

    m = _VM_TIME_RE.search(text)
    if m:
        result["pickup_time_str"] = _vm_sanitize(m.group(1).strip(), _VM_MAX_TIME_CHARS)

    return result
