"""Driver-facing web surface — Phase 4.5 walking skeleton.

A minimal browser-based chauffeur dashboard sitting on top of the existing
REST + auth machinery. Year-1 reality is that drivers receive trip
broadcasts via Twilio SMS and respond ACCEPT/DECLINE. This surface is the
follow-on that closes the SMS-reply brittleness for the Sunday-evening RSM
window: same accept/decline, but in-browser with one tap, and with a real
identity (bearer token) so we can extend to GPS pings and push later.

Walking-skeleton scope (v0.9.9). Out of scope, deferred to subsequent
releases:

    - Service Worker / manifest.json (PWA shell, install-to-home-screen).
    - Web Push notifications (Service Worker + VAPID).
    - Background GPS pings (Geolocation API + heartbeat to /drivers/{id}/location).
    - Trip detail view with map deep-link.
    - Status transitions beyond ACCEPT/DECLINE (EN_ROUTE → ON_SCENE → IN_TRANSIT → COMPLETED).

Style follows the same brass-on-dark Fireworks aesthetic as /dashboard and
/admin/*. CSS is inlined so the page works without external assets — a
chauffeur loading this on hotel-wifi in Brunswick shouldn't need Cloudflare
to be reachable for the page to render.
"""

from __future__ import annotations

import html as _html

from sqlalchemy import select
from sqlalchemy.orm import Session

import csp as _csp
from _styles import _fmt_dt, _fmt_fare, _status_label
from models import (
    BroadcastResponse,
    Driver,
    Trip,
    TripBroadcast,
    TripStatus,
)


_ACTIVE_TRIP_STATES = (
    TripStatus.ACCEPTED,
    TripStatus.EN_ROUTE_PICKUP,
    TripStatus.ON_SCENE,
    TripStatus.IN_TRANSIT,
)


# Valid forward-only transitions a chauffeur can drive from the trip detail
# page. Keys are the current status; values are the *next* status they can
# step into. The dispatcher's admin surface can still set any status (e.g.,
# cancel a trip out-of-band); this map only governs the driver-facing
# one-tap buttons. Mirrors the trip lifecycle in GUIDE.md.
DRIVER_NEXT_STATUS: dict[TripStatus, tuple[TripStatus, str]] = {
    TripStatus.ACCEPTED:        (TripStatus.EN_ROUTE_PICKUP, "Mark on the way"),
    TripStatus.EN_ROUTE_PICKUP: (TripStatus.ON_SCENE,        "Mark arrived"),
    TripStatus.ON_SCENE:        (TripStatus.IN_TRANSIT,      "Start trip"),
    TripStatus.IN_TRANSIT:      (TripStatus.COMPLETED,       "Mark complete"),
}


def driver_can_see_trip(session: Session, driver: Driver, trip: Trip) -> bool:
    """A driver may view a trip if (a) they're the assigned driver OR
    (b) the trip has an open broadcast addressed to them. Anything else
    is a 403 — drivers don't get to snoop on the wider dispatch picture.

    Caller is the route handler in app.py; this helper keeps the policy
    test-isolatable from the FastAPI dependency injection layer.
    """
    if trip.assigned_driver_id == driver.id:
        return True
    bc = session.scalars(
        select(TripBroadcast).where(
            TripBroadcast.trip_id == trip.id,
            TripBroadcast.driver_id == driver.id,
        )
    ).first()
    return bc is not None


def _doc(title: str, body: str, *, body_attrs: str = "", csrf_token: str = "") -> str:
    csrf_meta = (
        f'<meta name="csrf-token" content="{_html.escape(csrf_token)}" />'
        if csrf_token else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="robots" content="noindex, nofollow" />
{csrf_meta}
<!-- v0.9.10 PWA shell: manifest + Service Worker registered at end of body.
     Lets chauffeurs add to home screen and launch full-screen. Apple touch
     icon falls back to the same SVG; modern iOS Safari handles it. -->
<link rel="manifest" href="/driver/manifest.json" />
<link rel="icon" href="/driver/icon.svg" type="image/svg+xml" />
<link rel="apple-touch-icon" href="/driver/icon.svg" />
<meta name="theme-color" content="#161311" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<meta name="apple-mobile-web-app-title" content="Driver" />
<title>{_html.escape(title)} — Livery Dispatch driver</title>
<style{_csp.nonce_attr()}>
:root {{
  --ink:#0e0d0a; --paper:#fbf6e8; --paper-soft:rgba(251,246,232,0.65);
  --rule:rgba(201,169,97,0.28); --brass:#c9a961; --brass-hi:#dfc07a;
  --brass-deep:#8a6a32; --bg:#161311;
}}
* {{ box-sizing:border-box; }}
html, body {{ margin:0; padding:0; background:var(--bg); color:var(--paper);
  font:14px/1.5 "Hoefler Text","Garamond",serif;
  min-height:100%; }}
header.topbar {{ padding:16px 20px; border-bottom:1px solid var(--rule);
  display:flex; align-items:baseline; gap:14px;
  background:linear-gradient(180deg,#1c1916 0%,#161311 100%); }}
header.topbar .brand {{ font:600 17px/1 "Hoefler Text","Garamond",serif;
  letter-spacing:0.04em; }}
header.topbar .brand em {{ color:var(--brass-hi); font-style:italic; }}
header.topbar .who {{ margin-left:auto; font-size:11.5px;
  color:var(--paper-soft); letter-spacing:0.12em; text-transform:uppercase; }}
main.driver {{ padding:20px; max-width:560px; margin:0 auto; }}
section.block {{ margin:0 0 24px; padding:14px 16px;
  border:1px solid var(--rule); border-radius:3px;
  background:rgba(255,255,255,0.015); }}
section.block h2 {{ margin:0 0 10px; font:600 12px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.18em; text-transform:uppercase; color:var(--brass-hi); }}
.status-pill {{ display:inline-block; font:600 10.5px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.14em; text-transform:uppercase; padding:4px 10px;
  border-radius:2px; background:rgba(58,53,39,0.4); color:var(--paper-soft); }}
.status-pill.available {{ background:rgba(90,140,74,0.25); color:#a5d499; }}
.status-pill.on_trip {{ background:rgba(201,169,97,0.25); color:var(--brass-hi); }}
.status-pill.off_duty {{ background:rgba(136,186,234,0.18); color:#a3c8e8; }}
.status-pill.offline {{ background:rgba(120,110,90,0.2); color:rgba(251,246,232,0.55); }}
.empty {{ font-style:italic; color:var(--paper-soft); padding:8px 0; }}
.trip-card {{ padding:12px 0; border-top:1px solid var(--rule); }}
.trip-card:first-child {{ border-top:0; padding-top:0; }}
.trip-head {{ display:flex; gap:10px; align-items:baseline; }}
.trip-head .id {{ color:var(--brass); font:600 13px/1 "Lucida Console",monospace; }}
.trip-head .vc {{ font-size:10.5px; letter-spacing:0.14em; text-transform:uppercase;
  background:rgba(201,169,97,0.18); color:var(--brass-hi);
  padding:2px 7px; border-radius:2px; }}
.trip-meta {{ color:var(--paper-soft); font-size:12.5px; margin:4px 0 6px; }}
.trip-route {{ font:14px/1.4 "Hoefler Text","Garamond",serif; margin:4px 0 10px; }}
.trip-route .arrow {{ color:var(--brass); margin:0 6px; }}
.trip-actions {{ display:flex; gap:8px; margin-top:6px; }}
.trip-actions form {{ margin:0; flex:1; }}
.trip-actions button {{ width:100%; font:600 11.5px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.16em; text-transform:uppercase; padding:12px 14px;
  border-radius:3px; cursor:pointer; border:1px solid var(--brass-deep); }}
.trip-actions .accept {{ background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%);
  color:#16110a; }}
.trip-actions .decline {{ background:linear-gradient(180deg,#3a3527 0%,#22201a 100%);
  color:var(--brass-hi); }}
.flash {{ margin:0 0 16px; padding:10px 14px; border-radius:3px;
  border:1px solid var(--rule); background:rgba(201,169,97,0.06);
  color:var(--paper); font-size:13px; }}
.login-form {{ max-width:380px; margin:50px auto; padding:24px;
  border:1px solid var(--rule); border-radius:4px; background:rgba(255,255,255,0.02); }}
.login-form h1 {{ margin:0 0 8px; font:600 18px/1.2 "Hoefler Text","Garamond",serif; }}
.login-form p {{ margin:0 0 18px; color:var(--paper-soft); font-size:13px; }}
.login-form label {{ display:block; font:600 10.5px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.16em; text-transform:uppercase; color:var(--paper-soft);
  margin:14px 0 5px; }}
.login-form input[type="text"] {{ width:100%; padding:10px 12px;
  background:rgba(255,255,255,0.04); color:var(--paper); border:1px solid var(--rule);
  border-radius:2px; font:14px/1 "Lucida Console",monospace; }}
.login-form input[type="text"]:focus {{ outline:none; border-color:var(--brass);
  box-shadow:0 0 0 2px rgba(201,169,97,0.18); }}
.login-form button {{ margin-top:18px; width:100%; font:600 11.5px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.18em; text-transform:uppercase; padding:13px 16px;
  border-radius:3px; cursor:pointer; border:1px solid var(--brass-deep);
  background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%); color:#16110a; }}
.login-form .err {{ color:#f0a899; margin:10px 0 0; font-size:13px; }}
.back-link {{ font-size:12px; color:var(--paper-soft); text-decoration:none;
  letter-spacing:0.08em; }}
.back-link:hover {{ color:var(--brass-hi); }}
.map-link {{ color:var(--brass-hi); text-decoration:none; font-size:13px;
  letter-spacing:0.04em; }}
.map-link:hover {{ color:var(--brass); text-decoration:underline; }}
.map-link.primary {{ display:inline-block; padding:10px 16px;
  background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%); color:#16110a;
  border-radius:3px; font:600 11.5px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.14em; text-transform:uppercase; }}
.map-link.primary:hover {{ filter:brightness(1.1); text-decoration:none; }}
.map-row {{ margin:8px 0 0; }}
.directions-block {{ background:rgba(201,169,97,0.04); }}
.status-step {{ background:rgba(201,169,97,0.06); }}
.status-step form {{ margin:0; }}
.status-step-btn {{ width:100%; font:600 13px/1 "Lucida Grande",sans-serif;
  letter-spacing:0.18em; text-transform:uppercase; padding:16px 18px;
  border-radius:3px; cursor:pointer; border:1px solid var(--brass-deep);
  background:linear-gradient(180deg,#dfc07a 0%,#b8924d 100%); color:#16110a; }}
.status-step-btn:hover {{ filter:brightness(1.1); }}
.trip-detail-link {{ display:inline-block; margin-top:6px; font-size:12px;
  color:var(--brass-hi); text-decoration:none; }}
.trip-detail-link:hover {{ text-decoration:underline; }}
/* Small text styles that were inline style="" attributes — moved here because
   the nonce-only style-src CSP (no 'unsafe-inline') blocks inline style attrs. */
.muted-meta {{ margin:6px 0 0; color:var(--paper-soft); font-size:12.5px; }}
.push-intro {{ font-size:12.5px; color:var(--paper-soft); margin:0 0 10px; }}
.push-status {{ margin:8px 0 0; font-size:12px; color:var(--paper-soft); }}
.notes-pre {{ white-space:pre-wrap; margin:0; }}
footer.foot {{ padding:16px 20px; color:var(--paper-soft); font-size:11px;
  text-align:center; border-top:1px solid var(--rule); margin-top:24px;
  letter-spacing:0.04em; }}
</style>
</head>
<body {body_attrs}>
{body}
<script{_csp.nonce_attr()}>
// v0.9.10 PWA shell: register the Service Worker for offline app-shell.
// Wrapped + guarded so legacy browsers (no SW support) just no-op.
if ('serviceWorker' in navigator) {{
  window.addEventListener('load', function () {{
    navigator.serviceWorker.register('/driver/sw.js', {{ scope: '/driver/' }})
      .catch(function () {{ /* SW registration failed — page still works */ }});
  }});
}}

// v0.9.13 — GPS heartbeat. Pings POST /driver/location every 30s with the
// browser's current coords. Body[data-on-trip="1"] gates the loop so we
// don't burn battery while the chauffeur is parked at home with the page
// open. First ping fires immediately on load so the dispatcher's live map
// sees movement within seconds of login.
(function () {{
  if (!('geolocation' in navigator)) return;
  if (document.body.dataset.onTrip !== '1') return;
  // Interval is configurable via data-ping-interval-ms on body for tests;
  // production default is 30 seconds.
  var intervalMs = parseInt(document.body.dataset.pingIntervalMs || '30000', 10);

  // v0.9.14 — include the CSRF token from the page's <meta> so cookie-auth
  // POSTs aren't rejected. Bearer-auth fetches would skip the check anyway,
  // but the dashboard fetch path uses the cookie set by /driver/login.
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';

  function postLoc(pos) {{
    var headers = {{ 'Content-Type': 'application/json' }};
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
    return fetch('/driver/location', {{
      method: 'POST',
      headers: headers,
      credentials: 'same-origin',
      body: JSON.stringify({{ lat: pos.coords.latitude, lon: pos.coords.longitude }}),
    }}).catch(function () {{ /* network blip — next tick retries */ }});
  }}
  function tick() {{
    navigator.geolocation.getCurrentPosition(
      postLoc,
      function () {{ /* user denied / timeout — silently skip this tick */ }},
      {{ enableHighAccuracy: false, timeout: 8000, maximumAge: 10000 }}
    );
  }}
  tick();
  setInterval(tick, intervalMs);
}})();

// v1.1.0 — Web Push subscribe button. The dashboard renders a hidden
// #push-block; this script asks the server whether push is enabled +
// fetches the VAPID public key, reveals the block, and wires the button
// to navigator.serviceWorker.ready.then(reg => reg.pushManager.subscribe()).
(function () {{
  var btn = document.getElementById('push-enable-btn');
  if (!btn) return;
  var block = document.getElementById('push-block');
  var status = document.getElementById('push-status');
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {{
    if (status) status.textContent = 'This browser does not support push notifications.';
    if (block) block.hidden = false;
    btn.disabled = true;
    return;
  }}

  function setStatus(msg) {{ if (status) status.textContent = msg; }}

  // Base64url → Uint8Array for applicationServerKey.
  function urlBase64ToUint8Array(base64String) {{
    var padding = '='.repeat((4 - base64String.length % 4) % 4);
    var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    var raw = atob(base64);
    var arr = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; ++i) arr[i] = raw.charCodeAt(i);
    return arr;
  }}

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';

  fetch('/driver/push/vapid-key', {{ credentials: 'same-origin' }})
    .then(function (r) {{ return r.ok ? r.json() : null; }})
    .then(function (cfg) {{
      if (!cfg) return;
      if (!cfg.enabled || !cfg.publicKey) {{
        block.hidden = false;
        btn.disabled = true;
        setStatus('Push notifications not yet enabled for this account. Dispatcher will reach you by SMS.');
        return;
      }}
      block.hidden = false;

      // v1.1.18 (E10): the server had DELETE /driver/push/subscribe since
      // v1.1.0 but nothing called it, so a chauffeur couldn't turn push off.
      // When this device is already subscribed, show "Turn off": unsubscribe
      // in the browser, then drop the server row so broadcasts fall back to SMS.
      var offBtn = document.getElementById('push-disable-btn');
      function showOn(subscription) {{
        btn.hidden = true;
        if (!offBtn) return;
        offBtn.hidden = false;
        offBtn.disabled = false;
        offBtn.onclick = function () {{
          offBtn.disabled = true;
          var endpoint = subscription.endpoint;
          subscription.unsubscribe().catch(function () {{ /* already gone */ }}).then(function () {{
            var headers = {{ 'Content-Type': 'application/json' }};
            if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
            return fetch('/driver/push/subscribe', {{
              method: 'DELETE',
              headers: headers,
              credentials: 'same-origin',
              body: JSON.stringify({{ endpoint: endpoint }}),
            }});
          }}).then(function (r) {{
            // The browser subscription is gone either way, so show the
            // enable button again; but only claim success if the server
            // dropped its row too. Otherwise the next push to the dead
            // endpoint gets a 410 and the server cleans up then.
            offBtn.hidden = true;
            btn.hidden = false;
            btn.disabled = false;
            setStatus(r && r.ok
              ? 'Notifications are off. The dispatcher will reach you by SMS.'
              : 'Notifications are off on this device. The server will catch up on the next broadcast.');
          }}).catch(function () {{
            offBtn.disabled = false;
            setStatus('Could not turn off notifications. Try again.');
          }});
        }};
      }}
      navigator.serviceWorker.ready
        .then(function (reg) {{ return reg.pushManager.getSubscription(); }})
        .then(function (existing) {{
          if (existing) {{
            showOn(existing);
            setStatus('Notifications are on for this device.');
          }}
        }})
        .catch(function () {{ /* no SW yet — leave the enable button */ }});

      btn.addEventListener('click', function () {{
        btn.disabled = true;
        setStatus('Requesting permission...');
        Notification.requestPermission().then(function (perm) {{
          if (perm !== 'granted') {{
            btn.disabled = false;
            setStatus('Permission denied. SMS remains your channel.');
            return;
          }}
          return navigator.serviceWorker.ready.then(function (reg) {{
            return reg.pushManager.subscribe({{
              userVisibleOnly: true,
              applicationServerKey: urlBase64ToUint8Array(cfg.publicKey),
            }});
          }}).then(function (subscription) {{
            var sub = subscription.toJSON();
            var headers = {{ 'Content-Type': 'application/json' }};
            if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
            return fetch('/driver/push/subscribe', {{
              method: 'POST',
              headers: headers,
              credentials: 'same-origin',
              body: JSON.stringify({{ endpoint: sub.endpoint, keys: sub.keys }}),
            }}).then(function (r) {{
              if (r.ok) {{
                showOn(subscription);
                setStatus('Notifications enabled. The dispatcher will reach you here.');
              }} else {{
                btn.disabled = false;
                setStatus('Could not register this device. Try again or use SMS.');
              }}
            }});
          }});
        }}).catch(function () {{
          btn.disabled = false;
          setStatus('Could not enable notifications. SMS remains your channel.');
        }});
      }});
    }})
    .catch(function () {{ /* feature off or network blip — silently leave block hidden */ }});
}})();
</script>
</body>
</html>
"""


def render_login_page(*, error: str | None = None, flash: str | None = None) -> str:
    """Token-entry form. Submits POST /driver/login which sets a cookie."""
    err_html = f'<p class="err">{_html.escape(error)}</p>' if error else ""
    flash_html = f'<div class="flash" role="status">{_html.escape(flash)}</div>' if flash else ""
    body = f"""
<header class="topbar">
  <span class="brand">Livery <em>Dispatch</em></span>
  <span class="who">Driver</span>
</header>
<main class="driver">
{flash_html}
<form method="post" action="/driver/login" class="login-form" novalidate>
  <h1>Sign in</h1>
  <p>Enter the access token your dispatcher gave you. Tap-and-hold the
     "Save changes" message in SMS to copy the long string.</p>
  <label for="dt-token">Access token</label>
  <input type="text" id="dt-token" name="token" required autocomplete="off"
         spellcheck="false" autocapitalize="off" />
  {err_html}
  <button type="submit">Sign in</button>
</form>
</main>
<footer class="foot">Internal — not for public distribution.</footer>
"""
    return _doc("Sign in", body)


def render_dashboard(session: Session, driver: Driver, *,
                     flash: str | None = None, csrf_token: str = "") -> str:
    """Driver dashboard — pending broadcasts addressed to this driver, plus
    their currently-assigned trip if any."""

    # Active trip they're currently working — there should be at most one
    # since `accept_trip` is atomic + driver state flips to ON_TRIP on
    # acceptance.
    active_trip = session.scalars(
        select(Trip).where(
            Trip.assigned_driver_id == driver.id,
            Trip.status.in_(_ACTIVE_TRIP_STATES),
        ).order_by(Trip.id.desc())
    ).first()

    # Pending broadcasts addressed to this driver — ones where the trip is
    # still BROADCASTING and this driver hasn't responded yet. The view also
    # excludes broadcasts for trips this driver has already accepted (they
    # show up in active_trip above).
    pending_broadcasts: list[tuple[TripBroadcast, Trip]] = []
    bcs = session.scalars(
        select(TripBroadcast).where(
            TripBroadcast.driver_id == driver.id,
            TripBroadcast.response == BroadcastResponse.PENDING,
        ).order_by(TripBroadcast.sent_at.desc())
    ).all()
    for bc in bcs:
        trip = session.get(Trip, bc.trip_id) if bc.trip_id else None
        if trip is None or trip.status != TripStatus.BROADCASTING:
            continue
        pending_broadcasts.append((bc, trip))

    flash_html = f'<div class="flash" role="status">{_html.escape(flash)}</div>' if flash else ""

    status_pill = (
        f'<span class="status-pill {driver.status.value}">'
        f'{_status_label(driver.status)}</span>'
    )

    you_block = f"""
<section class="block">
  <h2>You</h2>
  <div><strong>{_html.escape(driver.name)}</strong> &middot; {status_pill}</div>
  <p class="muted-meta">
    {_html.escape(driver.phone)} &middot; classes {_html.escape(driver.vehicle_classes.upper())}
  </p>
</section>
<section class="block" id="push-block" hidden>
  <h2>Notifications</h2>
  <p class="push-intro">
    Enable push so the dispatcher can reach you when SMS is slow.
  </p>
  <button type="button" id="push-enable-btn" class="primary">Enable notifications</button>
  <button type="button" id="push-disable-btn" class="secondary" hidden>Turn off notifications</button>
  <p id="push-status" class="push-status"></p>
</section>"""

    if active_trip is not None:
        route = (
            f'{_html.escape(active_trip.pickup_label)} '
            f'<span class="arrow">&rarr;</span> {_html.escape(active_trip.dropoff_label)}'
        )
        active_block = f"""
<section class="block">
  <h2>Your current trip</h2>
  <div class="trip-card">
    <div class="trip-head">
      <span class="id">#{active_trip.id}</span>
      <span class="vc">{_html.escape(active_trip.vehicle_class.value.upper())}</span>
      <span class="status-pill {active_trip.status.value}">
        {_status_label(active_trip.status)}
      </span>
    </div>
    <div class="trip-meta">
      Pickup {_html.escape(_fmt_dt(active_trip.pickup_time))} &middot;
      passenger {_html.escape(active_trip.passenger_name)}
    </div>
    <div class="trip-route">{route}</div>
    <a class="trip-detail-link" href="/driver/trips/{active_trip.id}">Trip details &rarr;</a>
  </div>
</section>"""
    else:
        active_block = ""

    if pending_broadcasts:
        cards = []
        for bc, trip in pending_broadcasts:
            route = (
                f'{_html.escape(trip.pickup_label)} '
                f'<span class="arrow">&rarr;</span> {_html.escape(trip.dropoff_label)}'
            )
            cards.append(f"""
<div class="trip-card">
  <div class="trip-head">
    <span class="id">#{trip.id}</span>
    <span class="vc">{_html.escape(trip.vehicle_class.value.upper())}</span>
  </div>
  <div class="trip-meta">
    Pickup {_html.escape(_fmt_dt(trip.pickup_time))} &middot;
    passenger {_html.escape(trip.passenger_name)} &middot;
    {bc.distance_mi:.1f} mi from you
  </div>
  <div class="trip-route">{route}</div>
  <div class="trip-actions">
    <form method="post" action="/driver/trips/{trip.id}/accept">
      <input type="hidden" name="csrf_token" value="{_html.escape(csrf_token)}" />
      <button type="submit" class="accept">Accept</button>
    </form>
    <form method="post" action="/driver/trips/{trip.id}/decline">
      <input type="hidden" name="csrf_token" value="{_html.escape(csrf_token)}" />
      <button type="submit" class="decline">Decline</button>
    </form>
  </div>
  <a class="trip-detail-link" href="/driver/trips/{trip.id}">Trip details &rarr;</a>
</div>""")
        broadcast_block = (
            '<section class="block"><h2>Open broadcasts</h2>'
            + "".join(cards) + "</section>"
        )
    elif active_trip is None:
        broadcast_block = (
            '<section class="block"><h2>Open broadcasts</h2>'
            '<div class="empty">No open broadcasts — dispatcher will ping when a trip lands.</div>'
            '</section>'
        )
    else:
        # Already on a trip — don't show empty broadcasts state on top.
        broadcast_block = ""

    body = f"""
<header class="topbar">
  <span class="brand">Livery <em>Dispatch</em></span>
  <span class="who">{_html.escape(driver.name)}</span>
</header>
<main class="driver">
{flash_html}
{you_block}
{active_block}
{broadcast_block}
</main>
<footer class="foot">Internal — not for public distribution.</footer>
"""
    # Tell the inline GPS-ping script (v0.9.13) whether to fire heartbeats.
    # Only ping while the driver has an active trip — no need to burn battery
    # when they're parked at home checking the broadcast queue.
    on_trip_attr = 'data-on-trip="1"' if active_trip is not None else 'data-on-trip="0"'
    return _doc("Driver", body, body_attrs=on_trip_attr, csrf_token=csrf_token)


def _native_map_links(lat: float, lon: float, label: str) -> str:
    """Render three platform-specific map deep links for a single point.

    - `geo:LAT,LON` opens Android's default map app.
    - `https://maps.apple.com/?ll=…&q=…` opens Apple Maps on iOS.
    - `https://www.google.com/maps/search/?api=1&query=…` is the web
      fallback for desktop browsers and Android devices without a
      registered `geo:` handler.

    All three are rendered side-by-side; the chauffeur taps whichever
    matches their phone. No JS sniffing — the chauffeur picks.
    """
    label_safe = _html.escape(label or "")
    return (
        f'<a class="map-link" href="geo:{lat},{lon}?q={lat},{lon}({label_safe})">Android Maps</a>'
        f' &middot; '
        f'<a class="map-link" href="https://maps.apple.com/?ll={lat},{lon}&q={label_safe}"'
        f' target="_blank" rel="noopener noreferrer">Apple Maps</a>'
        f' &middot; '
        f'<a class="map-link" href="https://www.google.com/maps/search/?api=1&query={lat},{lon}"'
        f' target="_blank" rel="noopener noreferrer">Google Maps</a>'
    )


def _directions_link(p_lat: float, p_lon: float, d_lat: float, d_lon: float) -> str:
    """Google Maps directions URL — works in any browser, hands off to
    Maps app on phones. Stays as a single fallback so the chauffeur isn't
    juggling three "directions" choices on top of three "open in maps"
    choices for each endpoint."""
    return (
        f'<a class="map-link primary"'
        f' href="https://www.google.com/maps/dir/?api=1&origin={p_lat},{p_lon}&destination={d_lat},{d_lon}"'
        f' target="_blank" rel="noopener noreferrer">Pickup &rarr; Dropoff directions</a>'
    )


def render_trip_detail_page(session: Session, driver: Driver, trip: Trip,
                            *, flash: str | None = None, csrf_token: str = "") -> str:
    """Trip detail for a chauffeur — passenger info + map deep-links.

    Caller is `app.py`'s GET /driver/trips/{id}; this renderer assumes
    `driver_can_see_trip(session, driver, trip)` already passed."""
    status_label = _status_label(trip.status)
    flash_html = f'<div class="flash" role="status">{_html.escape(flash)}</div>' if flash else ""

    fare = _fmt_fare(trip.fare_cents, grouped=True)
    notes_html = (
        f'<section class="block"><h2>Dispatcher notes</h2>'
        f'<p class="notes-pre">{_html.escape(trip.notes)}</p></section>'
        if trip.notes else ""
    )

    pickup_links = _native_map_links(trip.pickup_lat, trip.pickup_lon, trip.pickup_label)
    dropoff_links = _native_map_links(trip.dropoff_lat, trip.dropoff_lon, trip.dropoff_label)
    directions = _directions_link(trip.pickup_lat, trip.pickup_lon,
                                  trip.dropoff_lat, trip.dropoff_lon)

    # Status-transition button — v0.9.12. Only renders if (a) the driver is
    # the assigned driver, and (b) there's a valid next state from the
    # current one. Each click POSTs to /driver/trips/{id}/status/{new} and
    # PRG-redirects back to this page with a flash.
    status_action = ""
    if trip.assigned_driver_id == driver.id and trip.status in DRIVER_NEXT_STATUS:
        next_status, label = DRIVER_NEXT_STATUS[trip.status]
        status_action = f"""
<section class="block status-step">
  <h2>Next step</h2>
  <form method="post" action="/driver/trips/{trip.id}/status/{next_status.value}">
    <input type="hidden" name="csrf_token" value="{_html.escape(csrf_token)}" />
    <button type="submit" class="status-step-btn">{_html.escape(label)} &rarr;</button>
  </form>
</section>"""

    body = f"""
<header class="topbar">
  <a href="/driver" class="back-link">&larr; Dashboard</a>
  <span class="brand">Livery <em>Dispatch</em></span>
  <span class="who">{_html.escape(driver.name)}</span>
</header>
<main class="driver">
{flash_html}
<section class="block">
  <h2>Trip #{trip.id} &middot; {_html.escape(status_label)}</h2>
  <div class="info-row">
    <strong>{_html.escape(trip.passenger_name)}</strong>
    {' &middot; <a href="tel:' + _html.escape(trip.passenger_phone) + '">' + _html.escape(trip.passenger_phone) + '</a>' if trip.passenger_phone else ''}
  </div>
  <p class="trip-meta">
    Pickup {_html.escape(_fmt_dt(trip.pickup_time))} &middot;
    {_html.escape(trip.vehicle_class.value.upper())} &middot;
    fare {_html.escape(fare)}
  </p>
</section>
{status_action}
<section class="block">
  <h2>Pickup</h2>
  <p class="trip-route">{_html.escape(trip.pickup_label)}</p>
  <p class="map-row">{pickup_links}</p>
</section>
<section class="block">
  <h2>Dropoff</h2>
  <p class="trip-route">{_html.escape(trip.dropoff_label)}</p>
  <p class="map-row">{dropoff_links}</p>
</section>
<section class="block directions-block">
  <h2>Directions</h2>
  <p class="map-row">{directions}</p>
</section>
{notes_html}
</main>
<footer class="foot">Internal — not for public distribution.</footer>
"""
    # GPS ping (v0.9.13): only while the viewer is the assigned driver AND
    # the trip is in an active state. Broadcast-only viewers don't ping.
    on_trip = (trip.assigned_driver_id == driver.id and trip.status in _ACTIVE_TRIP_STATES)
    on_trip_attr = 'data-on-trip="1"' if on_trip else 'data-on-trip="0"'
    return _doc(f"Trip #{trip.id}", body, body_attrs=on_trip_attr, csrf_token=csrf_token)
