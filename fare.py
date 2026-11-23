"""Fare calculator — turns a trip + rate card into a quoted fare.

Pure module. No DB, no HTTP. Given a Trip and a RateCard (or just the raw
inputs), returns a structured fare breakdown so the dispatcher can see exactly
where every cent came from.

Usage from the admin trip detail page:

    breakdown = quote_trip(trip, rate_card)
    breakdown.total_cents  # int — the suggested fare
    breakdown.lines        # list[(label, cents)] — every line item

The breakdown serializes cleanly to JSON for an API response, and renders as a
small table in the trip detail page.

Distance prefers OSRM road-network distance (via `_compute_distance_miles`,
the same engine the marketing site and live map use), falling back to haversine
(great-circle) on any OSRM failure or when `FARE_DISTANCE_PROVIDER=haversine`.
For the ~100-mile corridor the haversine fallback errs <10% on the longest legs
and effectively zero on short ones, so the quote stays a sound signal even when
OSRM is unreachable.

Surcharges:
    - airport_meet_surcharge: applied if `is_airport_meet=True` (caller's call;
      the simplest signal is checking if the pickup label / notes mention
      meet-and-greet or terminal-side service)
    - after_hours_surcharge: applied if pickup is between 22:00 and 06:00 local
    - peak_week_multiplier: applied to the subtotal (base + miles + hours)
      before surcharges. Caller decides when peak applies — `is_peak_week=True`
"""

from __future__ import annotations

import json as _json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from urllib import error as _urlerror
from urllib import request as _urlrequest

_log = logging.getLogger(__name__)

# Public OSRM demo server. Light-use limits apply; production should self-host
# OSRM or use a paid routing service. Override via OSRM_BASE_URL.
_OSRM_BASE = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
_OSRM_TIMEOUT_S = float(os.getenv("OSRM_TIMEOUT_S", "5.0"))
_M_PER_MI = 1609.344


@dataclass
class FareBreakdown:
    """Structured fare quote. All money in cents."""

    total_cents: int
    lines: list[tuple[str, int]] = field(default_factory=list)
    distance_miles: float = 0.0
    duration_hours: float = 0.0
    rate_card_id: int | None = None
    rate_card_name: str | None = None

    @property
    def total_dollars(self) -> float:
        return self.total_cents / 100.0

    def to_dict(self) -> dict:
        return {
            "total_cents": self.total_cents,
            "total_dollars": self.total_dollars,
            "distance_miles": round(self.distance_miles, 2),
            "duration_hours": round(self.duration_hours, 2),
            "rate_card_id": self.rate_card_id,
            "rate_card_name": self.rate_card_name,
            "lines": [{"label": l, "cents": c} for l, c in self.lines],
        }

    def as_text(self) -> str:
        """Compact one-line summary. Useful for flash messages."""
        parts = [f"${c / 100:.2f} {l}" for l, c in self.lines if c != 0]
        return " + ".join(parts) + f" = ${self.total_cents / 100:.2f}"


# ---------------------------------------------------------------------------
# Distance + duration heuristics
# ---------------------------------------------------------------------------


from core import haversine_miles  # canonical definition lives in core.py


def osrm_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float | None:
    """Road-network distance via the OSRM public routing API.

    Returns miles on success; returns `None` on any failure (timeout, HTTP
    error, parse error, empty route) so callers can fall back to haversine.
    Logs at debug level — failures are expected (rate-limit, network blip)
    and shouldn't pollute the dispatcher's stdout.

    OSRM expects `lon,lat` order in the URL — easy to get wrong, easy to
    test against (the JAX→Sea Island road distance is ~75 mi vs haversine's
    ~67 mi, so a swapped pair would produce a wildly different number).
    """
    try:
        url = (
            f"{_OSRM_BASE}/route/v1/driving/"
            f"{lon1},{lat1};{lon2},{lat2}?overview=false"
        )
        with _urlrequest.urlopen(url, timeout=_OSRM_TIMEOUT_S) as resp:  # nosec B310 — URL prefix is a fixed http(s) OSRM endpoint, never user-controlled
            if resp.status != 200:
                _log.debug("OSRM non-200: %s", resp.status)
                return None
            body = resp.read()
        data = _json.loads(body)
        routes = data.get("routes") or []
        if not routes:
            _log.debug("OSRM returned no routes")
            return None
        meters = float(routes[0].get("distance", 0.0))
        if meters <= 0:
            return None
        return meters / _M_PER_MI
    except (_urlerror.URLError, TimeoutError, ValueError, KeyError) as exc:
        _log.debug("OSRM lookup failed: %s", exc)
        return None


def _compute_distance_miles(pickup_lat, pickup_lon, dropoff_lat, dropoff_lon) -> float:
    """Distance for fare purposes. Prefers OSRM road-network distance; falls
    back to haversine on any OSRM failure (timeout, rate-limit, parse error).

    Behavior is env-tunable:

    - `FARE_DISTANCE_PROVIDER=osrm` (default in prod) — try OSRM first.
    - `FARE_DISTANCE_PROVIDER=haversine` — skip OSRM entirely. Used by tests
      so the suite doesn't depend on internet availability.
    """
    provider = os.getenv("FARE_DISTANCE_PROVIDER", "osrm").lower()
    if provider == "osrm":
        osrm_miles = osrm_distance_miles(pickup_lat, pickup_lon, dropoff_lat, dropoff_lon)
        if osrm_miles is not None:
            return osrm_miles
    return haversine_miles(pickup_lat, pickup_lon, dropoff_lat, dropoff_lon)


def _estimate_duration_hours(distance_miles: float) -> float:
    """Rough conversion from miles to driving hours for the corridor.
    Mixed-speed (highway + local) ≈ 45 mph average. Padded slightly for
    pickup hand-off + bag loading."""
    if distance_miles <= 0:
        return 0.25  # quarter-hour minimum on any move
    return (distance_miles / 45.0) + 0.25


# ---------------------------------------------------------------------------
# Fare quote
# ---------------------------------------------------------------------------


def quote_trip(
    *,
    pickup_lat: float,
    pickup_lon: float,
    dropoff_lat: float,
    dropoff_lon: float,
    pickup_time: datetime | None = None,
    base_fare_cents: int = 0,
    per_mile_cents: int = 0,
    per_hour_cents: int = 0,
    minimum_fare_cents: int = 0,
    airport_meet_surcharge_cents: int = 0,
    after_hours_surcharge_cents: int = 0,
    peak_week_multiplier: float = 1.0,
    is_airport_meet: bool = False,
    is_peak_week: bool = False,
    rate_card_id: int | None = None,
    rate_card_name: str | None = None,
) -> FareBreakdown:
    """Compute a fare quote from rate-card inputs.

    All money args are in cents; multiplier is a float >= 1.0.
    The signature avoids tight coupling to the RateCard ORM model so this
    function stays pure and trivially testable.
    """
    distance = _compute_distance_miles(pickup_lat, pickup_lon, dropoff_lat, dropoff_lon)
    duration = _estimate_duration_hours(distance)

    lines: list[tuple[str, int]] = []
    if base_fare_cents:
        lines.append(("Base fare", base_fare_cents))
    miles_cents = int(round(distance * per_mile_cents))
    if miles_cents:
        lines.append((f"Distance ({distance:.1f} mi)", miles_cents))
    hours_cents = int(round(duration * per_hour_cents))
    if hours_cents:
        lines.append((f"Time ({duration:.1f} hr)", hours_cents))

    subtotal = sum(c for _, c in lines)

    # Peak multiplier applies to the (base + miles + hours) subtotal.
    if peak_week_multiplier > 1.0 and is_peak_week:
        peak_addon = int(round(subtotal * (peak_week_multiplier - 1.0)))
        if peak_addon:
            lines.append((f"Peak week (×{peak_week_multiplier:.2f})", peak_addon))
            subtotal += peak_addon

    # Surcharges stack on top.
    if airport_meet_surcharge_cents and is_airport_meet:
        lines.append(("Airport meet-and-greet", airport_meet_surcharge_cents))
        subtotal += airport_meet_surcharge_cents

    after_hours = False
    if pickup_time is not None:
        hour = pickup_time.hour
        after_hours = hour >= 22 or hour < 6
    if after_hours_surcharge_cents and after_hours:
        lines.append(("After-hours surcharge", after_hours_surcharge_cents))
        subtotal += after_hours_surcharge_cents

    # Apply minimum.
    total = max(subtotal, minimum_fare_cents)
    if minimum_fare_cents and total > subtotal:
        bump = minimum_fare_cents - subtotal
        lines.append(("Minimum fare bump", bump))

    return FareBreakdown(
        total_cents=total,
        lines=lines,
        distance_miles=distance,
        duration_hours=duration,
        rate_card_id=rate_card_id,
        rate_card_name=rate_card_name,
    )


def quote_for_trip_and_card(trip, rate_card, *, is_peak_week: bool = False) -> FareBreakdown:
    """Convenience wrapper that pulls fields off ORM objects.

    `trip` is a Trip row; `rate_card` is a RateCard row. `is_peak_week` is the
    only flag the caller has to decide — it's a calendar decision and lives in
    operational notes (event calendar), not on the trip row.
    """
    # Heuristic: treat as airport meet-and-greet if pickup_label or notes
    # mention an airport, terminal, "meet", or "greet". The dispatcher can
    # always override the suggested fare directly.
    label_blob = " ".join(filter(None, [
        getattr(trip, "pickup_label", "") or "",
        getattr(trip, "notes", "") or "",
    ])).lower()
    airport_signals = ("airport", "terminal", "meet-and-greet", "meet and greet",
                       "jax", "sav", "bqk", "ksav", "kjax")
    is_airport_meet = any(s in label_blob for s in airport_signals)

    return quote_trip(
        pickup_lat=trip.pickup_lat,
        pickup_lon=trip.pickup_lon,
        dropoff_lat=trip.dropoff_lat,
        dropoff_lon=trip.dropoff_lon,
        pickup_time=trip.pickup_time,
        base_fare_cents=rate_card.base_fare_cents,
        per_mile_cents=rate_card.per_mile_cents,
        per_hour_cents=rate_card.per_hour_cents,
        minimum_fare_cents=rate_card.minimum_fare_cents,
        airport_meet_surcharge_cents=rate_card.airport_meet_surcharge_cents,
        after_hours_surcharge_cents=rate_card.after_hours_surcharge_cents,
        peak_week_multiplier=rate_card.peak_week_multiplier,
        is_airport_meet=is_airport_meet,
        is_peak_week=is_peak_week,
        rate_card_id=rate_card.id,
        rate_card_name=rate_card.name,
    )
