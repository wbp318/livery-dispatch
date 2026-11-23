"""Dispatcher live map — single-page Leaflet view of every entity in motion.

Renders a full-page Leaflet + CARTO Dark Matter map at /map that shows:

    - Driver positions, color-coded by current status (available / on_trip /
      off_duty / offline). "On trip" pins pulse for at-a-glance attention.
    - Subcontractor bases with translucent service-radius rings.
    - Active trip pickup + dropoff pins, connected by an OSRM road-following
      route line drawn over a brass shadow.
    - A floating side panel listing drivers, subs, and active trips for the
      cases when the dispatcher wants the list-view orientation; clicking a
      row pans + zooms the map to that entity.
    - Click-to-dispatch: the "+ New Trip" button enters a banner-led mode
      where the dispatcher clicks the map for pickup, then dropoff, then
      a modal collects passenger info and the trip is POSTed + broadcast
      in one move.

Style is the same brass-on-dark Fireworks 2008-2012 aesthetic locked into
`/dashboard` and the marketing site. Hoefler / Garamond for display
typography, Lucida Console for monospaced data, brass `#c9a961` accents.

Polls /map/data every 12 seconds to refresh positions. Default view covers
the SE US (GA + FL + SC + AL + neighboring) so the dispatcher sees the
operational context cleanly. Once any entity has a position, the view
auto-fits to the live bounds.
"""

from __future__ import annotations

import html as _html
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

import csp as _csp
from _styles import BASE_CSS
from models import (
    Driver,
    Reservation,
    ReservationStatus,
    Subcontractor,
    Trip,
    TripStatus,
)


ACTIVE_TRIP_STATUSES = (
    TripStatus.REQUESTED,
    TripStatus.BROADCASTING,
    TripStatus.ACCEPTED,
    TripStatus.EN_ROUTE_PICKUP,
    TripStatus.ON_SCENE,
    TripStatus.IN_TRANSIT,
)


def build_map_data(session: Session) -> dict:
    """Compose the JSON payload the live map polls.

    Returned as a plain dict (FastAPI validates against `MapData` on the way
    out). Kept as a function on this module so the dispatcher map's data
    shape co-evolves with the page itself.
    """
    drivers = list(session.scalars(select(Driver).order_by(Driver.name.asc())).all())
    subs = list(
        session.scalars(
            select(Subcontractor).where(Subcontractor.active.is_(True)).order_by(
                Subcontractor.tier.asc(), Subcontractor.name.asc()
            )
        ).all()
    )
    active_trips = list(
        session.scalars(
            select(Trip).where(Trip.status.in_(ACTIVE_TRIP_STATUSES)).order_by(Trip.pickup_time.asc())
        ).all()
    )

    return {
        "generated_at": datetime.now(timezone.utc),
        "drivers": [
            {
                "id": d.id,
                "name": d.name,
                "phone": d.phone,
                "status": d.status,
                "vehicle_classes": d.vehicle_classes,
                "lat": d.lat,
                "lon": d.lon,
            }
            for d in drivers
        ],
        "subcontractors": [
            {
                "id": s.id,
                "name": s.name,
                "tier": s.tier,
                "vehicle_classes": s.vehicle_classes,
                "base_lat": s.base_lat,
                "base_lon": s.base_lon,
                "service_radius_mi": s.service_radius_mi,
            }
            for s in subs
        ],
        "active_trips": [
            {
                "id": t.id,
                "passenger_name": t.passenger_name,
                "status": t.status,
                "vehicle_class": t.vehicle_class,
                "pickup_label": t.pickup_label,
                "pickup_lat": t.pickup_lat,
                "pickup_lon": t.pickup_lon,
                "dropoff_label": t.dropoff_label,
                "dropoff_lat": t.dropoff_lat,
                "dropoff_lon": t.dropoff_lon,
                "pickup_time": t.pickup_time,
                "assigned_driver_id": t.assigned_driver_id,
                "assigned_subcontractor_id": t.assigned_subcontractor_id,
            }
            for t in active_trips
        ],
    }


_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


def map_server_url() -> str:
    """The origin of our own map server (QGIS Server, gis/server/), or "".

    From ``MAP_SERVER_URL`` (v1.3.0). Only an ``https://`` origin is accepted
    (plain ``http://`` for localhost only, for local testing); any path, query
    or anything malformed is dropped, so the value is safe to put in the page
    and in the CSP. Empty means /map falls back to OpenStreetMap's standard
    tiles, darkened in the browser.
    """
    raw = os.getenv("MAP_SERVER_URL", "").strip()
    if not raw:
        return ""
    u = urlparse(raw)
    host = u.hostname or ""
    if not _HOST_RE.match(u.netloc or "") or not host:
        return ""
    local = host in ("localhost", "127.0.0.1")
    if u.scheme != "https" and not (u.scheme == "http" and local):
        return ""
    return f"{u.scheme}://{u.netloc}"


def render_map_page(csrf_token: str = "") -> str:
    """Render the live dispatcher map as a complete, self-contained HTML page.

    Module-level constants `_HEAD` and `_SCRIPTS` carry `__CSP_NONCE_ATTR__`
    placeholders that this function substitutes with the per-request nonce
    before returning. See `csp.py` for the rationale.

    ``csrf_token`` is the admin CSRF token bound to the dispatcher's session
    cookie. It lands in ``<meta name="csrf-token">`` and the page's two write
    fetches (``POST /trips``, ``POST /trips/{id}/broadcast``) echo it back as
    ``X-CSRF-Token`` (enforced on the REST surface since v1.1.13).
    """
    csrf_meta = (
        f'<meta name="csrf-token" content="{_html.escape(csrf_token)}" />'
        if csrf_token else ""
    )
    return _csp.inject_nonce(
        "<!doctype html>"
        + _HEAD.replace("__CSRF_META__", csrf_meta)
        + _BODY_OPEN
        + _APP_SHELL.replace('<div id="map" tabindex="0"',
                             f'<div id="map" data-qgis-url="{_html.escape(map_server_url())}" tabindex="0"', 1)
        + _SCRIPTS
        + "</body></html>"
    )


# ---------------------------------------------------------------------------
# HEAD — title, viewport, Leaflet CSS, embedded stylesheet
# ---------------------------------------------------------------------------

_HEAD = """<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="robots" content="noindex, nofollow" />
__CSRF_META__
<title>Livery Dispatch — Map</title>
<link rel="stylesheet"
      href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
      crossorigin="" />
<style __CSP_NONCE_ATTR__>%(css)s</style>
</head>""" % {"css": "__CSS_PLACEHOLDER__"}


# ---------------------------------------------------------------------------
# BODY — header, map, side panel, dispatch banner, new-trip modal
# ---------------------------------------------------------------------------

_BODY_OPEN = """<body>
<a class="skip-link" href="#map">Skip to map</a>
<header class="topbar">
  <div class="brand">Livery <em>Dispatch</em></div>
  <nav class="rolenav" aria-label="Primary">
    <a href="/dashboard">Dashboard</a>
    <a href="/map" class="is-active" aria-current="page">Live Map</a>
    <a href="/admin/trips">Trips</a>
    <a href="/admin/reservations">Reservations</a>
    <a href="/admin/inquiries">Inquiries</a>
    <a href="/admin/drivers">Drivers</a>
    <a href="/admin/subcontractors">Subs</a>
    <a href="/admin/rates">Rates</a>
    <a href="/admin/calls">Calls</a>
    <a href="/docs">API</a>
  </nav>
  <div class="topnav-actions">
    <button id="newTripBtn" type="button" class="cta">+ New Trip</button>
  </div>
</header>
"""

_APP_SHELL = """<main id="app" role="main">
  <div id="map" tabindex="0" aria-label="Live dispatcher map"></div>

  <aside id="sidepanel" aria-label="Live entities">
    <div class="panel-tabs" role="tablist">
      <button class="tab is-active" data-tab="trips" role="tab" aria-selected="true">Trips</button>
      <button class="tab" data-tab="drivers" role="tab" aria-selected="false">Drivers</button>
      <button class="tab" data-tab="subs" role="tab" aria-selected="false">Subs</button>
      <button class="tab" data-tab="legend" role="tab" aria-selected="false">Legend</button>
    </div>
    <div class="panel-body" data-tab="trips" role="tabpanel">
      <h3>Active trips</h3>
      <ul id="tripList" class="entity-list"><li class="empty">No active trips.</li></ul>
    </div>
    <div class="panel-body" data-tab="drivers" hidden role="tabpanel">
      <h3>Drivers</h3>
      <ul id="driverList" class="entity-list"><li class="empty">No drivers.</li></ul>
    </div>
    <div class="panel-body" data-tab="subs" hidden role="tabpanel">
      <h3>Subcontractors</h3>
      <ul id="subList" class="entity-list"><li class="empty">No subcontractors.</li></ul>
    </div>
    <div class="panel-body" data-tab="legend" hidden role="tabpanel">
      <h3>Legend</h3>
      <ul class="legend-list">
        <li><span class="legend-swatch pin-driver pin-available" aria-hidden="true"></span>Driver — available</li>
        <li><span class="legend-swatch pin-driver pin-on_trip" aria-hidden="true"></span>Driver — on trip <small>(pulses)</small></li>
        <li><span class="legend-swatch pin-driver pin-off_duty" aria-hidden="true"></span>Driver — off duty</li>
        <li><span class="legend-swatch pin-driver pin-offline" aria-hidden="true"></span>Driver — offline</li>
        <li><span class="legend-swatch pin-sub pin-tier-1" aria-hidden="true"></span>Subcontractor — Tier 1</li>
        <li><span class="legend-swatch pin-sub pin-tier-2" aria-hidden="true"></span>Subcontractor — Tier 2</li>
        <li><span class="legend-swatch pin-sub pin-tier-3" aria-hidden="true"></span>Subcontractor — Tier 3</li>
        <li><span class="legend-swatch pin-trip pin-pickup" aria-hidden="true">P</span>Trip pickup</li>
        <li><span class="legend-swatch pin-trip pin-dropoff" aria-hidden="true">D</span>Trip dropoff</li>
      </ul>
      <p class="legend-note">
        Road routes come from OSRM via this server's /osrm proxy
        (OSRM_BASE_URL). If routing is unreachable a straight-line
        fallback is shown.
      </p>
    </div>
    <footer class="panel-foot">
      <span id="refreshStamp" class="stamp">—</span>
      <button id="refreshNow" type="button" class="link">Refresh now</button>
    </footer>
  </aside>

  <div id="dispatchBanner" class="banner" hidden role="status" aria-live="polite">
    <span class="banner-text">Click the map to set the <strong>pickup</strong> location.</span>
    <button id="cancelDispatch" type="button" class="link">Cancel</button>
  </div>

  <div id="toast" class="toast" hidden role="status" aria-live="polite"></div>

  <div id="mapLegend" class="map-legend" aria-label="Map legend">
    <button id="mapLegendToggle" type="button" class="legend-toggle"
            aria-expanded="true" aria-controls="mapLegendBody">
      <span class="legend-title">Legend</span>
      <span class="legend-chevron" aria-hidden="true">&#x25BE;</span>
    </button>
    <div id="mapLegendBody" class="legend-body">
      <ul class="legend-rows">
        <li><span class="legend-pin pin-driver pin-available"></span>Driver &middot; available</li>
        <li><span class="legend-pin pin-driver pin-on_trip"></span>Driver &middot; on trip
            <small>(pulses)</small></li>
        <li><span class="legend-pin pin-driver pin-off_duty"></span>Driver &middot; off duty</li>
        <li><span class="legend-pin pin-driver pin-offline"></span>Driver &middot; offline</li>
        <li><span class="legend-pin pin-sub pin-tier-1">T1</span>Subcontractor &middot; Tier 1</li>
        <li><span class="legend-pin pin-sub pin-tier-2">T2</span>Subcontractor &middot; Tier 2</li>
        <li><span class="legend-pin pin-sub pin-tier-3">T3</span>Subcontractor &middot; Tier 3</li>
        <li><span class="legend-ring" aria-hidden="true"></span>Sub service radius</li>
        <li><span class="legend-pin pin-trip pin-pickup"><span class="diamond-glyph">P</span></span>Trip pickup</li>
        <li><span class="legend-pin pin-trip pin-dropoff"><span class="diamond-glyph">D</span></span>Trip dropoff</li>
        <li><span class="legend-line" aria-hidden="true"></span>OSRM route</li>
      </ul>
      <ul class="legend-rows legend-status">
        <li><span class="legend-pin pin-trip pin-status-broadcasting"></span>Broadcasting <small>(pulses)</small></li>
        <li><span class="legend-pin pin-trip pin-status-accepted"></span>Accepted</li>
        <li><span class="legend-pin pin-trip pin-status-en_route_pickup"></span>En route pickup</li>
        <li><span class="legend-pin pin-trip pin-status-on_scene"></span>On scene <small>(pulses)</small></li>
        <li><span class="legend-pin pin-trip pin-status-in_transit"></span>In transit</li>
      </ul>
      <div class="legend-opacity">
        <label for="basemapLight">Map lightness</label>
        <input type="range" id="basemapLight" min="100" max="700" step="20" value="100"
               aria-label="Basemap lightness" />
        <output id="basemapLightVal" for="basemapLight">0%</output>
      </div>
    </div>
  </div>

  <div id="newTripDrawer" class="drawer" hidden aria-labelledby="newTripTitle">
    <header class="drawer-head">
      <h2 id="newTripTitle">New trip</h2>
      <button id="closeDrawer" type="button" class="icon-btn" aria-label="Close">×</button>
    </header>
    <form id="newTripForm" class="drawer-body" novalidate>
      <fieldset>
        <legend>Pickup</legend>
        <div class="address-search">
          <label for="pickup_address">Search address
            <input type="text" id="pickup_address" name="pickup_address" autocomplete="off"
                   spellcheck="false"
                   aria-autocomplete="list" aria-controls="pickup-suggestions"
                   placeholder="JAX Airport &middot; 1 Cloister Drive &middot; Brunswick GA" />
          </label>
          <ul class="suggestions" id="pickup-suggestions" hidden role="listbox"></ul>
        </div>
        <label>Label
          <input type="text" name="pickup_label" required maxlength="255"
                 placeholder="JAX Airport, baggage claim" />
        </label>
        <div class="coords-row">
          <label class="coord">Lat <input type="number" step="any" name="pickup_lat" required readonly /></label>
          <label class="coord">Lng <input type="number" step="any" name="pickup_lon" required readonly /></label>
          <button type="button" id="repickPickup" class="link">Pick on map</button>
        </div>
      </fieldset>

      <fieldset>
        <legend>Dropoff</legend>
        <div class="address-search">
          <label for="dropoff_address">Search address
            <input type="text" id="dropoff_address" name="dropoff_address" autocomplete="off"
                   spellcheck="false"
                   aria-autocomplete="list" aria-controls="dropoff-suggestions"
                   placeholder="The Cloister Sea Island &middot; Ritz Amelia &middot; Jekyll Island Club" />
          </label>
          <ul class="suggestions" id="dropoff-suggestions" hidden role="listbox"></ul>
        </div>
        <label>Label
          <input type="text" name="dropoff_label" required maxlength="255"
                 placeholder="The Cloister, Sea Island" />
        </label>
        <div class="coords-row">
          <label class="coord">Lat <input type="number" step="any" name="dropoff_lat" required readonly /></label>
          <label class="coord">Lng <input type="number" step="any" name="dropoff_lon" required readonly /></label>
          <button type="button" id="repickDropoff" class="link">Pick on map</button>
        </div>
      </fieldset>

      <fieldset>
        <legend>Passenger</legend>
        <label>Name
          <input type="text" name="passenger_name" required maxlength="120"
                 placeholder="Mr. Henderson (Cloister guest)" />
        </label>
        <label>Phone
          <input type="tel" name="passenger_phone" required maxlength="32"
                 placeholder="+19045551234" />
        </label>
      </fieldset>

      <fieldset>
        <legend>Trip</legend>
        <label>Pickup time (local)
          <input type="datetime-local" name="pickup_time" required />
        </label>
        <label>Vehicle class
          <select name="vehicle_class" required>
            <option value="suv">SUV — Cadillac Escalade ESV</option>
            <option value="sprinter">Sprinter — Mercedes Sprinter Limited</option>
            <option value="sedan">Sedan</option>
          </select>
        </label>
        <label>Notes
          <textarea name="notes" rows="3" maxlength="500"
                    placeholder="Inside-terminal meet-and-greet; flight AA1234"></textarea>
        </label>
      </fieldset>

      <div class="form-actions">
        <button type="button" id="cancelTripBtn" class="ghost">Cancel</button>
        <button type="submit" id="createOnly" class="secondary"
                formnovalidate aria-label="Create trip without broadcasting">Create only</button>
        <button type="submit" id="createAndBroadcast" class="primary"
                aria-label="Create and broadcast to nearest drivers">Create &amp; broadcast</button>
      </div>
    </form>
  </div>
</main>
"""


# ---------------------------------------------------------------------------
# SCRIPTS — Leaflet + map app
# ---------------------------------------------------------------------------

_LEAFLET_JS = (
    '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" '
    'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" '
    'crossorigin=""></script>'
)


_SCRIPTS = _LEAFLET_JS + """<script __CSP_NONCE_ATTR__>%(js)s</script>""" % {"js": "__JS_PLACEHOLDER__"}


# ---------------------------------------------------------------------------
# CSS — Fireworks brass on dark, tuned for command-center density
# ---------------------------------------------------------------------------

_CSS = BASE_CSS + """
:root{
  --panel-bg:linear-gradient(180deg,rgba(20,22,28,0.96) 0%,rgba(14,16,22,0.92) 100%);
  --bevel:inset 0 1px 0 rgba(255,255,255,0.06),
          inset 0 -1px 0 rgba(0,0,0,0.5),
          0 8px 28px rgba(0,0,0,0.55);
}
html,body{height:100%;overflow:hidden;}

.skip-link{position:absolute;left:-9999px;top:0;background:var(--brass);color:var(--ink);
  padding:6px 10px;font-weight:600;z-index:10000;}
.skip-link:focus{left:8px;top:8px;outline:2px solid var(--brass-hi);}

/* ---- Topbar ---- */
.topbar{position:fixed;inset:0 0 auto 0;height:54px;z-index:1000;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 22px;
  background:linear-gradient(180deg,#15171f 0%,#0a0c12 100%);
  border-bottom:1px solid var(--brass-deep);
  box-shadow:0 6px 18px rgba(0,0,0,0.5);}
.topbar .brand{font:600 19px/1 "Hoefler Text","Garamond",serif;
  letter-spacing:0.01em;color:var(--paper);}
.topbar .brand em{font-style:italic;color:var(--brass);}
.topbar .rolenav{display:flex;align-items:center;gap:6px;}
.topbar .rolenav a{font:11px/1 "Lucida Grande",sans-serif;letter-spacing:0.22em;
  text-transform:uppercase;color:rgba(201,169,97,0.7);text-decoration:none;
  padding:8px 10px;border-radius:2px;border:1px solid transparent;
  transition:color 120ms ease,background 120ms ease,border-color 120ms ease;}
.topbar .rolenav a:hover{color:var(--brass-hi);background:rgba(201,169,97,0.08);}
.topbar .rolenav a.is-active{color:var(--brass-hi);
  border-color:var(--brass-deep);background:rgba(201,169,97,0.06);}
.topbar .role-divider{color:rgba(201,169,97,0.3);padding:0 2px;
  font:12px/1 "Hoefler Text",serif;}
.topbar .topnav-actions{display:flex;align-items:center;gap:10px;}
.topbar .topnav-actions a{color:rgba(251,246,232,0.7);text-decoration:none;
  font:11px/1 "Lucida Grande",sans-serif;letter-spacing:0.18em;
  text-transform:uppercase;padding:6px 8px;border-radius:2px;}
.topbar .topnav-actions a:hover{color:var(--brass-hi);background:rgba(201,169,97,0.07);}
.topbar .cta{background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%);
  border:1px solid var(--brass-deep);color:#16110a;font:600 11px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.14em;text-transform:uppercase;padding:9px 14px;border-radius:3px;
  cursor:pointer;box-shadow:var(--bevel),0 0 0 1px rgba(255,255,255,0.06) inset;
  transition:filter 120ms ease;}
.topbar .cta:hover{filter:brightness(1.08);}
.topbar .cta:active{filter:brightness(0.92);}

/* ---- Main app ---- */
#app{position:fixed;inset:54px 0 0 0;}
#map{position:absolute;inset:0;background:#070a0f;outline:none;}
#map.is-picking{cursor:crosshair;}

/* ---- Side panel ---- */
#sidepanel{position:absolute;top:18px;right:18px;width:340px;
  max-height:calc(100vh - 96px);
  background:var(--panel-bg);
  border:1px solid var(--brass-deep);border-radius:4px;
  box-shadow:var(--bevel);
  display:flex;flex-direction:column;
  backdrop-filter:blur(8px);
  z-index:600;}
.panel-tabs{display:flex;border-bottom:1px solid var(--rule);
  background:linear-gradient(180deg,rgba(255,255,255,0.03) 0%,rgba(0,0,0,0.05) 100%);}
.tab{flex:1;background:transparent;border:0;color:rgba(251,246,232,0.55);
  padding:10px 6px;font:600 10px/1 "Lucida Grande",sans-serif;letter-spacing:0.16em;
  text-transform:uppercase;cursor:pointer;border-bottom:2px solid transparent;}
.tab:hover{color:var(--brass-hi);}
.tab.is-active{color:var(--brass-hi);border-bottom-color:var(--brass);}
.panel-body{padding:12px 14px 6px;overflow-y:auto;max-height:62vh;}
.panel-body h3{margin:0 0 8px;font:600 10.5px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.18em;text-transform:uppercase;color:var(--brass);
  padding-bottom:6px;border-bottom:1px dashed rgba(201,169,97,0.4);}
.entity-list{list-style:none;margin:0;padding:0;}
.entity-list li{display:flex;align-items:center;gap:9px;padding:7px 6px;
  border-radius:3px;cursor:pointer;border-bottom:1px solid var(--rule-soft);
  transition:background 120ms ease;}
.entity-list li:hover{background:rgba(201,169,97,0.07);}
.entity-list li.empty{color:rgba(251,246,232,0.4);font-style:italic;
  border:1px dashed var(--rule);padding:14px;justify-content:center;cursor:default;
  border-radius:3px;}
.entity-list li.empty:hover{background:transparent;}
.entity-list .e-marker{flex:0 0 auto;width:22px;height:22px;
  display:flex;align-items:center;justify-content:center;
  font:600 9px/1 "Lucida Grande",sans-serif;letter-spacing:0.04em;}
.entity-list .e-text{flex:1;min-width:0;}
.entity-list .e-name{font-weight:600;color:var(--paper);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12.5px;}
.entity-list .e-meta{font:10.5px/1.3 "Lucida Console",Consolas,monospace;
  color:rgba(251,246,232,0.55);margin-top:2px;letter-spacing:0.02em;}
.entity-list .badge{font:9.5px/1 "Lucida Grande",sans-serif;letter-spacing:0.12em;
  text-transform:uppercase;padding:2px 6px;border-radius:2px;flex:0 0 auto;}
.entity-list .row-link{flex:0 0 auto;color:rgba(201,169,97,0.75);text-decoration:none;
  padding:4px 8px;border-radius:2px;font:600 16px/1 "Hoefler Text","Garamond",serif;
  margin-left:6px;transition:color 100ms ease,background 100ms ease,transform 100ms ease;}
.entity-list .row-link:hover{color:var(--brass-hi);background:rgba(201,169,97,0.14);
  transform:translateX(2px);}
.entity-list .row-link:focus-visible{outline:2px solid var(--brass-hi);outline-offset:2px;}
.badge.available,.badge.requested{background:rgba(154,208,138,0.18);color:#bce0ad;}
.badge.on_trip,.badge.broadcasting,.badge.en_route_pickup,
.badge.on_scene,.badge.in_transit,.badge.accepted{
  background:rgba(243,214,132,0.18);color:var(--brass-hi);}
.badge.off_duty,.badge.offline{background:rgba(58,53,39,0.5);color:rgba(251,246,232,0.55);}
.badge.tier-1{background:rgba(243,214,132,0.2);color:var(--brass-hi);}
.badge.tier-2{background:rgba(168,137,68,0.18);color:#d4b76e;}
.badge.tier-3{background:rgba(58,53,39,0.5);color:rgba(251,246,232,0.6);}

.legend-list{list-style:none;margin:0;padding:0;}
.legend-list li{display:flex;align-items:center;gap:10px;padding:6px 4px;
  border-bottom:1px solid var(--rule-soft);font-size:12px;}
.legend-list li:last-child{border-bottom:none;}
.legend-list small{color:rgba(251,246,232,0.55);margin-left:4px;}
.legend-swatch{flex:0 0 auto;width:22px;height:22px;display:inline-flex;
  align-items:center;justify-content:center;font:600 9.5px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.05em;}
.legend-note{margin:12px 0 4px;font-size:11px;color:rgba(251,246,232,0.55);
  font-style:italic;border-top:1px dashed var(--rule-soft);padding-top:10px;}

.panel-foot{display:flex;align-items:center;justify-content:space-between;
  padding:8px 14px;border-top:1px solid var(--rule);
  background:rgba(0,0,0,0.18);font-size:10.5px;color:rgba(251,246,232,0.6);}
.panel-foot .stamp{font:10.5px/1 "Lucida Console",Consolas,monospace;
  letter-spacing:0.04em;color:var(--brass);}
.panel-foot .stamp.fresh{animation:pulse-brass 1.6s ease-out 1;}
.link{background:transparent;border:0;color:var(--brass-hi);cursor:pointer;
  font:11px/1 "Lucida Grande",sans-serif;letter-spacing:0.06em;text-decoration:underline;
  text-underline-offset:2px;padding:2px 4px;}
.link:hover{color:var(--paper);}

/* ---- Dispatch banner (pickup/dropoff selection mode) ---- */
.banner{position:absolute;top:74px;left:50%;transform:translateX(-50%);
  display:flex;align-items:center;gap:14px;
  padding:10px 18px;border-radius:4px;
  background:linear-gradient(180deg,rgba(243,214,132,0.22) 0%,rgba(168,137,68,0.18) 100%);
  border:1px solid var(--brass-mid);
  box-shadow:0 12px 36px rgba(0,0,0,0.6),0 0 0 1px rgba(255,255,255,0.04) inset;
  color:var(--paper);font:12px/1 "Lucida Grande",sans-serif;letter-spacing:0.04em;
  z-index:700;backdrop-filter:blur(6px);}
.banner .banner-text strong{color:var(--brass-hi);}

/* ---- Map legend (persistent overlay, bottom-left) ---- */
.map-legend{position:absolute;bottom:24px;left:14px;
  background:var(--panel-bg);
  border:1px solid var(--brass-deep);border-radius:4px;
  box-shadow:var(--bevel);
  backdrop-filter:blur(8px);
  z-index:600;min-width:170px;max-width:220px;
  font:11.5px/1.35 "Lucida Grande","Helvetica Neue",sans-serif;}
.map-legend.is-collapsed .legend-body{display:none;}
.map-legend.is-collapsed{min-width:auto;}
.map-legend.is-collapsed .legend-chevron{transform:rotate(-90deg);}
.legend-toggle{display:flex;align-items:center;justify-content:space-between;
  width:100%;padding:8px 12px;background:transparent;border:0;cursor:pointer;
  border-bottom:1px solid var(--rule);
  color:var(--brass);font:600 10px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.22em;text-transform:uppercase;
  transition:color 100ms ease;}
.legend-toggle:hover{color:var(--brass-hi);}
.legend-toggle .legend-chevron{font-size:10px;transition:transform 160ms ease;}
.map-legend.is-collapsed .legend-toggle{border-bottom:none;}
.legend-body{padding:8px 12px 10px;}
.legend-rows{list-style:none;margin:0;padding:0;}
.legend-rows.legend-status{margin-top:8px;padding-top:8px;
  border-top:1px dashed var(--rule-soft);}
.legend-opacity{display:flex;align-items:center;gap:8px;margin-top:8px;
  padding-top:8px;border-top:1px dashed var(--rule-soft);color:var(--paper);
  font-size:11.5px;}
.legend-opacity label{flex:0 0 auto;}
.legend-opacity input[type=range]{flex:1 1 auto;min-width:60px;accent-color:var(--brass);
  cursor:pointer;}
.legend-opacity output{flex:0 0 auto;min-width:34px;text-align:right;
  color:rgba(251,246,232,0.6);font-variant-numeric:tabular-nums;}
.legend-rows li{display:flex;align-items:center;gap:9px;padding:3px 0;
  color:var(--paper);line-height:1.3;}
.legend-rows li small{color:rgba(251,246,232,0.5);font-size:10.5px;
  margin-left:auto;}
.legend-pin{flex:0 0 auto;width:18px;height:18px;display:inline-flex;
  align-items:center;justify-content:center;border-radius:50%;}
.legend-pin.pin-driver{position:static;border-width:1px;}
.legend-pin.pin-sub{border-radius:0;
  clip-path:polygon(50% 0%,100% 38%,82% 100%,18% 100%,0% 38%);
  font:700 8px/18px "Hoefler Text","Garamond",serif;color:#16110a;}
.legend-pin.pin-trip{border-radius:0;transform:rotate(45deg);
  width:14px;height:14px;display:inline-flex;
  border:1px solid #16110a;}
.legend-pin.pin-trip .diamond-glyph{transform:rotate(-45deg);
  font:700 9px/1 "Hoefler Text","Garamond",serif;color:#16110a;}
.legend-ring{flex:0 0 auto;width:18px;height:18px;border-radius:50%;
  border:1px dashed var(--brass);background:rgba(201,169,97,0.06);}
.legend-line{flex:0 0 auto;width:22px;height:3px;
  background:linear-gradient(to right,
    var(--brass) 0,var(--brass) 6px,transparent 6px,transparent 10px,
    var(--brass) 10px,var(--brass) 16px,transparent 16px,transparent 20px,
    var(--brass) 20px);}

@media (max-width:900px){
  .map-legend{bottom:auto;top:74px;left:18px;right:18px;max-width:none;}
  .map-legend.is-collapsed{max-width:120px;right:auto;}
}

/* ---- Toast (success / error) ---- */
.toast{position:absolute;bottom:24px;left:50%;transform:translateX(-50%);
  padding:10px 16px;border-radius:3px;
  background:linear-gradient(180deg,rgba(20,22,28,0.96) 0%,rgba(14,16,22,0.92) 100%);
  border:1px solid var(--brass-mid);box-shadow:var(--bevel);
  color:var(--paper);font:12px/1 "Lucida Grande",sans-serif;letter-spacing:0.04em;
  z-index:900;max-width:80vw;}
.toast.error{border-color:var(--bad);color:#f6cabd;}

/* ---- Drawer (new trip form) ---- */
.drawer{position:absolute;top:0;right:0;bottom:0;width:420px;max-width:96vw;
  background:var(--panel-bg);
  border-left:1px solid var(--brass-deep);
  box-shadow:-20px 0 50px rgba(0,0,0,0.55);
  display:flex;flex-direction:column;z-index:800;
  transform:translateX(0);transition:transform 220ms ease-out;}
.drawer[hidden]{display:none;}
.drawer.slide-out{transform:translateX(100%);}
.drawer-head{display:flex;align-items:center;justify-content:space-between;
  padding:14px 18px;border-bottom:1px solid var(--brass-deep);
  background:linear-gradient(180deg,#15171f 0%,#0a0c12 100%);}
.drawer-head h2{margin:0;font:600 15px/1 "Hoefler Text","Garamond",serif;
  letter-spacing:0.02em;color:var(--brass-hi);}
.icon-btn{background:transparent;border:0;color:var(--paper);font-size:22px;
  cursor:pointer;width:30px;height:30px;line-height:1;display:flex;
  align-items:center;justify-content:center;border-radius:50%;
  transition:background 120ms ease;}
.icon-btn:hover{background:rgba(201,169,97,0.15);color:var(--brass-hi);}
.drawer-body{padding:14px 18px 22px;overflow-y:auto;flex:1;}
.drawer-body fieldset{border:1px dashed var(--rule);border-radius:3px;
  padding:12px 14px;margin:0 0 14px;}
.drawer-body legend{font:600 10px/1 "Lucida Grande",sans-serif;letter-spacing:0.18em;
  text-transform:uppercase;color:var(--brass);padding:0 6px;}
.drawer-body label{display:block;margin:8px 0;font-size:11.5px;
  letter-spacing:0.04em;color:rgba(251,246,232,0.75);}
.drawer-body input,.drawer-body select,.drawer-body textarea{
  display:block;width:100%;margin-top:5px;
  background:rgba(0,0,0,0.35);border:1px solid var(--rule);
  border-radius:2px;color:var(--paper);
  font:12.5px/1.45 "Lucida Grande","Helvetica Neue",sans-serif;
  padding:8px 10px;}
.drawer-body input:focus,.drawer-body select:focus,.drawer-body textarea:focus{
  outline:none;border-color:var(--brass);box-shadow:0 0 0 2px rgba(201,169,97,0.18);}
.drawer-body input[readonly]{color:var(--brass-hi);
  font-family:"Lucida Console",Consolas,monospace;font-size:11.5px;}
.coords-row{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;
  align-items:end;}
.coords-row .coord{margin:0;}
.coords-row .link{align-self:center;margin-bottom:8px;}

/* ---- Address search (Nominatim forward-geocoding) ---- */
.address-search{position:relative;margin:0 0 10px;}
.address-search > label{margin:0;}
.address-search input[type="text"]{padding-right:32px;}
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
  font-size:10.5px;margin-top:3px;letter-spacing:0.01em;
  font-family:"Lucida Console",Consolas,monospace;}
.suggestions li.empty{color:rgba(251,246,232,0.5);font-style:italic;
  cursor:default;text-align:center;}
.suggestions li.empty:hover{background:transparent;color:rgba(251,246,232,0.5);}
.address-search.is-loading input[type="text"]{
  background-image:linear-gradient(90deg,transparent 0%,rgba(243,214,132,0.18) 50%,transparent 100%);
  background-repeat:no-repeat;background-size:40% 2px;background-position:0 100%;
  animation:loading-sweep 1.1s ease-in-out infinite;}
@keyframes loading-sweep{
  0%{background-position:-40% 100%;}
  100%{background-position:140% 100%;}
}
.form-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:6px;}
.form-actions button{
  font:600 11px/1 "Lucida Grande",sans-serif;letter-spacing:0.12em;
  text-transform:uppercase;padding:10px 14px;border-radius:3px;cursor:pointer;}
.form-actions .ghost{background:transparent;border:1px solid var(--rule);
  color:rgba(251,246,232,0.7);}
.form-actions .ghost:hover{color:var(--paper);border-color:var(--brass-mid);}
.form-actions .secondary{background:linear-gradient(180deg,#3a3527 0%,#22201a 100%);
  border:1px solid var(--brass-deep);color:var(--brass-hi);}
.form-actions .secondary:hover{filter:brightness(1.1);}
.form-actions .primary{background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%);
  border:1px solid var(--brass-deep);color:#16110a;
  box-shadow:var(--bevel),0 0 0 1px rgba(255,255,255,0.06) inset;}
.form-actions .primary:hover{filter:brightness(1.08);}
.form-actions .primary[disabled],.form-actions button[disabled]{opacity:0.6;
  cursor:not-allowed;}

/* ---- Map pins (divIcon) ---- */
.pin-wrap{background:transparent;border:0;}
.pin-driver{position:relative;width:24px;height:24px;border-radius:50%;
  background:radial-gradient(circle at 35% 28%,#f3d684 0%,#c9a961 60%,#8a6c34 100%);
  border:2px solid #16110a;color:#16110a;
  font:700 9px/1 "Lucida Grande",sans-serif;letter-spacing:0.04em;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 2px 6px rgba(0,0,0,0.7),0 0 0 1px rgba(255,255,255,0.1) inset;}
.pin-driver.pin-available{background:radial-gradient(circle at 35% 28%,#cfe9be 0%,#9ad08a 60%,#558246 100%);}
.pin-driver.pin-off_duty{background:radial-gradient(circle at 35% 28%,#cdc5b0 0%,#8a8472 60%,#4e4a3c 100%);}
.pin-driver.pin-offline{background:radial-gradient(circle at 35% 28%,#7a7464 0%,#4e4a3c 70%,#2a2820 100%);
  color:rgba(251,246,232,0.85);}
.pin-driver.pin-on_trip{animation:pulse-pin 2.0s ease-in-out infinite;}
@keyframes pulse-pin{
  0%,100%{box-shadow:0 2px 6px rgba(0,0,0,0.7),0 0 0 1px rgba(255,255,255,0.1) inset,
    0 0 0 0 rgba(243,214,132,0);}
  50%{box-shadow:0 2px 6px rgba(0,0,0,0.7),0 0 0 1px rgba(255,255,255,0.1) inset,
    0 0 0 10px rgba(243,214,132,0);}
}
.pin-driver.pin-on_trip::after{content:"";position:absolute;inset:-8px;border-radius:50%;
  background:radial-gradient(circle,rgba(243,214,132,0.45) 0%,transparent 70%);
  animation:pulse-halo 2.0s ease-out infinite;pointer-events:none;z-index:-1;}
@keyframes pulse-halo{
  0%{transform:scale(0.7);opacity:0.6;}
  100%{transform:scale(1.8);opacity:0;}
}

.pin-sub{width:24px;height:24px;
  clip-path:polygon(50% 0%,100% 38%,82% 100%,18% 100%,0% 38%);
  background:linear-gradient(180deg,#d8c890 0%,#a98944 100%);
  border:0;color:#16110a;
  font:700 11px/24px "Hoefler Text","Garamond",serif;
  text-align:center;
  box-shadow:0 2px 4px rgba(0,0,0,0.6);}
.pin-sub.pin-tier-1{background:linear-gradient(180deg,#f3d684 0%,#a98944 100%);}
.pin-sub.pin-tier-2{background:linear-gradient(180deg,#c0a868 0%,#7e6228 100%);color:#fbf6e8;}
.pin-sub.pin-tier-3{background:linear-gradient(180deg,#6e6857 0%,#3a3527 100%);color:#fbf6e8;}

.pin-trip{width:22px;height:22px;transform:rotate(45deg);
  background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%);
  border:1px solid #16110a;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 2px 5px rgba(0,0,0,0.6);}
.pin-trip span{transform:rotate(-45deg);
  font:700 11px/1 "Hoefler Text","Garamond",serif;color:#16110a;}
.pin-trip.pin-dropoff{background:linear-gradient(180deg,#fbf6e8 0%,#c8c0a2 100%);}
.pin-trip.pin-preview{background:linear-gradient(180deg,#f3d684 0%,#a98944 100%);
  animation:preview-pulse 1.4s ease-in-out infinite;}

/* Trip status overlays — color the pin by lifecycle so the dispatcher can
   read trip state without opening popups. Status class wins over the default
   pickup/dropoff colors. Pulsing animations highlight time-sensitive states
   (broadcasting awaiting accept, on-scene at pickup). */
.pin-trip.pin-status-requested,
.pin-trip.pin-dropoff.pin-status-requested{
  background:linear-gradient(180deg,#a8a290 0%,#6a6452 100%);color:#16110a;}
.pin-trip.pin-status-broadcasting,
.pin-trip.pin-dropoff.pin-status-broadcasting{
  background:linear-gradient(180deg,#f0c890 0%,#a87830 100%);color:#16110a;
  animation:status-pulse 1.6s ease-in-out infinite;}
.pin-trip.pin-status-accepted,
.pin-trip.pin-dropoff.pin-status-accepted{
  background:linear-gradient(180deg,#c5e0b0 0%,#7a9a5a 100%);color:#16110a;}
.pin-trip.pin-status-en_route_pickup,
.pin-trip.pin-dropoff.pin-status-en_route_pickup{
  background:linear-gradient(180deg,#f8c878 0%,#c08a30 100%);color:#16110a;}
.pin-trip.pin-status-on_scene,
.pin-trip.pin-dropoff.pin-status-on_scene{
  background:linear-gradient(180deg,#f3d684 0%,#a98944 100%);color:#16110a;
  animation:status-pulse 1.2s ease-in-out infinite;}
.pin-trip.pin-status-in_transit,
.pin-trip.pin-dropoff.pin-status-in_transit{
  background:linear-gradient(180deg,#9ec8f0 0%,#4070a0 100%);color:#16110a;}
@keyframes status-pulse{
  0%,100%{filter:brightness(1);}
  50%{filter:brightness(1.28);}
}
@keyframes preview-pulse{
  0%,100%{transform:rotate(45deg) scale(1);}
  50%{transform:rotate(45deg) scale(1.18);}
}

/* Leaflet popup skin to match aesthetic */
.leaflet-popup-content-wrapper{background:linear-gradient(180deg,#15171f 0%,#0a0c12 100%);
  color:var(--paper);border:1px solid var(--brass-mid);border-radius:3px;}
.leaflet-popup-content{margin:10px 14px;font:12px/1.5 "Lucida Grande",sans-serif;}
.leaflet-popup-content strong{display:block;font:600 13px/1.2 "Hoefler Text","Garamond",serif;
  color:var(--brass-hi);margin-bottom:4px;letter-spacing:0.02em;}
.leaflet-popup-content small{display:block;color:rgba(251,246,232,0.6);
  font-size:11px;margin-top:4px;letter-spacing:0.04em;
  font-family:"Lucida Console",Consolas,monospace;}
.leaflet-popup-tip{background:#15171f;border:1px solid var(--brass-mid);}
.leaflet-container .leaflet-popup-close-button{color:rgba(251,246,232,0.5);}

.leaflet-control-attribution{background:rgba(0,0,0,0.45);color:rgba(251,246,232,0.6);
  font-size:10px;}
.leaflet-control-attribution a{color:var(--brass);}
.leaflet-control-zoom a{background:#15171f;color:var(--brass-hi);
  border:1px solid var(--brass-deep);}
.leaflet-control-zoom a:hover{background:#1d2027;color:var(--paper);}

/* Pulse animation for stamp */
@keyframes pulse-brass{
  0%{color:var(--brass-hi);}
  100%{color:var(--brass);}
}

/* ---- Responsive ---- */
@media (max-width:900px){
  #sidepanel{width:calc(100vw - 36px);max-height:50vh;top:auto;bottom:18px;}
  .drawer{width:100vw;}
  .banner{left:18px;right:18px;transform:none;}
}
"""


# ---------------------------------------------------------------------------
# JS — map app
# ---------------------------------------------------------------------------

_JS = r"""
(function () {
  'use strict';

  // -------------------------------------------------------------------------
  // Config
  // -------------------------------------------------------------------------
  var REFRESH_MS = 12000;
  var DEFAULT_CENTER = [32.0, -82.5];   // SE US center — GA/FL/SC/AL all visible
  var DEFAULT_ZOOM = 6;
  var MAX_ZOOM = 16;
  var MIN_ZOOM = 4;

  // -------------------------------------------------------------------------
  // Leaflet map init. Background (v1.3.0): our own QGIS Server basemap when
  // MAP_SERVER_URL is set (#map[data-qgis-url]), else OpenStreetMap's standard
  // tiles darkened by a CSS filter. CARTO's free tiles now say "API KEY REQUIRED".
  // -------------------------------------------------------------------------
  var map = L.map('map', {
    center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM,
    minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM,
    zoomControl: true, attributionControl: true, scrollWheelZoom: true,
    worldCopyJump: false
  });

  var mapEl = document.getElementById('map');
  var MAP_SERVER = ((mapEl && mapEl.getAttribute('data-qgis-url')) || '').replace(/\/+$/, '');
  var OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
  var ROUTING_ATTR = ' &middot; routing &copy; <a href="https://project-osrm.org/">OSRM</a>';
  var baseTiles = MAP_SERVER
    ? L.tileLayer.wms(MAP_SERVER + '/ogc/coastal_basemap', {
        layers: 'basemap', styles: '', format: 'image/png', transparent: false, version: '1.3.0',
        maxZoom: 19, attribution: OSM_ATTR + ' &middot; map &copy; Livery Dispatch (QGIS Server)' + ROUTING_ATTR
      })
    : L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, attribution: OSM_ATTR + ROUTING_ATTR
      });
  baseTiles.addTo(map);
  // OSM's standard tiles are light; this turns them into the dark palette.
  var BASE_FILTER = MAP_SERVER ? '' : 'invert(1) hue-rotate(180deg) brightness(.9) contrast(.85) saturate(.55)';

  // Basemap lightness control — the CARTO basemap is intentionally dark.
  // Dragging right brightens it (way up) for readability without touching the
  // trip pins or route lines: the filter is applied only to the tile pane,
  // while pins (marker pane) and routes (overlay pane) live in separate
  // Leaflet panes above it. Persisted across reloads.
  (function setupLightness() {
    var slider = document.getElementById('basemapLight');
    var out = document.getElementById('basemapLightVal');
    if (!slider) return;
    var tilePane = map.getPane('tilePane');
    function apply(brightnessPct) {
      // 100% = native dark (filter cleared); higher = lighter. Pair brightness
      // with a touch less contrast at the top end so it lightens instead of
      // blowing out to pure glare.
      if (tilePane) {
        if (brightnessPct <= 100) {
          tilePane.style.filter = BASE_FILTER;
        } else {
          var contrast = Math.max(60, 100 - (brightnessPct - 100) / 12);
          tilePane.style.filter = (BASE_FILTER + ' brightness(' + (brightnessPct / 100) + ') contrast(' + contrast + '%)').trim();
        }
      }
      if (out) out.textContent = Math.round((brightnessPct - 100) / 6) + '%';
    }
    var saved = null;
    try { saved = window.localStorage.getItem('mapBasemapLight'); } catch (e) {}
    if (saved !== null && !isNaN(parseInt(saved, 10))) slider.value = saved;
    apply(parseInt(slider.value, 10));
    slider.addEventListener('input', function () {
      var pct = parseInt(slider.value, 10);
      apply(pct);
      try { window.localStorage.setItem('mapBasemapLight', String(pct)); } catch (e) {}
    });
  })();

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------
  var driverLayer = L.layerGroup().addTo(map);
  var subLayer = L.layerGroup().addTo(map);
  var subRadiusLayer = L.layerGroup().addTo(map);
  var tripLayer = L.layerGroup().addTo(map);
  var routeLayer = L.layerGroup().addTo(map);

  var lastData = { drivers: [], subcontractors: [], active_trips: [] };
  var fitToBoundsOnNextRefresh = true;
  var dispatchMode = null;                // null | 'pickup' | 'dropoff'
  var pending = {
    pickup_lat:null, pickup_lon:null, pickup_label:'',
    dropoff_lat:null, dropoff_lon:null, dropoff_label:''
  };
  var pickupMarker = null;
  var dropoffMarker = null;
  var previewRouteLayer = null;

  // -------------------------------------------------------------------------
  // Pin factories
  // -------------------------------------------------------------------------
  function driverIcon(d) {
    var glyph = d.vehicle_classes && d.vehicle_classes.indexOf('sprinter') >= 0 ? 'SP' : 'SV';
    return L.divIcon({
      className: 'pin-wrap',
      html: '<div class="pin-driver pin-' + d.status + '">' + glyph + '</div>',
      iconSize: [24, 24], iconAnchor: [12, 12], popupAnchor: [0, -14]
    });
  }
  function subIcon(s) {
    return L.divIcon({
      className: 'pin-wrap',
      html: '<div class="pin-sub pin-tier-' + s.tier + '">T' + s.tier + '</div>',
      iconSize: [24, 24], iconAnchor: [12, 12], popupAnchor: [0, -14]
    });
  }
  function tripIcon(role, status) {
    var label = role === 'pickup' ? 'P' : 'D';
    var statusClass = status ? ' pin-status-' + status : '';
    return L.divIcon({
      className: 'pin-wrap',
      html: '<div class="pin-trip pin-' + role + statusClass + '"><span>' + label + '</span></div>',
      iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -13]
    });
  }
  function previewPickupIcon() {
    return L.divIcon({
      className: 'pin-wrap',
      html: '<div class="pin-trip pin-preview"><span>P</span></div>',
      iconSize: [22, 22], iconAnchor: [11, 11]
    });
  }
  function previewDropoffIcon() {
    return L.divIcon({
      className: 'pin-wrap',
      html: '<div class="pin-trip pin-dropoff pin-preview"><span>D</span></div>',
      iconSize: [22, 22], iconAnchor: [11, 11]
    });
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------
  function renderAll(data) {
    driverLayer.clearLayers();
    subLayer.clearLayers();
    subRadiusLayer.clearLayers();
    tripLayer.clearLayers();
    routeLayer.clearLayers();

    var fitPoints = [];

    data.drivers.forEach(function (d) {
      if (d.lat == null || d.lon == null) return;
      fitPoints.push([d.lat, d.lon]);
      var m = L.marker([d.lat, d.lon], { icon: driverIcon(d), title: d.name });
      m.bindPopup(
        '<strong>' + escapeHtml(d.name) + '</strong>'
        + '<em>' + escapeHtml(badgeLabel(d.status)) + '</em>'
        + '<small>' + escapeHtml(d.phone) + ' &middot; ' + escapeHtml(d.vehicle_classes.toUpperCase()) + '</small>'
      );
      m.on('click', function () { selectTab('drivers'); highlightRow('driver-row-' + d.id); });
      m.addTo(driverLayer);
    });

    data.subcontractors.forEach(function (s) {
      if (s.base_lat == null || s.base_lon == null) return;
      fitPoints.push([s.base_lat, s.base_lon]);
      var m = L.marker([s.base_lat, s.base_lon], { icon: subIcon(s), title: s.name });
      m.bindPopup(
        '<strong>' + escapeHtml(s.name) + '</strong>'
        + '<em>Tier ' + s.tier + ' &middot; ' + escapeHtml(s.vehicle_classes.toUpperCase()) + '</em>'
        + (s.service_radius_mi != null
            ? '<small>service radius: ' + s.service_radius_mi.toFixed(0) + ' mi</small>'
            : '')
      );
      m.addTo(subLayer);
      if (s.service_radius_mi) {
        L.circle([s.base_lat, s.base_lon], {
          radius: s.service_radius_mi * 1609.34,
          color: tierColor(s.tier),
          weight: 1, opacity: 0.35,
          fillColor: tierColor(s.tier), fillOpacity: 0.04,
          dashArray: '4, 6'
        }).addTo(subRadiusLayer);
      }
    });

    data.active_trips.forEach(function (t) {
      fitPoints.push([t.pickup_lat, t.pickup_lon]);
      fitPoints.push([t.dropoff_lat, t.dropoff_lon]);
      var p = L.marker([t.pickup_lat, t.pickup_lon], { icon: tripIcon('pickup', t.status), title: 'Trip #' + t.id + ' pickup' });
      p.bindPopup(
        '<strong>Trip #' + t.id + ' &middot; pickup</strong>'
        + '<em>' + escapeHtml(t.passenger_name) + ' &middot; ' + escapeHtml(badgeLabel(t.status)) + '</em>'
        + '<small>' + escapeHtml(t.pickup_label) + '</small>'
        + '<small>' + escapeHtml(formatDateTime(t.pickup_time)) + ' &middot; '
        + escapeHtml(t.vehicle_class.toUpperCase()) + '</small>'
      );
      p.addTo(tripLayer);

      var d = L.marker([t.dropoff_lat, t.dropoff_lon], { icon: tripIcon('dropoff', t.status), title: 'Trip #' + t.id + ' dropoff' });
      d.bindPopup(
        '<strong>Trip #' + t.id + ' &middot; dropoff</strong>'
        + '<em>' + escapeHtml(t.passenger_name) + '</em>'
        + '<small>' + escapeHtml(t.dropoff_label) + '</small>'
      );
      d.addTo(tripLayer);

      // Straight-line skeleton first (so the trip is visible even if OSRM is slow)
      var skeleton = L.polyline(
        [[t.pickup_lat, t.pickup_lon], [t.dropoff_lat, t.dropoff_lon]],
        { color: '#c9a961', weight: 2, opacity: 0.35, dashArray: '4, 6' }
      ).addTo(routeLayer);
      // Then fetch OSRM and replace if it lands
      fetchOSRMRoute(t.pickup_lat, t.pickup_lon, t.dropoff_lat, t.dropoff_lon)
        .then(function (coords) {
          routeLayer.removeLayer(skeleton);
          L.polyline(coords, {
            color: 'rgba(0,0,0,0.65)', weight: 5, opacity: 0.75
          }).addTo(routeLayer);
          L.polyline(coords, {
            color: '#c9a961', weight: 3, opacity: 0.95, dashArray: '8, 6'
          }).addTo(routeLayer);
        })
        .catch(function () { /* skeleton stays */ });
    });

    renderSidebar(data);

    if (fitToBoundsOnNextRefresh && fitPoints.length) {
      map.fitBounds(L.latLngBounds(fitPoints), {
        paddingTopLeft: [50, 70],
        paddingBottomRight: [370, 50],
        maxZoom: 11
      });
      fitToBoundsOnNextRefresh = false;
    }
  }

  function renderSidebar(data) {
    // Drivers
    var driverUl = document.getElementById('driverList');
    if (data.drivers.length === 0) {
      driverUl.innerHTML = '<li class="empty">No drivers configured yet.</li>';
    } else {
      driverUl.innerHTML = data.drivers.map(function (d) {
        var hasLoc = d.lat != null && d.lon != null;
        return '<li id="driver-row-' + d.id + '" data-lat="' + (d.lat||'') + '" data-lon="' + (d.lon||'') + '">'
          + '<span class="e-marker pin-driver pin-' + d.status + '">'
          + (d.vehicle_classes.indexOf('sprinter') >= 0 ? 'SP' : 'SV') + '</span>'
          + '<span class="e-text">'
          + '<div class="e-name">' + escapeHtml(d.name) + '</div>'
          + '<div class="e-meta">' + escapeHtml(d.phone) + ' &middot; '
          + (hasLoc ? d.lat.toFixed(3) + ', ' + d.lon.toFixed(3) : 'no fix') + '</div>'
          + '</span>'
          + '<span class="badge ' + d.status + '">' + badgeLabel(d.status) + '</span>'
          + '<a class="row-link" href="/admin/drivers/' + d.id + '" '
          + 'title="Edit driver" aria-label="Edit driver" '
          + 'onclick="event.stopPropagation();">&rarr;</a>'
          + '</li>';
      }).join('');
    }
    bindRowClicks('driverList');

    // Subs
    var subUl = document.getElementById('subList');
    if (data.subcontractors.length === 0) {
      subUl.innerHTML = '<li class="empty">No subcontractors configured.</li>';
    } else {
      subUl.innerHTML = data.subcontractors.map(function (s) {
        var hasLoc = s.base_lat != null && s.base_lon != null;
        return '<li data-lat="' + (s.base_lat||'') + '" data-lon="' + (s.base_lon||'') + '">'
          + '<span class="e-marker pin-sub pin-tier-' + s.tier + '">T' + s.tier + '</span>'
          + '<span class="e-text">'
          + '<div class="e-name">' + escapeHtml(s.name) + '</div>'
          + '<div class="e-meta">' + escapeHtml(s.vehicle_classes.toUpperCase()) + ' &middot; '
          + (s.service_radius_mi != null ? s.service_radius_mi.toFixed(0) + ' mi radius' : 'no radius') + '</div>'
          + '</span>'
          + '<span class="badge tier-' + s.tier + '">Tier ' + s.tier + '</span>'
          + '<a class="row-link" href="/admin/subcontractors/' + s.id + '" '
          + 'title="Edit subcontractor" aria-label="Edit subcontractor" '
          + 'onclick="event.stopPropagation();">&rarr;</a>'
          + '</li>';
      }).join('');
    }
    bindRowClicks('subList');

    // Trips
    var tripUl = document.getElementById('tripList');
    if (data.active_trips.length === 0) {
      tripUl.innerHTML = '<li class="empty">No active trips.</li>';
    } else {
      tripUl.innerHTML = data.active_trips.map(function (t) {
        return '<li data-lat="' + t.pickup_lat + '" data-lon="' + t.pickup_lon + '">'
          + '<span class="e-marker pin-trip pin-pickup"><span style="transform:rotate(-45deg);font:700 9px/1 \'Hoefler Text\',serif;">' + t.id + '</span></span>'
          + '<span class="e-text">'
          + '<div class="e-name">#' + t.id + ' &middot; ' + escapeHtml(t.passenger_name) + '</div>'
          + '<div class="e-meta">' + escapeHtml(truncate(t.pickup_label, 18))
          + ' &rarr; ' + escapeHtml(truncate(t.dropoff_label, 18)) + ' &middot; '
          + escapeHtml(formatDateTime(t.pickup_time)) + '</div>'
          + '</span>'
          + '<span class="badge ' + t.status + '">' + badgeLabel(t.status) + '</span>'
          + '<a class="row-link" href="/admin/trips/' + t.id + '" '
          + 'title="Edit trip" aria-label="Edit trip" '
          + 'onclick="event.stopPropagation();">&rarr;</a>'
          + '</li>';
      }).join('');
    }
    bindRowClicks('tripList');
  }

  function bindRowClicks(ulId) {
    document.querySelectorAll('#' + ulId + ' li').forEach(function (li) {
      if (li.classList.contains('empty')) return;
      li.addEventListener('click', function () {
        var lat = parseFloat(li.dataset.lat);
        var lon = parseFloat(li.dataset.lon);
        if (!isNaN(lat) && !isNaN(lon)) {
          map.setView([lat, lon], Math.max(map.getZoom(), 11), { animate: true });
        }
      });
    });
  }

  function highlightRow(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.style.background = 'rgba(243,214,132,0.18)';
    setTimeout(function(){ el.style.background = ''; }, 1500);
  }

  // -------------------------------------------------------------------------
  // Data fetching + refresh loop
  // -------------------------------------------------------------------------
  function loadMapData() {
    return fetch('/map/data', { headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        lastData = data;
        renderAll(data);
        stampRefresh(data.generated_at);
      })
      .catch(function (err) {
        showToast('Map refresh failed: ' + err.message, true);
      });
  }
  function stampRefresh(iso) {
    var el = document.getElementById('refreshStamp');
    if (!el) return;
    var d = new Date(iso);
    el.textContent = 'Updated ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':'
      + pad(d.getUTCSeconds()) + ' UTC';
    el.classList.remove('fresh');
    void el.offsetWidth;  // reflow to restart animation
    el.classList.add('fresh');
  }

  // -------------------------------------------------------------------------
  // OSRM routing
  // -------------------------------------------------------------------------
  // Same-origin proxy (GET /osrm/..., osrm_proxy.py) — OSRM_BASE_URL decides
  // whether that's the self-hosted container or the public demo, so the map
  // and fare backend always route against the same engine.
  function fetchOSRMRoute(lat1, lon1, lat2, lon2) {
    var url = '/osrm/route/v1/driving/'
      + (+lon1).toFixed(6) + ',' + (+lat1).toFixed(6) + ';'
      + (+lon2).toFixed(6) + ',' + (+lat2).toFixed(6)
      + '?overview=full&geometries=geojson';
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error('OSRM ' + r.status);
      return r.json();
    }).then(function (data) {
      if (!data.routes || !data.routes.length) throw new Error('OSRM no route');
      return data.routes[0].geometry.coordinates.map(function (c) { return [c[1], c[0]]; });
    });
  }

  // -------------------------------------------------------------------------
  // Click-to-dispatch
  // -------------------------------------------------------------------------
  var banner = document.getElementById('dispatchBanner');
  var bannerText = banner.querySelector('.banner-text');
  var newTripBtn = document.getElementById('newTripBtn');
  var cancelDispatchBtn = document.getElementById('cancelDispatch');
  var drawer = document.getElementById('newTripDrawer');
  var form = document.getElementById('newTripForm');
  var mapEl = document.getElementById('map');
  var sidepanel = document.getElementById('sidepanel');
  var toastEl_forStop = document.getElementById('toast');

  // Prevent overlay clicks from also registering as Leaflet map clicks. The
  // banner sits visually on top of the map, so without this Leaflet treats a
  // click on Cancel as a pin-drop at the underlying map coordinates. Same
  // defensive treatment for the side panel, the drawer, the toast, and the
  // bottom-left legend overlay so every overlay swallows its own input cleanly.
  [banner, drawer, sidepanel, toastEl_forStop, document.querySelector('.topbar'),
   document.getElementById('mapLegend')]
    .forEach(function (el) {
      if (el && L && L.DomEvent) {
        L.DomEvent.disableClickPropagation(el);
        L.DomEvent.disableScrollPropagation(el);
      }
    });

  // Wire up the legend collapse toggle.
  var legend = document.getElementById('mapLegend');
  var legendToggle = document.getElementById('mapLegendToggle');
  if (legendToggle && legend) {
    legendToggle.addEventListener('click', function () {
      var collapsed = legend.classList.toggle('is-collapsed');
      legendToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });
  }

  newTripBtn.addEventListener('click', startDispatchFlow);
  cancelDispatchBtn.addEventListener('click', function (e) {
    e.preventDefault();
    cancelDispatchFlow();
  });
  document.getElementById('closeDrawer').addEventListener('click', closeDrawer);
  document.getElementById('cancelTripBtn').addEventListener('click', closeDrawer);
  document.getElementById('repickPickup').addEventListener('click', function () { setMode('pickup'); });
  document.getElementById('repickDropoff').addEventListener('click', function () { setMode('dropoff'); });

  // Forward-geocoding search (Nominatim) for pickup + dropoff inputs.
  bindAddressSearch(
    document.getElementById('pickup_address'),
    document.getElementById('pickup-suggestions'),
    'pickup'
  );
  bindAddressSearch(
    document.getElementById('dropoff_address'),
    document.getElementById('dropoff-suggestions'),
    'dropoff'
  );

  map.on('click', function (e) {
    if (!dispatchMode) return;
    if (dispatchMode === 'pickup') {
      placePickup(e.latlng);
      if (pending.dropoff_lat == null) setMode('dropoff');
      else { setMode(null); openDrawer(); }
    } else if (dispatchMode === 'dropoff') {
      placeDropoff(e.latlng);
      setMode(null);
      openDrawer();
    }
  });

  function startDispatchFlow() {
    // Open the drawer first — the dispatcher's primary input is now type-to-search
    // (Apple-Maps style); map-click is the fallback via the per-field "Pick on map"
    // buttons. Clear any prior state so the drawer is a clean slate.
    pending = { pickup_lat:null,pickup_lon:null,pickup_label:'',
                dropoff_lat:null,dropoff_lon:null,dropoff_label:'' };
    if (pickupMarker) { map.removeLayer(pickupMarker); pickupMarker = null; }
    if (dropoffMarker) { map.removeLayer(dropoffMarker); dropoffMarker = null; }
    if (previewRouteLayer) { map.removeLayer(previewRouteLayer); previewRouteLayer = null; }
    form.reset();
    openDrawer();
    setTimeout(function () {
      var first = document.getElementById('pickup_address');
      if (first) first.focus();
    }, 60);
  }

  function cancelDispatchFlow() {
    setMode(null);
    if (pickupMarker) { map.removeLayer(pickupMarker); pickupMarker = null; }
    if (dropoffMarker) { map.removeLayer(dropoffMarker); dropoffMarker = null; }
    if (previewRouteLayer) { map.removeLayer(previewRouteLayer); previewRouteLayer = null; }
  }

  function setMode(mode) {
    dispatchMode = mode;
    if (mode === 'pickup') {
      bannerText.innerHTML = 'Click the map to set the <strong>pickup</strong> location.';
      banner.hidden = false;
      mapEl.classList.add('is-picking');
    } else if (mode === 'dropoff') {
      bannerText.innerHTML = 'Click the map to set the <strong>dropoff</strong> location.';
      banner.hidden = false;
      mapEl.classList.add('is-picking');
    } else {
      banner.hidden = true;
      mapEl.classList.remove('is-picking');
    }
  }

  function placePickup(latlng, knownLabel) {
    pending.pickup_lat = latlng.lat;
    pending.pickup_lon = latlng.lng;
    if (pickupMarker) map.removeLayer(pickupMarker);
    pickupMarker = L.marker(latlng, { icon: previewPickupIcon() }).addTo(map);
    form.elements.pickup_lat.value = latlng.lat.toFixed(6);
    form.elements.pickup_lon.value = latlng.lng.toFixed(6);
    if (knownLabel) {
      // Came from forward-geocoding (typed address) — we already have a good label.
      pending.pickup_label = knownLabel;
      form.elements.pickup_label.value = knownLabel;
    } else {
      // Came from map-click — start with bare coords and refine via reverse-geocode.
      if (!form.elements.pickup_label.value) {
        form.elements.pickup_label.value = '(' + latlng.lat.toFixed(4) + ', ' + latlng.lng.toFixed(4) + ')';
      }
      reverseGeocode(latlng).then(function (label) {
        if (label) {
          pending.pickup_label = label;
          var current = form.elements.pickup_label.value;
          // Only overwrite if dispatcher hasn't typed something custom
          if (current.startsWith('(') && current.endsWith(')')) {
            form.elements.pickup_label.value = label;
          }
        }
      });
    }
    refreshPreviewRoute();
  }

  function placeDropoff(latlng, knownLabel) {
    pending.dropoff_lat = latlng.lat;
    pending.dropoff_lon = latlng.lng;
    if (dropoffMarker) map.removeLayer(dropoffMarker);
    dropoffMarker = L.marker(latlng, { icon: previewDropoffIcon() }).addTo(map);
    form.elements.dropoff_lat.value = latlng.lat.toFixed(6);
    form.elements.dropoff_lon.value = latlng.lng.toFixed(6);
    if (knownLabel) {
      pending.dropoff_label = knownLabel;
      form.elements.dropoff_label.value = knownLabel;
    } else {
      if (!form.elements.dropoff_label.value) {
        form.elements.dropoff_label.value = '(' + latlng.lat.toFixed(4) + ', ' + latlng.lng.toFixed(4) + ')';
      }
      reverseGeocode(latlng).then(function (label) {
        if (label) {
          pending.dropoff_label = label;
          var current = form.elements.dropoff_label.value;
          if (current.startsWith('(') && current.endsWith(')')) {
            form.elements.dropoff_label.value = label;
          }
        }
      });
    }
    refreshPreviewRoute();
  }

  // ---------------------------------------------------------------------------
  // Address search (Nominatim forward-geocoding)
  // ---------------------------------------------------------------------------
  function bindAddressSearch(input, suggestionsEl, role) {
    if (!input || !suggestionsEl) return;
    var debounceTimer = null;
    var activeIndex = -1;
    var lastQuery = '';

    function clearSuggestions() {
      suggestionsEl.innerHTML = '';
      suggestionsEl.hidden = true;
      activeIndex = -1;
    }

    function renderEmpty(msg) {
      suggestionsEl.innerHTML = '<li class="empty">' + escapeHtml(msg) + '</li>';
      suggestionsEl.hidden = false;
      activeIndex = -1;
    }

    function renderSuggestions(results) {
      if (!results || !results.length) { renderEmpty('No matches. Try a more specific address.'); return; }
      suggestionsEl.innerHTML = results.map(function (r) {
        var parts = (r.display_name || '').split(',').map(function (s) { return s.trim(); });
        var primary = parts.slice(0, 2).join(', ') || r.display_name || '(no name)';
        var secondary = parts.slice(2, 5).join(', ');
        var compactLabel = parts.slice(0, 3).join(', ') || primary;
        return '<li role="option" data-lat="' + r.lat + '" data-lon="' + r.lon
          + '" data-name="' + escapeHtml(compactLabel) + '">'
          + escapeHtml(primary)
          + (secondary ? '<small>' + escapeHtml(secondary) + '</small>' : '')
          + '</li>';
      }).join('');
      suggestionsEl.hidden = false;
      activeIndex = -1;
    }

    function search(q) {
      if (q.length < 3) { clearSuggestions(); return; }
      if (q === lastQuery) return;
      lastQuery = q;
      input.parentElement.classList.add('is-loading');
      var url = 'https://nominatim.openstreetmap.org/search?format=json&limit=6'
        + '&countrycodes=us&q=' + encodeURIComponent(q);
      fetch(url, { headers: { 'Accept-Language': 'en' } })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (results) {
          input.parentElement.classList.remove('is-loading');
          renderSuggestions(results);
        })
        .catch(function () {
          input.parentElement.classList.remove('is-loading');
          renderEmpty('Search unavailable. Click on map instead.');
        });
    }

    input.addEventListener('input', function () {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { search(input.value.trim()); }, 280);
    });

    input.addEventListener('focus', function () {
      if (suggestionsEl.children.length && input.value.trim().length >= 3) {
        suggestionsEl.hidden = false;
      }
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
      var latlng = L.latLng(lat, lon);
      if (role === 'pickup') placePickup(latlng, name);
      else placeDropoff(latlng, name);
      map.flyTo([lat, lon], Math.max(map.getZoom(), 11), { duration: 0.7 });
      input.value = name;
      clearSuggestions();
    });

    // Click outside the input + dropdown dismisses suggestions.
    document.addEventListener('click', function (e) {
      if (!input.contains(e.target) && !suggestionsEl.contains(e.target)) {
        suggestionsEl.hidden = true;
      }
    });
  }

  function refreshPreviewRoute() {
    if (pending.pickup_lat == null || pending.dropoff_lat == null) return;
    if (previewRouteLayer) map.removeLayer(previewRouteLayer);
    var skeleton = L.polyline(
      [[pending.pickup_lat, pending.pickup_lon], [pending.dropoff_lat, pending.dropoff_lon]],
      { color: '#f3d684', weight: 3, opacity: 0.55, dashArray: '6, 6' }
    ).addTo(map);
    previewRouteLayer = skeleton;
    fetchOSRMRoute(pending.pickup_lat, pending.pickup_lon,
                   pending.dropoff_lat, pending.dropoff_lon)
      .then(function (coords) {
        if (previewRouteLayer === skeleton) {
          map.removeLayer(skeleton);
          previewRouteLayer = L.polyline(coords, {
            color: '#f3d684', weight: 3, opacity: 0.95, dashArray: '8, 6'
          }).addTo(map);
        }
      })
      .catch(function () { /* skeleton stays */ });
  }

  function openDrawer() {
    drawer.hidden = false;
    if (!form.elements.pickup_time.value) {
      // Default pickup time to "now + 90 min" in local time, formatted for
      // datetime-local input (yyyy-MM-ddTHH:mm).
      var when = new Date(Date.now() + 90 * 60 * 1000);
      form.elements.pickup_time.value = toLocalDatetimeLocal(when);
    }
    form.elements.passenger_name.focus();
  }
  function closeDrawer() {
    drawer.hidden = true;
    form.reset();
    cancelDispatchFlow();
  }

  // Submission
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var broadcast = (document.activeElement && document.activeElement.id === 'createAndBroadcast');
    submitTrip(broadcast);
  });

  // Admin CSRF token from the csrf-token meta tag (rendered server-side,
  // bound to the session cookie). Required on every REST write since v1.1.13.
  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : '';
  }

  function submitTrip(broadcast) {
    var btns = form.querySelectorAll('button');
    btns.forEach(function (b) { b.disabled = true; });

    var payload = {
      passenger_name: form.elements.passenger_name.value.trim(),
      passenger_phone: form.elements.passenger_phone.value.trim(),
      pickup_label: form.elements.pickup_label.value.trim(),
      pickup_lat: parseFloat(form.elements.pickup_lat.value),
      pickup_lon: parseFloat(form.elements.pickup_lon.value),
      dropoff_label: form.elements.dropoff_label.value.trim(),
      dropoff_lat: parseFloat(form.elements.dropoff_lat.value),
      dropoff_lon: parseFloat(form.elements.dropoff_lon.value),
      pickup_time: new Date(form.elements.pickup_time.value).toISOString(),
      vehicle_class: form.elements.vehicle_class.value,
      notes: form.elements.notes.value.trim() || null
    };

    fetch('/trips', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify(payload)
    })
    .then(function (r) {
      if (!r.ok) return r.json().then(function (err) {
        throw new Error(err.detail ? JSON.stringify(err.detail) : 'HTTP ' + r.status);
      });
      return r.json();
    })
    .then(function (trip) {
      if (!broadcast) {
        showToast('Trip #' + trip.id + ' created (not broadcast).');
        closeDrawer();
        loadMapData();
        return;
      }
      return fetch('/trips/' + trip.id + '/broadcast', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrfToken() }
      })
        .then(function (br) {
          if (!br.ok) return br.json().then(function (err) {
            throw new Error('Created trip #' + trip.id + ', but broadcast failed: '
              + (err.detail || ('HTTP ' + br.status)));
          });
          showToast('Trip #' + trip.id + ' created and broadcast.');
          closeDrawer();
          loadMapData();
        });
    })
    .catch(function (err) {
      showToast(err.message, true);
    })
    .finally(function () {
      btns.forEach(function (b) { b.disabled = false; });
    });
  }

  // -------------------------------------------------------------------------
  // Tabs
  // -------------------------------------------------------------------------
  document.querySelectorAll('.panel-tabs .tab').forEach(function (t) {
    t.addEventListener('click', function () { selectTab(t.dataset.tab); });
  });
  function selectTab(name) {
    document.querySelectorAll('.panel-tabs .tab').forEach(function (t) {
      var on = t.dataset.tab === name;
      t.classList.toggle('is-active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.panel-body').forEach(function (b) {
      b.hidden = (b.dataset.tab !== name);
    });
  }

  // -------------------------------------------------------------------------
  // Manual refresh
  // -------------------------------------------------------------------------
  document.getElementById('refreshNow').addEventListener('click', function () {
    loadMapData();
  });

  // -------------------------------------------------------------------------
  // Esc closes drawer / cancels dispatch
  // -------------------------------------------------------------------------
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (!drawer.hidden) closeDrawer();
      else if (dispatchMode) cancelDispatchFlow();
    }
  });

  // -------------------------------------------------------------------------
  // Reverse geocoding (Nominatim — free, polite)
  // -------------------------------------------------------------------------
  // Nominatim's usage policy asks for a unique User-Agent and < 1 req/sec.
  // Browsers won't let us set User-Agent, but the Referer (set automatically
  // by the browser to the dispatcher origin) is sufficient ID. We only call
  // this on dispatcher clicks, which is far below 1 rps in practice.
  function reverseGeocode(latlng) {
    var url = 'https://nominatim.openstreetmap.org/reverse?lat='
      + encodeURIComponent(latlng.lat) + '&lon=' + encodeURIComponent(latlng.lng)
      + '&format=json&zoom=16&addressdetails=0';
    return fetch(url, { headers: { 'Accept-Language': 'en' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || !j.display_name) return null;
        // Truncate the verbose Nominatim display name to the first 3 parts —
        // "1 Cinnamon Beach Way, St Simons Island, Glynn County" is what we want;
        // we don't need the state, country, and postcode appended.
        var parts = j.display_name.split(',').map(function (s) { return s.trim(); });
        return parts.slice(0, 3).join(', ');
      })
      .catch(function () { return null; });
  }

  // -------------------------------------------------------------------------
  // Toast
  // -------------------------------------------------------------------------
  var toastEl = document.getElementById('toast');
  var toastTimer = null;
  function showToast(msg, isError) {
    toastEl.textContent = msg;
    toastEl.classList.toggle('error', !!isError);
    toastEl.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, isError ? 6000 : 3500);
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c];
    });
  }
  function truncate(s, n) {
    if (s == null) return '';
    if (s.length <= n) return s;
    return s.slice(0, n - 1) + '…';
  }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function formatDateTime(iso) {
    if (!iso) return '—';
    // Render in the dispatcher's local timezone, matching how pickup_time was
    // entered (the datetime-local input is local-time). Showing the stored UTC
    // instant unlabeled here would read as local on an Eastern dispatch desk
    // and mislead the most operationally critical field by 4-5 hours.
    var d = new Date(iso);
    return (d.getMonth()+1) + '-' + pad(d.getDate()) + ' '
      + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function toLocalDatetimeLocal(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function badgeLabel(s) {
    return String(s || '').replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }
  function tierColor(tier) {
    return tier === 1 ? '#f3d684' : (tier === 2 ? '#a98944' : '#6e6857');
  }

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------
  loadMapData();
  setInterval(loadMapData, REFRESH_MS);
})();
"""


# Substitute the CSS and JS into the head/scripts strings now that they're
# defined. Done after definition so the multi-line `_CSS` and `_JS` strings can
# contain `%` / `{` characters without triggering format-string parsing.
_HEAD = _HEAD.replace("__CSS_PLACEHOLDER__", _CSS)
_SCRIPTS = _SCRIPTS.replace("__JS_PLACEHOLDER__", _JS)
