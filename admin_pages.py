"""Admin pages — editable list + detail views for every entity.

Companion to dashboard.py (read-only at-a-glance) and map_view.py (live spatial
view). These pages are where the dispatcher actually *edits* trips,
reservations, inquiries, drivers, and subcontractors.

Architecture is intentionally classical: server-rendered HTML, form POSTs,
303 See Other redirects on success. No JavaScript framework, no client-side
state. Each form action maps 1:1 to a server-side route, which calls the
existing core / API logic. Works in any browser, survives an internet hiccup,
and trivially auditable.

Pages exported (each is rendered by a function in this module; routes live in
app.py):

    /trips                    trips_list_page(session, status_filter=None)
    /trips/{id}               trip_detail_page(session, trip, flash=None)
    /reservations             reservations_list_page(session, status_filter)
    /reservations/{id}        reservation_detail_page(session, reservation, flash)
    /inquiries                inquiries_list_page(session, status_filter, kind_filter)
    /inquiries/{id}           inquiry_detail_page(session, inquiry, flash)
    /drivers                  drivers_list_page(session)
    /drivers/{id}             driver_detail_page(session, driver, flash)
    /subcontractors           subs_list_page(session)
    /subcontractors/{id}      sub_detail_page(session, sub, flash)

Style is shared with dashboard.py (brass-on-dark Fireworks aesthetic). The CSS
is embedded so the module is self-contained.
"""

from __future__ import annotations

import contextvars
import html as _html
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    BroadcastResponse,
    CallLog,
    Driver,
    DriverStatus,
    Inquiry,
    InquiryKind,
    InquiryStatus,
    RateCard,
    Reservation,
    ReservationStatus,
    Subcontractor,
    SubcontractorTier,
    Trip,
    TripStatus,
    VehicleClass,
)
import csp as _csp
import fare as _fare
from _styles import BASE_CSS, _fmt_dt, _fmt_fare, _status_label, _truncate

_csrf_token_var: contextvars.ContextVar[str] = contextvars.ContextVar("_csrf_token", default="")


def set_csrf_token(token: str) -> None:
    _csrf_token_var.set(token)


def _csrf_hidden() -> str:
    token = _csrf_token_var.get()
    if not token:
        return ""
    return f'<input type="hidden" name="csrf_token" value="{_html.escape(token)}" />'


# ---------------------------------------------------------------------------
# Shared chrome
# ---------------------------------------------------------------------------


def _doc_open(title: str, active_nav: str) -> str:
    """Open every admin page with the same chrome — brand, nav, CSS."""
    nav_items = [
        ("Dashboard", "/dashboard", "dashboard"),
        ("Live map", "/map", "map"),
        ("Trips", "/admin/trips", "trips"),
        ("Reservations", "/admin/reservations", "reservations"),
        ("Inquiries", "/admin/inquiries", "inquiries"),
        ("Drivers", "/admin/drivers", "drivers"),
        ("Subs", "/admin/subcontractors", "subs"),
        ("Rates", "/admin/rates", "rates"),
        ("Calls", "/admin/calls", "calls"),
        ("Audit", "/admin/audit", "audit"),
        ("API", "/docs", "api"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="{"is-active" if key == active_nav else ""}">{_html.escape(label)}</a>'
        for label, href, key in nav_items
    )
    csrf_meta = ""
    token = _csrf_token_var.get()
    if token:
        csrf_meta = f'<meta name="csrf-token" content="{_html.escape(token)}" />'
    logout_html = (
        '<form method="post" action="/admin/logout" class="logout-form">'
        '<button type="submit" class="logout-btn">Sign out</button>'
        '</form>'
    )
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        '<meta name="robots" content="noindex, nofollow" />'
        + csrf_meta
        + f'<title>{_html.escape(title)} — Livery Dispatch</title>'
        f'<style{_csp.nonce_attr()}>' + _CSS + "</style>"
        "</head><body>"
        '<header><div class="brand">Livery <em>Dispatch</em></div>'
        '<nav class="topnav" aria-label="Primary">' + nav_html + "</nav>"
        + logout_html
        + '<div class="role">Dispatch</div></header>'
        "<main>"
    )


def _doc_close() -> str:
    token = _csrf_token_var.get()
    csrf_script = ""
    if token:
        csrf_script = (
            f'<script{_csp.nonce_attr()}>'
            "document.querySelectorAll('form[method=\"post\"]').forEach(function(f){"
            "var i=document.createElement('input');i.type='hidden';"
            f"i.name='csrf_token';i.value='{_html.escape(token)}';"
            "f.appendChild(i);"
            "});"
            "</script>"
        )
    return (
        "<footer>Internal — not for public distribution.</footer>"
        + csrf_script
        + "</main></body></html>"
    )


def render_login_page(error: str | None = None, flash: str | None = None) -> str:
    err_html = (
        f'<div class="flash flash-err" role="alert">{_html.escape(error)}</div>'
        if error else ""
    )
    flash_html = (
        f'<div class="flash" role="status">{_html.escape(flash)}</div>'
        if flash else ""
    )
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        '<meta name="robots" content="noindex, nofollow" />'
        "<title>Sign in — Livery Dispatch</title>"
        f'<style{_csp.nonce_attr()}>' + _CSS + """
        .login-wrap{max-width:360px;margin:80px auto;padding:32px;
          border:1px solid var(--rule);border-radius:6px;background:var(--ink-2);}
        .login-wrap h1{margin:0 0 24px;font-size:20px;text-align:center;
          color:var(--brass-hi);letter-spacing:0.04em;}
        .login-wrap label{display:block;margin-bottom:4px;font-size:12px;
          color:var(--brass);text-transform:uppercase;letter-spacing:0.08em;}
        .login-wrap input[type="text"],.login-wrap input[type="password"]{
          width:100%;padding:8px 10px;margin-bottom:16px;border:1px solid var(--rule);
          border-radius:3px;background:var(--ink);color:var(--paper);font-size:14px;}
        .login-wrap input:focus{outline:none;border-color:var(--brass);}
        .login-wrap button{width:100%;padding:10px;border:none;border-radius:3px;
          background:var(--brass-deep);color:var(--paper);font-size:14px;
          cursor:pointer;text-transform:uppercase;letter-spacing:0.1em;}
        .login-wrap button:hover{background:var(--brass-mid);}
        .flash-err{background:rgba(227,121,95,0.15);color:var(--bad);
          border:1px solid rgba(227,121,95,0.3);padding:8px 12px;border-radius:3px;
          margin-bottom:16px;font-size:12px;}
        """ + "</style>"
        "</head><body>"
        '<div class="login-wrap">'
        '<h1>Livery <em>Dispatch</em></h1>'
        + flash_html + err_html +
        '<form method="POST" action="/admin/login">'
        '<label for="username">Username</label>'
        '<input type="text" id="username" name="username" autocomplete="username" required />'
        '<label for="password">Password</label>'
        '<input type="password" id="password" name="password" autocomplete="current-password" required />'
        '<button type="submit">Sign in</button>'
        "</form></div></body></html>"
    )


def _flash(flash: str | None) -> str:
    if not flash:
        return ""
    return f'<div class="flash" role="status">{_html.escape(flash)}</div>'


def _page_head(title: str, subtitle: str | None = None,
               back_href: str | None = None, back_label: str | None = None) -> str:
    back = ""
    if back_href:
        back = f'<a class="back-link" href="{_html.escape(back_href)}">&larr; {_html.escape(back_label or "Back")}</a>'
    sub = f'<p class="page-subtitle">{_html.escape(subtitle)}</p>' if subtitle else ""
    return (
        '<section class="page-head">'
        + back
        + f'<h1>{_html.escape(title)}</h1>'
        + sub
        + "</section>"
    )


def _filter_chips(current: str | None, options: list[tuple[str, str, int]],
                  base_url: str, param: str = "status",
                  extra_params: dict[str, str] | None = None) -> str:
    """Render a row of filter chips. `options` is [(label, value, count)];
    value=None means "All" (no query param for `param`). `current` is the
    active value. `extra_params` is preserved on every chip href so multi-
    dimensional filters (e.g. active=true & tier=1 on /admin/subcontractors)
    don't reset each other when one chip is clicked."""
    base_qs = "&".join(
        f"{_html.escape(k)}={_html.escape(v)}"
        for k, v in (extra_params or {}).items() if v
    )
    parts = []
    for label, value, count in options:
        qs_bits = []
        if value:
            qs_bits.append(f"{param}={_html.escape(value)}")
        if base_qs:
            qs_bits.append(base_qs)
        href = base_url + ("?" + "&".join(qs_bits) if qs_bits else "")
        is_active = (value == current) or (value is None and not current)
        cls = "filter-chip is-active" if is_active else "filter-chip"
        parts.append(
            f'<a class="{cls}" href="{href}">'
            f'{_html.escape(label)}<span class="filter-count">{count}</span></a>'
        )
    return '<div class="filter-row">' + "".join(parts) + "</div>"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_dt_input(dt: datetime | None) -> str:
    """ISO-ish format suitable for HTML5 datetime-local input."""
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M")


def _badge(value, cls_prefix: str = "status") -> str:
    if value is None:
        return '<span class="badge">—</span>'
    v = value.value if hasattr(value, "value") else str(value)
    label = _status_label(value)
    return f'<span class="badge {cls_prefix}-{_html.escape(v)}">{_html.escape(label)}</span>'


def _info_grid(rows: list[tuple[str, str]]) -> str:
    """Render (label, value) pairs as an info-grid. The label is escaped; the
    value is treated as pre-trusted HTML (callers pass already-escaped text or
    intentional markup like links/badges)."""
    return '<div class="info-grid">' + "".join(
        f'<div class="info-row"><div class="info-label">{_html.escape(label)}</div>'
        f'<div class="info-value">{value}</div></div>'
        for label, value in rows
    ) + "</div>"


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


_ACTIVE_TRIP_STATUSES = (
    TripStatus.REQUESTED,
    TripStatus.BROADCASTING,
    TripStatus.ACCEPTED,
    TripStatus.EN_ROUTE_PICKUP,
    TripStatus.ON_SCENE,
    TripStatus.IN_TRANSIT,
)


def trips_list_page(session: Session, status_filter: str | None = None,
                    vehicle_class_filter: str | None = None,
                    flash: str | None = None) -> str:
    # Counts per filter so the chip values are accurate.
    all_trips = list(session.scalars(select(Trip).order_by(Trip.id.desc())).all())
    by_status: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for t in all_trips:
        by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
        by_class[t.vehicle_class.value] = by_class.get(t.vehicle_class.value, 0) + 1
    active_count = sum(1 for t in all_trips if t.status in _ACTIVE_TRIP_STATUSES)

    # Apply filters. Status and vehicle_class are independent dimensions; both
    # narrow when set. Mirrors the /admin/subcontractors active+tier pattern.
    trips = all_trips
    if status_filter == "active":
        trips = [t for t in trips if t.status in _ACTIVE_TRIP_STATUSES]
    elif status_filter:
        trips = [t for t in trips if t.status.value == status_filter]
    if vehicle_class_filter:
        trips = [t for t in trips if t.vehicle_class.value == vehicle_class_filter]

    drivers_by_id = {d.id: d for d in session.scalars(select(Driver)).all()}
    subs_by_id = {s.id: s for s in session.scalars(select(Subcontractor)).all()}

    filter_options = [
        ("All", None, len(all_trips)),
        ("Active", "active", active_count),
        ("Requested", "requested", by_status.get("requested", 0)),
        ("Broadcasting", "broadcasting", by_status.get("broadcasting", 0)),
        ("Completed", "completed", by_status.get("completed", 0)),
        ("Cancelled", "cancelled", by_status.get("cancelled", 0)),
        ("No drivers", "no_drivers", by_status.get("no_drivers", 0)),
    ]
    class_options = [
        ("All classes", None, len(all_trips)),
        ("SUV", "suv", by_class.get("suv", 0)),
        ("Sprinter", "sprinter", by_class.get("sprinter", 0)),
        ("Sedan", "sedan", by_class.get("sedan", 0)),
    ]

    if not trips:
        body = '<div class="empty">No trips match this filter.</div>'
    else:
        rows = []
        for t in trips:
            assigned = "—"
            if t.assigned_driver_id and t.assigned_driver_id in drivers_by_id:
                assigned = f'<span class="tag own">{_html.escape(drivers_by_id[t.assigned_driver_id].name)}</span>'
            elif t.assigned_subcontractor_id and t.assigned_subcontractor_id in subs_by_id:
                assigned = f'<span class="tag sub">{_html.escape(subs_by_id[t.assigned_subcontractor_id].name)}</span>'
            rows.append(
                f'<tr class="clickable" onclick="window.location.href=\'/admin/trips/{t.id}\'">'
                f'<td class="id"><a href="/admin/trips/{t.id}">#{t.id}</a></td>'
                f'<td>{_badge(t.status)}</td>'
                f'<td class="time">{_html.escape(_fmt_dt(t.pickup_time))}</td>'
                f'<td class="passenger"><a href="/admin/trips/{t.id}">{_html.escape(t.passenger_name)}</a></td>'
                f'<td class="route">{_html.escape(_truncate(t.pickup_label, 28))} '
                f'<span class="arrow">→</span> {_html.escape(_truncate(t.dropoff_label, 28))}</td>'
                f'<td class="vehicle">{_html.escape(t.vehicle_class.value.upper())}</td>'
                f'<td>{assigned}</td>'
                f'<td class="fare">{_fmt_fare(t.fare_cents)}</td>'
                f'</tr>'
            )
        body = (
            '<table class="data-table"><thead><tr>'
            "<th>ID</th><th>Status</th><th>Pickup</th><th>Passenger</th>"
            "<th>Route</th><th>Class</th><th>Assigned</th><th>Fare</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    subtitle_bits = [f"{len(trips)} of {len(all_trips)} total"]
    if status_filter or vehicle_class_filter:
        filter_desc = []
        if status_filter:
            filter_desc.append(f"status={status_filter}")
        if vehicle_class_filter:
            filter_desc.append(f"class={vehicle_class_filter}")
        subtitle_bits.append(f"filtered by {' · '.join(filter_desc)}")
    return (
        _doc_open("Trips", "trips")
        + _flash(flash)
        + _page_head("Trips", subtitle=" · ".join(subtitle_bits),
                     back_href="/dashboard", back_label="Dashboard")
        + _filter_chips(status_filter, filter_options, "/admin/trips",
                        param="status",
                        extra_params={"vehicle_class": vehicle_class_filter or ""})
        + _filter_chips(vehicle_class_filter, class_options, "/admin/trips",
                        param="vehicle_class",
                        extra_params={"status": status_filter or ""})
        + '<section class="block">'
        + body
        + "</section>"
        + _doc_close()
    )


def trip_detail_page(session: Session, trip: Trip, flash: str | None = None) -> str:
    driver = session.get(Driver, trip.assigned_driver_id) if trip.assigned_driver_id else None
    sub = session.get(Subcontractor, trip.assigned_subcontractor_id) if trip.assigned_subcontractor_id else None

    # Inline-edit shortcuts: clicking a value in the info panel scrolls + focuses
    # the matching field in the edit form on the right. Each value is a button-
    # styled link with `data-focus="<input-id>"` so the JS can wire it up
    # generically. Phone is a `tel:` link; the small "directions" link opens
    # Google Maps in a new tab (kept as a secondary affordance, not the main click).
    passenger_name_html = (
        f'<a class="edit-link" data-focus="ef-passenger_name">'
        f'{_html.escape(trip.passenger_name) if trip.passenger_name else "<em>(not set — click to add)</em>"}'
        f'</a>'
    )
    phone_html = (
        f'<a class="edit-link" data-focus="ef-passenger_phone">'
        f'{_html.escape(trip.passenger_phone) if trip.passenger_phone else "<em>(not set — click to add)</em>"}'
        f'</a>'
        + (f'<br><small><a href="tel:{_html.escape(trip.passenger_phone)}" class="inline-link">call now</a></small>'
           if trip.passenger_phone else "")
    )
    pickup_html = (
        f'<a class="edit-link" data-focus="ef-pickup_label">'
        f'{_html.escape(trip.pickup_label) if trip.pickup_label else "<em>(no label — click to add)</em>"}'
        f'</a>'
        f'<br><small>{trip.pickup_lat:.5f}, {trip.pickup_lon:.5f} &middot; '
        f'<a href="https://www.google.com/maps/search/?api=1&query={trip.pickup_lat},{trip.pickup_lon}" '
        f'target="_blank" rel="noopener noreferrer" class="inline-link">directions</a></small>'
    )
    dropoff_html = (
        f'<a class="edit-link" data-focus="ef-dropoff_label">'
        f'{_html.escape(trip.dropoff_label) if trip.dropoff_label else "<em>(no label — click to add)</em>"}'
        f'</a>'
        f'<br><small>{trip.dropoff_lat:.5f}, {trip.dropoff_lon:.5f} &middot; '
        f'<a href="https://www.google.com/maps/dir/?api=1&origin={trip.pickup_lat},{trip.pickup_lon}'
        f'&destination={trip.dropoff_lat},{trip.dropoff_lon}" target="_blank" rel="noopener noreferrer" '
        f'class="inline-link">directions</a></small>'
    )
    assigned_driver_html = (
        f'<a href="/admin/drivers/{driver.id}">{_html.escape(driver.name)}</a>'
        f'<br><small><a href="tel:{_html.escape(driver.phone)}">{_html.escape(driver.phone)}</a></small>'
    ) if driver else "—"
    assigned_sub_html = (
        f'<a href="/admin/subcontractors/{sub.id}">{_html.escape(sub.name)}</a>'
        f'<br><small><a href="tel:{_html.escape(sub.dispatch_phone)}">{_html.escape(sub.dispatch_phone)}</a></small>'
    ) if sub else "—"

    info_rows = [
        ("Passenger", passenger_name_html),
        ("Phone", phone_html),
        ("Pickup", pickup_html),
        ("Dropoff", dropoff_html),
        ("Pickup time", _html.escape(_fmt_dt(trip.pickup_time))),
        ("Vehicle class", _html.escape(trip.vehicle_class.value.upper())),
        ("Status", _badge(trip.status)),
        ("Assigned driver", assigned_driver_html),
        ("Assigned sub", assigned_sub_html),
        ("Fare", _fmt_fare(trip.fare_cents)),
        ("Markup", _fmt_fare(trip.markup_cents) if trip.markup_cents is not None else "—"),
        ("Created", _html.escape(_fmt_dt(trip.created_at))),
    ]
    info_html = _info_grid(info_rows)

    # Status edit form
    status_options = "".join(
        f'<option value="{s.value}"{" selected" if s == trip.status else ""}>{_status_label(s)}</option>'
        for s in TripStatus
    )

    # Action buttons depend on current state
    action_buttons = []
    # Quote-fare form — surface the rate-card choice and peak-week toggle so
    # the dispatcher knows exactly which card they're quoting against. The
    # action handler (POST /admin/trips/{id}/action/quote) accepts both as
    # form fields; empty rate_card_id falls back to "first active for class".
    quote_cards = list(session.scalars(
        select(RateCard).where(
            RateCard.vehicle_class == trip.vehicle_class,
            RateCard.active.is_(True),
        ).order_by(RateCard.id.desc())
    ).all())
    if quote_cards:
        rate_options = '<option value="">(auto — most recent)</option>' + "".join(
            f'<option value="{rc.id}">{_html.escape(rc.name)}</option>'
            for rc in quote_cards
        )
        # Button text avoids the literal "...from..." after a <select> so
        # bandit's B608 SQL-injection regex (select\s.*from\s) doesn't
        # false-positive on this HTML template.
        quote_form_html = f'''
        <form method="post" action="/admin/trips/{trip.id}/action/quote" class="action-form quote-form">
          <label class="action-label">Rate card
            <select name="rate_card_id">{rate_options}</select>
          </label>
          <label class="action-check">
            <input type="checkbox" name="is_peak_week" value="1" />
            Peak week (apply card multiplier)
          </label>
          <button type="submit" class="secondary">Quote fare using rate card</button>
        </form>'''
    else:
        quote_form_html = (
            f'<form method="post" action="/admin/trips/{trip.id}/action/quote" class="action-form quote-form">'
            f'<p class="action-note">No active rate card for vehicle class <strong>'
            f'{_html.escape(trip.vehicle_class.value.upper())}</strong>. '
            f'<a href="/admin/rates" class="inline-link">Manage rate cards →</a></p>'
            f'<button type="submit" class="secondary" disabled>Quote fare using rate card</button>'
            f'</form>'
        )
    action_buttons.append(quote_form_html)
    if trip.status in (TripStatus.REQUESTED, TripStatus.NO_DRIVERS):
        action_buttons.append(_action_button(f"/admin/trips/{trip.id}/action/broadcast",
                                              "Broadcast to own fleet", primary=True))
        action_buttons.append(_action_button(f"/admin/trips/{trip.id}/action/broadcast-overflow",
                                              "Broadcast to subs", primary=False))
    if trip.status in (TripStatus.BROADCASTING, TripStatus.NO_DRIVERS):
        action_buttons.append(_action_button(f"/admin/trips/{trip.id}/action/broadcast-overflow",
                                              "Hand off to subs", primary=True))
    if trip.status in _ACTIVE_TRIP_STATUSES and trip.status != TripStatus.REQUESTED:
        action_buttons.append(_action_button(f"/admin/trips/{trip.id}/action/complete",
                                              "Mark complete", primary=True))
    if trip.assigned_subcontractor_id and trip.status != TripStatus.CANCELLED:
        # E9 (v1.1.18): a sub that accepted by SMS without a price leaves the
        # fare empty; this records its cost and applies the markup.
        pending = trip.fare_cents is None
        action_buttons.append(
            f'<form method="post" action="/admin/trips/{trip.id}/action/sub-cost" class="action-form">'
            f'<label class="action-label">Sub\'s quoted cost ($)'
            f'<input type="text" name="quoted_cost_dollars" inputmode="decimal" '
            f'placeholder="e.g. 450" required /></label>'
            f'<button type="submit" class="{"primary" if pending else "secondary"}">'
            f'{"Record sub cost (fare pending)" if pending else "Re-record sub cost"}</button>'
            f'</form>'
        )
    if trip.status not in (TripStatus.COMPLETED, TripStatus.CANCELLED):
        action_buttons.append(_action_button(f"/admin/trips/{trip.id}/action/cancel",
                                              "Cancel trip", primary=False, danger=True,
                                              confirm="Cancel this trip?"))

    # Broadcasts log
    broadcasts_html = ""
    if trip.broadcasts:
        bc_rows = []
        for bc in sorted(trip.broadcasts, key=lambda b: b.sent_at):
            recipient = "—"
            if bc.driver_id:
                d = session.get(Driver, bc.driver_id)
                recipient = f"Driver: {_html.escape(d.name)}" if d else f"Driver #{bc.driver_id}"
            elif bc.subcontractor_id:
                s = session.get(Subcontractor, bc.subcontractor_id)
                recipient = f"Sub: {_html.escape(s.name)}" if s else f"Sub #{bc.subcontractor_id}"
            bc_rows.append(
                "<tr>"
                f"<td>{_html.escape(_fmt_dt(bc.sent_at))}</td>"
                f"<td>{recipient}</td>"
                f"<td>{_badge(bc.response, 'response')}</td>"
                f"<td>{bc.distance_mi:.1f} mi</td>"
                "</tr>"
            )
        broadcasts_html = (
            '<section class="block"><h2>Broadcast log</h2>'
            '<table class="data-table"><thead><tr>'
            "<th>Sent</th><th>Recipient</th><th>Response</th><th>Distance</th>"
            "</tr></thead><tbody>"
            + "".join(bc_rows)
            + "</tbody></table></section>"
        )

    vclass_options = "".join(
        f'<option value="{v.value}"{" selected" if v == trip.vehicle_class else ""}>{v.value.upper()}</option>'
        for v in VehicleClass
    )

    edit_form = f'''
    <form method="post" action="/admin/trips/{trip.id}/edit" class="edit-form" id="trip-edit-form">
      <h3>Edit trip</h3>
      <label>Passenger name
        <input type="text" name="passenger_name" id="ef-passenger_name" maxlength="120"
               value="{_html.escape(trip.passenger_name or "")}" />
      </label>
      <label>Passenger phone
        <input type="tel" name="passenger_phone" id="ef-passenger_phone" maxlength="32"
               value="{_html.escape(trip.passenger_phone or "")}"
               placeholder="+19045551234" />
      </label>
      <div class="address-search">
        <label for="ef-pickup_label">Pickup label
          (<a href="#" class="search-hint" data-for="ef-pickup_label">search address</a>)
          <input type="text" name="pickup_label" id="ef-pickup_label" maxlength="255"
                 autocomplete="off" spellcheck="false"
                 value="{_html.escape(trip.pickup_label or "")}" />
        </label>
        <ul class="suggestions" id="ef-pickup_label-suggestions" hidden role="listbox"></ul>
      </div>
      <div class="coords-row-2">
        <label>Pickup lat
          <input type="number" step="any" name="pickup_lat" id="ef-pickup_lat"
                 value="{trip.pickup_lat}" />
        </label>
        <label>Pickup lng
          <input type="number" step="any" name="pickup_lon" id="ef-pickup_lon"
                 value="{trip.pickup_lon}" />
        </label>
      </div>
      <div class="address-search">
        <label for="ef-dropoff_label">Dropoff label
          (<a href="#" class="search-hint" data-for="ef-dropoff_label">search address</a>)
          <input type="text" name="dropoff_label" id="ef-dropoff_label" maxlength="255"
                 autocomplete="off" spellcheck="false"
                 value="{_html.escape(trip.dropoff_label or "")}" />
        </label>
        <ul class="suggestions" id="ef-dropoff_label-suggestions" hidden role="listbox"></ul>
      </div>
      <div class="coords-row-2">
        <label>Dropoff lat
          <input type="number" step="any" name="dropoff_lat" id="ef-dropoff_lat"
                 value="{trip.dropoff_lat}" />
        </label>
        <label>Dropoff lng
          <input type="number" step="any" name="dropoff_lon" id="ef-dropoff_lon"
                 value="{trip.dropoff_lon}" />
        </label>
      </div>
      <label>Pickup time
        <input type="datetime-local" name="pickup_time" id="ef-pickup_time"
               value="{_fmt_dt_input(trip.pickup_time)}" />
      </label>
      <label>Vehicle class
        <select name="vehicle_class">{vclass_options}</select>
      </label>
      <label>Status
        <select name="status" required>{status_options}</select>
      </label>
      <label>Fare (cents)
        <input type="number" name="fare_cents" min="0"
               value="{trip.fare_cents if trip.fare_cents is not None else ''}" />
      </label>
      <label>Notes
        <textarea name="notes" rows="3" maxlength="500">{_html.escape(trip.notes or "")}</textarea>
      </label>
      <div class="form-actions">
        <button type="submit" class="primary">Save changes</button>
      </div>
    </form>
    '''

    return _csp.inject_nonce(
        _doc_open(f"Trip #{trip.id}", "trips")
        + _flash(flash)
        + _page_head(f"Trip #{trip.id} — {trip.passenger_name or '(no name)'}",
                     subtitle=f"{trip.vehicle_class.value.upper()} · {trip.pickup_label or '(no pickup)'} → {trip.dropoff_label or '(no dropoff)'}",
                     back_href="/admin/trips", back_label="All trips")
        + '<section class="two-col">'
        + '<div class="col">' + '<section class="block"><h2>Trip details</h2>' + info_html + "</section>"
        + broadcasts_html
        + '</div>'
        + '<div class="col">'
        + '<section class="block"><h2>Actions</h2>' + '<div class="action-stack">' + "".join(action_buttons) + "</div></section>"
        + '<section class="block">' + edit_form + "</section>"
        + '</div>'
        + '</section>'
        + _TRIP_DETAIL_JS
        + _doc_close()
    )


# Inline JS for the trip detail page — click-to-focus on the left info panel
# and Nominatim forward-geocoding autocomplete on the pickup_label and
# dropoff_label inputs in the right edit form. Same pattern as map_view.py's
# bindAddressSearch, but adapted to update the form's hidden lat/lng inputs
# (ef-pickup_lat / ef-pickup_lon / ef-dropoff_lat / ef-dropoff_lon) when a
# suggestion is picked, so a single form submit carries label + coords together.
_TRIP_DETAIL_JS = """
<script __CSP_NONCE_ATTR__>
(function () {
  'use strict';

  // ---- Click-to-focus: info-panel value -> right-side edit field ----
  document.querySelectorAll('a.edit-link[data-focus]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      var target = document.getElementById(a.dataset.focus);
      if (!target) return;
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setTimeout(function () { target.focus(); target.select && target.select(); }, 280);
    });
  });
  document.querySelectorAll('a.search-hint[data-for]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      var target = document.getElementById(a.dataset.for);
      if (target) { target.focus(); target.select && target.select(); }
    });
  });

  // ---- Nominatim autocomplete on pickup_label / dropoff_label ----
  function bindAddressSearch(input, suggestionsEl, latInput, lonInput) {
    if (!input || !suggestionsEl) return;
    var debounceTimer = null;
    var activeIndex = -1;
    var lastQuery = '';

    function clearSuggestions() {
      suggestionsEl.innerHTML = '';
      suggestionsEl.hidden = true;
      activeIndex = -1;
    }
    function renderResults(results) {
      if (!results || !results.length) {
        suggestionsEl.innerHTML = '<li class="empty">No matches. Try a more specific address.</li>';
        suggestionsEl.hidden = false;
        return;
      }
      suggestionsEl.innerHTML = results.map(function (r) {
        var parts = (r.display_name || '').split(',').map(function (s) { return s.trim(); });
        var primary = parts.slice(0, 2).join(', ');
        var secondary = parts.slice(2, 5).join(', ');
        var compact = parts.slice(0, 3).join(', ');
        return '<li role="option" data-lat="' + r.lat + '" data-lon="' + r.lon
          + '" data-name="' + escapeAttr(compact) + '">'
          + escapeHtml(primary)
          + (secondary ? '<small>' + escapeHtml(secondary) + '</small>' : '')
          + '</li>';
      }).join('');
      suggestionsEl.hidden = false;
      activeIndex = -1;
    }
    function doSearch(q) {
      if (q.length < 3) { clearSuggestions(); return; }
      if (q === lastQuery) return;
      lastQuery = q;
      var url = 'https://nominatim.openstreetmap.org/search?format=json&limit=6'
        + '&countrycodes=us&q=' + encodeURIComponent(q);
      fetch(url, { headers: { 'Accept-Language': 'en' } })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(renderResults)
        .catch(function () {
          suggestionsEl.innerHTML = '<li class="empty">Search unavailable.</li>';
          suggestionsEl.hidden = false;
        });
    }

    input.addEventListener('input', function () {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { doSearch(input.value.trim()); }, 280);
    });
    input.addEventListener('keydown', function (e) {
      var items = suggestionsEl.querySelectorAll('li:not(.empty)');
      if (e.key === 'ArrowDown' && items.length) {
        e.preventDefault();
        activeIndex = (activeIndex + 1) % items.length;
        updateActive(items);
      } else if (e.key === 'ArrowUp' && items.length) {
        e.preventDefault();
        activeIndex = (activeIndex - 1 + items.length) % items.length;
        updateActive(items);
      } else if (e.key === 'Enter') {
        if (activeIndex >= 0 && items[activeIndex]) {
          e.preventDefault();
          items[activeIndex].click();
        }
      } else if (e.key === 'Escape') {
        clearSuggestions();
      }
    });
    function updateActive(items) {
      items.forEach(function (li, i) { li.classList.toggle('is-active', i === activeIndex); });
      if (activeIndex >= 0 && items[activeIndex]) {
        items[activeIndex].scrollIntoView({ block: 'nearest' });
      }
    }
    suggestionsEl.addEventListener('click', function (e) {
      var li = e.target.closest('li');
      if (!li || li.classList.contains('empty')) return;
      var lat = parseFloat(li.dataset.lat);
      var lon = parseFloat(li.dataset.lon);
      var name = li.dataset.name;
      if (isNaN(lat) || isNaN(lon)) return;
      input.value = name;
      if (latInput) latInput.value = lat.toFixed(6);
      if (lonInput) lonInput.value = lon.toFixed(6);
      clearSuggestions();
    });
    document.addEventListener('click', function (e) {
      if (!input.contains(e.target) && !suggestionsEl.contains(e.target)) {
        suggestionsEl.hidden = true;
      }
    });
  }

  bindAddressSearch(
    document.getElementById('ef-pickup_label'),
    document.getElementById('ef-pickup_label-suggestions'),
    document.getElementById('ef-pickup_lat'),
    document.getElementById('ef-pickup_lon')
  );
  bindAddressSearch(
    document.getElementById('ef-dropoff_label'),
    document.getElementById('ef-dropoff_label-suggestions'),
    document.getElementById('ef-dropoff_lat'),
    document.getElementById('ef-dropoff_lon')
  );

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c];
    });
  }
  function escapeAttr(s) {
    return String(s == null ? '' : s).replace(/"/g, '&quot;');
  }
})();
</script>
"""


def _action_button(action_url: str, label: str, *, primary: bool = False,
                   danger: bool = False, confirm: str | None = None) -> str:
    cls = "primary" if primary else ("danger" if danger else "secondary")
    confirm_attr = f' onclick="return confirm(\'{_html.escape(confirm)}\')"' if confirm else ""
    return (
        f'<form method="post" action="{_html.escape(action_url)}" class="action-form">'
        f'<button type="submit" class="{cls}"{confirm_attr}>{_html.escape(label)}</button>'
        '</form>'
    )


def _inline_action_button(action_url: str, label: str, *, primary: bool = False,
                          danger: bool = False, confirm: str | None = None,
                          return_to: str | None = None) -> str:
    cls = "primary" if primary else ("danger" if danger else "secondary")
    confirm_attr = f' onclick="return confirm(\'{_html.escape(confirm)}\')"' if confirm else ""
    return_field = (f'<input type="hidden" name="return_to" value="{_html.escape(return_to)}" />'
                    if return_to else "")
    return (
        f'<form method="post" action="{_html.escape(action_url)}" class="inline-action-form">'
        f'{return_field}'
        f'<button type="submit" class="{cls}"{confirm_attr}>{_html.escape(label)}</button>'
        '</form>'
    )


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def reservations_list_page(session: Session, status_filter: str | None = None,
                           flash: str | None = None) -> str:
    all_r = list(session.scalars(select(Reservation).order_by(Reservation.id.desc())).all())
    by_status: dict[str, int] = {}
    for r in all_r:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1

    if status_filter:
        rows = [r for r in all_r if r.status.value == status_filter]
    else:
        # Default: hide spam.
        rows = [r for r in all_r if r.status != ReservationStatus.SPAM]

    filter_options = [
        ("Pending", "pending_review", by_status.get("pending_review", 0)),
        ("Confirmed", "confirmed", by_status.get("confirmed", 0)),
        ("Converted", "converted", by_status.get("converted", 0)),
        ("Declined", "declined", by_status.get("declined", 0)),
        ("Spam", "spam", by_status.get("spam", 0)),
        ("All (excl. spam)", None, sum(1 for r in all_r if r.status != ReservationStatus.SPAM)),
    ]

    has_pending = any(r.status == ReservationStatus.PENDING_REVIEW for r in rows)

    if not rows:
        body = '<div class="empty">No reservations match this filter.</div>'
    else:
        tr_rows = []
        for r in rows:
            when = (r.pickup_at and _fmt_dt(r.pickup_at)) or f"{r.pickup_date or '—'} {r.pickup_time_str or ''}".strip()
            checkbox = (
                f'<input type="checkbox" name="ids" value="{r.id}" class="bulk-check" />'
                if r.status == ReservationStatus.PENDING_REVIEW else ""
            )
            if r.status == ReservationStatus.PENDING_REVIEW:
                actions_cell = (
                    _inline_action_button(f"/admin/reservations/{r.id}/action/confirm",
                                          "Confirm", primary=True, return_to="list")
                    + _inline_action_button(f"/admin/reservations/{r.id}/action/decline",
                                            "Decline", danger=True, return_to="list",
                                            confirm="Decline this reservation?")
                    + _inline_action_button(f"/admin/reservations/{r.id}/action/spam",
                                            "Spam", danger=True, return_to="list",
                                            confirm="Mark this reservation as spam?")
                )
            else:
                actions_cell = '<span class="muted">—</span>'
            tr_rows.append(
                f'<tr class="clickable" onclick="window.location.href=\'/admin/reservations/{r.id}\'">'
                f'<td class="bulk-col" onclick="event.stopPropagation()">{checkbox}</td>'
                f'<td class="id"><a href="/admin/reservations/{r.id}">#{r.id}</a></td>'
                f'<td>{_badge(r.status)}</td>'
                f'<td class="passenger"><a href="/admin/reservations/{r.id}">{_html.escape(r.name)}</a></td>'
                f'<td class="route">{_html.escape(_truncate(r.pickup_label, 22))} '
                f'<span class="arrow">→</span> {_html.escape(_truncate(r.dropoff_label, 22))}</td>'
                f'<td class="time">{_html.escape(when)}</td>'
                f'<td>{_html.escape(r.passengers or "—")}</td>'
                f'<td>{_html.escape(r.vehicle_pref or "—")}</td>'
                f'<td>{_html.escape(_fmt_dt(r.created_at))}</td>'
                f'<td class="actions" onclick="event.stopPropagation()">{actions_cell}</td>'
                f'</tr>'
            )
        body = (
            '<table class="data-table" id="reservations-table"><thead><tr>'
            '<th class="bulk-col"><input type="checkbox" id="bulk-select-all" title="Select all" /></th>'
            "<th>ID</th><th>Status</th><th>Name</th><th>Route</th><th>When</th>"
            "<th>Pax</th><th>Pref</th><th>Received</th><th>Actions</th>"
            "</tr></thead><tbody>"
            + "".join(tr_rows)
            + "</tbody></table>"
        )

    bulk_bar = ""
    if has_pending:
        bulk_bar = (
            '<div class="bulk-bar" id="bulk-bar" hidden>'
            '<span id="bulk-count">0</span> selected · '
            '<button type="button" class="danger" id="bulk-spam-btn">'
            'Decline selected as spam</button>'
            '</div>'
        )

    bulk_js = """
<script __CSP_NONCE_ATTR__>
(function () {
  var selectAll = document.getElementById('bulk-select-all');
  var checks = document.querySelectorAll('.bulk-check');
  var bar = document.getElementById('bulk-bar');
  var countEl = document.getElementById('bulk-count');
  var btn = document.getElementById('bulk-spam-btn');
  if (!selectAll || !checks.length || !bar) return;

  function updateBar() {
    var n = document.querySelectorAll('.bulk-check:checked').length;
    countEl.textContent = n;
    bar.hidden = (n === 0);
  }
  selectAll.addEventListener('change', function () {
    checks.forEach(function (c) { c.checked = selectAll.checked; });
    updateBar();
  });
  checks.forEach(function (c) { c.addEventListener('change', updateBar); });

  btn.addEventListener('click', function () {
    var ids = [];
    document.querySelectorAll('.bulk-check:checked').forEach(function (c) {
      ids.push(c.value);
    });
    if (!ids.length) return;
    if (!confirm('Mark ' + ids.length + ' reservation(s) as spam?')) return;
    var form = document.createElement('form');
    form.method = 'POST';
    form.action = '/admin/reservations/bulk-spam';
    ids.forEach(function (id) {
      var inp = document.createElement('input');
      inp.type = 'hidden'; inp.name = 'ids'; inp.value = id;
      form.appendChild(inp);
    });
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (csrfMeta) {
      var ci = document.createElement('input');
      ci.type = 'hidden'; ci.name = 'csrf_token'; ci.value = csrfMeta.content;
      form.appendChild(ci);
    }
    document.body.appendChild(form);
    form.submit();
  });
})();
</script>
""" if has_pending else ""

    return _csp.inject_nonce(
        _doc_open("Reservations", "reservations")
        + _flash(flash)
        + _page_head("Reservations", subtitle=f"{len(rows)} shown",
                     back_href="/dashboard", back_label="Dashboard")
        + _filter_chips(status_filter, filter_options, "/admin/reservations")
        + bulk_bar
        + '<section class="block">' + body + "</section>"
        + bulk_js
        + _doc_close()
    )


def reservation_detail_page(session: Session, r: Reservation, flash: str | None = None) -> str:
    when = (r.pickup_at and _fmt_dt(r.pickup_at)) or f"{r.pickup_date or '—'} {r.pickup_time_str or ''}".strip()
    info_rows = [
        ("Name", _html.escape(r.name)),
        ("Email", f'<a href="mailto:{_html.escape(r.email)}">{_html.escape(r.email)}</a>'),
        ("Phone", f'<a href="tel:{_html.escape(r.phone)}">{_html.escape(r.phone)}</a>'),
        ("Pickup", _html.escape(r.pickup_label)),
        ("Dropoff", _html.escape(r.dropoff_label)),
        ("When", _html.escape(when)),
        ("Passengers", _html.escape(r.passengers or "—")),
        ("Vehicle preference", _html.escape(r.vehicle_pref or "—")),
        ("Flight", _html.escape(r.flight or "—")),
        ("Status", _badge(r.status)),
        ("Source", _html.escape(r.source or "—")),
        ("Received", _html.escape(_fmt_dt(r.created_at))),
        ("Linked trip", f'<a href="/trips/{r.linked_trip_id}">#{r.linked_trip_id}</a>' if r.linked_trip_id else "—"),
    ]
    info_html = _info_grid(info_rows)

    notes_html = f'<div class="notes-block"><h3>Guest notes</h3><p>{_html.escape(r.notes or "(none)")}</p></div>'

    status_options = "".join(
        f'<option value="{s.value}"{" selected" if s == r.status else ""}>{_status_label(s)}</option>'
        for s in ReservationStatus
    )

    edit_form = f'''
    <form method="post" action="/admin/reservations/{r.id}/edit" class="edit-form">
      <h3>Edit reservation</h3>
      <label>Status
        <select name="status" required>{status_options}</select>
      </label>
      <label>Linked trip ID (when promoting)
        <input type="number" name="linked_trip_id" min="1"
               value="{r.linked_trip_id or ''}" placeholder="(none)" />
      </label>
      <label>Dispatcher notes
        <textarea name="dispatcher_notes" rows="4" maxlength="2000">{_html.escape(r.dispatcher_notes or "")}</textarea>
      </label>
      <div class="form-actions">
        <button type="submit" class="primary">Save changes</button>
      </div>
    </form>
    '''

    action_buttons = []
    if r.status == ReservationStatus.PENDING_REVIEW:
        action_buttons.append(_action_button(f"/admin/reservations/{r.id}/action/confirm",
                                              "Mark confirmed", primary=True))
        action_buttons.append(_action_button(f"/admin/reservations/{r.id}/action/decline",
                                              "Decline", primary=False, danger=True,
                                              confirm="Decline this reservation?"))
        action_buttons.append(_action_button(f"/admin/reservations/{r.id}/action/spam",
                                              "Mark spam", primary=False, danger=True,
                                              confirm="Mark this reservation as spam?"))
    # Promote-to-Trip is available pre-conversion (anything that's not spam,
    # declined, or already converted). The dispatcher reviews the pre-filled
    # form before submitting — no auto-conversion.
    if r.status in (ReservationStatus.PENDING_REVIEW, ReservationStatus.CONFIRMED):
        action_buttons.append(
            f'<a class="action-link primary" href="/admin/reservations/{r.id}/promote">'
            f'Promote to Trip</a>'
        )

    return (
        _doc_open(f"Reservation #{r.id}", "reservations")
        + _flash(flash)
        + _page_head(f"Reservation #{r.id} — {r.name}",
                     subtitle=f"{r.pickup_label} → {r.dropoff_label} · {when}",
                     back_href="/admin/reservations", back_label="All reservations")
        + '<section class="two-col">'
        + '<div class="col">'
        + '<section class="block"><h2>Reservation</h2>' + info_html + "</section>"
        + ('<section class="block">' + notes_html + "</section>" if r.notes else "")
        + '</div>'
        + '<div class="col">'
        + ('<section class="block"><h2>Actions</h2><div class="action-stack">' + "".join(action_buttons) + "</div></section>" if action_buttons else "")
        + '<section class="block">' + edit_form + "</section>"
        + '</div>'
        + '</section>'
        + _doc_close()
    )


def promote_to_trip_form_page(session: Session, r: Reservation, flash: str | None = None) -> str:
    """Render a trip-create form pre-populated from a reservation.

    The dispatcher fills in what the reservation can't carry (lat/lon
    coordinates and a confirmed pickup_time when only date+time strings
    were captured) and clicks Submit to create the Trip and link it back
    to the reservation. Matches Year-1's human-in-the-loop posture — the
    dispatcher reviews + confirms before anything commits.
    """
    # Pickup time pre-fill: prefer the parsed datetime, else stitch the
    # raw date + time strings, else leave blank.
    if r.pickup_at is not None:
        pickup_time_val = r.pickup_at.strftime("%Y-%m-%dT%H:%M")
    elif r.pickup_date and r.pickup_time_str:
        pickup_time_val = f"{r.pickup_date}T{r.pickup_time_str}"
    else:
        pickup_time_val = ""

    vehicle_value = (r.vehicle_class.value if r.vehicle_class is not None else "suv")
    vehicle_options = "".join(
        f'<option value="{v.value}"{" selected" if v.value == vehicle_value else ""}>'
        f'{v.value.upper()}</option>'
        for v in VehicleClass
    )

    notes_value = _html.escape(r.notes or "")

    form_html = f'''
    <form method="post" action="/admin/reservations/{r.id}/promote" class="edit-form promote-form">
      <p class="small-meta">Pre-filled from reservation #{r.id}. Review each field, then click
      <strong>Create trip &amp; link</strong> below. The reservation will be marked
      <code>converted</code> and linked to the new Trip.</p>

      <fieldset>
        <legend>Passenger</legend>
        <label>Name
          <input type="text" name="passenger_name" required maxlength="120"
                 value="{_html.escape(r.name)}" />
        </label>
        <label>Phone
          <input type="tel" name="passenger_phone" required maxlength="32"
                 value="{_html.escape(r.phone)}" />
        </label>
      </fieldset>

      <fieldset>
        <legend>Pickup</legend>
        <label>Label
          <input type="text" name="pickup_label" required maxlength="255"
                 value="{_html.escape(r.pickup_label)}" />
        </label>
        <div class="coords-row-2">
          <label>Lat
            <input type="number" step="any" name="pickup_lat" required
                   placeholder="30.4941" />
          </label>
          <label>Lon
            <input type="number" step="any" name="pickup_lon" required
                   placeholder="-81.6879" />
          </label>
        </div>
        <p class="small-meta">Coordinates are required. Use the <a href="/map">live map</a>
        new-trip flow for click-to-set coords, or look them up by name.</p>
      </fieldset>

      <fieldset>
        <legend>Dropoff</legend>
        <label>Label
          <input type="text" name="dropoff_label" required maxlength="255"
                 value="{_html.escape(r.dropoff_label)}" />
        </label>
        <div class="coords-row-2">
          <label>Lat
            <input type="number" step="any" name="dropoff_lat" required
                   placeholder="31.1969" />
          </label>
          <label>Lon
            <input type="number" step="any" name="dropoff_lon" required
                   placeholder="-81.3438" />
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Trip</legend>
        <label>Pickup time (local)
          <input type="datetime-local" name="pickup_time" required
                 value="{_html.escape(pickup_time_val)}" />
        </label>
        <label>Vehicle class
          <select name="vehicle_class" required>{vehicle_options}</select>
        </label>
        <label>Notes
          <textarea name="notes" rows="3" maxlength="500">{notes_value}</textarea>
        </label>
      </fieldset>

      <div class="form-actions">
        <button type="submit" class="primary">Create trip &amp; link</button>
      </div>
    </form>
    '''

    return (
        _doc_open(f"Promote reservation #{r.id}", "reservations")
        + _flash(flash)
        + _page_head(f"Promote reservation #{r.id} → new Trip",
                     subtitle=f"{r.name} · {r.pickup_label} → {r.dropoff_label}",
                     back_href=f"/admin/reservations/{r.id}",
                     back_label=f"Reservation #{r.id}")
        + '<section class="block">' + form_html + "</section>"
        + _doc_close()
    )


# ---------------------------------------------------------------------------
# Inquiries
# ---------------------------------------------------------------------------


def inquiries_list_page(session: Session, status_filter: str | None = None,
                        kind_filter: str | None = None,
                        search: str | None = None,
                        flash: str | None = None) -> str:
    all_i = list(session.scalars(select(Inquiry).order_by(Inquiry.id.desc())).all())
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for i in all_i:
        by_status[i.status.value] = by_status.get(i.status.value, 0) + 1
        by_kind[i.kind.value] = by_kind.get(i.kind.value, 0) + 1

    rows = all_i
    if status_filter:
        rows = [i for i in rows if i.status.value == status_filter]
    else:
        rows = [i for i in rows if i.status != InquiryStatus.SPAM]
    if kind_filter:
        rows = [i for i in rows if i.kind.value == kind_filter]
    if search:
        q = search.lower()
        rows = [i for i in rows if q in i.name.lower()
                or q in i.email.lower()
                or (i.notes and q in i.notes.lower())]

    status_extra = {}
    if kind_filter:
        status_extra["kind"] = kind_filter
    if search:
        status_extra["q"] = search
    filter_options = [
        ("Pending", "pending_review", by_status.get("pending_review", 0)),
        ("Responded", "responded", by_status.get("responded", 0)),
        ("Closed", "closed", by_status.get("closed", 0)),
        ("Spam", "spam", by_status.get("spam", 0)),
        ("All (excl. spam)", None, sum(1 for i in all_i if i.status != InquiryStatus.SPAM)),
    ]

    kind_extra = {}
    if status_filter:
        kind_extra["status"] = status_filter
    if search:
        kind_extra["q"] = search
    kind_options = [
        (k.value.replace("_", " ").title(), k.value, by_kind.get(k.value, 0))
        for k in InquiryKind
    ]
    kind_options.append(("All kinds", None, len(all_i)))

    search_qs_parts = []
    if status_filter:
        search_qs_parts.append(f"status={_html.escape(status_filter)}")
    if kind_filter:
        search_qs_parts.append(f"kind={_html.escape(kind_filter)}")
    search_qs = "&".join(search_qs_parts)
    search_form = (
        f'<form method="get" action="/admin/inquiries" class="search-form">'
        + (f'<input type="hidden" name="status" value="{_html.escape(status_filter)}" />' if status_filter else "")
        + (f'<input type="hidden" name="kind" value="{_html.escape(kind_filter)}" />' if kind_filter else "")
        + f'<input type="search" name="q" placeholder="Search name, email, notes…" '
        + f'value="{_html.escape(search or "")}" />'
        + f'<button type="submit" class="secondary">Search</button>'
        + (f' <a class="inline-link" href="/admin/inquiries{"?" + search_qs if search_qs else ""}"'
           f' style="margin-left:6px">Clear</a>' if search else "")
        + '</form>'
    )

    if not rows:
        body = '<div class="empty">No inquiries match this filter.</div>'
    else:
        tr_rows = []
        for i in rows:
            detail = ""
            if i.details_json:
                try:
                    d = json.loads(i.details_json)
                    detail = ", ".join(f"{k}: {v}" for k, v in d.items() if v)[:100]
                except (ValueError, TypeError):
                    detail = ""
            tr_rows.append(
                f'<tr class="clickable" onclick="window.location.href=\'/admin/inquiries/{i.id}\'">'
                f'<td class="id"><a href="/admin/inquiries/{i.id}">#{i.id}</a></td>'
                f'<td>{_badge(i.kind, "kind")}</td>'
                f'<td>{_badge(i.status)}</td>'
                f'<td class="passenger"><a href="/admin/inquiries/{i.id}">{_html.escape(i.name)}</a></td>'
                f'<td>{_html.escape(i.email)}</td>'
                f'<td class="time">{_html.escape(_fmt_dt(i.created_at))}</td>'
                f'<td>{_html.escape(detail)}</td>'
                f'</tr>'
            )
        body = (
            '<table class="data-table"><thead><tr>'
            "<th>ID</th><th>Kind</th><th>Status</th><th>Name</th><th>Email</th>"
            "<th>Received</th><th>Detail</th>"
            "</tr></thead><tbody>"
            + "".join(tr_rows)
            + "</tbody></table>"
        )

    return (
        _doc_open("Inquiries", "inquiries")
        + _flash(flash)
        + _page_head("Inquiries", subtitle=f"{len(rows)} shown",
                     back_href="/dashboard", back_label="Dashboard")
        + _filter_chips(status_filter, filter_options, "/admin/inquiries",
                        extra_params=status_extra)
        + _filter_chips(kind_filter, kind_options, "/admin/inquiries",
                        param="kind", extra_params=kind_extra)
        + search_form
        + '<section class="block">' + body + "</section>"
        + _doc_close()
    )


def inquiry_detail_page(session: Session, i: Inquiry, flash: str | None = None) -> str:
    details = None
    if i.details_json:
        try:
            details = json.loads(i.details_json)
        except (ValueError, TypeError):
            details = None

    info_rows = [
        ("Kind", _badge(i.kind, "kind")),
        ("Name", _html.escape(i.name)),
        ("Email", f'<a href="mailto:{_html.escape(i.email)}">{_html.escape(i.email)}</a>'),
        ("Phone", (f'<a href="tel:{_html.escape(i.phone)}">{_html.escape(i.phone)}</a>' if i.phone else "—")),
        ("Status", _badge(i.status)),
        ("Source", _html.escape(i.source or "—")),
        ("Received", _html.escape(_fmt_dt(i.created_at))),
    ]
    info_html = _info_grid(info_rows)

    details_html = ""
    if details:
        d_rows = "".join(
            f'<div class="info-row"><div class="info-label">{_html.escape(str(k))}</div>'
            f'<div class="info-value">{_html.escape(str(v))}</div></div>'
            for k, v in details.items()
        )
        details_html = '<section class="block"><h2>Kind-specific details</h2><div class="info-grid">' + d_rows + "</div></section>"

    notes_html = ""
    if i.notes:
        notes_html = f'<section class="block"><h3>Submitter notes</h3><p>{_html.escape(i.notes)}</p></section>'

    status_options = "".join(
        f'<option value="{s.value}"{" selected" if s == i.status else ""}>{_status_label(s)}</option>'
        for s in InquiryStatus
    )

    edit_form = f'''
    <form method="post" action="/admin/inquiries/{i.id}/edit" class="edit-form">
      <h3>Edit inquiry</h3>
      <label>Status
        <select name="status" required>{status_options}</select>
      </label>
      <label>Dispatcher notes
        <textarea name="dispatcher_notes" rows="4" maxlength="2000">{_html.escape(i.dispatcher_notes or "")}</textarea>
      </label>
      <div class="form-actions">
        <button type="submit" class="primary">Save changes</button>
      </div>
    </form>
    '''

    action_buttons = []
    if i.status == InquiryStatus.PENDING_REVIEW:
        action_buttons.append(_action_button(f"/admin/inquiries/{i.id}/action/responded",
                                              "Mark responded", primary=True))
    if i.status != InquiryStatus.CLOSED:
        action_buttons.append(_action_button(f"/admin/inquiries/{i.id}/action/closed",
                                              "Close inquiry", primary=False))

    return (
        _doc_open(f"Inquiry #{i.id}", "inquiries")
        + _flash(flash)
        + _page_head(f"Inquiry #{i.id} — {i.name}",
                     subtitle=f"{i.kind.value.replace('_', ' ').title()}",
                     back_href="/admin/inquiries", back_label="All inquiries")
        + '<section class="two-col">'
        + '<div class="col">'
        + '<section class="block"><h2>Inquiry</h2>' + info_html + "</section>"
        + details_html
        + notes_html
        + '</div>'
        + '<div class="col">'
        + ('<section class="block"><h2>Actions</h2><div class="action-stack">' + "".join(action_buttons) + "</div></section>" if action_buttons else "")
        + '<section class="block">' + edit_form + "</section>"
        + '</div>'
        + '</section>'
        + _doc_close()
    )


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


def drivers_list_page(session: Session, status_filter: str | None = None,
                      flash: str | None = None) -> str:
    all_drivers = list(session.scalars(select(Driver).order_by(Driver.name.asc())).all())
    by_status: dict[str, int] = {}
    for d in all_drivers:
        by_status[d.status.value] = by_status.get(d.status.value, 0) + 1

    if status_filter:
        drivers = [d for d in all_drivers if d.status.value == status_filter]
    else:
        drivers = all_drivers

    filter_options = [
        ("All", None, len(all_drivers)),
        ("Available", "available", by_status.get("available", 0)),
        ("On trip", "on_trip", by_status.get("on_trip", 0)),
        ("Off duty", "off_duty", by_status.get("off_duty", 0)),
        ("Offline", "offline", by_status.get("offline", 0)),
    ]

    rows = []
    for d in drivers:
        loc = f"{d.lat:.3f}, {d.lon:.3f}" if d.lat is not None and d.lon is not None else "no fix"
        rows.append(
            f'<tr class="clickable" onclick="window.location.href=\'/admin/drivers/{d.id}\'">'
            f'<td class="id"><a href="/admin/drivers/{d.id}">#{d.id}</a></td>'
            f'<td class="passenger"><a href="/admin/drivers/{d.id}">{_html.escape(d.name)}</a></td>'
            f'<td>{_badge(d.status)}</td>'
            f'<td class="vehicle">{_html.escape(d.vehicle_classes.upper())}</td>'
            f'<td class="phone">{_html.escape(d.phone)}</td>'
            f'<td class="time">{_html.escape(loc)}</td>'
            f'</tr>'
        )
    body = (
        '<table class="data-table"><thead><tr>'
        "<th>ID</th><th>Name</th><th>Status</th><th>Classes</th><th>Phone</th><th>Last fix</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    ) if rows else '<div class="empty">No drivers match this filter.</div>'
    return (
        _doc_open("Drivers", "drivers")
        + _flash(flash)
        + _page_head("Drivers", subtitle=f"{len(drivers)} of {len(all_drivers)} on roster",
                     back_href="/dashboard", back_label="Dashboard")
        + _filter_chips(status_filter, filter_options, "/admin/drivers")
        + '<section class="block">' + body + "</section>"
        + _doc_close()
    )


def driver_detail_page(session: Session, d: Driver, flash: str | None = None) -> str:
    info_rows = [
        ("Name", _html.escape(d.name)),
        ("Phone", f'<a href="tel:{_html.escape(d.phone)}">{_html.escape(d.phone)}</a>'),
        ("Status", _badge(d.status)),
        ("Vehicle classes", _html.escape(d.vehicle_classes.upper())),
        ("Last lat / lon", f"{d.lat:.5f}, {d.lon:.5f}" if d.lat is not None and d.lon is not None else "no fix"),
        ("Updated", _html.escape(_fmt_dt(d.updated_at))),
    ]
    info_html = _info_grid(info_rows)

    status_options = "".join(
        f'<option value="{s.value}"{" selected" if s == d.status else ""}>{_status_label(s)}</option>'
        for s in DriverStatus
    )

    edit_form = f'''
    <form method="post" action="/admin/drivers/{d.id}/edit" class="edit-form">
      <h3>Edit driver</h3>
      <label>Status
        <select name="status" required>{status_options}</select>
      </label>
      <label>Vehicle classes (comma-separated, e.g. <code>suv,sprinter</code>)
        <input type="text" name="vehicle_classes" required maxlength="64"
               value="{_html.escape(d.vehicle_classes)}" />
      </label>
      <div class="coords-row-2">
        <label>Lat
          <input type="number" step="any" name="lat"
                 value="{d.lat if d.lat is not None else ''}" />
        </label>
        <label>Lng
          <input type="number" step="any" name="lon"
                 value="{d.lon if d.lon is not None else ''}" />
        </label>
      </div>
      <div class="form-actions">
        <button type="submit" class="primary">Save changes</button>
      </div>
    </form>
    '''

    # Driver access token block (v0.9.9 — Phase 4.5 walking skeleton).
    # The token is the bearer credential the chauffeur uses to sign into
    # the /driver web surface. Surfacing it here is what makes onboarding
    # a chauffeur a single step: hand them the URL + token, they paste it
    # into /driver/login.
    token_display = d.auth_token if d.auth_token else "(not yet generated)"
    token_block = f'''
    <section class="block">
      <h2>Access token</h2>
      <p style="margin:0 0 8px;color:rgba(251,246,232,0.7);font-size:12.5px">
        Hand this token to the chauffeur for /driver/login. Rotate if a phone
        is lost or the token leaks; the old value is invalidated immediately.
      </p>
      <code style="display:block;padding:10px 12px;background:rgba(255,255,255,0.04);
                   border:1px solid var(--rule);border-radius:2px;
                   font:11.5px/1.4 'Lucida Console',Consolas,monospace;
                   color:var(--brass-hi);word-break:break-all">{_html.escape(token_display)}</code>
      <form method="post" action="/admin/drivers/{d.id}/action/rotate-token"
            class="action-form" style="margin-top:12px">
        <button type="submit" class="secondary"
                onclick="return confirm('Rotate the token? The current value will stop working immediately.')">
          Rotate token
        </button>
      </form>
    </section>
    '''

    return (
        _doc_open(f"Driver #{d.id}", "drivers")
        + _flash(flash)
        + _page_head(f"{d.name}", subtitle=f"Driver #{d.id} · {d.phone}",
                     back_href="/admin/drivers", back_label="All drivers")
        + '<section class="two-col">'
        + '<div class="col">'
        + '<section class="block"><h2>Driver</h2>' + info_html + "</section>"
        + token_block
        + '</div>'
        + '<div class="col"><section class="block">' + edit_form + "</section></div>"
        + '</section>'
        + _doc_close()
    )


# ---------------------------------------------------------------------------
# Subcontractors
# ---------------------------------------------------------------------------


def subs_list_page(session: Session, active_filter: str | None = None,
                   tier_filter: str | None = None, flash: str | None = None) -> str:
    all_subs = list(session.scalars(select(Subcontractor).order_by(Subcontractor.tier.asc(),
                                                                    Subcontractor.name.asc())).all())
    active_count = sum(1 for s in all_subs if s.active)
    inactive_count = len(all_subs) - active_count
    tier_counts: dict[int, int] = {}
    for s in all_subs:
        tier_counts[int(s.tier)] = tier_counts.get(int(s.tier), 0) + 1

    subs = all_subs
    if active_filter == "true":
        subs = [s for s in subs if s.active]
    elif active_filter == "false":
        subs = [s for s in subs if not s.active]
    if tier_filter:
        try:
            wanted = int(tier_filter)
            subs = [s for s in subs if int(s.tier) == wanted]
        except ValueError:
            pass

    active_options = [
        ("All", None, len(all_subs)),
        ("Active", "true", active_count),
        ("Inactive", "false", inactive_count),
    ]
    tier_options = [
        ("All tiers", None, len(all_subs)),
        ("Tier 1", "1", tier_counts.get(1, 0)),
        ("Tier 2", "2", tier_counts.get(2, 0)),
        ("Tier 3", "3", tier_counts.get(3, 0)),
    ]

    rows = []
    for s in subs:
        radius = f"{s.service_radius_mi:.0f} mi" if s.service_radius_mi else "—"
        rows.append(
            f'<tr class="clickable" onclick="window.location.href=\'/admin/subcontractors/{s.id}\'">'
            f'<td class="id"><a href="/admin/subcontractors/{s.id}">#{s.id}</a></td>'
            f'<td class="passenger"><a href="/admin/subcontractors/{s.id}">{_html.escape(s.name)}</a></td>'
            f'<td><span class="badge tier-{int(s.tier)}">Tier {int(s.tier)}</span></td>'
            f'<td class="vehicle">{_html.escape(s.vehicle_classes.upper())}</td>'
            f'<td>{s.markup_pct:.0f}%</td>'
            f'<td>{_html.escape(radius)}</td>'
            f'<td>{"Active" if s.active else "Inactive"}</td>'
            f'<td class="phone">{_html.escape(s.dispatch_phone)}</td>'
            f'</tr>'
        )
    body = (
        '<table class="data-table"><thead><tr>'
        "<th>ID</th><th>Operator</th><th>Tier</th><th>Classes</th><th>Markup</th>"
        "<th>Radius</th><th>Active</th><th>Phone</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    ) if rows else '<div class="empty">No subcontractors match this filter.</div>'
    return (
        _doc_open("Subcontractors", "subs")
        + _flash(flash)
        + _page_head("Subcontractors", subtitle=f"{len(subs)} of {len(all_subs)} on roster",
                     back_href="/dashboard", back_label="Dashboard")
        + _filter_chips(active_filter, active_options, "/admin/subcontractors",
                        param="active", extra_params={"tier": tier_filter or ""})
        + _filter_chips(tier_filter, tier_options, "/admin/subcontractors",
                        param="tier", extra_params={"active": active_filter or ""})
        + '<section class="block">' + body + "</section>"
        + _doc_close()
    )


def sub_detail_page(session: Session, s: Subcontractor, flash: str | None = None) -> str:
    info_rows = [
        ("Name", _html.escape(s.name)),
        ("Phone", f'<a href="tel:{_html.escape(s.dispatch_phone)}">{_html.escape(s.dispatch_phone)}</a>'),
        ("Email", (f'<a href="mailto:{_html.escape(s.dispatch_email)}">{_html.escape(s.dispatch_email)}</a>' if s.dispatch_email else "—")),
        ("Tier", f'<span class="badge tier-{int(s.tier)}">Tier {int(s.tier)}</span>'),
        ("Vehicle classes", _html.escape(s.vehicle_classes.upper())),
        ("Markup", f"{s.markup_pct:.1f}%"),
        ("Base lat / lon", f"{s.base_lat:.5f}, {s.base_lon:.5f}" if s.base_lat is not None and s.base_lon is not None else "—"),
        ("Service radius", f"{s.service_radius_mi:.0f} mi" if s.service_radius_mi else "—"),
        ("Active", "Yes" if s.active else "No"),
    ]
    info_html = _info_grid(info_rows)

    tier_options = "".join(
        f'<option value="{t.value}"{" selected" if t == s.tier else ""}>Tier {t.value}</option>'
        for t in SubcontractorTier
    )

    edit_form = f'''
    <form method="post" action="/admin/subcontractors/{s.id}/edit" class="edit-form">
      <h3>Edit subcontractor</h3>
      <label>Tier
        <select name="tier" required>{tier_options}</select>
      </label>
      <label>Vehicle classes (comma-separated)
        <input type="text" name="vehicle_classes" required maxlength="64"
               value="{_html.escape(s.vehicle_classes)}" />
      </label>
      <label>Markup % (cost-plus)
        <input type="number" step="0.1" name="markup_pct" min="0" max="100"
               value="{s.markup_pct}" required />
      </label>
      <label>Service radius (mi)
        <input type="number" step="any" name="service_radius_mi" min="0"
               value="{s.service_radius_mi if s.service_radius_mi is not None else ''}" />
      </label>
      <label class="checkbox-label">
        <input type="checkbox" name="active" value="1" {"checked" if s.active else ""} />
        Active
      </label>
      <label>Notes
        <textarea name="notes" rows="3" maxlength="500">{_html.escape(s.notes or "")}</textarea>
      </label>
      <div class="form-actions">
        <button type="submit" class="primary">Save changes</button>
      </div>
    </form>
    '''

    settlement_link = (
        f'<section class="block">'
        f'<h2>Accounting</h2>'
        f'<p><a class="inline-link" href="/admin/subcontractors/{s.id}/settlement">'
        f'Settlement &amp; payouts &rarr;</a> '
        f'<small>Per-trip ledger, totals, and CSV export.</small></p>'
        f'</section>'
    )

    return (
        _doc_open(f"Subcontractor #{s.id}", "subs")
        + _flash(flash)
        + _page_head(f"{s.name}", subtitle=f"Tier {int(s.tier)} · {s.dispatch_phone}",
                     back_href="/admin/subcontractors", back_label="All subcontractors")
        + '<section class="two-col">'
        + '<div class="col">'
        + '<section class="block"><h2>Operator</h2>' + info_html + "</section>"
        + settlement_link
        + '</div>'
        + '<div class="col"><section class="block">' + edit_form + "</section></div>"
        + '</section>'
        + _doc_close()
    )


# ---------------------------------------------------------------------------
# Subcontractor settlement (per-trip ledger + CSV export)
# ---------------------------------------------------------------------------


_DEFAULT_SETTLEMENT_DAYS = 30
_SETTLEMENT_PERIOD_OPTIONS = [
    ("7 days", 7),
    ("30 days", 30),
    ("90 days", 90),
    ("All time", 0),
]


def _settlement_trips(session: Session, sub: Subcontractor, days: int,
                      date_from: str | None = None,
                      date_to: str | None = None) -> list[Trip]:
    """Return COMPLETED trips assigned to this sub, optionally limited to the
    last `days` (0 means no time bound) or an explicit date range. Ordered
    most-recent first. `date_from`/`date_to` override `days` when set.

    Dated by ``completed_at`` (v1.1.20), falling back to ``updated_at`` only
    for a legacy row the migration couldn't backfill. Editing a finished trip
    no longer moves it into the current period."""
    done_at = func.coalesce(Trip.completed_at, Trip.updated_at)
    q = select(Trip).where(
        Trip.assigned_subcontractor_id == sub.id,
        Trip.status == TripStatus.COMPLETED,
    )
    if date_from or date_to:
        if date_from:
            try:
                dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                q = q.where(done_at >= dt_from)
            except ValueError:
                pass
        if date_to:
            try:
                dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc)
                q = q.where(done_at <= dt_to)
            except ValueError:
                pass
    elif days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        q = q.where(done_at >= cutoff)
    q = q.order_by(done_at.desc())
    return list(session.scalars(q).all())


def _settlement_row(t: Trip) -> dict:
    """Per-trip ledger entry shaped for both HTML rendering and CSV export."""
    fare = t.fare_cents or 0
    markup = t.markup_cents or 0
    payout = max(0, fare - markup)
    done = t.completed_at or t.updated_at  # v1.1.20; legacy fallback
    if done is not None:
        ua = done if done.tzinfo else done.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - ua).days)
    else:
        age_days = None
    return {
        "trip_id": t.id,
        "completed_at": done,
        "route": f"{t.pickup_label} → {t.dropoff_label}",
        "passenger_name": t.passenger_name,
        "vehicle_class": t.vehicle_class.value,
        "fare_cents": fare,
        "markup_cents": markup,
        "payout_cents": payout,
        "age_days": age_days,
    }


def sub_settlement_page(session: Session, sub: Subcontractor,
                        days: int = _DEFAULT_SETTLEMENT_DAYS,
                        date_from: str | None = None,
                        date_to: str | None = None,
                        flash: str | None = None) -> str:
    """Per-trip ledger for a sub: completed trips in the window, totals,
    and a link to the CSV export. The dispatcher uses this view to
    reconcile what the operator owes the sub at month-end (or quarter-end)."""
    trips = _settlement_trips(session, sub, days, date_from=date_from, date_to=date_to)
    rows = [_settlement_row(t) for t in trips]

    total_fare = sum(r["fare_cents"] for r in rows)
    total_markup = sum(r["markup_cents"] for r in rows)
    total_payout = sum(r["payout_cents"] for r in rows)
    oldest = rows[-1]["completed_at"] if rows else None

    using_date_range = bool(date_from or date_to)
    if using_date_range:
        period_label = f"{date_from or '…'} → {date_to or '…'}"
    else:
        period_label = f"Last {days} days" if days > 0 else "All time"

    # Quick-select chips (only shown when not using a custom date range).
    chips_html = ""
    if not using_date_range:
        period_chip_opts = [
            (label, str(d) if d else "0", sum(1 for _ in _settlement_trips(session, sub, d)))
            for label, d in _SETTLEMENT_PERIOD_OPTIONS
        ]
        chips_html = _filter_chips(
            str(days), period_chip_opts,
            f"/admin/subcontractors/{sub.id}/settlement",
            param="days",
        )

    # Date-range picker form — always shown so the dispatcher can switch from
    # chip-based to date-range and back.
    date_range_form = (
        f'<form method="get" action="/admin/subcontractors/{sub.id}/settlement" '
        f'class="date-range-form">'
        f'<label>From <input type="date" name="date_from" value="{_html.escape(date_from or "")}" /></label>'
        f'<label>To <input type="date" name="date_to" value="{_html.escape(date_to or "")}" /></label>'
        f'<button type="submit" class="secondary">Apply range</button>'
        + (f' <a class="inline-link" href="/admin/subcontractors/{sub.id}/settlement"'
           f' style="margin-left:8px">Clear</a>' if using_date_range else "")
        + '</form>'
    )

    header_facts = [
        ("Operator", _html.escape(sub.name)),
        ("Tier", f'<span class="badge tier-{int(sub.tier)}">Tier {int(sub.tier)}</span>'),
        ("Markup", f"{sub.markup_pct:.1f}%"),
        ("Period", _html.escape(period_label)),
        ("Trips completed", str(len(rows))),
        ("Oldest in window", _html.escape(_fmt_dt(oldest)) if oldest else "—"),
        ("Total fare (billed)", _fmt_fare(total_fare)),
        ("Total markup (margin)", _fmt_fare(total_markup)),
        ("Total payout owed to sub", f'<strong>{_fmt_fare(total_payout)}</strong>'),
    ]
    header_html = _info_grid(header_facts)

    # Vehicle-class breakdown summary table.
    by_class: dict[str, dict[str, int]] = {}
    for r in rows:
        vc = r["vehicle_class"]
        if vc not in by_class:
            by_class[vc] = {"count": 0, "fare": 0, "markup": 0, "payout": 0}
        by_class[vc]["count"] += 1
        by_class[vc]["fare"] += r["fare_cents"]
        by_class[vc]["markup"] += r["markup_cents"]
        by_class[vc]["payout"] += r["payout_cents"]
    if len(by_class) > 1:
        vc_rows = "".join(
            f'<tr><td class="vehicle">{_html.escape(vc.upper())}</td>'
            f'<td>{d["count"]}</td>'
            f'<td class="fare">{_fmt_fare(d["fare"])}</td>'
            f'<td class="fare">{_fmt_fare(d["markup"])}</td>'
            f'<td class="fare"><strong>{_fmt_fare(d["payout"])}</strong></td></tr>'
            for vc, d in sorted(by_class.items())
        )
        breakdown_html = (
            '<section class="block"><h2>By vehicle class</h2>'
            '<table class="data-table"><thead><tr>'
            '<th>Class</th><th>Trips</th><th>Fare</th><th>Markup</th><th>Payout</th>'
            '</tr></thead><tbody>' + vc_rows + '</tbody></table></section>'
        )
    else:
        breakdown_html = ""

    if rows:
        body_rows = []
        for r in rows:
            body_rows.append(
                f'<tr class="clickable" onclick="window.location.href=\'/admin/trips/{r["trip_id"]}\'">'
                f'<td class="id"><a href="/admin/trips/{r["trip_id"]}">#{r["trip_id"]}</a></td>'
                f'<td class="time">{_html.escape(_fmt_dt(r["completed_at"]))}</td>'
                f'<td class="route">{_html.escape(_truncate(r["route"], 56))}</td>'
                f'<td class="passenger">{_html.escape(r["passenger_name"])}</td>'
                f'<td class="vehicle">{_html.escape(r["vehicle_class"].upper())}</td>'
                f'<td class="fare">{_fmt_fare(r["fare_cents"])}</td>'
                f'<td class="fare">{_fmt_fare(r["markup_cents"])}</td>'
                f'<td class="fare"><strong>{_fmt_fare(r["payout_cents"])}</strong></td>'
                f'<td>{r["age_days"] if r["age_days"] is not None else "—"}</td>'
                f'</tr>'
            )
        table = (
            '<table class="data-table"><thead><tr>'
            "<th>Trip</th><th>Completed</th><th>Route</th><th>Passenger</th>"
            "<th>Class</th><th>Fare</th><th>Markup</th><th>Payout</th><th>Age (d)</th>"
            "</tr></thead><tbody>"
            + "".join(body_rows)
            + "</tbody></table>"
        )
    else:
        table = '<div class="empty">No completed trips for this sub in the selected window.</div>'

    csv_qs = f"days={days}"
    if using_date_range:
        csv_qs = "&".join(f"{k}={v}" for k, v in [("date_from", date_from), ("date_to", date_to)] if v)
    csv_url = f"/admin/subcontractors/{sub.id}/settlement.csv?{csv_qs}"

    return (
        _doc_open(f"Settlement — {sub.name}", "subs")
        + _flash(flash)
        + _page_head(f"Settlement — {sub.name}",
                     subtitle=f"Per-trip payout ledger · {period_label}",
                     back_href=f"/admin/subcontractors/{sub.id}",
                     back_label="Back to subcontractor")
        + '<section class="block">' + header_html
        + f'<p style="margin-top:14px"><a class="inline-link" href="{csv_url}">'
        + 'Export this view as CSV &darr;</a></p>'
        + '</section>'
        + chips_html
        + date_range_form
        + breakdown_html
        + '<section class="block">' + table + "</section>"
        + _doc_close()
    )


def sub_settlement_csv(session: Session, sub: Subcontractor,
                       days: int = _DEFAULT_SETTLEMENT_DAYS,
                       date_from: str | None = None,
                       date_to: str | None = None) -> str:
    """Same data as sub_settlement_page, shaped as CSV for offline accounting.

    Pure string output — the route handler wraps it in a Response with the
    right Content-Type + Content-Disposition headers. No Python `csv` module
    dependency; the data is well-formed enough (no commas inside cell values
    after a basic sanitize) that a hand-rolled join is simpler than wiring
    csv.writer + StringIO.
    """
    trips = _settlement_trips(session, sub, days, date_from=date_from, date_to=date_to)
    headers = [
        "trip_id", "completed_at_utc", "vehicle_class",
        "passenger_name", "pickup_label", "dropoff_label",
        "fare_cents", "markup_cents", "payout_cents", "age_days",
    ]

    def _csv_field(v) -> str:
        s = "" if v is None else str(v)
        # If the value contains a comma, quote, or newline, wrap in quotes
        # and double any embedded quotes per RFC 4180.
        if any(ch in s for ch in (",", '"', "\n", "\r")):
            s = '"' + s.replace('"', '""') + '"'
        return s

    lines = [",".join(headers)]
    for t in trips:
        r = _settlement_row(t)
        lines.append(",".join(_csv_field(v) for v in [
            r["trip_id"],
            r["completed_at"].strftime("%Y-%m-%dT%H:%M:%S") if r["completed_at"] else "",
            r["vehicle_class"],
            r["passenger_name"],
            t.pickup_label,
            t.dropoff_label,
            r["fare_cents"],
            r["markup_cents"],
            r["payout_cents"],
            r["age_days"] if r["age_days"] is not None else "",
        ]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Rate cards
# ---------------------------------------------------------------------------


def rates_list_page(session: Session, flash: str | None = None) -> str:
    cards = list(session.scalars(
        select(RateCard).order_by(RateCard.vehicle_class.asc(), RateCard.name.asc())
    ).all())
    if not cards:
        body = ('<div class="empty">No rate cards yet, so live quotes on the site will soft-fail. '
                'Create one below, or load the planning-grade defaults with '
                '<code>python cli.py rates seed-defaults</code>.</div>')
    else:
        rows = []
        for c in cards:
            rows.append(
                f'<tr class="clickable" onclick="window.location.href=\'/admin/rates/{c.id}\'">'
                f'<td class="id"><a href="/admin/rates/{c.id}">#{c.id}</a></td>'
                f'<td class="passenger"><a href="/admin/rates/{c.id}">{_html.escape(c.name)}</a></td>'
                f'<td class="vehicle">{_html.escape(c.vehicle_class.value.upper())}</td>'
                f'<td class="fare">{_fmt_fare(c.base_fare_cents)}</td>'
                f'<td class="fare">{_fmt_fare(c.per_mile_cents)}/mi</td>'
                f'<td class="fare">{_fmt_fare(c.per_hour_cents)}/hr</td>'
                f'<td>{c.peak_week_multiplier:.2f}×</td>'
                f'<td>{"Active" if c.active else "Inactive"}</td>'
                f'</tr>'
            )
        body = (
            '<table class="data-table"><thead><tr>'
            "<th>ID</th><th>Name</th><th>Class</th><th>Base</th><th>Mile</th>"
            "<th>Hour</th><th>Peak</th><th>Active</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return (
        _doc_open("Rate cards", "rates")
        + _flash(flash)
        + _page_head("Rate cards",
                     subtitle=f"{len(cards)} configured",
                     back_href="/dashboard", back_label="Dashboard")
        + '<section class="block">'
        + '<form method="get" action="/admin/rates/new" class="action-form">'
        + '<button type="submit" class="primary">+ New rate card</button></form>'
        + body + "</section>"
        + _doc_close()
    )


def _rate_form(action: str, c: RateCard | None, heading: str, submit_label: str) -> str:
    """Rate-card form shared by the edit page and the new page (E6, v1.1.18)."""
    v = lambda attr, default: getattr(c, attr) if c is not None else default  # noqa: E731
    current_vc = c.vehicle_class if c is not None else None
    vc_options = ("" if c is not None else '<option value="" selected disabled>Choose…</option>') + "".join(
        f'<option value="{vc.value}"{" selected" if vc == current_vc else ""}>{vc.value.upper()}</option>'
        for vc in VehicleClass
    )
    active_checked = "checked" if v("active", True) else ""
    return f'''
    <form method="post" action="{_html.escape(action)}" class="edit-form">
      <h3>{_html.escape(heading)}</h3>
      <label>Name
        <input type="text" name="name" maxlength="120"
               value="{_html.escape(v("name", ""))}" required />
      </label>
      <label>Vehicle class
        <select name="vehicle_class" required>{vc_options}</select>
      </label>
      <div class="coords-row-2">
        <label>Base fare (cents)
          <input type="number" name="base_fare_cents" min="0" value="{v("base_fare_cents", 0)}" />
        </label>
        <label>Minimum fare (cents)
          <input type="number" name="minimum_fare_cents" min="0" value="{v("minimum_fare_cents", 0)}" />
        </label>
      </div>
      <div class="coords-row-2">
        <label>Per mile (cents)
          <input type="number" name="per_mile_cents" min="0" value="{v("per_mile_cents", 0)}" />
        </label>
        <label>Per hour (cents)
          <input type="number" name="per_hour_cents" min="0" value="{v("per_hour_cents", 0)}" />
        </label>
      </div>
      <div class="coords-row-2">
        <label>Airport meet surcharge (cents)
          <input type="number" name="airport_meet_surcharge_cents" min="0"
                 value="{v("airport_meet_surcharge_cents", 0)}" />
        </label>
        <label>After-hours surcharge (cents)
          <input type="number" name="after_hours_surcharge_cents" min="0"
                 value="{v("after_hours_surcharge_cents", 0)}" />
        </label>
      </div>
      <label>Peak week multiplier
        <input type="number" step="0.01" name="peak_week_multiplier" min="1.0" max="5.0"
               value="{v("peak_week_multiplier", 1.0)}" />
      </label>
      <label class="checkbox-label">
        <input type="checkbox" name="active" value="1" {active_checked} />
        Active
      </label>
      <label>Notes
        <textarea name="notes" rows="3" maxlength="1000">{_html.escape(v("notes", "") or "")}</textarea>
      </label>
      <div class="form-actions">
        <button type="submit" class="primary">{_html.escape(submit_label)}</button>
      </div>
    </form>'''


def rate_new_page(flash: str | None = None) -> str:
    """Blank rate-card form (E6, v1.1.18)."""
    return (
        _doc_open("New rate card", "rates")
        + _flash(flash)
        + _page_head("New rate card",
                     subtitle="Sample rates: replace with your own before going live",
                     back_href="/admin/rates", back_label="All rate cards")
        + '<section class="block">'
        + _rate_form("/admin/rates/new", None, "New rate card", "Create rate card")
        + "</section>"
        + _doc_close()
    )


def rate_detail_page(session: Session, c: RateCard, flash: str | None = None) -> str:
    info_rows = [
        ("Name", _html.escape(c.name)),
        ("Vehicle class", _html.escape(c.vehicle_class.value.upper())),
        ("Base fare", _fmt_fare(c.base_fare_cents)),
        ("Per mile", _fmt_fare(c.per_mile_cents)),
        ("Per hour", _fmt_fare(c.per_hour_cents)),
        ("Airport meet surcharge", _fmt_fare(c.airport_meet_surcharge_cents)),
        ("After-hours surcharge", _fmt_fare(c.after_hours_surcharge_cents)),
        ("Peak week multiplier", f"{c.peak_week_multiplier:.2f}×"),
        ("Minimum fare", _fmt_fare(c.minimum_fare_cents)),
        ("Active", "Yes" if c.active else "No"),
    ]
    info_html = _info_grid(info_rows)

    edit_form = _rate_form(f"/admin/rates/{c.id}/edit", c, "Edit rate card", "Save changes")

    return (
        _doc_open(f"Rate card #{c.id}", "rates")
        + _flash(flash)
        + _page_head(c.name,
                     subtitle=f"{c.vehicle_class.value.upper()} · {_fmt_fare(c.base_fare_cents)} base + "
                              f"{_fmt_fare(c.per_mile_cents)}/mi + {_fmt_fare(c.per_hour_cents)}/hr",
                     back_href="/admin/rates", back_label="All rate cards")
        + '<section class="two-col">'
        + '<div class="col"><section class="block"><h2>Rate</h2>' + info_html + "</section></div>"
        + '<div class="col"><section class="block">' + edit_form + "</section></div>"
        + '</section>'
        + _doc_close()
    )


# ---------------------------------------------------------------------------
# Call logs
# ---------------------------------------------------------------------------


def calls_list_page(session: Session, flash: str | None = None) -> str:
    calls = list(session.scalars(
        select(CallLog).order_by(CallLog.started_at.desc())
    ).all())
    if not calls:
        body = (
            '<div class="empty">No call logs yet. Wire Twilio Voice to '
            '<code>/webhooks/twilio/voice</code> to start capturing.</div>'
        )
    else:
        rows = []
        for c in calls:
            transcript = _truncate(c.transcription or "", 80)
            outcome_class = "answered" if c.outcome == "answered" else (
                "voicemail" if c.outcome == "voicemail" else "missed"
            )
            duration = (f"{c.duration_seconds // 60}m {c.duration_seconds % 60}s"
                        if c.duration_seconds else "—")
            rows.append(
                f'<tr class="clickable" onclick="window.location.href=\'/admin/calls/{c.id}\'">'
                f'<td class="id"><a href="/admin/calls/{c.id}">#{c.id}</a></td>'
                f'<td><span class="badge call-{outcome_class}">{_html.escape(c.outcome.replace("_", " ").title())}</span></td>'
                f'<td class="phone">{_html.escape(c.from_phone)}</td>'
                f'<td class="time">{_html.escape(_fmt_dt(c.started_at))}</td>'
                f'<td>{_html.escape(duration)}</td>'
                f'<td>{_html.escape(transcript)}</td>'
                f'</tr>'
            )
        body = (
            '<table class="data-table"><thead><tr>'
            "<th>ID</th><th>Outcome</th><th>From</th><th>Started</th>"
            "<th>Duration</th><th>Transcript</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return (
        _doc_open("Calls", "calls")
        + _flash(flash)
        + _page_head("Call log",
                     subtitle=f"{len(calls)} calls captured",
                     back_href="/dashboard", back_label="Dashboard")
        + '<section class="block">' + body + "</section>"
        + _doc_close()
    )


def call_detail_page(session: Session, c: CallLog, flash: str | None = None) -> str:
    duration = (f"{c.duration_seconds // 60}m {c.duration_seconds % 60}s"
                if c.duration_seconds else "—")
    info_rows = [
        ("Outcome", _badge(type("X", (), {"value": c.outcome})(), "call")),
        ("From", f'<a href="tel:{_html.escape(c.from_phone)}">{_html.escape(c.from_phone)}</a>'),
        ("To (dispatch)", _html.escape(c.to_phone)),
        ("Direction", _html.escape(c.direction.title())),
        ("Started", _html.escape(_fmt_dt(c.started_at))),
        ("Ended", _html.escape(_fmt_dt(c.ended_at))),
        ("Duration", _html.escape(duration)),
        ("Twilio SID", f'<code>{_html.escape(c.twilio_call_sid or "—")}</code>'),
        ("Linked reservation",
         (f'<a href="/admin/reservations/{c.linked_reservation_id}">'
          f'#{c.linked_reservation_id}</a>' if c.linked_reservation_id else "—")),
    ]
    info_html = _info_grid(info_rows)

    recording_html = ""
    if c.recording_url:
        recording_html = (
            '<section class="block"><h2>Voicemail recording</h2>'
            f'<audio controls preload="none" src="{_html.escape(c.recording_url)}" class="audio-player"></audio>'
            f'<p class="small-meta">Recording URL: <a href="{_html.escape(c.recording_url)}" '
            f'target="_blank" rel="noopener noreferrer" class="inline-link">open</a> · '
            f'{c.recording_duration_seconds or 0}s</p>'
            "</section>"
        )

    transcript_html = ""
    if c.transcription:
        transcript_html = (
            '<section class="block"><h2>Transcription</h2>'
            f'<div class="notes-block"><p>{_html.escape(c.transcription)}</p></div>'
            "</section>"
        )

    edit_form = f'''
    <form method="post" action="/admin/calls/{c.id}/edit" class="edit-form">
      <h3>Dispatcher notes</h3>
      <label>Notes
        <textarea name="dispatcher_notes" rows="4" maxlength="2000">{_html.escape(c.dispatcher_notes or "")}</textarea>
      </label>
      <label>Linked reservation ID
        <input type="number" name="linked_reservation_id" min="1"
               value="{c.linked_reservation_id or ''}" placeholder="(none)" />
      </label>
      <div class="form-actions">
        <button type="submit" class="primary">Save</button>
      </div>
    </form>
    '''

    return (
        _doc_open(f"Call #{c.id}", "calls")
        + _flash(flash)
        + _page_head(f"Call from {c.from_phone}",
                     subtitle=f"{c.outcome.replace('_', ' ').title()} · {_fmt_dt(c.started_at)}",
                     back_href="/admin/calls", back_label="All calls")
        + '<section class="two-col">'
        + '<div class="col"><section class="block"><h2>Call</h2>' + info_html + "</section>"
        + recording_html
        + transcript_html
        + '</div>'
        + '<div class="col"><section class="block">' + edit_form + "</section></div>"
        + '</section>'
        + _doc_close()
    )


# ---------------------------------------------------------------------------
# Admin audit log
# ---------------------------------------------------------------------------


def audit_log_page(session: Session, flash: str | None = None) -> str:
    from models import AdminAuditLog
    entries = list(session.scalars(
        select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(200)
    ).all())

    if not entries:
        body = '<div class="empty">No audit log entries yet.</div>'
    else:
        tr_rows = []
        for e in entries:
            tr_rows.append(
                f'<tr>'
                f'<td class="id">#{e.id}</td>'
                f'<td class="time">{_html.escape(_fmt_dt(e.timestamp))}</td>'
                f'<td><span class="badge method-{e.method.lower()}">{_html.escape(e.method)}</span></td>'
                f'<td class="route">{_html.escape(e.path)}</td>'
                f'<td>{_html.escape(e.user_id or "—")}</td>'
                f'<td class="status-code">{e.status_code}</td>'
                f'<td class="hash" title="{_html.escape(e.payload_hash or "")}">'
                f'{_html.escape((e.payload_hash or "—")[:12])}</td>'
                f'</tr>'
            )
        body = (
            '<table class="data-table"><thead><tr>'
            "<th>ID</th><th>Timestamp</th><th>Method</th><th>Path</th>"
            "<th>User</th><th>Status</th><th>Payload hash</th>"
            "</tr></thead><tbody>"
            + "".join(tr_rows)
            + "</tbody></table>"
        )

    return (
        _doc_open("Audit Log", "audit")
        + _flash(flash)
        + _page_head("Admin Audit Log", subtitle=f"{len(entries)} most recent entries",
                     back_href="/dashboard", back_label="Dashboard")
        + '<section class="block">' + body + "</section>"
        + _doc_close()
    )


# ---------------------------------------------------------------------------
# CSS — extends the Fireworks aesthetic, sized for admin density
# ---------------------------------------------------------------------------


_CSS = BASE_CSS + """
html{scroll-behavior:smooth;scroll-padding-top:18px;}

header{display:flex;align-items:center;gap:18px;
  padding:12px 22px;border-bottom:1px solid var(--brass-deep);
  background:linear-gradient(180deg,#15171f 0%,#0a0c12 100%);
  position:sticky;top:0;z-index:50;}
header .brand{font:600 19px/1 "Hoefler Text","Garamond",serif;
  letter-spacing:0.01em;color:var(--paper);flex:0 0 auto;}
header .brand em{font-style:italic;color:var(--brass);}
header .topnav{display:flex;align-items:center;gap:4px;flex:1;justify-content:center;
  flex-wrap:wrap;}
header .topnav a{color:rgba(251,246,232,0.7);text-decoration:none;
  font:11px/1 "Lucida Grande",sans-serif;letter-spacing:0.16em;
  text-transform:uppercase;padding:8px 10px;border-radius:2px;
  border:1px solid transparent;
  transition:color 120ms ease,background 120ms ease,border-color 120ms ease;}
header .topnav a:hover{color:var(--brass-hi);background:rgba(201,169,97,0.07);}
header .topnav a.is-active{color:var(--brass-hi);
  border-color:var(--brass-deep);background:rgba(201,169,97,0.06);}
header .role{font:10px/1 "Lucida Grande",sans-serif;letter-spacing:0.22em;
  text-transform:uppercase;color:var(--brass);flex:0 0 auto;}
header .logout-form{flex:0 0 auto;margin:0;}
header .logout-btn{background:none;border:1px solid var(--rule);color:rgba(251,246,232,0.6);
  font:10px/1 "Lucida Grande",sans-serif;letter-spacing:0.12em;text-transform:uppercase;
  padding:5px 10px;border-radius:2px;cursor:pointer;transition:color 120ms,border-color 120ms;}
header .logout-btn:hover{color:var(--bad);border-color:var(--bad);}

main{padding:22px 24px 80px;max-width:1480px;margin:0 auto;}

.page-head{margin-bottom:18px;}
.page-head .back-link{display:inline-block;font:11px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.14em;text-transform:uppercase;color:var(--brass);
  text-decoration:none;margin-bottom:8px;padding:4px 0;}
.page-head .back-link:hover{color:var(--brass-hi);}
.page-head h1{font:600 28px/1.1 "Hoefler Text","Garamond",serif;
  margin:0 0 4px;color:var(--paper);letter-spacing:0.01em;}
.page-head .page-subtitle{margin:0;color:rgba(251,246,232,0.6);
  font:12px/1.4 "Lucida Grande",sans-serif;}

.filter-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px;
  padding-bottom:12px;border-bottom:1px dashed var(--rule-soft);}
.filter-chip{display:inline-flex;align-items:center;gap:8px;
  padding:6px 12px;border-radius:2px;text-decoration:none;
  font:11px/1 "Lucida Grande",sans-serif;letter-spacing:0.12em;
  text-transform:uppercase;color:rgba(251,246,232,0.7);
  border:1px solid var(--rule);background:rgba(255,255,255,0.015);
  transition:color 120ms ease,background 120ms ease,border-color 120ms ease;}
.filter-chip:hover{color:var(--brass-hi);border-color:var(--brass-mid);
  background:rgba(201,169,97,0.07);}
.filter-chip.is-active{color:var(--brass-hi);border-color:var(--brass);
  background:linear-gradient(180deg,rgba(201,169,97,0.18) 0%,rgba(201,169,97,0.05) 100%);}
.filter-count{font-family:"Lucida Console",Consolas,monospace;font-size:10.5px;
  color:rgba(251,246,232,0.55);}
.filter-chip.is-active .filter-count{color:var(--brass-hi);}

section.block{margin-bottom:22px;}
.block h2{font:600 13px/1 "Lucida Grande",sans-serif;letter-spacing:0.18em;
  text-transform:uppercase;color:var(--brass);margin:0 0 10px;
  padding-bottom:5px;border-bottom:1px dashed rgba(201,169,97,0.4);}
.block h3{font:600 13px/1 "Hoefler Text","Garamond",serif;
  color:var(--brass-hi);margin:0 0 10px;}

.two-col{display:grid;grid-template-columns:1.4fr 1fr;gap:22px;}
@media (max-width:1100px){.two-col{grid-template-columns:1fr;}}
.col{display:flex;flex-direction:column;gap:18px;}

table.data-table{width:100%;border-collapse:collapse;font-size:12.5px;
  background:rgba(255,255,255,0.02);border:1px solid var(--rule);
  border-radius:3px;overflow:hidden;}
table.data-table thead th{text-align:left;padding:8px 12px;
  font:600 10px/1 "Lucida Grande",sans-serif;letter-spacing:0.14em;
  text-transform:uppercase;color:var(--brass);
  background:rgba(255,255,255,0.03);border-bottom:1px solid var(--rule);}
table.data-table tbody td{padding:9px 12px;border-bottom:1px solid rgba(58,53,39,0.5);
  vertical-align:middle;}
table.data-table tbody tr:nth-child(even){background:var(--row-alt);}
table.data-table tbody tr:last-child td{border-bottom:none;}
table.data-table tbody tr.clickable{cursor:pointer;
  transition:background 100ms ease;}
table.data-table tbody tr.clickable:hover{background:rgba(201,169,97,0.08);}
table.data-table td.id{font:600 11px/1 "Lucida Console",Consolas,monospace;
  color:var(--brass);width:54px;}
table.data-table td.passenger a,table.data-table td.id a{color:inherit;
  text-decoration:none;}
table.data-table td.passenger{font-weight:600;}
table.data-table td.time,table.data-table td.fare{
  font:11.5px/1 "Lucida Console",Consolas,monospace;color:var(--brass-hi);
  white-space:nowrap;}
table.data-table td.vehicle,table.data-table td.phone{
  font:11px/1 "Lucida Console",Consolas,monospace;}
.arrow{color:var(--brass);}

/* Info grid — read-only details on detail pages */
.info-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:0;
  background:rgba(255,255,255,0.02);border:1px solid var(--rule);
  border-radius:3px;overflow:hidden;}
@media (max-width:700px){.info-grid{grid-template-columns:1fr;}}
.info-row{padding:9px 14px;border-bottom:1px solid rgba(58,53,39,0.5);
  display:grid;grid-template-columns:140px 1fr;gap:14px;}
.info-row:nth-child(even){background:var(--row-alt);}
.info-label{font:10px/1.2 "Lucida Grande",sans-serif;letter-spacing:0.14em;
  text-transform:uppercase;color:var(--brass);padding-top:2px;}
.info-value{color:var(--paper);font-size:12.5px;line-height:1.45;}
.info-value a{color:var(--brass-hi);text-decoration:none;}
.info-value a:hover{color:var(--paper);}
.info-value small{color:rgba(251,246,232,0.55);
  font:11px/1.3 "Lucida Console",Consolas,monospace;}
.info-value small a{color:var(--brass);text-decoration:underline;
  text-underline-offset:2px;}
.info-value small a:hover{color:var(--brass-hi);}
.info-value em{color:rgba(251,246,232,0.45);font-style:italic;}
.inline-link{color:var(--brass)!important;text-decoration:underline;
  text-underline-offset:2px;}
.inline-link:hover{color:var(--brass-hi)!important;}

/* Inline-edit click targets on the info panel */
a.edit-link{display:inline-block;color:var(--paper);text-decoration:none;
  cursor:pointer;padding:2px 6px;margin:-2px -6px;border-radius:2px;
  border:1px dashed transparent;
  transition:background 120ms ease,border-color 120ms ease,color 120ms ease;}
a.edit-link:hover{background:rgba(201,169,97,0.12);border-color:var(--brass-mid);
  color:var(--brass-hi);}
a.edit-link em{color:rgba(251,246,232,0.5);}
a.search-hint{font:11px/1 "Lucida Grande",sans-serif;color:var(--brass);
  text-decoration:underline;text-underline-offset:2px;margin-left:4px;
  letter-spacing:0.02em;text-transform:lowercase;}
a.search-hint:hover{color:var(--brass-hi);}

/* Nominatim suggestions dropdown */
.address-search{position:relative;}
.suggestions{position:absolute;top:100%;left:0;right:0;
  max-height:260px;overflow-y:auto;
  background:linear-gradient(180deg,#15171f 0%,#0a0c12 100%);
  border:1px solid var(--brass-mid);border-radius:3px;
  list-style:none;margin:3px 0 0;padding:0;
  box-shadow:0 12px 28px rgba(0,0,0,0.6),0 0 0 1px rgba(255,255,255,0.04) inset;
  z-index:50;}
.suggestions li{padding:8px 12px;cursor:pointer;
  font:12.5px/1.35 "Lucida Grande",sans-serif;color:var(--paper);
  border-bottom:1px solid var(--rule-soft);
  transition:background 100ms ease,color 100ms ease;}
.suggestions li:last-child{border-bottom:none;}
.suggestions li:hover,.suggestions li.is-active{
  background:rgba(201,169,97,0.12);color:var(--brass-hi);}
.suggestions li small{display:block;color:rgba(251,246,232,0.55);
  font-size:10.5px;margin-top:3px;
  font-family:"Lucida Console",Consolas,monospace;}
.suggestions li.empty{color:rgba(251,246,232,0.5);font-style:italic;
  cursor:default;text-align:center;}

/* Forms */
.edit-form{display:flex;flex-direction:column;gap:10px;}
.edit-form label{display:block;font:11px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.04em;color:rgba(251,246,232,0.75);}
.edit-form label.checkbox-label{display:flex;align-items:center;gap:8px;}
.edit-form input,.edit-form select,.edit-form textarea{
  display:block;width:100%;margin-top:5px;
  background:rgba(0,0,0,0.35);border:1px solid var(--rule);
  border-radius:2px;color:var(--paper);
  font:12.5px/1.45 "Lucida Grande","Helvetica Neue",sans-serif;
  padding:8px 10px;}
.edit-form input:focus,.edit-form select:focus,.edit-form textarea:focus{
  outline:none;border-color:var(--brass);box-shadow:0 0 0 2px rgba(201,169,97,0.18);}
.edit-form code{font:11px/1 "Lucida Console",Consolas,monospace;
  color:var(--brass-hi);background:rgba(201,169,97,0.08);padding:1px 4px;border-radius:2px;}
.coords-row-2{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.form-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:6px;}
.form-actions button,.action-stack button{
  font:600 11px/1 "Lucida Grande",sans-serif;letter-spacing:0.14em;
  text-transform:uppercase;padding:10px 16px;border-radius:3px;cursor:pointer;
  border:1px solid var(--brass-deep);transition:filter 120ms ease;}
.form-actions button:hover,.action-stack button:hover{filter:brightness(1.1);}
.form-actions .primary,.action-stack .primary{
  background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%);color:#16110a;}
.form-actions .secondary,.action-stack .secondary{
  background:linear-gradient(180deg,#3a3527 0%,#22201a 100%);color:var(--brass-hi);}
.form-actions .danger,.action-stack .danger{
  background:linear-gradient(180deg,#7a3a2a 0%,#4e2520 100%);color:#fbe2d8;
  border-color:#5e2a20;}

.action-stack{display:flex;flex-direction:column;gap:8px;}
.action-form{margin:0;}
.action-form button{width:100%;}
.action-link{display:block;text-align:center;text-decoration:none;
  font:600 11px/1 "Lucida Grande",sans-serif;letter-spacing:0.12em;
  text-transform:uppercase;padding:10px 16px;border-radius:3px;
  border:1px solid var(--brass-deep);transition:filter 120ms ease;}
.action-link:hover{filter:brightness(1.1);}
.action-link.primary{
  background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%);color:#16110a;}
.action-link.secondary{
  background:linear-gradient(180deg,#3a3527 0%,#22201a 100%);color:var(--brass-hi);}

td.actions{white-space:nowrap;}
td.actions .muted{color:rgba(251,246,232,0.35);}
.inline-action-form{display:inline-block;margin:0 3px 0 0;}
.inline-action-form button{
  font:600 9.5px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.12em;text-transform:uppercase;
  padding:5px 8px;border-radius:2px;cursor:pointer;
  border:1px solid var(--brass-deep);width:auto;
  transition:filter 120ms ease;}
.inline-action-form button:hover{filter:brightness(1.1);}
.inline-action-form .primary{
  background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%);color:#16110a;}
.inline-action-form .secondary{
  background:linear-gradient(180deg,#3a3527 0%,#22201a 100%);color:var(--brass-hi);}
.inline-action-form .danger{
  background:linear-gradient(180deg,#7a3a2a 0%,#4e2520 100%);color:#fbe2d8;
  border-color:#5e2a20;}

.quote-form{display:flex;flex-direction:column;gap:8px;padding:10px 12px;
  border:1px dashed var(--rule);border-radius:3px;background:rgba(255,255,255,0.015);}
.quote-form .action-label{font:600 10px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.14em;text-transform:uppercase;color:rgba(251,246,232,0.65);
  display:flex;flex-direction:column;gap:4px;}
.quote-form select{font:12.5px/1.4 "Hoefler Text","Garamond",serif;
  color:rgba(251,246,232,0.92);background:rgba(255,255,255,0.04);
  border:1px solid var(--rule);border-radius:2px;padding:6px 8px;}
.quote-form .action-check{font:12.5px/1.4 "Hoefler Text","Garamond",serif;
  color:rgba(251,246,232,0.8);display:flex;align-items:center;gap:6px;
  letter-spacing:0;text-transform:none;}
.quote-form .action-note{margin:0;font:12.5px/1.5 "Hoefler Text","Garamond",serif;
  color:rgba(251,246,232,0.65);}

.notes-block{padding:14px 16px;border:1px dashed var(--rule);border-radius:3px;
  background:rgba(255,255,255,0.015);}
.notes-block p{margin:0;color:rgba(251,246,232,0.85);font-size:12.5px;
  line-height:1.55;white-space:pre-wrap;}

.date-range-form{display:flex;align-items:center;gap:10px;margin:10px 0 14px;
  font:12px/1.3 "Lucida Grande",sans-serif;color:rgba(251,246,232,0.7);}
.date-range-form input[type="date"]{font:12px "Lucida Grande",sans-serif;
  padding:4px 6px;border:1px solid var(--rule);border-radius:2px;
  background:var(--bg);color:var(--text);}
.date-range-form label{display:flex;align-items:center;gap:4px;}

.search-form{display:flex;align-items:center;gap:8px;margin:8px 0 14px;
  font:12px/1.3 "Lucida Grande",sans-serif;color:rgba(251,246,232,0.7);}
.search-form input[type="search"]{font:12px "Lucida Grande",sans-serif;
  padding:5px 8px;border:1px solid var(--rule);border-radius:2px;
  background:var(--bg);color:var(--text);min-width:220px;}

.method-post{background:rgba(90,140,74,0.25);color:#a5d499;}
.method-patch{background:rgba(201,169,97,0.25);color:var(--brass-hi);}
.method-delete{background:rgba(168,80,58,0.25);color:#f0a899;}
.hash{font-family:monospace;font-size:11px;color:rgba(251,246,232,0.5);}

.bulk-col{width:32px;text-align:center;}
.bulk-bar{display:flex;align-items:center;gap:12px;margin-bottom:14px;
  padding:10px 14px;border:1px solid var(--warn);border-radius:3px;
  background:rgba(240,160,80,0.08);color:var(--warn);
  font:12px/1.3 "Lucida Grande",sans-serif;}
.bulk-bar button{font:600 10px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.12em;text-transform:uppercase;padding:7px 12px;
  border-radius:2px;cursor:pointer;border:1px solid #5e2a20;
  background:linear-gradient(180deg,#7a3a2a 0%,#4e2520 100%);color:#fbe2d8;}
.bulk-bar button:hover{filter:brightness(1.1);}

.audio-player{width:100%;height:38px;margin-top:6px;}
.small-meta{font:11px/1.4 "Lucida Grande",sans-serif;color:rgba(251,246,232,0.55);
  margin-top:6px;}

.flash{margin-bottom:16px;padding:11px 14px;border-radius:3px;
  border:1px solid var(--brass-mid);background:rgba(201,169,97,0.10);
  color:var(--brass-hi);font:12px/1.4 "Lucida Grande",sans-serif;
  letter-spacing:0.02em;}
"""
