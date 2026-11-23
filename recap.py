"""Daily-recap aggregation logic.

Pure data helpers — no typer / no CLI surface — so the recap is unit-testable
without dragging in the CLI dependency stack. The CLI command in cli.py calls
build_recap() and pretty-prints the result; tests call build_recap() directly
and assert on the dict shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Inquiry, Reservation, ReservationStatus, Trip, TripStatus


def build_recap(session: Session, when: datetime) -> dict:
    """Aggregate trip + reservation + inquiry metrics for the calendar day
    containing `when` (UTC).

    Day boundaries are computed in UTC against created_at. Returns a dict
    shaped for human-readable rendering by the CLI; keys are stable so an
    eventual web `/recap` route can serialize the same data.
    """

    day_start = datetime(when.year, when.month, when.day)
    day_end = day_start + timedelta(days=1)

    trips = (
        session.execute(
            select(Trip).where(Trip.created_at >= day_start, Trip.created_at < day_end)
        )
        .scalars()
        .all()
    )

    by_status: dict[str, int] = {}
    by_vehicle: dict[str, int] = {}
    own_fleet = sub_fleet = 0
    fare_total = markup_total = 0
    completed_count = 0
    fare_missing_count = 0
    fare_missing_trip_ids: list[int] = []
    for t in trips:
        by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
        by_vehicle[t.vehicle_class.value] = by_vehicle.get(t.vehicle_class.value, 0) + 1
        if t.assigned_driver_id:
            own_fleet += 1
        elif t.assigned_subcontractor_id:
            sub_fleet += 1
        if t.status == TripStatus.COMPLETED:
            completed_count += 1
            if t.fare_cents is not None:
                fare_total += t.fare_cents
            else:
                # Completed-without-a-fare is a real Year-1 case (manual fare entry
                # lag, invoice in dispute, fare TBD). Don't silently exclude — count
                # them so the dispatcher's end-of-shift number is honest.
                fare_missing_count += 1
                fare_missing_trip_ids.append(t.id)
            if t.markup_cents:
                markup_total += t.markup_cents

    reservations_today = (
        session.execute(
            select(Reservation).where(
                Reservation.created_at >= day_start, Reservation.created_at < day_end
            )
        )
        .scalars()
        .all()
    )
    inquiries_today = (
        session.execute(
            select(Inquiry).where(
                Inquiry.created_at >= day_start, Inquiry.created_at < day_end
            )
        )
        .scalars()
        .all()
    )

    return {
        "date": day_start.strftime("%Y-%m-%d"),
        "total_trips": len(trips),
        "by_status": by_status,
        "by_vehicle": by_vehicle,
        "own_fleet": own_fleet,
        "sub_fleet": sub_fleet,
        "completed": completed_count,
        "fare_total_cents": fare_total,
        "markup_total_cents": markup_total,
        "net_revenue_cents": fare_total - markup_total,
        "fare_missing_count": fare_missing_count,
        "fare_missing_trip_ids": fare_missing_trip_ids,
        "reservations_received": len(reservations_today),
        "reservations_converted": sum(
            1 for r in reservations_today if r.status == ReservationStatus.CONVERTED
        ),
        "inquiries_received": len(inquiries_today),
    }
