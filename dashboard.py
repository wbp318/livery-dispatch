"""Dispatcher dashboard — single auto-refreshing HTML view.

The dispatcher's everyday workspace. Renders one page with everything that
needs attention:

    - Active trips (broadcasting, accepted, in any active driver state)
    - Pending reservations from the marketing-site reserve form
    - Pending inquiries (wedding / vendor / early-access)
    - Driver roster with current status
    - Subcontractor roster (Tier-1 first)
    - Today's at-a-glance counts at the top

Auto-refresh every 30 seconds via `<meta http-equiv="refresh">` so the dispatcher
doesn't have to manually reload during a shift. No JavaScript — works on
any browser, survives an internet hiccup gracefully, easy to debug.

Style is hand-rolled CSS inline in the page (no extra assets, no build
step). Theme is the Fireworks brass-on-dark palette adapted for tight
information density. Anything that needs immediate attention is brass;
neutral state is paper-on-ink.
"""

from __future__ import annotations

import html as _html
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

import csp as _csp
from _styles import BASE_CSS, _fmt_dt, _fmt_fare, _status_label, _truncate
from models import (
    Driver,
    DriverStatus,
    Inquiry,
    InquiryStatus,
    Reservation,
    ReservationStatus,
    Subcontractor,
    SubcontractorTier,
    Trip,
    TripStatus,
)


ACTIVE_TRIP_STATUSES = (
    # REQUESTED is included so "Create only" trips from /map's click-to-dispatch
    # appear on the dashboard immediately, not only after the dispatcher
    # broadcasts. A requested-but-not-broadcast trip is a dispatcher action
    # item — needs to be visible.
    TripStatus.REQUESTED,
    TripStatus.BROADCASTING,
    TripStatus.ACCEPTED,
    TripStatus.EN_ROUTE_PICKUP,
    TripStatus.ON_SCENE,
    TripStatus.IN_TRANSIT,
)


def render_dashboard(session: Session, *, refresh_seconds: int = 30) -> str:
    """Render the dashboard as a complete HTML document."""

    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day)
    today_end = today_start + timedelta(days=1)

    active_trips = (
        session.execute(
            select(Trip)
            .where(Trip.status.in_(ACTIVE_TRIP_STATUSES))
            .order_by(Trip.pickup_time.asc())
        )
        .scalars()
        .all()
    )
    pending_reservations = (
        session.execute(
            select(Reservation)
            .where(Reservation.status == ReservationStatus.PENDING_REVIEW)
            .order_by(Reservation.created_at.asc())
        )
        .scalars()
        .all()
    )
    pending_inquiries = (
        session.execute(
            select(Inquiry)
            .where(Inquiry.status == InquiryStatus.PENDING_REVIEW)
            .order_by(Inquiry.created_at.asc())
        )
        .scalars()
        .all()
    )
    drivers = session.execute(select(Driver).order_by(Driver.name.asc())).scalars().all()
    subs = (
        session.execute(
            select(Subcontractor)
            .where(Subcontractor.active == True)  # noqa: E712 — SQLAlchemy idiom
            .order_by(Subcontractor.tier.asc(), Subcontractor.name.asc())
        )
        .scalars()
        .all()
    )
    today_trips = (
        session.execute(
            select(Trip).where(
                Trip.created_at >= today_start, Trip.created_at < today_end
            )
        )
        .scalars()
        .all()
    )

    counts = {
        "active_trips": len(active_trips),
        "pending_reservations": len(pending_reservations),
        "pending_inquiries": len(pending_inquiries),
        "drivers_available": sum(1 for d in drivers if d.status == DriverStatus.AVAILABLE),
        "drivers_on_trip": sum(1 for d in drivers if d.status == DriverStatus.ON_TRIP),
        "today_total_trips": len(today_trips),
        "today_completed": sum(1 for t in today_trips if t.status == TripStatus.COMPLETED),
    }

    parts = [
        _doc_open(refresh_seconds, now),
        _summary_bar(counts, now, refresh_seconds),
        _active_trips_section(active_trips, drivers, subs),
        _two_column_section(
            _reservations_section(pending_reservations),
            _inquiries_section(pending_inquiries),
        ),
        _two_column_section(
            _drivers_section(drivers),
            _subs_section(subs),
        ),
        _doc_close(),
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _summary_bar(counts: dict[str, int], now: datetime, refresh_seconds: int) -> str:
    chips = [
        _chip("Active trips", counts["active_trips"], anchor="/admin/trips?status=active",
              urgent=counts["active_trips"] > 0),
        _chip("Pending reservations", counts["pending_reservations"],
              anchor="/admin/reservations?status=pending_review",
              urgent=counts["pending_reservations"] > 0),
        _chip("Pending inquiries", counts["pending_inquiries"],
              anchor="/admin/inquiries?status=pending_review",
              urgent=counts["pending_inquiries"] > 0),
        _chip("Drivers available", counts["drivers_available"], anchor="/admin/drivers"),
        _chip("Drivers on trip", counts["drivers_on_trip"], anchor="/admin/drivers"),
        _chip("Today rides", f'{counts["today_completed"]}/{counts["today_total_trips"]}',
              anchor="/admin/trips"),
    ]
    return (
        '<section class="summary">'
        '<div class="summary-grid">'
        + "".join(chips)
        + "</div>"
        f'<div class="updated">Updated {now.strftime("%Y-%m-%d %H:%M:%S")} UTC · auto-refresh every {int(refresh_seconds)}s</div>'
        "</section>"
    )


def _chip(label: str, value, *, urgent: bool = False, anchor: str | None = None) -> str:
    cls = "chip urgent" if urgent else "chip"
    body = (
        f'<span class="chip-value">{_html.escape(str(value))}</span>'
        f'<span class="chip-label">{_html.escape(label)}</span>'
    )
    if anchor:
        # Rendered as a real anchor so right-click → open-in-new-tab, copy-link,
        # and keyboard Tab-focus all behave like proper navigation.
        return f'<a class="{cls}" href="{_html.escape(anchor)}" aria-label="{_html.escape(label)}: {_html.escape(str(value))}">{body}</a>'
    return f'<div class="{cls}">{body}</div>'


def _active_trips_section(trips, drivers, subs) -> str:
    driver_by_id = {d.id: d for d in drivers}
    sub_by_id = {s.id: s for s in subs}
    if not trips:
        body = '<div class="empty">No trips currently in motion.</div>'
    else:
        rows = []
        for t in trips:
            if t.assigned_driver_id:
                d = driver_by_id.get(t.assigned_driver_id)
                assigned = f"<span class='tag own'>{_html.escape(d.name if d else f'#{t.assigned_driver_id}')}</span>"
            elif t.assigned_subcontractor_id:
                s = sub_by_id.get(t.assigned_subcontractor_id)
                assigned = f"<span class='tag sub'>{_html.escape(s.name if s else f'#{t.assigned_subcontractor_id}')}</span>"
            else:
                assigned = '<span class="tag none">—</span>'

            rows.append(
                f"<tr class='clickable' onclick=\"window.location.href='/admin/trips/{t.id}'\">"
                f"<td class='id'><a href='/admin/trips/{t.id}'>#{t.id}</a></td>"
                f"<td class='status status-{_html.escape(t.status.value)}'>{_html.escape(_status_label(t.status))}</td>"
                f"<td class='time'>{_html.escape(_fmt_dt(t.pickup_time, '%m-%d %H:%M'))}</td>"
                f"<td class='passenger'><a href='/admin/trips/{t.id}'>{_html.escape(t.passenger_name)}</a></td>"
                f"<td class='route'>{_html.escape(_truncate(t.pickup_label, 28))} <span class='arrow'>→</span> {_html.escape(_truncate(t.dropoff_label, 28))}</td>"
                f"<td class='vehicle'>{_html.escape(t.vehicle_class.value.upper())}</td>"
                f"<td class='assigned'>{assigned}</td>"
                f"<td class='fare'>{_fmt_fare(t.fare_cents)}</td>"
                "</tr>"
            )
        body = (
            '<table class="trips"><thead><tr>'
            "<th>ID</th><th>Status</th><th>Pickup time</th><th>Passenger</th>"
            "<th>Route</th><th>Class</th><th>Assigned</th><th>Fare</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return (
        '<section class="block" id="active-trips">'
        '<h2>Active trips</h2>'
        + body
        + "</section>"
    )


def _reservations_section(reservations) -> str:
    if not reservations:
        body = '<div class="empty">No pending reservations to review.</div>'
    else:
        rows = []
        for r in reservations:
            when = _fmt_dt(r.pickup_at, "%m-%d %H:%M") if r.pickup_at else (r.pickup_date or "—") + " " + (r.pickup_time_str or "")
            vclass = (r.vehicle_class.value.upper() if r.vehicle_class else (r.vehicle_pref or "—"))
            rows.append(
                f"<tr class='clickable' onclick=\"window.location.href='/admin/reservations/{r.id}'\">"
                f"<td class='id'><a href='/admin/reservations/{r.id}'>#{r.id}</a></td>"
                f"<td class='passenger'><a href='/admin/reservations/{r.id}'>{_html.escape(r.name)}</a></td>"
                f"<td class='route'>{_html.escape(_truncate(r.pickup_label, 22))} <span class='arrow'>→</span> {_html.escape(_truncate(r.dropoff_label, 22))}</td>"
                f"<td class='time'>{_html.escape(when.strip())}</td>"
                f"<td class='vehicle'>{_html.escape(vclass)}</td>"
                f"<td class='source'>{_html.escape(_short_source(r.source))}</td>"
                "</tr>"
            )
        body = (
            '<table class="reservations"><thead><tr>'
            "<th>ID</th><th>Name</th><th>Route</th><th>When</th><th>Pref</th><th>Source</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return (
        '<div class="block" id="pending-reservations">'
        '<h2>Pending reservations</h2>'
        + body
        + "</div>"
    )


def _inquiries_section(inquiries) -> str:
    if not inquiries:
        body = '<div class="empty">No pending inquiries.</div>'
    else:
        rows = []
        for i in inquiries:
            extra = ""
            if i.details_json:
                try:
                    d = json.loads(i.details_json)
                    extra = ", ".join(f"{k}: {v}" for k, v in d.items() if v)[:80]
                except (ValueError, TypeError):
                    extra = ""
            rows.append(
                f"<tr class='clickable' onclick=\"window.location.href='/admin/inquiries/{i.id}'\">"
                f"<td class='id'><a href='/admin/inquiries/{i.id}'>#{i.id}</a></td>"
                f"<td class='kind kind-{_html.escape(i.kind.value)}'>{_html.escape(_status_label(i.kind))}</td>"
                f"<td class='passenger'><a href='/admin/inquiries/{i.id}'>{_html.escape(i.name)}</a></td>"
                f"<td class='time'>{_html.escape(_fmt_dt(i.created_at, '%m-%d %H:%M'))}</td>"
                f"<td class='detail'>{_html.escape(extra)}</td>"
                "</tr>"
            )
        body = (
            '<table class="inquiries"><thead><tr>'
            "<th>ID</th><th>Kind</th><th>Name</th><th>Received</th><th>Detail</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return (
        '<div class="block" id="pending-inquiries">'
        '<h2>Pending inquiries</h2>'
        + body
        + "</div>"
    )


def _drivers_section(drivers) -> str:
    if not drivers:
        body = '<div class="empty">No drivers configured yet.</div>'
    else:
        rows = []
        for d in drivers:
            rows.append(
                f"<tr class='clickable' onclick=\"window.location.href='/admin/drivers/{d.id}'\">"
                f"<td class='id'><a href='/admin/drivers/{d.id}'>#{d.id}</a></td>"
                f"<td class='passenger'><a href='/admin/drivers/{d.id}'>{_html.escape(d.name)}</a></td>"
                f"<td class='status status-{_html.escape(d.status.value)}'>{_html.escape(_status_label(d.status))}</td>"
                f"<td class='vehicle'>{_html.escape(d.vehicle_classes.upper())}</td>"
                f"<td class='phone'>{_html.escape(d.phone)}</td>"
                "</tr>"
            )
        body = (
            '<table class="drivers"><thead><tr>'
            "<th>ID</th><th>Name</th><th>Status</th><th>Classes</th><th>Phone</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return (
        '<div class="block" id="drivers">'
        '<h2>Drivers</h2>'
        + body
        + "</div>"
    )


def _subs_section(subs) -> str:
    if not subs:
        body = '<div class="empty">No subcontractors configured.</div>'
    else:
        rows = []
        for s in subs:
            rows.append(
                f"<tr class='clickable' onclick=\"window.location.href='/admin/subcontractors/{s.id}'\">"
                f"<td class='id'><a href='/admin/subcontractors/{s.id}'>#{s.id}</a></td>"
                f"<td class='passenger'><a href='/admin/subcontractors/{s.id}'>{_html.escape(s.name)}</a></td>"
                f"<td class='tier tier-{int(s.tier)}'>Tier {int(s.tier)}</td>"
                f"<td class='vehicle'>{_html.escape(s.vehicle_classes.upper())}</td>"
                f"<td class='markup'>{s.markup_pct:.0f}%</td>"
                f"<td class='phone'>{_html.escape(s.dispatch_phone)}</td>"
                "</tr>"
            )
        body = (
            '<table class="subs"><thead><tr>'
            "<th>ID</th><th>Operator</th><th>Tier</th><th>Classes</th><th>Markup</th><th>Phone</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return (
        '<div class="block" id="subcontractors">'
        '<h2>Subcontractors</h2>'
        + body
        + "</div>"
    )


def _two_column_section(left: str, right: str) -> str:
    return (
        '<section class="two-col">'
        f'<div class="col">{left}</div>'
        f'<div class="col">{right}</div>'
        '</section>'
    )


# ---------------------------------------------------------------------------
# Document scaffolding
# ---------------------------------------------------------------------------


def _doc_open(refresh_seconds: int, now: datetime) -> str:
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}" />'
        '<meta name="robots" content="noindex, nofollow" />'
        '<title>Livery Dispatch</title>'
        f'<style{_csp.nonce_attr()}>' + _CSS + "</style>"
        "</head><body>"
        '<header><div class="brand">Livery <em>Dispatch</em></div>'
        '<nav class="topnav" aria-label="Primary">'
        '<a href="/dashboard" class="is-active">Dashboard</a>'
        '<a href="/map">Live map</a>'
        '<a href="/admin/trips">Trips</a>'
        '<a href="/admin/reservations">Reservations</a>'
        '<a href="/admin/inquiries">Inquiries</a>'
        '<a href="/admin/drivers">Drivers</a>'
        '<a href="/admin/subcontractors">Subs</a>'
        '<a href="/admin/rates">Rates</a>'
        '<a href="/admin/calls">Calls</a>'
        '<a href="/admin/audit">Audit</a>'
        '<a href="/docs">API</a>'
        '</nav>'
        '<div class="role">Dispatch</div></header>'
        '<main>'
    )


def _doc_close() -> str:
    return (
        '<footer>Internal — not for public distribution.</footer>'
        "</main></body></html>"
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _short_source(source: str | None) -> str:
    if not source:
        return "—"
    # Source format is "sitePATH" e.g. "site/sea-island.html#reserve"; the
    # interesting part is the page slug.
    src = source.removeprefix("site")
    # Trim trailing fragment / query for compact display.
    return src.split("#", 1)[0].split("?", 1)[0] or "/"


# ---------------------------------------------------------------------------
# Stylesheet — Fireworks brass on dark, tuned for info density
# ---------------------------------------------------------------------------


_CSS = BASE_CSS + """
header{display:flex;align-items:center;justify-content:space-between;
  padding:14px 22px;border-bottom:1px solid var(--brass-deep);
  background:linear-gradient(180deg,#15171f 0%,#0d0f14 100%);}
header .brand{font:600 19px/1 "Hoefler Text","Garamond",serif;color:var(--paper);
  letter-spacing:0.01em;}
header .brand em{color:var(--brass);font-style:italic;}
header .role{font:10px/1 "Lucida Grande",sans-serif;letter-spacing:0.22em;
  text-transform:uppercase;color:var(--brass);}
header .topnav{display:flex;align-items:center;gap:8px;}
header .topnav a{color:rgba(251,246,232,0.65);text-decoration:none;
  font:11px/1 "Lucida Grande",sans-serif;letter-spacing:0.16em;
  text-transform:uppercase;padding:6px 10px;border-radius:2px;
  border:1px solid transparent;}
header .topnav a:hover{color:var(--brass-hi);background:rgba(201,169,97,0.07);}
header .topnav a.is-active{color:var(--brass-hi);border-color:var(--brass-deep);
  background:rgba(201,169,97,0.06);}
main{padding:18px 22px 80px;max-width:1480px;margin:0 auto;}
section{margin-bottom:24px;}
h2{font:600 13px/1 "Lucida Grande",sans-serif;letter-spacing:0.18em;
  text-transform:uppercase;color:var(--brass);margin:0 0 8px;
  padding-bottom:5px;border-bottom:1px dashed rgba(201,169,97,0.4);}

.summary-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;}
.chip{display:block;padding:10px 12px;border:1px solid var(--rule);border-radius:4px;
  background:rgba(255,255,255,0.02);text-decoration:none;color:inherit;
  transition:border-color 120ms ease,background 120ms ease,transform 120ms ease;}
a.chip{cursor:pointer;}
a.chip:hover{border-color:var(--brass-mid);
  background:linear-gradient(180deg,rgba(201,169,97,0.10) 0%,rgba(201,169,97,0.03) 100%);
  transform:translateY(-1px);}
a.chip:focus-visible{outline:2px solid var(--brass-hi);outline-offset:2px;}
.chip.urgent{border-color:var(--brass-mid);
  background:linear-gradient(180deg,rgba(201,169,97,0.10) 0%,rgba(201,169,97,0.02) 100%);}
a.chip.urgent:hover{
  background:linear-gradient(180deg,rgba(201,169,97,0.16) 0%,rgba(201,169,97,0.04) 100%);
  border-color:var(--brass);}
.chip-value{display:block;font:600 22px/1 "Hoefler Text","Garamond",serif;color:var(--paper);}
.chip.urgent .chip-value{color:var(--brass-hi);}
.chip-label{display:block;font:10px/1 "Lucida Grande",sans-serif;letter-spacing:0.16em;
  text-transform:uppercase;color:var(--brass);margin-top:6px;}

/* Smooth-scroll the in-page jumps from the summary chips so the jump feels
   like navigation, not a teleport. Honor reduced-motion preferences. */
html{scroll-behavior:smooth;scroll-padding-top:18px;}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto;}}
.updated{margin-top:8px;font:10.5px/1 "Lucida Grande",sans-serif;
  color:rgba(251,246,232,0.55);letter-spacing:0.04em;}

.block{margin-bottom:18px;}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:22px;}
@media (max-width:1100px){.two-col{grid-template-columns:1fr;}
  .summary-grid{grid-template-columns:repeat(3,1fr);}}

table{width:100%;border-collapse:collapse;font-size:12.5px;
  background:rgba(255,255,255,0.02);border:1px solid var(--rule);
  border-radius:3px;overflow:hidden;}
thead th{text-align:left;padding:6px 10px;
  font:600 10px/1 "Lucida Grande",sans-serif;letter-spacing:0.14em;
  text-transform:uppercase;color:var(--brass);
  background:rgba(255,255,255,0.03);border-bottom:1px solid var(--rule);}
tbody td{padding:7px 10px;border-bottom:1px solid rgba(58,53,39,0.5);
  vertical-align:middle;color:var(--paper);}
tbody td a{color:inherit;text-decoration:none;}
tbody td.id a{color:var(--brass);}
tbody tr:nth-child(even){background:var(--row-alt);}
tbody tr:last-child td{border-bottom:none;}
tbody tr.clickable{cursor:pointer;transition:background 100ms ease;}
tbody tr.clickable:hover{background:rgba(201,169,97,0.08);}
tbody tr.clickable:hover td.passenger a{color:var(--brass-hi);}

td.id{font:600 11px/1 "Lucida Console",Consolas,monospace;
  color:var(--brass);width:42px;}
td.passenger{font-weight:600;}
td.time{font:11.5px/1 "Lucida Console",Consolas,monospace;color:var(--brass-hi);
  white-space:nowrap;}
td.route{font-size:12px;}
.arrow{color:var(--brass);}
td.vehicle{font:11px/1 "Lucida Console",Consolas,monospace;
  color:var(--brass-hi);letter-spacing:0.04em;}
td.fare{font:11.5px/1 "Lucida Console",Consolas,monospace;color:var(--paper);
  text-align:right;}
td.phone{font:11px/1 "Lucida Console",Consolas,monospace;
  color:rgba(251,246,232,0.7);}

td.source,td.detail{color:rgba(251,246,232,0.7);font-size:11.5px;}
"""
