"""Dispatcher CLI tool.

Usage examples:

    python cli.py drivers add "Jane Doe" "+19045551212" --classes suv,sprinter
    python cli.py drivers list
    python cli.py drivers status 1 available --lat 31.15 --lon -81.49

    python cli.py trips create \
        --passenger-name "Mr Smith" --passenger-phone "+19045555555" \
        --pickup "JAX Terminal" --pickup-lat 30.4941 --pickup-lon -81.6878 \
        --dropoff "Cloister, Sea Island" --dropoff-lat 31.1969 --dropoff-lon -81.3438 \
        --pickup-time "2026-11-12T15:00" --vehicle-class suv

    python cli.py trips broadcast 1
    python cli.py trips accept 1 --driver 1
    python cli.py trips status 1 en_route_pickup
    python cli.py trips list --status accepted
"""

from __future__ import annotations

from datetime import datetime, timezone

import sys

import typer
from sqlalchemy import select

import core
from database import SessionLocal, init_db
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

app = typer.Typer(no_args_is_help=True, help="Livery Dispatch CLI")
drivers_app = typer.Typer(no_args_is_help=True, help="Driver operations")
trips_app = typer.Typer(no_args_is_help=True, help="Trip operations")
subs_app = typer.Typer(no_args_is_help=True, help="Subcontractor (overflow) operations")
reservations_app = typer.Typer(no_args_is_help=True, help="Customer-side reservation inquiries")
inquiries_app = typer.Typer(no_args_is_help=True, help="Wedding / vendor / early-access inquiries")
app.add_typer(drivers_app, name="drivers")
app.add_typer(trips_app, name="trips")
app.add_typer(subs_app, name="subs")
app.add_typer(reservations_app, name="reservations")
app.add_typer(inquiries_app, name="inquiries")
rates_app = typer.Typer(no_args_is_help=True, help="Rate cards (fare quoting reference)")
app.add_typer(rates_app, name="rates")

# E10 (v1.1.18): on a Windows cp1252 console, printing a label with "→" or
# "—" raised UnicodeEncodeError and killed `trips list` / `reservations list`.
# Keep the console's encoding but replace what it can't show.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------------------
# Daily recap
# ---------------------------------------------------------------------------


from recap import build_recap as _build_recap  # noqa: E402 — keep public surface stable


@app.command("recap")
def recap_cmd(
    date_str: str | None = typer.Option(
        None,
        "--date",
        help="Date to recap as YYYY-MM-DD (UTC). Defaults to today.",
    ),
):
    """Print a one-screen recap of trips, reservations, and inquiries for a day.

    Pairs with the live /dashboard view: the dashboard shows now; recap
    shows then. Run end-of-shift to capture the day's numbers; run in the
    morning with --date YYYY-MM-DD to look at yesterday.
    """
    if date_str:
        try:
            when = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            typer.echo(f"Invalid --date '{date_str}'; expected YYYY-MM-DD", err=True)
            raise typer.Exit(1)
    else:
        when = datetime.now(timezone.utc)

    with SessionLocal() as s:
        r = _build_recap(s, when)

    typer.echo(f"=== Dispatch recap — {r['date']} ===")
    typer.echo(f"Trips created:        {r['total_trips']}")
    typer.echo(f"  Completed:          {r['completed']}")
    if r["by_status"]:
        for status, count in sorted(r["by_status"].items()):
            if status != "completed":
                typer.echo(f"  {status:18s}{count}")
    if r["by_vehicle"]:
        typer.echo("By vehicle class:")
        for vc, count in sorted(r["by_vehicle"].items()):
            typer.echo(f"  {vc:18s}{count}")
    typer.echo(f"Own-fleet assigned:   {r['own_fleet']}")
    typer.echo(f"Sub assigned:         {r['sub_fleet']}")
    typer.echo(f"Fare total:           ${r['fare_total_cents']/100:,.2f}")
    typer.echo(f"  Sub markup paid:    ${r['markup_total_cents']/100:,.2f}")
    typer.echo(f"  Net revenue:        ${r['net_revenue_cents']/100:,.2f}")
    if r["fare_missing_count"]:
        missing_ids = ", ".join(f"#{i}" for i in r["fare_missing_trip_ids"])
        typer.echo(
            f"  ⚠ {r['fare_missing_count']} completed trip(s) with no fare entered: {missing_ids}",
            err=True,
        )
    typer.echo(f"Reservations:         {r['reservations_received']} received, {r['reservations_converted']} converted")
    typer.echo(f"Inquiries received:   {r['inquiries_received']}")


@app.callback()
def _bootstrap():
    """Ensure tables exist before any command runs."""
    init_db()


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


@drivers_app.command("add")
def driver_add(
    name: str,
    phone: str,
    classes: str = typer.Option("suv", help="Comma-separated vehicle classes: suv,sprinter,sedan"),
):
    with SessionLocal() as s:
        d = Driver(name=name, phone=phone, vehicle_classes=classes)
        s.add(d)
        s.commit()
        s.refresh(d)
        typer.echo(f"Created driver {d.id}: {d.name} ({d.phone}) — classes: {d.vehicle_classes}")


@drivers_app.command("list")
def driver_list():
    with SessionLocal() as s:
        for d in s.scalars(select(Driver)).all():
            loc = f"({d.lat:.4f}, {d.lon:.4f})" if d.lat is not None and d.lon is not None else "(no location)"
            typer.echo(f"#{d.id:3d}  {d.status.value:10s}  {d.name:30s}  {d.phone:18s}  classes={d.vehicle_classes}  {loc}")


@drivers_app.command("status")
def driver_status(
    driver_id: int,
    new_status: DriverStatus,
    lat: float | None = typer.Option(None),
    lon: float | None = typer.Option(None),
):
    with SessionLocal() as s:
        d = s.get(Driver, driver_id)
        if d is None:
            typer.echo(f"Driver {driver_id} not found", err=True)
            raise typer.Exit(1)
        d.status = new_status
        if lat is not None:
            d.lat = lat
        if lon is not None:
            d.lon = lon
        s.commit()
        typer.echo(f"Driver {d.id} status={d.status.value} location=({d.lat}, {d.lon})")


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


@trips_app.command("create")
def trip_create(
    passenger_name: str = typer.Option(..., "--passenger-name"),
    passenger_phone: str = typer.Option(..., "--passenger-phone"),
    pickup: str = typer.Option(..., "--pickup", help="Pickup label"),
    pickup_lat: float = typer.Option(..., "--pickup-lat"),
    pickup_lon: float = typer.Option(..., "--pickup-lon"),
    dropoff: str = typer.Option(..., "--dropoff"),
    dropoff_lat: float = typer.Option(..., "--dropoff-lat"),
    dropoff_lon: float = typer.Option(..., "--dropoff-lon"),
    pickup_time: datetime = typer.Option(..., "--pickup-time", formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]),
    vehicle_class: VehicleClass = typer.Option(VehicleClass.SUV, "--vehicle-class"),
    notes: str | None = typer.Option(None, "--notes"),
):
    with SessionLocal() as s:
        t = Trip(
            passenger_name=passenger_name,
            passenger_phone=passenger_phone,
            pickup_label=pickup,
            pickup_lat=pickup_lat,
            pickup_lon=pickup_lon,
            dropoff_label=dropoff,
            dropoff_lat=dropoff_lat,
            dropoff_lon=dropoff_lon,
            pickup_time=pickup_time,
            vehicle_class=vehicle_class,
            notes=notes,
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        typer.echo(f"Created trip {t.id} ({t.vehicle_class.value}): {t.pickup_label} → {t.dropoff_label} at {t.pickup_time}")


@trips_app.command("list")
def trip_list(status: TripStatus | None = typer.Option(None)):
    with SessionLocal() as s:
        stmt = select(Trip)
        if status is not None:
            stmt = stmt.where(Trip.status == status)
        for t in s.scalars(stmt.order_by(Trip.id.desc())).all():
            assigned = f"driver={t.assigned_driver_id}" if t.assigned_driver_id else "unassigned"
            typer.echo(f"#{t.id:3d}  {t.status.value:14s}  {t.vehicle_class.value:8s}  {t.passenger_name:25s}  {assigned}  {t.pickup_label} → {t.dropoff_label}")


@trips_app.command("broadcast")
def trip_broadcast(trip_id: int):
    with SessionLocal() as s:
        t = s.get(Trip, trip_id)
        if t is None:
            typer.echo(f"Trip {trip_id} not found", err=True)
            raise typer.Exit(1)
        try:
            broadcasts = core.broadcast_trip(s, t)
        except core.TripNotBroadcastable as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        if not broadcasts:
            typer.echo(f"No available drivers; trip status now {t.status.value}", err=True)
            raise typer.Exit(2)
        for b in broadcasts:
            typer.echo(f"  → driver {b.driver_id} ({b.distance_mi:.1f} mi)")


@trips_app.command("broadcast-overflow")
def trip_broadcast_overflow(trip_id: int):
    """Hand off a trip to subcontractors (tier 1 first, then 2, then 3)."""
    with SessionLocal() as s:
        t = s.get(Trip, trip_id)
        if t is None:
            typer.echo(f"Trip {trip_id} not found", err=True)
            raise typer.Exit(1)
        try:
            broadcasts = core.broadcast_to_subcontractors(s, t)
        except core.TripNotBroadcastable as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        if not broadcasts:
            typer.echo(f"No active subcontractors matched; trip status now {t.status.value}", err=True)
            raise typer.Exit(2)
        for b in broadcasts:
            typer.echo(f"  → sub {b.subcontractor_id} ({b.distance_mi:.1f} mi)")


@trips_app.command("sub-accept")
def trip_sub_accept(
    trip_id: int,
    sub_id: int = typer.Option(..., "--sub"),
    quoted_cost_cents: int | None = typer.Option(None, "--quoted-cost-cents"),
):
    with SessionLocal() as s:
        try:
            t = core.accept_trip_subcontractor(s, trip_id, sub_id, quoted_cost_cents=quoted_cost_cents)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(
            f"Trip {t.id} ACCEPTED by subcontractor {t.assigned_subcontractor_id}; "
            f"fare={t.fare_cents}, markup={t.markup_cents}"
        )


@trips_app.command("sub-decline")
def trip_sub_decline(trip_id: int, sub_id: int = typer.Option(..., "--sub")):
    with SessionLocal() as s:
        try:
            core.decline_trip_subcontractor(s, trip_id, sub_id)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Trip {trip_id} declined by subcontractor {sub_id}")


@trips_app.command("accept")
def trip_accept(trip_id: int, driver_id: int = typer.Option(..., "--driver")):
    with SessionLocal() as s:
        try:
            t = core.accept_trip(s, trip_id, driver_id)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Trip {t.id} ACCEPTED by driver {t.assigned_driver_id}")


@trips_app.command("decline")
def trip_decline(trip_id: int, driver_id: int = typer.Option(..., "--driver")):
    with SessionLocal() as s:
        try:
            core.decline_trip(s, trip_id, driver_id)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Trip {trip_id} declined by driver {driver_id}")


@trips_app.command("status")
def trip_status(trip_id: int, new_status: TripStatus, fare_cents: int | None = typer.Option(None, "--fare-cents")):
    with SessionLocal() as s:
        try:
            t = core.update_trip_status(s, trip_id, new_status, fare_cents=fare_cents)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Trip {t.id} status={t.status.value}")


# ---------------------------------------------------------------------------
# Subcontractors
# ---------------------------------------------------------------------------


@trips_app.command("sub-cost")
def trip_sub_cost(
    trip_id: int,
    cost_cents: int = typer.Option(..., "--cost-cents", help="Sub's quoted cost in cents"),
):
    """Record an overflow sub's cost after an SMS accept with no price (E9)."""
    with SessionLocal() as s:
        try:
            t = core.record_subcontractor_cost(s, trip_id, cost_cents)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        typer.echo(f"Trip {t.id}: fare {t.fare_cents} cents (markup {t.markup_cents}).")


@rates_app.command("list")
def rates_list():
    """Show every rate card."""
    from models import RateCard
    with SessionLocal() as s:
        cards = list(s.scalars(select(RateCard).order_by(RateCard.id)).all())
        if not cards:
            typer.echo("No rate cards. Run: python cli.py rates seed-defaults")
            return
        for c in cards:
            typer.echo(
                f"#{c.id} {c.vehicle_class.value:<9} {'active' if c.active else 'inactive':<8} "
                f"base {c.base_fare_cents} + {c.per_mile_cents}/mi + {c.per_hour_cents}/hr  {c.name}"
            )


@rates_app.command("seed-defaults")
def rates_seed_defaults():
    """Load the planning-grade SUV + Sprinter cards if there are none (E6).

    Safe to run on a production database: it does nothing when any rate card
    already exists, so it never overwrites the dispatcher's edits.
    """
    from models import RateCard
    from rate_defaults import planning_rate_cards
    init_db()
    with SessionLocal() as s:
        existing = s.query(RateCard).count()
        if existing:
            typer.echo(f"{existing} rate card(s) already exist; nothing seeded.")
            return
        cards = planning_rate_cards()
        s.add_all(cards)
        s.commit()
        typer.echo(f"Seeded {len(cards)} planning-grade rate cards: "
                   + ", ".join(c.name for c in cards))

@subs_app.command("add")
def sub_add(
    name: str,
    dispatch_phone: str,
    classes: str = typer.Option("suv", help="Comma-separated vehicle classes"),
    tier: int = typer.Option(1, help="1 (preferred) / 2 (backup) / 3 (last-resort)"),
    markup_pct: float = typer.Option(15.0, help="Cost-plus markup percentage"),
    base_lat: float | None = typer.Option(None),
    base_lon: float | None = typer.Option(None),
    service_radius_mi: float | None = typer.Option(None),
    notes: str | None = typer.Option(None),
):
    with SessionLocal() as s:
        try:
            tier_enum = SubcontractorTier(tier)
        except ValueError as exc:
            typer.echo(f"Invalid --tier {tier}; expected 1, 2, or 3", err=True)
            raise typer.Exit(1) from exc
        sub = Subcontractor(
            name=name,
            dispatch_phone=dispatch_phone,
            vehicle_classes=classes,
            tier=tier_enum,
            markup_pct=markup_pct,
            base_lat=base_lat,
            base_lon=base_lon,
            service_radius_mi=service_radius_mi,
            notes=notes,
        )
        s.add(sub)
        s.commit()
        s.refresh(sub)
        typer.echo(
            f"Created subcontractor {sub.id}: {sub.name} (tier {sub.tier.value}, "
            f"{sub.markup_pct:.0f}% markup) — classes: {sub.vehicle_classes}"
        )


@subs_app.command("list")
def sub_list(active_only: bool = typer.Option(False, "--active-only")):
    with SessionLocal() as s:
        stmt = select(Subcontractor)
        if active_only:
            stmt = stmt.where(Subcontractor.active.is_(True))
        for sub in s.scalars(stmt).all():
            base = (
                f"({sub.base_lat:.4f}, {sub.base_lon:.4f})"
                if sub.base_lat is not None and sub.base_lon is not None
                else "(no base)"
            )
            active_flag = "ACTIVE" if sub.active else "inactive"
            typer.echo(
                f"#{sub.id:3d}  T{sub.tier.value}  {active_flag:8s}  "
                f"{sub.name:35s}  {sub.dispatch_phone:18s}  "
                f"classes={sub.vehicle_classes:18s}  markup={sub.markup_pct:.0f}%  {base}"
            )


@subs_app.command("deactivate")
def sub_deactivate(sub_id: int):
    with SessionLocal() as s:
        sub = s.get(Subcontractor, sub_id)
        if sub is None:
            typer.echo(f"Subcontractor {sub_id} not found", err=True)
            raise typer.Exit(1)
        sub.active = False
        s.commit()
        typer.echo(f"Subcontractor {sub.id} ({sub.name}) deactivated")


# ---------------------------------------------------------------------------
# Reservations (inquiries from the marketing site reserve form)
# ---------------------------------------------------------------------------


@reservations_app.command("list")
def reservation_list(status: ReservationStatus | None = typer.Option(None, "--status")):
    with SessionLocal() as s:
        stmt = select(Reservation)
        if status is not None:
            stmt = stmt.where(Reservation.status == status)
        for r in s.scalars(stmt.order_by(Reservation.id.desc())).all():
            when = (
                r.pickup_at.strftime("%Y-%m-%d %H:%M")
                if r.pickup_at is not None
                else f"{r.pickup_date or '?'} {r.pickup_time_str or '?'}"
            )
            link = f" -> trip #{r.linked_trip_id}" if r.linked_trip_id else ""
            typer.echo(
                f"#{r.id:3d}  {r.status.value:14s}  {r.name:25s}  "
                f"{r.pickup_label:30s} -> {r.dropoff_label:30s}  {when}{link}"
            )


@reservations_app.command("show")
def reservation_show(reservation_id: int):
    with SessionLocal() as s:
        r = s.get(Reservation, reservation_id)
        if r is None:
            typer.echo(f"Reservation {reservation_id} not found", err=True)
            raise typer.Exit(1)
        typer.echo(f"Reservation #{r.id}  status={r.status.value}")
        typer.echo(f"  {r.name}  {r.email}  {r.phone}")
        typer.echo(f"  Pickup:  {r.pickup_label}")
        typer.echo(f"  Dropoff: {r.dropoff_label}")
        when = (
            r.pickup_at.strftime("%Y-%m-%d %H:%M")
            if r.pickup_at is not None
            else f"{r.pickup_date or '?'} {r.pickup_time_str or '?'}"
        )
        typer.echo(f"  When:    {when}")
        if r.passengers:
            typer.echo(f"  Pax:     {r.passengers}")
        if r.vehicle_pref:
            typer.echo(f"  Vehicle: {r.vehicle_pref} (class={r.vehicle_class.value if r.vehicle_class else '-'})")
        if r.flight:
            typer.echo(f"  Flight:  {r.flight}")
        if r.notes:
            typer.echo(f"  Notes:   {r.notes}")
        if r.dispatcher_notes:
            typer.echo(f"  Disp:    {r.dispatcher_notes}")
        if r.linked_trip_id:
            typer.echo(f"  Trip:    #{r.linked_trip_id}")


@reservations_app.command("set-status")
def reservation_set_status(
    reservation_id: int,
    new_status: ReservationStatus,
    dispatcher_notes: str | None = typer.Option(None, "--notes"),
):
    with SessionLocal() as s:
        r = s.get(Reservation, reservation_id)
        if r is None:
            typer.echo(f"Reservation {reservation_id} not found", err=True)
            raise typer.Exit(1)
        r.status = new_status
        if dispatcher_notes is not None:
            r.dispatcher_notes = dispatcher_notes
        s.commit()
        typer.echo(f"Reservation {r.id} status={r.status.value}")


@reservations_app.command("link-trip")
def reservation_link_trip(reservation_id: int, trip_id: int = typer.Option(..., "--trip")):
    """Mark a reservation as CONVERTED and attach the Trip created from it."""
    with SessionLocal() as s:
        r = s.get(Reservation, reservation_id)
        if r is None:
            typer.echo(f"Reservation {reservation_id} not found", err=True)
            raise typer.Exit(1)
        if s.get(Trip, trip_id) is None:
            typer.echo(f"Trip {trip_id} not found", err=True)
            raise typer.Exit(1)
        r.linked_trip_id = trip_id
        r.status = ReservationStatus.CONVERTED
        s.commit()
        typer.echo(f"Reservation {r.id} -> trip #{trip_id} (CONVERTED)")


@inquiries_app.command("list")
def inquiry_list(
    status: InquiryStatus | None = typer.Option(None, "--status"),
    kind: InquiryKind | None = typer.Option(None, "--kind"),
):
    with SessionLocal() as s:
        stmt = select(Inquiry)
        if status is not None:
            stmt = stmt.where(Inquiry.status == status)
        if kind is not None:
            stmt = stmt.where(Inquiry.kind == kind)
        for inq in s.scalars(stmt.order_by(Inquiry.id.desc())).all():
            typer.echo(
                f"#{inq.id:3d}  {inq.kind.value:13s}  {inq.status.value:14s}  "
                f"{inq.name:25s}  {inq.email}"
            )


@inquiries_app.command("show")
def inquiry_show(inquiry_id: int):
    import json as _json

    with SessionLocal() as s:
        inq = s.get(Inquiry, inquiry_id)
        if inq is None:
            typer.echo(f"Inquiry {inquiry_id} not found", err=True)
            raise typer.Exit(1)
        typer.echo(f"Inquiry #{inq.id}  kind={inq.kind.value}  status={inq.status.value}")
        typer.echo(f"  {inq.name}  {inq.email}  {inq.phone or '-'}")
        if inq.notes:
            typer.echo(f"  Notes:   {inq.notes}")
        if inq.details_json:
            try:
                details = _json.loads(inq.details_json)
                for k, v in details.items():
                    typer.echo(f"  {k:8s} {v}")
            except (ValueError, TypeError):
                typer.echo(f"  (raw): {inq.details_json}")
        if inq.dispatcher_notes:
            typer.echo(f"  Disp:    {inq.dispatcher_notes}")


@inquiries_app.command("set-status")
def inquiry_set_status(
    inquiry_id: int,
    new_status: InquiryStatus,
    dispatcher_notes: str | None = typer.Option(None, "--notes"),
):
    with SessionLocal() as s:
        inq = s.get(Inquiry, inquiry_id)
        if inq is None:
            typer.echo(f"Inquiry {inquiry_id} not found", err=True)
            raise typer.Exit(1)
        inq.status = new_status
        if dispatcher_notes is not None:
            inq.dispatcher_notes = dispatcher_notes
        s.commit()
        typer.echo(f"Inquiry {inq.id} status={inq.status.value}")


if __name__ == "__main__":
    app()
