"""FastAPI application entry point."""

from __future__ import annotations

import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from xml.sax.saxutils import escape as _xml_escape

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

import admin_auth as _auth
import admin_pages as _admin
import anchors as _anchors
import csp as _csp
import driver_pages as _driver_pages
import core
import dashboard as _dashboard
import map_view as _map_view
from database import get_session, init_db
from fastapi import Form
from fastapi.responses import RedirectResponse
from models import (
    AdminAuditLog,
    CallLog,
    Driver,
    DriverPushSubscription,
    DriverStatus,
    Inquiry,
    InquiryStatus,
    RateCard,
    Reservation,
    ReservationStatus,
    Subcontractor,
    Trip,
    TripStatus,
    VehicleClass,
)
import fare as _fare
import osrm_proxy as _osrm
from notifications import get_provider, push_notifications_enabled
from schemas import (
    DriverCreate,
    DriverLocationUpdate,
    DriverOut,
    DriverStatusUpdate,
    InquiryCreate,
    InquiryOut,
    InquiryUpdate,
    MapData,
    PushSubscriptionIn,
    PushSubscriptionOut,
    PushUnsubscribeIn,
    PushVAPIDKeyOut,
    QuoteRequest,
    QuoteResponse,
    QuoteLine,
    ReservationCreate,
    ReservationOut,
    ReservationUpdate,
    SubAcceptRequest,
    SubcontractorCreate,
    SubcontractorOut,
    SubcontractorUpdate,
    TripBroadcastOut,
    TripCreate,
    TripOut,
    TripStatusUpdate,
    WebhookAck,
    _normalize_phone,
)
import json as _json


# Map the site form's vehicle-preference dropdown to the dispatch enum. The
# "multi-vehicle" case stays None — that's a wedding-party / group inquiry the
# dispatcher resolves manually (one Sprinter? two Escalades + one Sprinter?).
_VEHICLE_PREF_TO_CLASS: dict[str, VehicleClass] = {
    "escalade-esv": VehicleClass.SUV,
    "sprinter-limited": VehicleClass.SPRINTER,
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


# E2 (v1.1.17): no interactive API docs in production. /docs, /redoc and
# /openapi.json are auth-exempt (Swagger UI can't carry the admin session +
# CSRF token anyway), so in production they would publish the full route map
# to anyone. Disabling them at construction means the routes don't exist —
# 404, not 401. Dev and tests keep them.
_IS_PRODUCTION = os.getenv("DISPATCH_ENV", "").lower() == "production"

app = FastAPI(
    title="Livery Dispatch",
    description="Dispatch system for a premium car service.",
    version="1.5.0",
    lifespan=lifespan,
    docs_url=None if _IS_PRODUCTION else "/docs",
    redoc_url=None if _IS_PRODUCTION else "/redoc",
    openapi_url=None if _IS_PRODUCTION else "/openapi.json",
)


# CORS — needed so the marketing site's reserve form (served from a different
# origin once both are deployed: Netlify for the static site, Fly.io / Railway
# for this API) can POST /reservations from the browser. Origins come from
# CORS_ORIGINS as a comma-separated list. In production (DISPATCH_ENV=production)
# we refuse to start with a permissive wildcard — that's a deploy-time safety net
# so a forgotten env var doesn't ship a wide-open API. Dev / tests default to "*".
_cors_origins_raw = os.getenv("CORS_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()] or ["*"]
if os.getenv("DISPATCH_ENV", "").lower() == "production" and (
    _cors_origins == ["*"] or any(o == "*" for o in _cors_origins)
):
    raise RuntimeError(
        "CORS_ORIGINS must be pinned to specific origins in production "
        "(DISPATCH_ENV=production). Set CORS_ORIGINS to your site origin(s), "
        "e.g. CORS_ORIGINS='https://example.com,https://www.example.com'."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request-size cap (v1.4.1)
# ---------------------------------------------------------------------------
#
# SECURITY.md's pre-production list wanted a proxy that caps request bodies:
# the pentest probes showed 50 KB+ bodies are rejected by the field limits,
# but only after the whole body has been read and parsed. Fly's proxy
# documents no body-size limit, so the cap lives here, ahead of everything
# that reads a body (_capture_body_for_audit, form parsing, the routes).
#
#   MAX_REQUEST_BYTES   default 32768 (32 KiB), re-read on every request like
#                       the rate-limit vars. Production refuses to boot on a
#                       value that isn't a whole number of at least 1024.
#
# The largest legitimate body is a few KB: a Twilio voicemail-transcription
# callback, or a bulk-spam form carrying a few hundred ids. Two ways to a 413:
# a Content-Length over the cap is refused before any of the body is read; a
# body sent without one (chunked) is read here, up to the cap, before the app
# sees it, and refused the moment it crosses. Pure ASGI rather than
# BaseHTTPMiddleware, so it can own `receive`. Registered just before
# _security_headers (so just inside it): the 413 still gets CSP,
# X-Frame-Options and HSTS.

MAX_REQUEST_BYTES_DEFAULT = 32 * 1024
_MAX_REQUEST_BYTES_FLOOR = 1024


def _parse_max_request_bytes(raw: str) -> int | None:
    """The cap ``raw`` asks for, or None if it isn't a whole number >= 1024."""
    try:
        value = int(raw.strip().replace("_", ""))
    except ValueError:
        return None
    return value if value >= _MAX_REQUEST_BYTES_FLOOR else None


def _max_request_bytes() -> int:
    raw = os.getenv("MAX_REQUEST_BYTES", "").strip()
    if not raw:
        return MAX_REQUEST_BYTES_DEFAULT
    return _parse_max_request_bytes(raw) or MAX_REQUEST_BYTES_DEFAULT


class _RequestSizeLimitMiddleware:
    def __init__(self, app):
        self.app = app

    @staticmethod
    async def _refuse(send, limit: int) -> None:
        body = (
            '{"detail":"Request body too large (limit %d bytes)"}' % limit
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # The rest of the body is never read, so the connection
                # can't be reused for another request.
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _max_request_bytes()

        declared = None
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None:
            # uvicorn frames the body by Content-Length, so the app can never
            # receive more than was declared.
            if declared > limit:
                await self._refuse(send, limit)
                return
            await self.app(scope, receive, send)
            return

        # No Content-Length (chunked, or no body at all): read the body here,
        # up to the cap, before the app sees any of it, then replay it. Reading
        # first, rather than counting inside the app's own reads, keeps the
        # 413 deterministic: FastAPI turns any exception raised while it
        # parses a body into a 400.
        buffered = []
        received = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break                                   # http.disconnect
            received += len(message.get("body", b""))
            if received > limit:
                await self._refuse(send, limit)
                return
            if not message.get("more_body", False):
                break

        async def replay_receive():
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)


# ---------------------------------------------------------------------------
# Production configuration fail-closed check (v1.0.3)
# ---------------------------------------------------------------------------
#
# When DISPATCH_ENV=production, refuse to boot unless every credential /
# secret / required-token env var is configured. Runs after the CORS guard
# above so CORS_ORIGINS errors surface first (most operators set it first).
#
# Silent fallback to a random per-process secret is fine in dev (admin
# sessions just don't survive restarts) but a deploy-time foot-gun in
# production: ops would never notice ADMIN_SESSION_SECRET was missing
# until admins started getting logged out at random intervals.
#
# Why each variable is gated:
#   ADMIN_USERNAME        — empty → admin login is unreachable
#   ADMIN_PASSWORD_HASH   — empty or non-bcrypt → authenticate() returns False
#                            for every password
#   ADMIN_SESSION_SECRET  — empty → falls back to random urlsafe-32; sessions
#                            do not survive restarts (operational, not crypto)
#   DRIVER_CSRF_SECRET    — same fallback issue on the driver surface
#   TWILIO_AUTH_TOKEN     — required for Twilio webhook signature verification;
#                            without it the /webhooks/twilio/* endpoints are
#                            unauthenticated and forgeable. Only gated when
#                            NOTIFICATION_PROVIDER=twilio (i.e. Twilio actually
#                            in use).
#
# DISPATCHER_PHONE is NOT gated — its absence degrades the Voice live-person
# flow (caller drops to voicemail instead of ringing the dispatcher) but doesn't
# create a security gap. Worth a startup warning, separate concern.

def _validate_production_config() -> None:
    if os.getenv("DISPATCH_ENV", "").lower() != "production":
        return

    errors: list[str] = []

    if not os.getenv("ADMIN_USERNAME", "").strip():
        errors.append("ADMIN_USERNAME must be set")

    pw_hash = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
    if not pw_hash:
        errors.append("ADMIN_PASSWORD_HASH must be set")
    elif not (pw_hash.startswith("$2a$") or pw_hash.startswith("$2b$") or pw_hash.startswith("$2y$")):
        errors.append(
            "ADMIN_PASSWORD_HASH must be a bcrypt hash (starts with $2a$/$2b$/$2y$). "
            "Generate with: python -c \"from admin_auth import hash_password; print(hash_password('yourpass'))\""
        )

    session_secret = os.getenv("ADMIN_SESSION_SECRET", "").strip()
    if len(session_secret) < 32:
        errors.append(
            "ADMIN_SESSION_SECRET must be at least 32 characters "
            "(generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\")"
        )

    driver_secret = os.getenv("DRIVER_CSRF_SECRET", "").strip()
    if len(driver_secret) < 32:
        errors.append(
            "DRIVER_CSRF_SECRET must be at least 32 characters "
            "(generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\")"
        )

    notif_provider = os.getenv("NOTIFICATION_PROVIDER", "console").lower()
    if notif_provider == "console":
        errors.append(
            "NOTIFICATION_PROVIDER=console must not be used in production — "
            "SMS bodies and passenger phone numbers print to stdout. "
            "Set NOTIFICATION_PROVIDER=twilio (and TWILIO_* credentials)."
        )
    if notif_provider == "twilio" and not os.getenv("TWILIO_AUTH_TOKEN", "").strip():
        errors.append(
            "TWILIO_AUTH_TOKEN must be set when NOTIFICATION_PROVIDER=twilio "
            "(required for webhook signature verification — without it, /webhooks/twilio/* is forgeable)"
        )
    # E4 (v1.1.17): notifications.get_provider() falls back to the console
    # provider when ANY of the three Twilio vars is missing. The token was
    # already required; without the SID or the FROM number production would
    # silently print SMS bodies + passenger phones to stdout and page nobody.
    if notif_provider == "twilio":
        for var in ("TWILIO_ACCOUNT_SID", "TWILIO_FROM_NUMBER"):
            if not os.getenv(var, "").strip():
                errors.append(
                    f"{var} must be set when NOTIFICATION_PROVIDER=twilio "
                    "(otherwise notifications.get_provider() silently falls back to "
                    "the console provider: no SMS sent, PII printed to logs)"
                )

    # E5 (v1.1.17): behind a TLS-terminating proxy (Fly.io) the app sees
    # http:// and the proxy's socket address, so (a) Twilio signatures — which
    # sign the public https URL — never verify, and (b) every visitor shares
    # one per-IP rate-limit bucket. Both are now explicit settings.
    public_base = os.getenv("PUBLIC_BASE_URL", "").strip()
    if notif_provider == "twilio" and not public_base.startswith("https://"):
        errors.append(
            "PUBLIC_BASE_URL must be set to the public https:// origin Twilio calls "
            "(e.g. https://dispatch.example.com) — Twilio signs that URL, and "
            "behind Fly's TLS proxy the app otherwise sees http:// and rejects every webhook"
        )
    if not os.getenv("CLIENT_IP_HEADER", "").strip():
        errors.append(
            "CLIENT_IP_HEADER must be set: the header your edge proxy SETS to the real "
            "client IP (Fly.io: CLIENT_IP_HEADER=Fly-Client-IP), or CLIENT_IP_HEADER=none "
            "when nothing sits in front of uvicorn. Without it every visitor shares the "
            "proxy's IP and one rate-limit bucket"
        )

    # v1.4.1: a typo here would otherwise fall back to the default silently.
    raw_cap = os.getenv("MAX_REQUEST_BYTES", "").strip()
    if raw_cap and _parse_max_request_bytes(raw_cap) is None:
        errors.append(
            f"MAX_REQUEST_BYTES={raw_cap!r} must be a whole number of bytes, at least "
            f"{_MAX_REQUEST_BYTES_FLOOR} (or unset for the {MAX_REQUEST_BYTES_DEFAULT}-byte default)"
        )

    # Web push (v1.1.0) — when the feature flag is on, VAPID credentials
    # are required. Without them the broadcast loop would crash mid-fanout
    # trying to sign push payloads.
    if os.getenv("PUSH_NOTIFICATIONS_ENABLED", "false").lower() == "true":
        if not os.getenv("VAPID_PRIVATE_KEY", "").strip():
            errors.append(
                "VAPID_PRIVATE_KEY must be set when PUSH_NOTIFICATIONS_ENABLED=true "
                "(generate with: from py_vapid import Vapid; v = Vapid(); v.generate_keys(); "
                "v.save_key('vapid_priv.pem'); v.save_public_key('vapid_pub.pem'))"
            )
        if not os.getenv("VAPID_PUBLIC_KEY", "").strip():
            errors.append(
                "VAPID_PUBLIC_KEY must be set when PUSH_NOTIFICATIONS_ENABLED=true"
            )
        if not os.getenv("VAPID_CONTACT_EMAIL", "").strip():
            errors.append(
                "VAPID_CONTACT_EMAIL must be set when PUSH_NOTIFICATIONS_ENABLED=true "
                "(VAPID spec requires a mailto:/https: contact for the push service)"
            )

    if errors:
        raise RuntimeError(
            "Production configuration is incomplete. Set DISPATCH_ENV=production "
            "only when these are configured:\n  - " + "\n  - ".join(errors)
        )



def _warn_on_public_osrm_in_production() -> None:
    """Non-fatal: the public OSRM demo is non-commercial-use-only with no
    SLA, so a paid production deploy should point OSRM_BASE_URL at the
    self-hosted container (osrm/, DEPLOY.md). Warn rather than
    refuse — every routing consumer degrades gracefully (fare -> haversine,
    maps -> straight-line skeleton), so it must not block a soft launch."""
    if os.getenv("DISPATCH_ENV", "").lower() == "production" and _osrm.using_public_demo():
        import logging
        logging.getLogger("dispatch").warning(
            "OSRM_BASE_URL points at the public OSRM demo (non-commercial use only, "
            "~1 req/s, no SLA). Self-host it: see osrm/README.md."
        )


def _warn_on_public_tiles_in_production() -> None:
    """Non-fatal (v1.3.0): without MAP_SERVER_URL the dispatcher's /map draws
    OpenStreetMap's public standard tiles, whose usage policy discourages heavy
    commercial use. Point it at our own map server (gis/server/) instead."""
    if os.getenv("DISPATCH_ENV", "").lower() == "production" and not _map_view.map_server_url():
        import logging
        logging.getLogger("dispatch").warning(
            "MAP_SERVER_URL is not set: /map uses OpenStreetMap's public tiles. Deploy our "
            "map server (gis/server/README.md) and set MAP_SERVER_URL to its https origin."
        )


_validate_production_config()
_warn_on_public_osrm_in_production()
_warn_on_public_tiles_in_production()


# ---------------------------------------------------------------------------
# HTTP security headers (v1.0.2)
# ---------------------------------------------------------------------------
#
# Defense-in-depth headers applied to every response:
#
#   Content-Security-Policy   — clickjacking via frame-ancestors 'none';
#                                XSS exfiltration via locked img/connect/font
#                                sources; per-request `'nonce-{nonce}'` on
#                                script-src/style-src (v1.0.13) — every
#                                inline <script>/<style> the templates emit
#                                carries the matching nonce attribute, so
#                                'unsafe-inline' is gone and an HTML-injection
#                                attacker can't execute <script>...</script>
#                                because they can't guess the 128-bit nonce.
#   Strict-Transport-Security — production-only. Dev runs over HTTP, where
#                                HSTS is a no-op anyway.
#   X-Frame-Options           — DENY. Redundant with frame-ancestors for
#                                modern browsers, but older clients still
#                                check this header.
#   X-Content-Type-Options    — nosniff. Stops MIME-confusion attacks on
#                                user-supplied bytes (e.g. uploaded CSV /
#                                voicemail transcripts rendered later).
#   Referrer-Policy           — strict-origin-when-cross-origin. Prevents
#                                URL paths (which include trip IDs, driver
#                                tokens in error edge cases) from leaking
#                                in cross-origin Referer headers.
#   Permissions-Policy        — driver PWA needs geolocation; everything
#                                else (camera, mic, payment, USB sensors)
#                                stays off.
#   Cross-Origin-Opener-Policy — same-origin. Hardens against side-channel
#                                attacks (Spectre-class window.opener probes).
#
# /docs, /redoc, /openapi.json are exempted from CSP — Swagger UI loads JS
# from jsdelivr.net and a strict policy would break the dev docs. They exist
# only outside production: the FastAPI(...) constructor disables them when
# DISPATCH_ENV=production (E2, v1.1.17). HSTS, X-Frame, etc. still apply.
#
# Registration order (E1, v1.1.17): this middleware is registered LAST, at the
# bottom of the middleware section, which makes it the OUTERMOST layer
# (Starlette: last added = outermost). Before v1.1.17 it was innermost, so the
# 401 / 307 / 403 responses that the auth and CSRF middlewares return
# directly never passed through it and shipped without CSP, X-Frame-Options or
# HSTS. Keep it last; tests/test_prod_hardening.py pins the order.

_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}' https://unpkg.com; "
    "style-src 'self' 'nonce-{nonce}' https://unpkg.com; "
    # Map tiles (v1.3.0): OpenStreetMap's standard tiles, plus our own map server
    # (QGIS Server) when MAP_SERVER_URL is set. {map_server} is "" or " https://host".
    # CARTO's free tiles left the policy when they started demanding an API key.
    "img-src 'self' data: blob: https://tile.openstreetmap.org https://unpkg.com{map_server}; "
    "connect-src 'self' https://nominatim.openstreetmap.org; "
    "font-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'; "
    "manifest-src 'self'; "
    "worker-src 'self'"
)

def _map_server_src() -> str:
    """" https://maps.example" for the CSP img-src, or "" (MAP_SERVER_URL, v1.3.0)."""
    origin = _map_view.map_server_url()
    return f" {origin}" if origin else ""


_PERMISSIONS_POLICY = (
    "geolocation=(self), microphone=(), camera=(), payment=(), "
    "usb=(), accelerometer=(), gyroscope=(), magnetometer=()"
)

# Only outside production — in production those routes don't exist (E2), so
# nothing needs the exemption and the 404 they return gets the full CSP.
_CSP_EXEMPT_PATHS = (
    frozenset() if _IS_PRODUCTION else frozenset({"/docs", "/redoc", "/openapi.json"})
)


async def _security_headers(request: Request, call_next):
    # Generate the per-request CSP nonce BEFORE the handler runs so the
    # rendering helpers (admin_pages, driver_pages, map_view, dashboard)
    # can read it from the contextvar and inject `nonce="..."` into every
    # inline <script>/<style> tag.
    nonce = _csp.generate_nonce()
    _csp.set_nonce(nonce)

    response = await call_next(request)
    if request.url.path not in _CSP_EXEMPT_PATHS:
        response.headers.setdefault(
            "Content-Security-Policy",
            _CSP_TEMPLATE.format(nonce=nonce, map_server=_map_server_src()),
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if os.getenv("DISPATCH_ENV", "").lower() == "production":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains; preload",
        )
    return response


# ---------------------------------------------------------------------------
# Rate limiting (public form endpoints: /reservations, /inquiries)
# ---------------------------------------------------------------------------
#
# Public unauthenticated endpoints are spam magnets without a rate cap. We use a
# small in-process per-IP sliding-window counter — first line of defense, not the
# only line. Production should pair this with an upstream gateway (Cloudflare,
# NGINX, API Gateway) for stronger DDoS protection. The in-process limiter
# resets on restart, so it's a brake, not a bank vault.
#
# `/reservations` and `/inquiries` share one per-IP bucket: 6 form submissions
# from the same IP in 60 seconds is suspicious regardless of which form they
# hit. A wedding planner submitting one wedding-inquiry + a reservation for a
# coordinator's site visit + an early-access signup is a real flow (3 hits);
# anything past the cap looks like a script.
#
# Configuration via env so tests can disable it cleanly (otherwise tests
# bursting >limit calls would collide with normal tests sharing the process):
#   PUBLIC_FORM_RATE_LIMIT_ENABLED  default "true"
#   PUBLIC_FORM_RATE_WINDOW_S       default "60"
#   PUBLIC_FORM_RATE_MAX            default "5" (per IP per window)
_rate_history: dict[str, list[float]] = {}

# Separate bucket for login endpoints (v1.0.4). Brute-forcing a 32-byte
# URL-safe driver token is computationally infeasible on entropy alone, but
# rate limiting is cheap belt-and-suspenders against compromised-token replay
# and slowloris-style credential stuffing. Public-form bucket is intentionally
# separate so a chauffeur retrying a typo'd token can't get blocked by a
# wedding-planner spamming the contact form from the same NAT'd IP.
_login_rate_history: dict[str, list[float]] = {}
# Own bucket for the public /osrm route proxy (v1.1.15): the corridor page
# and resort pages fire a few route requests per view, which must not eat
# the 5/min public-form budget a visitor needs to then submit a booking.
_osrm_rate_history: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Rate-limit key for a request (E5, v1.1.17).

    ``CLIENT_IP_HEADER`` names a header the edge proxy SETS to the real client
    address (Fly.io: ``Fly-Client-IP``). Only use a header the proxy
    overwrites, never one it appends to: a client can send its own
    ``X-Forwarded-For``. If the value is a list anyway, the LAST entry is used,
    since that one was written by the hop closest to us. Unset or ``none`` means
    no proxy, so the socket peer is used, which was the only behaviour before
    v1.1.17. A configured header that's missing on a request also falls back
    to the socket peer.
    """
    header = os.getenv("CLIENT_IP_HEADER", "").strip()
    if header and header.lower() != "none":
        raw = (request.headers.get(header) or "").strip()
        if raw:
            return raw.split(",")[-1].strip()[:64] or "unknown"
    return (request.client.host if request.client else "unknown") or "unknown"


def _rate_limited(
    history: dict[str, list[float]],
    request: Request,
    *,
    enabled_env: str,
    window_env: str,
    window_default: float,
    max_env: str,
    max_default: int,
) -> bool:
    """Shared per-IP sliding-window limiter. Both wrappers below delegate here;
    env vars are re-read on every call so tests can monkeypatch and verify."""
    if os.getenv(enabled_env, "true").lower() != "true":
        return False
    try:
        window_s = float(os.getenv(window_env, str(window_default)))
        max_hits = int(os.getenv(max_env, str(max_default)))
    except (TypeError, ValueError):
        window_s, max_hits = window_default, max_default
    ip = _client_ip(request)
    now = time.monotonic()
    cutoff = now - window_s
    hits = history.setdefault(ip, [])
    while hits and hits[0] < cutoff:
        hits.pop(0)
    if len(hits) >= max_hits:
        return True
    hits.append(now)
    return False


def _login_rate_limited(request: Request) -> bool:
    """Per-IP rate limit on login endpoints. More permissive than the
    public-form limiter — a chauffeur retrying after a typo shouldn't hit the
    same wall as a script. Tunable via:
      LOGIN_RATE_LIMIT_ENABLED  default "true"
      LOGIN_RATE_WINDOW_S       default "60"
      LOGIN_RATE_MAX            default "10" (per IP per window)
    """
    return _rate_limited(
        _login_rate_history, request,
        enabled_env="LOGIN_RATE_LIMIT_ENABLED",
        window_env="LOGIN_RATE_WINDOW_S", window_default=60.0,
        max_env="LOGIN_RATE_MAX", max_default=10,
    )


def _osrm_proxy_rate_limited(request: Request) -> bool:
    """Per-IP limit on the public GET /osrm/* route proxy. Generous — a map
    page makes 1-2 route calls and most are cache hits — but it caps anyone
    using the endpoint as a free routing relay. Tunable via:
      OSRM_PROXY_RATE_LIMIT_ENABLED  default "true"
      OSRM_PROXY_RATE_WINDOW_S       default "60"
      OSRM_PROXY_RATE_MAX            default "60" (per IP per window)
    """
    return _rate_limited(
        _osrm_rate_history, request,
        enabled_env="OSRM_PROXY_RATE_LIMIT_ENABLED",
        window_env="OSRM_PROXY_RATE_WINDOW_S", window_default=60.0,
        max_env="OSRM_PROXY_RATE_MAX", max_default=60,
    )


def _public_form_rate_limited(request: Request) -> bool:
    """Return True if the request exceeds the per-IP window. Checked at call
    time (env vars re-read on every call) so tests can monkeypatch and verify.
    Tunable via:
      PUBLIC_FORM_RATE_LIMIT_ENABLED  default "true"
      PUBLIC_FORM_RATE_WINDOW_S       default "60"
      PUBLIC_FORM_RATE_MAX            default "5" (per IP per window)
    """
    return _rate_limited(
        _rate_history, request,
        enabled_env="PUBLIC_FORM_RATE_LIMIT_ENABLED",
        window_env="PUBLIC_FORM_RATE_WINDOW_S", window_default=60.0,
        max_env="PUBLIC_FORM_RATE_MAX", max_default=5,
    )


# ---------------------------------------------------------------------------
# Admin audit log middleware
# ---------------------------------------------------------------------------
#
# Logs every write (POST/PATCH/DELETE) to /admin/* paths. Append-only — the
# row is inserted after the response is produced so it captures the status
# code. payload_hash is SHA-256 of the raw request body; the body itself is
# NOT stored to avoid PII bloat. user_id is the authenticated admin username
# (admin auth landed v1.0.0); it is null only for the rare unauthenticated
# write that still reaches an /admin/* path before the auth gate rejects it.

import hashlib as _hashlib
from starlette.middleware.base import BaseHTTPMiddleware


class _AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        method = request.method
        path = request.url.path
        if method in ("POST", "PATCH", "DELETE") and path.startswith("/admin/"):
            body_bytes = getattr(request.state, "_audit_body", b"")
            payload_hash = _hashlib.sha256(body_bytes).hexdigest() if body_bytes else None
            user_id = _auth.get_admin_user(request)
            from database import SessionLocal
            with SessionLocal() as sess:
                sess.add(AdminAuditLog(
                    user_id=user_id,
                    method=method,
                    path=path,
                    payload_hash=payload_hash,
                    status_code=response.status_code,
                ))
                sess.commit()
        return response


@app.middleware("http")
async def _capture_body_for_audit(request: Request, call_next):
    """Read and cache the request body before it's consumed by form parsing,
    so the audit middleware can hash it without double-reading. Also extracts
    the CSRF token from form submissions for the admin auth middleware.

    The body is read for two independent reasons: every /admin/* write is
    audit-hashed, and every admin-gated write that arrived without an
    X-CSRF-Token header (flagged by the auth middleware) needs its form body
    checked for a ``csrf_token`` field. Since v1.1.13 the second case also
    covers the REST surface (/trips, /drivers, /subcontractors, PATCH
    /reservations/{id}, PATCH /inquiries/{id}) — see ``_csrf_required``."""
    method = request.method
    path = request.url.path
    needs_csrf = getattr(request.state, "_needs_csrf_check", False)
    is_admin_write = method in ("POST", "PATCH", "DELETE") and path.startswith("/admin/")
    if is_admin_write or needs_csrf:
        body = await request.body()
        if is_admin_write:
            request.state._audit_body = body
        # CSRF check for form submissions — the auth middleware flagged this
        # request as needing a form-body CSRF check.
        if needs_csrf:
            from urllib.parse import parse_qs
            ct = (request.headers.get("content-type") or "").lower()
            csrf_val = ""
            if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
                try:
                    parsed = parse_qs(body.decode("utf-8", errors="replace"))
                    csrf_val = (parsed.get("csrf_token", [""])[0]).strip()
                except Exception:
                    pass
            if not csrf_val:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Missing CSRF token"}, status_code=403)
            cookie = request.cookies.get(_auth.COOKIE_NAME) or ""
            expected = _auth.make_admin_csrf_token(cookie)
            import hmac as _hmac_mod
            if not _hmac_mod.compare_digest(csrf_val, expected):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Invalid CSRF token"}, status_code=403)
    return await call_next(request)


app.add_middleware(_AuditLogMiddleware)


# ---------------------------------------------------------------------------
# Admin auth middleware (v1.0.0)
# ---------------------------------------------------------------------------
#
# Gates every admin-facing route behind the session cookie. Public endpoints
# (/health, /reservations POST, /inquiries POST, /quote, /webhooks/*, /driver/*)
# are excluded. The driver surface has its own per-driver auth (Phase 4.5).

_AUTH_EXEMPT = frozenset({
    "/health", "/", "/admin/login", "/admin/logout",
    "/docs", "/openapi.json", "/redoc",
})


def _route_requires_admin(method: str, path: str) -> bool:
    if path in _AUTH_EXEMPT:
        return False
    if path.startswith("/driver/") or path.startswith("/webhooks/"):
        return False
    if path.startswith("/admin/") or path.startswith("/map"):
        return True
    if path == "/dashboard":
        return True
    if path.startswith("/trips") or path.startswith("/subcontractors"):
        return True
    if path.startswith("/drivers"):
        return True
    if path in ("/reservations", "/inquiries", "/quote"):
        # Bare collection paths: POST is the public marketing-site form
        # (open); GET (list) is dispatcher-only.
        return method == "GET"
    if path.startswith("/reservations/") or path.startswith("/inquiries/"):
        # Detail + PATCH on a specific reservation/inquiry are dispatcher-only
        # for EVERY method. These previously fell through to the open default
        # below because the check above exact-matches the collection path —
        # an unauthenticated IDOR that leaked customer PII (name / email /
        # phone / flight / itinerary / dispatcher notes) and allowed status
        # tampering by enumerating ids. (/quote has no detail route, so it
        # needs no sub-path gating.) The /admin/reservations/* HTML surface is
        # already gated by the /admin/ prefix branch above.
        return True
    return False


_CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CSRF_EXEMPT = frozenset({"/admin/login", "/admin/logout"})


def _csrf_required(method: str, path: str) -> bool:
    """True for every state-changing request on the admin-gated surface.

    Admin auth is cookie-only (no bearer variant), so every admin-gated
    mutation is a cookie-auth mutation and gets the same CSRF contract as
    /admin/* forms: a token bound to the session cookie, presented as an
    ``X-CSRF-Token`` header or a ``csrf_token`` form field.

    Before v1.1.13 this was ``path.startswith("/admin/")``, which left the
    REST mutations (/trips, /drivers, /subcontractors, PATCH
    /reservations/{id}, PATCH /inquiries/{id}) relying on SameSite=Lax + the
    JSON preflight alone. Public POSTs (/reservations, /inquiries, /quote)
    aren't admin-gated, so they stay token-free for the marketing site.
    """
    if method not in _CSRF_METHODS or path in _CSRF_EXEMPT:
        return False
    return _route_requires_admin(method, path)


@app.middleware("http")
async def _admin_auth_middleware(request: Request, call_next):
    if _route_requires_admin(request.method, request.url.path):
        user = _auth.get_admin_user(request)
        if user is None:
            accept = request.headers.get("accept", "")
            if "text/html" in accept:
                return RedirectResponse(url="/admin/login", status_code=307)
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        # Set CSRF token in contextvar for form rendering.
        cookie = request.cookies.get(_auth.COOKIE_NAME) or ""
        _admin.set_csrf_token(_auth.make_admin_csrf_token(cookie))
        # CSRF on every admin-gated cookie-auth mutation — mirrors the driver
        # CSRF pattern. The token is bound to the session cookie value so a
        # cross-session token can't be replayed.
        if _csrf_required(request.method, request.url.path):
            csrf_header = (request.headers.get("x-csrf-token") or "").strip()
            if csrf_header:
                expected = _auth.make_admin_csrf_token(cookie)
                import hmac as _hmac_mod
                if not _hmac_mod.compare_digest(csrf_header, expected):
                    from fastapi.responses import JSONResponse
                    return JSONResponse({"detail": "Invalid CSRF token"}, status_code=403)
            else:
                # Form submissions carry the token in the body — checked in
                # _capture_body_for_audit (inner middleware, after the body
                # is read). JSON requests without the header 403 there too.
                request.state._needs_csrf_check = True
    return await call_next(request)


# v1.4.1: the request-size cap sits just inside _security_headers, outside
# everything that reads a body. See the comment block above
# _RequestSizeLimitMiddleware.
app.add_middleware(_RequestSizeLimitMiddleware)


# E1 (v1.1.17): registered last on purpose → outermost. Every response —
# including the ones _admin_auth_middleware / _capture_body_for_audit return
# without calling the app (401, 307 to /admin/login, CSRF 403) — now gets the
# security headers. See the comment block above _CSP_TEMPLATE.
app.middleware("http")(_security_headers)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "service": "livery-dispatch", "version": app.version}


@app.get("/", include_in_schema=False)
def root_redirect():
    """Bare-domain hit lands on the dispatcher dashboard.
    Prevents the confusing `{"detail":"Not Found"}` 404 when someone visits
    `http://localhost:8000/` without knowing about /dashboard or /map."""
    return RedirectResponse(url="/dashboard", status_code=307)


# ---------------------------------------------------------------------------
# Dispatcher dashboard
# ---------------------------------------------------------------------------
#
# Single auto-refreshing HTML page that surfaces everything that needs the
# dispatcher's attention: active trips, pending reservations, pending
# inquiries, driver statuses, and subcontractor roster. Renders without
# JavaScript (uses meta-refresh) so it survives an internet hiccup. See
# dashboard.py for the template + style.
#
# Year-1 reality: the dispatcher sits at /dashboard during operating hours. No
# auth on this route Year-1 (same posture as the rest of the REST surface);
# put it behind a VPN, basic-auth proxy, or auth-token gate before exposing
# to the public internet. Tracked under the Phase 4.5 per-driver-auth gap.


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(session: Session = Depends(get_session)):
    return HTMLResponse(_dashboard.render_dashboard(session))


# ---------------------------------------------------------------------------
# Live dispatcher map
# ---------------------------------------------------------------------------
#
# Full-page Leaflet + CARTO Dark Matter map of every entity in the system:
# driver positions (live, color-coded by status), subcontractor bases (with
# service-radius rings), and active trip pickups+dropoffs (with OSRM-routed
# lines connecting them). The page polls `/map/data` every ~12 seconds for
# refreshed positions and supports click-to-dispatch: pick pickup on the map,
# pick dropoff on the map, fill the modal, and the trip is created and
# broadcast in one move.
#
# Same auth posture as /dashboard: no per-route auth Year-1, sits behind the
# same VPN / basic-auth proxy as the rest of the surface.


@app.get("/map", response_class=HTMLResponse, include_in_schema=False)
def dispatcher_map(request: Request):
    # The auth middleware already gated this route; mint the CSRF token from
    # the same session cookie it verified so the map's REST writes carry it.
    cookie = request.cookies.get(_auth.COOKIE_NAME) or ""
    return HTMLResponse(_map_view.render_map_page(
        csrf_token=_auth.make_admin_csrf_token(cookie) if cookie else ""
    ))


@app.get("/map/data", response_model=MapData)
def map_data(session: Session = Depends(get_session)):
    return _map_view.build_map_data(session)


# ---------------------------------------------------------------------------
# Admin pages — editable list + detail views for every entity
# ---------------------------------------------------------------------------
#
# Server-rendered HTML; form POSTs PRG-pattern (POST → process → 303 See Other
# back to the detail page with a flash message in the query string). No
# JavaScript framework, no client state to manage. Works in any browser,
# survives an internet hiccup. Same Fireworks aesthetic as /dashboard.


def _flash_msg(request: Request) -> str | None:
    return request.query_params.get("ok")


# ---------------------------------------------------------------------------
# Admin authentication (v1.0.0)
# ---------------------------------------------------------------------------


@app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
def admin_login_page(request: Request):
    if _auth.get_admin_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return HTMLResponse(_admin.render_login_page(flash=_flash_msg(request)))


@app.post("/admin/login", include_in_schema=False)
def admin_login_submit(request: Request,
                       username: str = Form(""),
                       password: str = Form("")):
    # Rate limit (v1.0.8) — shares the login bucket with /driver/login so a
    # single attacker burning attempts on the admin password also burns
    # against their driver-token guesses. Separate from the public-form
    # bucket so a wedding-planner spamming /inquiries can't lock out the
    # dispatcher trying to log in.
    if _login_rate_limited(request):
        return HTMLResponse(
            _admin.render_login_page(error="Too many attempts. Wait a moment and try again."),
            status_code=429,
        )
    username = username.strip()
    password = password.strip()
    if not username or not password:
        return HTMLResponse(
            _admin.render_login_page(error="Username and password are required."),
            status_code=400,
        )
    if not _auth.authenticate(username, password):
        return HTMLResponse(
            _admin.render_login_page(error="Invalid credentials."),
            status_code=401,
        )
    _is_prod = os.getenv("DISPATCH_ENV", "").lower() == "production"
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        _auth.COOKIE_NAME,
        value=_auth.create_session_cookie(username),
        httponly=True,
        samesite="lax",
        secure=_is_prod,
        max_age=_auth.SESSION_MAX_AGE,
        path="/",
    )
    return response


@app.post("/admin/logout", include_in_schema=False)
def admin_logout():
    response = RedirectResponse(url="/admin/login?ok=Signed+out.", status_code=303)
    response.delete_cookie(_auth.COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Driver-surface authentication (Phase 4.5 walking skeleton)
# ---------------------------------------------------------------------------
#
# Each Driver row carries an opaque `auth_token` (lazy-generated). The driver
# web surface accepts the token via either:
#
#   1. `Authorization: Bearer <token>` header — for direct API calls.
#   2. `driver_token` cookie — set by the login form, used by subsequent
#      page navigation. Cookie is HttpOnly so JS can't read it; Lax SameSite
#      so concurrent navigation works without breaking the dispatcher's
#      separate browser session.
#
# Both routes resolve to the same `get_authenticated_driver` dependency
# which returns the Driver row or raises 401. Walking-skeleton constraints
# (intentional, for the v0.9.9 scope):
#
#   - No token expiry — rotation via admin surface is the revocation path.
#   - No CSRF protection on the POST handlers yet — the cookie is SameSite
#     Lax which mitigates most cross-site POSTs, but a hardened version
#     would add a CSRF token or require the bearer-header variant for
#     state-changing calls. Tracked under v1.0 hardening.

_DRIVER_TOKEN_COOKIE = "driver_token"

# CSRF protection (v0.9.14) — defense-in-depth on top of the SameSite=Lax
# cookie. Bearer-auth POSTs skip CSRF entirely (other origins can't set
# Authorization headers via simple form submits); cookie-auth POSTs MUST
# present a matching `csrf_token` form field OR `X-CSRF-Token` header.
#
# Secret resolution:
#   - Env var `DRIVER_CSRF_SECRET` if set (production must set this — token
#     stability across uvicorn restarts depends on it).
#   - Otherwise a per-process random secret minted at module load. Tokens
#     issued by one uvicorn instance are invalid after restart; acceptable
#     for dev/test, NOT for production.

def _resolve_csrf_secret() -> bytes:
    val = os.getenv("DRIVER_CSRF_SECRET")
    if val:
        return val.encode("utf-8")
    import secrets as _secrets
    return _secrets.token_urlsafe(32).encode("utf-8")


_CSRF_SECRET = _resolve_csrf_secret()


def make_csrf_token(driver_id: int) -> str:
    """HMAC-SHA256 hex digest of the driver's id signed with the server
    secret. Driver-id-bound so a token for driver A can't be used to
    authorize a POST as driver B."""
    import hashlib
    import hmac
    return hmac.new(
        _CSRF_SECRET,
        str(driver_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _request_used_bearer_auth(request: Request) -> bool:
    """True if the request was authenticated via the `Authorization: Bearer`
    header (rather than the cookie). Bearer-auth requests don't need CSRF
    because cross-site requests can't set arbitrary Authorization headers."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return False
    parts = auth.strip().split(None, 1)
    return len(parts) == 2 and parts[0].lower() == "bearer"


def verify_csrf_token(request: Request, driver: Driver, form_token: str | None) -> None:
    """Raises HTTPException(403) when CSRF validation fails. Call after
    `get_authenticated_driver` so we already know who the request claims
    to be. Bearer-auth requests skip the check; cookie-auth requests must
    present a token matching `make_csrf_token(driver.id)` via either the
    form field or the `X-CSRF-Token` header."""
    if _request_used_bearer_auth(request):
        return
    header_token = request.headers.get("x-csrf-token") or request.headers.get("X-CSRF-Token")
    presented = (form_token or header_token or "").strip()
    if not presented:
        raise HTTPException(status_code=403, detail="Missing CSRF token")
    expected = make_csrf_token(driver.id)
    import hmac as _hmac
    if not _hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _extract_driver_token(request: Request) -> str | None:
    """Pull the driver token from the Authorization header first, then the
    cookie. Returns None if neither is set or the header has the wrong
    shape (we silently ignore malformed headers rather than 401 immediately
    so the cookie fallback still gets a chance)."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth:
        parts = auth.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
            if token:
                return token
    cookie_val = request.cookies.get(_DRIVER_TOKEN_COOKIE)
    if cookie_val:
        return cookie_val.strip() or None
    return None


def get_authenticated_driver(request: Request,
                             session: Session = Depends(get_session)) -> Driver:
    """FastAPI Depends — returns the Driver matching the request's bearer
    token, or raises 401. Use on every /driver/* route that needs identity.

    Walking-skeleton choice: tokens are matched directly against the
    `drivers.auth_token` column (URL-safe random 32-byte). No HMAC, no
    expiry. Rotation via /admin/drivers/{id}/action/rotate-token
    invalidates the prior token immediately."""
    token = _extract_driver_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing driver token")
    driver = session.scalars(select(Driver).where(Driver.auth_token == token)).first()
    if driver is None:
        raise HTTPException(status_code=401, detail="Invalid driver token")
    return driver


# ---------------------------------------------------------------------------
# Driver web surface (Phase 4.5 walking skeleton)
# ---------------------------------------------------------------------------

@app.get("/driver/login", response_class=HTMLResponse, include_in_schema=False)
def driver_login_page(request: Request):
    return HTMLResponse(_driver_pages.render_login_page(flash=_flash_msg(request)))


@app.post("/driver/login", include_in_schema=False)
def driver_login_submit(request: Request,
                        token: str = Form(""),
                        session: Session = Depends(get_session)):
    # Default "" rather than Form(...) so an empty submission reaches the
    # handler's own 400 branch. Newer Starlette rejects required empty
    # strings at validation with 422 before this code runs.
    if _login_rate_limited(request):
        return HTMLResponse(
            _driver_pages.render_login_page(error="Too many attempts. Wait a moment and try again."),
            status_code=429,
        )
    token = token.strip()
    if not token:
        return HTMLResponse(
            _driver_pages.render_login_page(error="Enter your access token."),
            status_code=400,
        )
    driver = session.scalars(select(Driver).where(Driver.auth_token == token)).first()
    if driver is None:
        return HTMLResponse(
            _driver_pages.render_login_page(error="Token not recognized. Check with dispatch."),
            status_code=401,
        )
    # Cookie is HttpOnly so JS can't read it; Lax SameSite so navigating to
    # the dispatcher's surface in another tab still works. Not setting
    # `secure=True` here because dev runs over plain HTTP; production
    # deploys must terminate TLS (Fly/Render/Cloudflare).
    _is_prod = os.getenv("DISPATCH_ENV", "").lower() == "production"
    response = RedirectResponse(url="/driver", status_code=303)
    response.set_cookie(
        _DRIVER_TOKEN_COOKIE,
        value=driver.auth_token or "",
        httponly=True,
        samesite="lax",
        secure=_is_prod,
        max_age=60 * 60 * 24 * 30,  # 30 days
        path="/driver",
    )
    return response


@app.post("/driver/logout", include_in_schema=False)
def driver_logout():
    response = RedirectResponse(url="/driver/login", status_code=303)
    response.delete_cookie(_DRIVER_TOKEN_COOKIE, path="/driver")
    return response


@app.get("/driver", response_class=HTMLResponse, include_in_schema=False)
def driver_dashboard(request: Request,
                     driver: Driver = Depends(get_authenticated_driver),
                     session: Session = Depends(get_session)):
    return HTMLResponse(_driver_pages.render_dashboard(
        session, driver, flash=_flash_msg(request),
        csrf_token=make_csrf_token(driver.id),
    ))


@app.get("/driver/trips/{trip_id}", response_class=HTMLResponse, include_in_schema=False)
def driver_trip_detail(trip_id: int, request: Request,
                       driver: Driver = Depends(get_authenticated_driver),
                       session: Session = Depends(get_session)):
    """Trip detail for a chauffeur. The driver may view the trip only if
    they're the assigned driver OR have an open broadcast on it — see
    `_driver_pages.driver_can_see_trip`. Anything else returns 403."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    if not _driver_pages.driver_can_see_trip(session, driver, trip):
        raise HTTPException(status_code=403, detail="Not your trip")
    return HTMLResponse(_driver_pages.render_trip_detail_page(
        session, driver, trip, flash=_flash_msg(request),
        csrf_token=make_csrf_token(driver.id),
    ))


@app.post("/driver/trips/{trip_id}/accept", include_in_schema=False)
def driver_action_accept(trip_id: int, request: Request,
                         csrf_token: str | None = Form(None),
                         driver: Driver = Depends(get_authenticated_driver),
                         session: Session = Depends(get_session)):
    verify_csrf_token(request, driver, csrf_token)
    try:
        core.accept_trip(session, trip_id=trip_id, driver_id=driver.id)
    except ValueError as exc:
        return RedirectResponse(url=f"/driver?ok={_urlencode(f'Could not accept: {exc}')}", status_code=303)
    return RedirectResponse(url=f"/driver?ok={_urlencode(f'Accepted trip #{trip_id}.')}", status_code=303)


@app.post("/driver/trips/{trip_id}/decline", include_in_schema=False)
def driver_action_decline(trip_id: int, request: Request,
                          csrf_token: str | None = Form(None),
                          driver: Driver = Depends(get_authenticated_driver),
                          session: Session = Depends(get_session)):
    verify_csrf_token(request, driver, csrf_token)
    try:
        core.decline_trip(session, trip_id=trip_id, driver_id=driver.id)
    except ValueError as exc:
        return RedirectResponse(url=f"/driver?ok={_urlencode(f'Could not decline: {exc}')}", status_code=303)
    return RedirectResponse(url=f"/driver?ok={_urlencode(f'Declined trip #{trip_id}.')}", status_code=303)


@app.post("/driver/location", include_in_schema=False)
def driver_action_location(payload: DriverLocationUpdate, request: Request,
                           driver: Driver = Depends(get_authenticated_driver),
                           session: Session = Depends(get_session)):
    """v0.9.13 — chauffeur PWA heartbeat. The inline JS in driver_pages
    pings here every ~30s with navigator.geolocation coords. The driver
    is identified by their bearer token (or driver_token cookie), so the
    path doesn't carry the id — eliminates the chance of a driver
    spoofing another driver's location.

    Returns JSON `{"ok": true, "lat": …, "lon": …}` so the JS can
    surface a brief acknowledgement in dev tools. No HTML redirect —
    this is a fetch endpoint, not a form submission.
    """
    # JSON fetch endpoint — no form body, so the CSRF token comes via the
    # X-CSRF-Token header. verify_csrf_token() reads either form or header.
    verify_csrf_token(request, driver, None)
    driver.lat = payload.lat
    driver.lon = payload.lon
    session.commit()
    return {"ok": True, "lat": driver.lat, "lon": driver.lon}


# ---------------------------------------------------------------------------
# Web push subscriptions (v1.1.0)
# ---------------------------------------------------------------------------


@app.get("/driver/push/vapid-key", include_in_schema=False)
def driver_push_vapid_key(driver: Driver = Depends(get_authenticated_driver)) -> PushVAPIDKeyOut:
    """Return the VAPID public key the browser uses in `pushManager.subscribe()`.

    Public key is intentionally not a secret — the W3C spec says clients
    must have it. We gate the endpoint on driver auth anyway so the public
    key isn't trivially scrapable from anonymous traffic. `enabled=false`
    means the feature flag is off; the dashboard renders the button in a
    disabled state with a tooltip explaining why."""
    return PushVAPIDKeyOut(
        publicKey=os.getenv("VAPID_PUBLIC_KEY", "").strip(),
        enabled=push_notifications_enabled(),
    )


@app.post("/driver/push/subscribe", include_in_schema=False)
def driver_push_subscribe(
    payload: PushSubscriptionIn,
    request: Request,
    driver: Driver = Depends(get_authenticated_driver),
    session: Session = Depends(get_session),
):
    """Persist a push subscription for the authenticated driver.

    Re-subscribing from the same (driver, endpoint) tuple updates the row
    rather than creating a duplicate — the W3C subscribe call can return a
    fresh `keys` payload on the same endpoint (e.g. after a browser clears
    storage), and we want the most recent material to take effect.

    CSRF: cookie-auth POSTs require X-CSRF-Token; bearer-auth skips per
    the existing pattern (matches /driver/location)."""
    verify_csrf_token(request, driver, None)

    existing = session.scalars(
        select(DriverPushSubscription).where(
            DriverPushSubscription.driver_id == driver.id,
            DriverPushSubscription.endpoint == payload.endpoint,
        )
    ).first()

    user_agent = request.headers.get("user-agent", "")[:300] or None

    if existing is not None:
        existing.p256dh = payload.keys.p256dh
        existing.auth = payload.keys.auth
        existing.user_agent = user_agent
        # last_seen_at refreshes via the onupdate hook.
        session.commit()
        session.refresh(existing)
        return PushSubscriptionOut.model_validate(existing)

    sub = DriverPushSubscription(
        driver_id=driver.id,
        endpoint=payload.endpoint,
        p256dh=payload.keys.p256dh,
        auth=payload.keys.auth,
        user_agent=user_agent,
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return PushSubscriptionOut.model_validate(sub)


@app.delete("/driver/push/subscribe", include_in_schema=False)
def driver_push_unsubscribe(
    payload: PushUnsubscribeIn,
    request: Request,
    driver: Driver = Depends(get_authenticated_driver),
    session: Session = Depends(get_session),
):
    """Remove this device's subscription. Called when the chauffeur taps
    "Disable notifications" or when the SW detects the subscription was
    revoked browser-side."""
    verify_csrf_token(request, driver, None)
    existing = session.scalars(
        select(DriverPushSubscription).where(
            DriverPushSubscription.driver_id == driver.id,
            DriverPushSubscription.endpoint == payload.endpoint,
        )
    ).first()
    if existing is not None:
        session.delete(existing)
        session.commit()
    return {"ok": True}


@app.post("/driver/trips/{trip_id}/status/{new_status}", include_in_schema=False)
def driver_action_status(trip_id: int, new_status: str, request: Request,
                         csrf_token: str | None = Form(None),
                         driver: Driver = Depends(get_authenticated_driver),
                         session: Session = Depends(get_session)):
    """Chauffeur-side lifecycle transition: ACCEPTED → EN_ROUTE_PICKUP →
    ON_SCENE → IN_TRANSIT → COMPLETED. Only the assigned driver may step
    the status, and only along the valid path defined in
    `driver_pages.DRIVER_NEXT_STATUS`. Anything else is 403/400.

    On COMPLETED, `core.update_trip_status` flips the driver back to
    AVAILABLE — same behavior as the admin trip-edit form.
    """
    verify_csrf_token(request, driver, csrf_token)
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    if trip.assigned_driver_id != driver.id:
        raise HTTPException(status_code=403, detail="Not your trip")

    try:
        target = TripStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown status '{new_status}'")

    allowed = _driver_pages.DRIVER_NEXT_STATUS.get(trip.status)
    if allowed is None or allowed[0] != target:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid transition: trip is {trip.status.value}; "
                f"the only next step is "
                f"{(allowed[0].value if allowed else 'none — wait for the dispatcher')}."
            ),
        )

    core.update_trip_status(session, trip_id=trip_id, new_status=target)
    return RedirectResponse(
        url=f"/driver/trips/{trip_id}?ok={_urlencode(f'Status: {target.value.replace(chr(95), chr(32)).title()}')}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Driver PWA shell (v0.9.10) — manifest + Service Worker + icon
# ---------------------------------------------------------------------------
#
# These three routes make /driver installable as a home-screen Progressive
# Web App. Walking-skeleton scope: app-shell cache only (CSS/HTML for
# login + dashboard), NO fetch interception of API calls — caching an
# accept/decline POST or stale broadcast data would be operationally
# dangerous. Push notifications and GPS pings are subsequent chunks.

# Cache name bumps when the SW changes — clients reload the cached shell.
_PWA_CACHE_VERSION = "driver-v1"


@app.get("/driver/manifest.json", include_in_schema=False)
def driver_manifest():
    """W3C Web App Manifest — describes the home-screen install experience.

    Icon uses the SVG endpoint below at `purpose: maskable any` so it works
    as both the iOS apple-touch and the Android adaptive icon source. SVG
    is resolution-independent so we don't need to ship 192/512 PNG variants.
    """
    body = {
        "name": "Livery Dispatch — Driver",
        "short_name": "Driver",
        "description": "Chauffeur dashboard for Livery Dispatch.",
        "start_url": "/driver",
        "scope": "/driver",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": "#161311",
        "background_color": "#161311",
        "lang": "en",
        "icons": [
            {
                "src": "/driver/icon.svg",
                "type": "image/svg+xml",
                "sizes": "any",
                "purpose": "any maskable",
            },
        ],
    }
    import json as _json_mod
    return Response(
        content=_json_mod.dumps(body),
        media_type="application/manifest+json",
    )


# Minimal Service Worker — caches the app shell on install, serves it on
# fetch for navigation requests when the network's slow. Does NOT intercept
# /trips, /drivers, /reservations, or anything ending in `/accept` / `/decline`
# — those must always hit the live backend or the dispatcher's view of the
# world diverges from the chauffeur's.
_DRIVER_SW_JS = """\
// Livery Dispatch driver PWA Service Worker (v0.9.10 / push v1.1.0)
// App-shell cache + push notification handler. Do NOT add API fetch
// interception — caching an accept/decline POST or stale broadcast data
// would be operationally dangerous.
const CACHE = '__CACHE__';
const SHELL = [
  '/driver/login',
  '/driver/manifest.json',
  '/driver/icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => null)
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // Drop any old caches when CACHE version bumps.
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Only intercept GETs to the app shell — let everything else (POSTs, API,
  // accept/decline, etc.) go straight to network.
  if (event.request.method !== 'GET') return;
  if (!SHELL.includes(url.pathname)) return;
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});

// --- Push notification handler (v1.1.0) ---
// Payload shape comes from core.broadcast_trip:
//   { trip_id, title, body, vehicle_class, pickup_label, dropoff_label,
//     pickup_time, distance_mi }
// Tapping the notification opens /driver/trips/{trip_id} so the chauffeur
// lands on the accept/decline screen.
self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (err) {
    payload = { title: 'Livery Dispatch', body: 'New broadcast.' };
  }
  const title = payload.title || 'Livery Dispatch — New Trip';
  const options = {
    body: payload.body || 'Tap to review.',
    icon: '/driver/icon.svg',
    badge: '/driver/icon.svg',
    tag: 'broadcast-' + (payload.trip_id || 'unknown'),
    renotify: true,
    requireInteraction: true,
    data: { trip_id: payload.trip_id },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const tripId = event.notification.data && event.notification.data.trip_id;
  const url = tripId ? ('/driver/trips/' + tripId) : '/driver';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      // If a /driver tab is already open, focus it and navigate.
      for (const w of wins) {
        if (w.url.indexOf('/driver') !== -1 && 'focus' in w) {
          return w.focus().then(() => w.navigate ? w.navigate(url) : null);
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
""".replace("__CACHE__", _PWA_CACHE_VERSION)


@app.get("/driver/sw.js", include_in_schema=False)
def driver_service_worker():
    return Response(
        content=_DRIVER_SW_JS,
        media_type="application/javascript; charset=utf-8",
        headers={
            # The Service Worker file itself must NEVER be cached — otherwise
            # we can't ship new versions to already-installed clients.
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


# Brass-on-dark "PC" mark — same aesthetic as the dashboard. Inline SVG is
# resolution-independent, ~600 bytes, no asset pipeline needed. iOS will
# bitmap-render this at apple-touch-icon time.
_DRIVER_ICON_SVG = """\
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#1c1916"/>
      <stop offset="100%" stop-color="#0e0d0a"/>
    </linearGradient>
    <linearGradient id="brass" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#dfc07a"/>
      <stop offset="100%" stop-color="#8a6a32"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="80" fill="url(#bg)"/>
  <rect x="32" y="32" width="448" height="448" rx="64" fill="none"
        stroke="#c9a961" stroke-width="3" stroke-opacity="0.4"/>
  <text x="256" y="328" text-anchor="middle"
        font-family="'Hoefler Text','Garamond',serif" font-size="240"
        font-weight="600" fill="url(#brass)" letter-spacing="-8">PC</text>
</svg>
"""


@app.get("/driver/icon.svg", include_in_schema=False)
def driver_icon():
    return Response(
        content=_DRIVER_ICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---- Trips ----

@app.get("/admin/trips", response_class=HTMLResponse, include_in_schema=False)
def trips_page_list_alias(request: Request, status: str | None = None,
                          vehicle_class: str | None = None,
                          session: Session = Depends(get_session)):
    """Alias to /trips for direct HTMl access (the bare /trips URL is the
    JSON list endpoint added earlier — keep its REST behavior intact)."""
    return HTMLResponse(_admin.trips_list_page(
        session,
        status_filter=status,
        vehicle_class_filter=vehicle_class,
        flash=_flash_msg(request),
    ))


@app.get("/admin/trips/{trip_id}", response_class=HTMLResponse, include_in_schema=False)
def trip_page_detail(trip_id: int, request: Request, session: Session = Depends(get_session)):
    t = session.get(Trip, trip_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return HTMLResponse(_admin.trip_detail_page(session, t, flash=_flash_msg(request)))


@app.post("/admin/trips/{trip_id}/edit", include_in_schema=False)
def trip_edit_form(
    trip_id: int,
    status: TripStatus = Form(...),
    fare_cents: str | None = Form(None),
    notes: str | None = Form(None),
    passenger_name: str | None = Form(None),
    passenger_phone: str | None = Form(None),
    pickup_label: str | None = Form(None),
    dropoff_label: str | None = Form(None),
    pickup_lat: str | None = Form(None),
    pickup_lon: str | None = Form(None),
    dropoff_lat: str | None = Form(None),
    dropoff_lon: str | None = Form(None),
    pickup_time: str | None = Form(None),
    vehicle_class: str | None = Form(None),
    session: Session = Depends(get_session),
):
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")

    # Free-text fields — only update if a value was provided (forms always send "" for empty).
    if passenger_name is not None:
        trip.passenger_name = passenger_name.strip()
    if passenger_phone is not None and passenger_phone.strip():
        try:
            trip.passenger_phone = _normalize_phone(passenger_phone, "passenger_phone")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if pickup_label is not None and pickup_label.strip():
        trip.pickup_label = pickup_label.strip()
    if dropoff_label is not None and dropoff_label.strip():
        trip.dropoff_label = dropoff_label.strip()
    # Coordinate updates — applied together with their labels when the
    # dispatcher picks a Nominatim suggestion, or independently if they
    # type raw lat/lng. Silently skip non-numeric input; reject numeric
    # values outside Earth's range with 422, matching the v1.1.7 coordinate
    # contract enforced on the create + promote paths. Validate before the
    # setattr so a bad value never mutates the trip.
    for fld_name, raw in (
        ("pickup_lat", pickup_lat),
        ("pickup_lon", pickup_lon),
        ("dropoff_lat", dropoff_lat),
        ("dropoff_lon", dropoff_lon),
    ):
        if raw not in (None, ""):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            in_range = (
                -90.0 <= value <= 90.0
                if fld_name.endswith("_lat")
                else -180.0 <= value <= 180.0
            )
            if not in_range:
                raise HTTPException(
                    status_code=422,
                    detail="Coordinates out of range: latitude -90..90, longitude -180..180.",
                )
            setattr(trip, fld_name, value)
    if pickup_time not in (None, ""):
        try:
            from datetime import datetime as _dt
            # datetime-local form values look like "2026-05-18T14:30"
            trip.pickup_time = _dt.fromisoformat(pickup_time)
        except (ValueError, TypeError):
            pass
    if vehicle_class:
        try:
            trip.vehicle_class = VehicleClass(vehicle_class)
        except ValueError:
            pass
    if notes is not None:
        trip.notes = notes
    session.commit()

    # Status change goes through core so side-effects (driver state flip on
    # COMPLETED, etc.) fire correctly. The form always submits the current
    # status (and re-submits the current fare) even on a pure field edit, so
    # only route through the state machine when the status actually changes —
    # otherwise update_trip_status would reject the no-op X→X self-transition
    # (and reject ALL edits on COMPLETED/CANCELLED trips, whose transition set
    # is empty). An unchanged-status fare edit is applied directly.
    fc: int | None = None
    if fare_cents not in (None, ""):
        try:
            fc = max(0, int(fare_cents))
        except ValueError:
            fc = None
    if status != trip.status:
        try:
            core.update_trip_status(session, trip_id, status, fare_cents=fc)
        except ValueError as exc:
            return RedirectResponse(
                url=f"/admin/trips/{trip_id}?ok={_urlencode(f'Update failed: {exc}')}",
                status_code=303,
            )
    elif fc is not None:
        trip.fare_cents = fc
        session.commit()
    return RedirectResponse(
        url=f"/admin/trips/{trip_id}?ok={_urlencode('Trip updated.')}", status_code=303
    )


@app.post("/admin/trips/{trip_id}/action/broadcast", include_in_schema=False)
def trip_action_broadcast(trip_id: int, session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    try:
        broadcasts = core.broadcast_trip(session, trip)
    except core.TripNotBroadcastable as exc:
        return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode(f'Failed: {exc}')}", status_code=303)
    msg = (
        f"Broadcast to {len(broadcasts)} drivers."
        if broadcasts
        else (
            "No more available drivers in radius; earlier offers are still open."
            if core.has_pending_offers(trip)
            else "No available drivers in radius; trip set to no_drivers."
        )
    )
    return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode(msg)}", status_code=303)


@app.post("/admin/trips/{trip_id}/action/broadcast-overflow", include_in_schema=False)
def trip_action_broadcast_overflow(trip_id: int, session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    try:
        broadcasts = core.broadcast_to_subcontractors(session, trip)
    except core.TripNotBroadcastable as exc:
        return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode(f'Failed: {exc}')}", status_code=303)
    msg = (
        f"Overflow broadcast to {len(broadcasts)} subcontractors."
        if broadcasts
        else "No active subs in radius for this vehicle class."
        + (" Earlier offers are still open." if core.has_pending_offers(trip) else "")
    )
    return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode(msg)}", status_code=303)


@app.post("/admin/trips/{trip_id}/action/sub-cost", include_in_schema=False)
def trip_action_sub_cost(
    trip_id: int,
    quoted_cost_dollars: str = Form(""),
    session: Session = Depends(get_session),
):
    """Record an overflow sub's quoted cost; fare = cost + the sub's markup (E9)."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    try:
        cents = int(round(float(quoted_cost_dollars.strip().lstrip("$").replace(",", "")) * 100))
        updated = core.record_subcontractor_cost(session, trip_id, cents)
    except ValueError as exc:
        msg = f"Failed: {exc}" if str(exc) else "Failed: enter the sub's cost in dollars"
        return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode(msg)}", status_code=303)
    msg = (f"Sub cost ${cents / 100:.2f} + markup ${updated.markup_cents / 100:.2f} "
           f"→ fare ${updated.fare_cents / 100:.2f}.")
    return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode(msg)}", status_code=303)


@app.post("/admin/trips/{trip_id}/action/complete", include_in_schema=False)
def trip_action_complete(trip_id: int, session: Session = Depends(get_session)):
    try:
        core.update_trip_status(session, trip_id, TripStatus.COMPLETED)
    except ValueError as exc:
        return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode(f'Failed: {exc}')}", status_code=303)
    return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode('Trip marked complete.')}", status_code=303)


@app.post("/admin/trips/{trip_id}/action/cancel", include_in_schema=False)
def trip_action_cancel(trip_id: int, session: Session = Depends(get_session)):
    try:
        core.update_trip_status(session, trip_id, TripStatus.CANCELLED)
    except ValueError as exc:
        return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode(f'Failed: {exc}')}", status_code=303)
    return RedirectResponse(url=f"/admin/trips/{trip_id}?ok={_urlencode('Trip cancelled.')}", status_code=303)


# ---- Reservations ----

@app.get("/admin/reservations", response_class=HTMLResponse, include_in_schema=False)
def reservations_page_list_alias(request: Request, status: str | None = None, session: Session = Depends(get_session)):
    return HTMLResponse(_admin.reservations_list_page(session, status_filter=status, flash=_flash_msg(request)))


@app.get("/admin/reservations/{reservation_id}", response_class=HTMLResponse, include_in_schema=False)
def reservation_page_detail(reservation_id: int, request: Request, session: Session = Depends(get_session)):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return HTMLResponse(_admin.reservation_detail_page(session, r, flash=_flash_msg(request)))


@app.post("/admin/reservations/{reservation_id}/edit", include_in_schema=False)
def reservation_edit_form(
    reservation_id: int,
    status: ReservationStatus = Form(...),
    linked_trip_id: str | None = Form(None),
    dispatcher_notes: str | None = Form(None),
    session: Session = Depends(get_session),
):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    # Resolve the linked trip first so the CONVERTED guard below can see a link
    # supplied in the same submit.
    if linked_trip_id not in (None, ""):
        try:
            tid = int(linked_trip_id)
            if session.get(Trip, tid):
                r.linked_trip_id = tid
        except ValueError:
            pass
    # CONVERTED is reachable only via the promote action (one reservation → at
    # most one trip). Reject a manual flip to CONVERTED with no backing trip —
    # it would strand the reservation (shows converted, has no trip, can never
    # be promoted). Idempotent re-saves of an already-converted row are fine.
    if (
        status == ReservationStatus.CONVERTED
        and r.status != ReservationStatus.CONVERTED
        and r.linked_trip_id is None
    ):
        raise HTTPException(
            status_code=409,
            detail="Use the promote action to convert a reservation to a trip.",
        )
    r.status = status
    if dispatcher_notes is not None:
        r.dispatcher_notes = dispatcher_notes
    session.commit()
    return RedirectResponse(
        url=f"/admin/reservations/{reservation_id}?ok={_urlencode('Reservation updated.')}",
        status_code=303,
    )


def _reservation_action_redirect(reservation_id: int, return_to: str | None, message: str) -> RedirectResponse:
    if return_to == "list":
        url = f"/admin/reservations?ok={_urlencode(message)}"
    else:
        url = f"/admin/reservations/{reservation_id}?ok={_urlencode(message)}"
    return RedirectResponse(url=url, status_code=303)


@app.post("/admin/reservations/{reservation_id}/action/confirm", include_in_schema=False)
def reservation_action_confirm(reservation_id: int,
                               return_to: str | None = Form(None),
                               session: Session = Depends(get_session)):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    r.status = ReservationStatus.CONFIRMED
    session.commit()
    return _reservation_action_redirect(reservation_id, return_to, "Reservation confirmed.")


@app.post("/admin/reservations/{reservation_id}/action/decline", include_in_schema=False)
def reservation_action_decline(reservation_id: int,
                               return_to: str | None = Form(None),
                               session: Session = Depends(get_session)):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    r.status = ReservationStatus.DECLINED
    session.commit()
    return _reservation_action_redirect(reservation_id, return_to, "Reservation declined.")


@app.post("/admin/reservations/{reservation_id}/action/spam", include_in_schema=False)
def reservation_action_spam(reservation_id: int,
                            return_to: str | None = Form(None),
                            session: Session = Depends(get_session)):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    r.status = ReservationStatus.SPAM
    session.commit()
    return _reservation_action_redirect(reservation_id, return_to, "Reservation marked as spam.")


@app.post("/admin/reservations/bulk-spam", include_in_schema=False)
async def reservation_bulk_spam(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    raw_ids = form.multi_items()
    count = 0
    for key, raw in raw_ids:
        if key != "ids":
            continue
        try:
            rid = int(raw)
        except (TypeError, ValueError):
            continue
        r = session.get(Reservation, rid)
        if r is not None and r.status == ReservationStatus.PENDING_REVIEW:
            r.status = ReservationStatus.SPAM
            count += 1
    session.commit()
    return RedirectResponse(
        url=f"/admin/reservations?ok={_urlencode(f'{count} reservation(s) marked as spam.')}",
        status_code=303,
    )


@app.get("/admin/reservations/{reservation_id}/promote", response_class=HTMLResponse, include_in_schema=False)
def reservation_promote_form(reservation_id: int, request: Request,
                             session: Session = Depends(get_session)):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if r.status in (ReservationStatus.CONVERTED, ReservationStatus.SPAM,
                    ReservationStatus.DECLINED):
        raise HTTPException(status_code=409,
                            detail=f"Reservation is {r.status.value}; cannot promote.")
    return HTMLResponse(_admin.promote_to_trip_form_page(session, r, flash=_flash_msg(request)))


@app.post("/admin/reservations/{reservation_id}/promote", include_in_schema=False)
def reservation_promote_submit(
    reservation_id: int,
    passenger_name: str = Form(...),
    passenger_phone: str = Form(...),
    pickup_label: str = Form(...),
    pickup_lat: float = Form(...),
    pickup_lon: float = Form(...),
    dropoff_label: str = Form(...),
    dropoff_lat: float = Form(...),
    dropoff_lon: float = Form(...),
    pickup_time: str = Form(...),
    vehicle_class: VehicleClass = Form(...),
    notes: str | None = Form(None),
    session: Session = Depends(get_session),
):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if r.status in (ReservationStatus.CONVERTED, ReservationStatus.SPAM,
                    ReservationStatus.DECLINED):
        raise HTTPException(status_code=409,
                            detail=f"Reservation is {r.status.value}; cannot promote.")
    try:
        when = datetime.fromisoformat(pickup_time)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid pickup_time.")
    if not core.coords_in_range(pickup_lat, pickup_lon) or not core.coords_in_range(
        dropoff_lat, dropoff_lon
    ):
        raise HTTPException(
            status_code=422,
            detail="Coordinates out of range: latitude -90..90, longitude -180..180.",
        )
    try:
        passenger_phone = _normalize_phone(passenger_phone, "passenger_phone")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    trip = Trip(
        passenger_name=passenger_name,
        passenger_phone=passenger_phone,
        pickup_label=pickup_label,
        pickup_lat=pickup_lat,
        pickup_lon=pickup_lon,
        dropoff_label=dropoff_label,
        dropoff_lat=dropoff_lat,
        dropoff_lon=dropoff_lon,
        pickup_time=when,
        vehicle_class=vehicle_class,
        notes=notes or None,
        status=TripStatus.REQUESTED,
    )
    session.add(trip)
    session.flush()
    r.linked_trip_id = trip.id
    r.status = ReservationStatus.CONVERTED
    session.commit()
    return RedirectResponse(
        url=f"/admin/trips/{trip.id}?ok={_urlencode(f'Promoted from reservation #{reservation_id}.')}",
        status_code=303,
    )


# ---- Inquiries ----

@app.get("/admin/inquiries", response_class=HTMLResponse, include_in_schema=False)
def inquiries_page_list_alias(request: Request, status: str | None = None, kind: str | None = None,
                              q: str | None = None, session: Session = Depends(get_session)):
    return HTMLResponse(
        _admin.inquiries_list_page(session, status_filter=status, kind_filter=kind,
                                   search=q, flash=_flash_msg(request))
    )


@app.get("/admin/inquiries/{inquiry_id}", response_class=HTMLResponse, include_in_schema=False)
def inquiry_page_detail(inquiry_id: int, request: Request, session: Session = Depends(get_session)):
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    return HTMLResponse(_admin.inquiry_detail_page(session, inq, flash=_flash_msg(request)))


@app.post("/admin/inquiries/{inquiry_id}/edit", include_in_schema=False)
def inquiry_edit_form(
    inquiry_id: int,
    status: InquiryStatus = Form(...),
    dispatcher_notes: str | None = Form(None),
    session: Session = Depends(get_session),
):
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    inq.status = status
    if dispatcher_notes is not None:
        inq.dispatcher_notes = dispatcher_notes
    session.commit()
    return RedirectResponse(
        url=f"/admin/inquiries/{inquiry_id}?ok={_urlencode('Inquiry updated.')}",
        status_code=303,
    )


@app.post("/admin/inquiries/{inquiry_id}/action/responded", include_in_schema=False)
def inquiry_action_responded(inquiry_id: int, session: Session = Depends(get_session)):
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    inq.status = InquiryStatus.RESPONDED
    session.commit()
    return RedirectResponse(
        url=f"/admin/inquiries/{inquiry_id}?ok={_urlencode('Marked responded.')}",
        status_code=303,
    )


@app.post("/admin/inquiries/{inquiry_id}/action/closed", include_in_schema=False)
def inquiry_action_closed(inquiry_id: int, session: Session = Depends(get_session)):
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    inq.status = InquiryStatus.CLOSED
    session.commit()
    return RedirectResponse(
        url=f"/admin/inquiries/{inquiry_id}?ok={_urlencode('Inquiry closed.')}",
        status_code=303,
    )


# ---- Drivers ----

@app.get("/admin/drivers", response_class=HTMLResponse, include_in_schema=False)
def drivers_page_list_alias(request: Request, status: str | None = None,
                            session: Session = Depends(get_session)):
    return HTMLResponse(
        _admin.drivers_list_page(session, status_filter=status, flash=_flash_msg(request))
    )


@app.get("/admin/drivers/{driver_id}", response_class=HTMLResponse, include_in_schema=False)
def driver_page_detail(driver_id: int, request: Request, session: Session = Depends(get_session)):
    d = session.get(Driver, driver_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Driver not found")
    # Lazy-mint the bearer token on first admin view so a dispatcher visiting
    # this page on a pre-v0.9.9 driver row sees a real value instead of "(not
    # yet generated)". `ensure_auth_token` is a no-op when the column is set.
    if not d.auth_token:
        d.ensure_auth_token()
        session.commit()
    return HTMLResponse(_admin.driver_detail_page(session, d, flash=_flash_msg(request)))


@app.post("/admin/drivers/{driver_id}/action/rotate-token", include_in_schema=False)
def driver_action_rotate_token(driver_id: int, session: Session = Depends(get_session)):
    """Re-mint the driver's bearer token. The old value stops working
    immediately (it's compared by exact match in `get_authenticated_driver`).
    Used when a chauffeur loses a phone or the token leaks.

    v1.1.0 — also cascade-deletes every push subscription owned by this
    driver. Rotating the token means "this device is no longer trusted",
    and the same logic applies to the device's push subscription. The
    chauffeur must re-subscribe on each device after rotation."""
    d = session.get(Driver, driver_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Driver not found")
    import secrets as _secrets
    d.auth_token = _secrets.token_urlsafe(32)
    # Cascade-delete push subscriptions. Tied to the rotation, not the
    # Driver row itself (the FK is ON DELETE CASCADE for the row-delete
    # case; this is the rotation-but-row-stays case).
    session.query(DriverPushSubscription).filter(
        DriverPushSubscription.driver_id == driver_id
    ).delete(synchronize_session=False)
    session.commit()
    return RedirectResponse(
        url=f"/admin/drivers/{driver_id}?ok={_urlencode('Token rotated. The previous value is invalidated; push subscriptions for all devices were dropped.')}",
        status_code=303,
    )


@app.post("/admin/drivers/{driver_id}/edit", include_in_schema=False)
def driver_edit_form(
    driver_id: int,
    status: DriverStatus = Form(...),
    vehicle_classes: str = Form(...),
    lat: str | None = Form(None),
    lon: str | None = Form(None),
    session: Session = Depends(get_session),
):
    d = session.get(Driver, driver_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Driver not found")
    d.status = status
    d.vehicle_classes = vehicle_classes.strip()
    if lat not in (None, ""):
        try:
            d.lat = float(lat)
        except ValueError:
            pass
    if lon not in (None, ""):
        try:
            d.lon = float(lon)
        except ValueError:
            pass
    session.commit()
    return RedirectResponse(
        url=f"/admin/drivers/{driver_id}?ok={_urlencode('Driver updated.')}",
        status_code=303,
    )


# ---- Subcontractors ----

@app.get("/admin/subcontractors", response_class=HTMLResponse, include_in_schema=False)
def subs_page_list_alias(request: Request, active: str | None = None, tier: str | None = None,
                         session: Session = Depends(get_session)):
    return HTMLResponse(
        _admin.subs_list_page(session, active_filter=active, tier_filter=tier,
                              flash=_flash_msg(request))
    )


@app.get("/admin/subcontractors/{sub_id}", response_class=HTMLResponse, include_in_schema=False)
def sub_page_detail(sub_id: int, request: Request, session: Session = Depends(get_session)):
    s = session.get(Subcontractor, sub_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Subcontractor not found")
    return HTMLResponse(_admin.sub_detail_page(session, s, flash=_flash_msg(request)))


@app.post("/admin/subcontractors/{sub_id}/edit", include_in_schema=False)
def sub_edit_form(
    sub_id: int,
    tier: int = Form(...),
    vehicle_classes: str = Form(...),
    markup_pct: float = Form(...),
    service_radius_mi: str | None = Form(None),
    active: str | None = Form(None),
    notes: str | None = Form(None),
    session: Session = Depends(get_session),
):
    s = session.get(Subcontractor, sub_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Subcontractor not found")
    from models import SubcontractorTier as _ST
    s.tier = _ST(tier)
    s.vehicle_classes = vehicle_classes.strip()
    s.markup_pct = markup_pct
    if service_radius_mi not in (None, ""):
        try:
            s.service_radius_mi = float(service_radius_mi)
        except ValueError:
            pass
    else:
        s.service_radius_mi = None
    s.active = bool(active)
    if notes is not None:
        s.notes = notes
    session.commit()
    return RedirectResponse(
        url=f"/admin/subcontractors/{sub_id}?ok={_urlencode('Subcontractor updated.')}",
        status_code=303,
    )


def _coerce_settlement_days(raw: str | None, default: int = 30) -> int:
    """Parse the ?days= query param into a safe non-negative int. 0 means
    "all time" — no time bound. Invalid input → default."""
    if raw is None or raw == "":
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


@app.get("/admin/subcontractors/{sub_id}/settlement", response_class=HTMLResponse, include_in_schema=False)
def sub_settlement_page(sub_id: int, request: Request, days: str | None = None,
                        date_from: str | None = None, date_to: str | None = None,
                        session: Session = Depends(get_session)):
    s = session.get(Subcontractor, sub_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Subcontractor not found")
    return HTMLResponse(_admin.sub_settlement_page(
        session, s, days=_coerce_settlement_days(days),
        date_from=date_from, date_to=date_to, flash=_flash_msg(request),
    ))


@app.get("/admin/subcontractors/{sub_id}/settlement.csv", include_in_schema=False)
def sub_settlement_csv(sub_id: int, days: str | None = None,
                       date_from: str | None = None, date_to: str | None = None,
                       session: Session = Depends(get_session)):
    s = session.get(Subcontractor, sub_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Subcontractor not found")
    n_days = _coerce_settlement_days(days)
    csv_body = _admin.sub_settlement_csv(session, s, days=n_days,
                                         date_from=date_from, date_to=date_to)
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in s.name.lower()).strip("_") or f"sub{s.id}"
    if date_from or date_to:
        period = f"{date_from or 'start'}_{date_to or 'end'}"
    else:
        period = f"{n_days}d" if n_days > 0 else "alltime"
    filename = f"settlement_{safe_name}_{period}.csv"
    return Response(
        content=csv_body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- Trip "Quote fare" action ----

@app.post("/admin/trips/{trip_id}/action/quote", include_in_schema=False)
def trip_action_quote(
    trip_id: int,
    rate_card_id: str | None = Form(None),
    is_peak_week: str | None = Form(None),
    session: Session = Depends(get_session),
):
    """Compute a suggested fare from a rate card and fill the trip's fare.

    The dispatcher picks a rate card (or we auto-pick the first active one
    matching the trip's vehicle class) and the fare calculator returns a
    line-itemized breakdown. We apply the total to `trip.fare_cents` and
    surface the breakdown in the flash message so the dispatcher can sanity-
    check the math before sending the quote to the guest.
    """
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")

    # Resolve the rate card. If the dispatcher specified one, use it; else
    # auto-pick the first active card for the trip's vehicle class.
    card: RateCard | None = None
    if rate_card_id not in (None, ""):
        try:
            card = session.get(RateCard, int(rate_card_id))
        except ValueError:
            card = None
    if card is None:
        card = session.scalars(
            select(RateCard).where(
                RateCard.vehicle_class == trip.vehicle_class,
                RateCard.active.is_(True),
            ).order_by(RateCard.id.asc())
        ).first()
    if card is None:
        return RedirectResponse(
            url=f"/admin/trips/{trip_id}?ok={_urlencode('No active rate card for vehicle class ' + trip.vehicle_class.value)}",
            status_code=303,
        )

    breakdown = _fare.quote_for_trip_and_card(
        trip, card, is_peak_week=bool(is_peak_week),
    )
    trip.fare_cents = breakdown.total_cents
    session.commit()
    return RedirectResponse(
        url=f"/admin/trips/{trip_id}?ok={_urlencode('Quoted: ' + breakdown.as_text())}",
        status_code=303,
    )


# ---- Rate cards ----

@app.get("/admin/rates", response_class=HTMLResponse, include_in_schema=False)
def rates_page_list(request: Request, session: Session = Depends(get_session)):
    return HTMLResponse(_admin.rates_list_page(session, flash=_flash_msg(request)))


@app.get("/admin/rates/new", response_class=HTMLResponse, include_in_schema=False)
def rate_page_new(request: Request):
    """Blank rate-card form (E6, v1.1.18). Declared before /admin/rates/{rate_id}
    so "new" isn't parsed as an id."""
    return HTMLResponse(_admin.rate_new_page(flash=_flash_msg(request)))


@app.post("/admin/rates/new", include_in_schema=False)
def rate_create_form(
    name: str = Form(""),
    vehicle_class: str = Form(""),
    base_fare_cents: int = Form(0),
    per_mile_cents: int = Form(0),
    per_hour_cents: int = Form(0),
    airport_meet_surcharge_cents: int = Form(0),
    after_hours_surcharge_cents: int = Form(0),
    peak_week_multiplier: float = Form(1.0),
    minimum_fare_cents: int = Form(0),
    active: str | None = Form(None),
    notes: str | None = Form(None),
    session: Session = Depends(get_session),
):
    """Create a rate card (E6). Same clamping as the edit form. Before v1.1.18
    a fresh production DB had no cards and no way to make one."""
    def fail(msg: str):
        return RedirectResponse(url=f"/admin/rates/new?ok={_urlencode('Failed: ' + msg)}", status_code=303)

    if not name.strip():
        return fail("a name is required")
    try:
        vc = VehicleClass(vehicle_class)
    except ValueError:
        return fail(f"unknown vehicle class {vehicle_class!r}")
    card = RateCard(
        name=name.strip()[:120],
        vehicle_class=vc,
        base_fare_cents=max(0, base_fare_cents),
        per_mile_cents=max(0, per_mile_cents),
        per_hour_cents=max(0, per_hour_cents),
        airport_meet_surcharge_cents=max(0, airport_meet_surcharge_cents),
        after_hours_surcharge_cents=max(0, after_hours_surcharge_cents),
        peak_week_multiplier=max(1.0, peak_week_multiplier),
        minimum_fare_cents=max(0, minimum_fare_cents),
        active=bool(active),
        notes=(notes or None),
    )
    session.add(card)
    session.commit()
    return RedirectResponse(
        url=f"/admin/rates/{card.id}?ok={_urlencode('Rate card created.')}",
        status_code=303,
    )


@app.get("/admin/rates/{rate_id}", response_class=HTMLResponse, include_in_schema=False)
def rate_page_detail(rate_id: int, request: Request, session: Session = Depends(get_session)):
    c = session.get(RateCard, rate_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Rate card not found")
    return HTMLResponse(_admin.rate_detail_page(session, c, flash=_flash_msg(request)))


@app.post("/admin/rates/{rate_id}/edit", include_in_schema=False)
def rate_edit_form(
    rate_id: int,
    name: str = Form(...),
    vehicle_class: str = Form(...),
    base_fare_cents: int = Form(0),
    per_mile_cents: int = Form(0),
    per_hour_cents: int = Form(0),
    airport_meet_surcharge_cents: int = Form(0),
    after_hours_surcharge_cents: int = Form(0),
    peak_week_multiplier: float = Form(1.0),
    minimum_fare_cents: int = Form(0),
    active: str | None = Form(None),
    notes: str | None = Form(None),
    session: Session = Depends(get_session),
):
    c = session.get(RateCard, rate_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Rate card not found")
    c.name = name.strip()
    try:
        c.vehicle_class = VehicleClass(vehicle_class)
    except ValueError:
        pass
    c.base_fare_cents = max(0, base_fare_cents)
    c.per_mile_cents = max(0, per_mile_cents)
    c.per_hour_cents = max(0, per_hour_cents)
    c.airport_meet_surcharge_cents = max(0, airport_meet_surcharge_cents)
    c.after_hours_surcharge_cents = max(0, after_hours_surcharge_cents)
    c.peak_week_multiplier = max(1.0, peak_week_multiplier)
    c.minimum_fare_cents = max(0, minimum_fare_cents)
    c.active = bool(active)
    if notes is not None:
        c.notes = notes
    session.commit()
    return RedirectResponse(
        url=f"/admin/rates/{rate_id}?ok={_urlencode('Rate card updated.')}",
        status_code=303,
    )


# ---- Call logs ----

@app.get("/admin/calls", response_class=HTMLResponse, include_in_schema=False)
def calls_page_list(request: Request, session: Session = Depends(get_session)):
    return HTMLResponse(_admin.calls_list_page(session, flash=_flash_msg(request)))


@app.get("/admin/calls/{call_id}", response_class=HTMLResponse, include_in_schema=False)
def call_page_detail(call_id: int, request: Request, session: Session = Depends(get_session)):
    c = session.get(CallLog, call_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return HTMLResponse(_admin.call_detail_page(session, c, flash=_flash_msg(request)))


@app.post("/admin/calls/{call_id}/edit", include_in_schema=False)
def call_edit_form(
    call_id: int,
    dispatcher_notes: str | None = Form(None),
    linked_reservation_id: str | None = Form(None),
    session: Session = Depends(get_session),
):
    c = session.get(CallLog, call_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if dispatcher_notes is not None:
        c.dispatcher_notes = dispatcher_notes
    if linked_reservation_id not in (None, ""):
        try:
            rid = int(linked_reservation_id)
            if session.get(Reservation, rid):
                c.linked_reservation_id = rid
        except ValueError:
            pass
    session.commit()
    return RedirectResponse(
        url=f"/admin/calls/{call_id}?ok={_urlencode('Call notes saved.')}",
        status_code=303,
    )


# ---- Admin audit log ----

@app.get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
def audit_log_page(request: Request, session: Session = Depends(get_session)):
    return HTMLResponse(_admin.audit_log_page(session, flash=_flash_msg(request)))


# ---------------------------------------------------------------------------
# Twilio Voice — inbound call routing + voicemail capture
# ---------------------------------------------------------------------------
#
# Year-1 call posture (premium black-car standard):
#   1. Caller dials the Twilio number.
#   2. POST /webhooks/twilio/voice returns TwiML that <Dial>s the dispatcher's
#      cell (DISPATCHER_PHONE) with a 20-second ring timeout.
#   3. If the dispatcher answers, the call connects and we log it as
#      `outcome=answered` once Twilio fires the status callback.
#   4. If the dispatcher doesn't answer in 20s, Twilio falls through to
#      <Record> + transcription, capped at 90 seconds.
#   5. POST /webhooks/twilio/voicemail receives the recording URL +
#      transcription. We create a CallLog row, optionally promote it to a
#      Reservation if the transcript looks like a ride request, and SMS the
#      dispatcher the transcript so they can call back inside 30 minutes.
#
# Phase D (LLM-driven intake) plans to layer on top of step 5.


def _twilio_xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


@app.post("/webhooks/twilio/voice", include_in_schema=False)
async def twilio_voice_webhook(request: Request, session: Session = Depends(get_session)):
    """Inbound voice call entry point.

    Orchestrates the unified call flow:
      1. Log the call.
      2. If no dispatcher phone → voicemail only.
      3. If quiet hours → after-hours message + SMS booking + optional voicemail.
      4. Identify caller (driver / subcontractor / customer / unknown).
      5. Customer with active trip hears trip status before the dial.
      6. Dial the dispatcher with caller-aware timeout and whisper URL.
    """
    form = await request.form()
    if not _verify_twilio_signature(request, dict(form)):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    call_sid = form.get("CallSid")
    from_phone = (form.get("From") or "").strip()
    to_phone = (form.get("To") or "").strip()

    log: CallLog | None = None
    if call_sid:
        existing = session.scalars(
            select(CallLog).where(CallLog.twilio_call_sid == call_sid)
        ).first()
        if existing is None:
            log = CallLog(
                twilio_call_sid=call_sid,
                from_phone=from_phone or "(unknown)",
                to_phone=to_phone or "(unknown)",
                direction="inbound",
                outcome="ringing",
            )
            session.add(log)
            session.commit()
        else:
            log = existing

    dispatcher_phone = os.getenv("DISPATCHER_PHONE", "").strip()
    if not dispatcher_phone:
        return _twilio_xml(_TWIML_VOICEMAIL_ONLY)

    # --- Feature 3: after-hours smart fallback ---
    if os.getenv("QUIET_HOURS_ENABLED", "true").lower() != "false" and core.is_quiet_hours():
        if log is not None:
            log.outcome = "after_hours"
            session.commit()
        if from_phone.startswith("+"):
            try:
                get_provider().send_sms(
                    from_phone,
                    "Thanks for calling Livery Dispatch! We're currently away. "
                    "Text us at this number to book a ride, or leave a voicemail "
                    "and we'll call you back first thing."
                )
            except Exception:
                pass
        return _twilio_xml(_TWIML_AFTER_HOURS)

    # --- Feature 4: identify caller for routing ---
    caller = core.identify_caller(session, from_phone)

    # --- Feature 5: caller-side trip status ---
    if caller.kind == "customer" and caller.active_trip is not None:
        driver_name = caller.driver_for_trip.name if caller.driver_for_trip else "Your driver"
        status_desc = core.trip_status_description(caller.active_trip.status, audience="passenger")
        pre_dial = (
            f'  <Say voice="Polly.Joanna-Neural">'
            f'{_xml_escape(driver_name)} {_xml_escape(status_desc)}. '
            f'Connecting you to dispatch now.</Say>\n'
        )
    else:
        pre_dial = '  <Say voice="Polly.Joanna-Neural">Livery Dispatch. One moment.</Say>\n'

    # --- Feature 4: dial timeout by caller kind ---
    dial_timeout = 10 if (caller.kind == "driver" and caller.active_trip is not None) else 20

    # --- Feature 2: whisper URL with caller identity ---
    whisper_params = f"kind={_urlencode(caller.kind)}"
    if caller.name:
        whisper_params += f"&name={_urlencode(caller.name)}"
    if caller.active_trip is not None:
        whisper_params += (
            f"&trip_id={caller.active_trip.id}"
            f"&trip_status={_urlencode(caller.active_trip.status.value)}"
        )
    whisper_url = f"/webhooks/twilio/whisper?{whisper_params}"

    return _twilio_xml(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Response>\n'
        f'{pre_dial}'
        f'  <Dial timeout="{dial_timeout}" answerOnBridge="true" '
        f'action="/webhooks/twilio/dial-status" method="POST">\n'
        f'    <Number url="{_xml_escape(whisper_url)}" method="POST">'
        f'{_xml_escape(dispatcher_phone)}</Number>\n'
        f'  </Dial>\n'
        f'</Response>'
    )


_TWIML_VOICEMAIL_ONLY = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Response>\n'
    '  <Say voice="Polly.Joanna-Neural">Thank you for calling Livery Dispatch. '
    'Please leave your name, callback number, and the details of your ride. '
    'We will return your call within thirty minutes.</Say>\n'
    '  <Record maxLength="90" playBeep="true" transcribe="true" '
    'transcribeCallback="/webhooks/twilio/voicemail" '
    'action="/webhooks/twilio/voicemail-done" method="POST" />\n'
    '  <Say voice="Polly.Joanna-Neural">No message detected. Goodbye.</Say>\n'
    '  <Hangup/>\n'
    '</Response>'
)

_TWIML_AFTER_HOURS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Response>\n'
    '  <Say voice="Polly.Joanna-Neural">Thank you for calling Livery Dispatch. '
    'We are currently away. We just sent you a text with booking details. '
    'You can also leave a message after the beep and we will call you back '
    'first thing in the morning.</Say>\n'
    '  <Record maxLength="90" playBeep="true" transcribe="true" '
    'transcribeCallback="/webhooks/twilio/voicemail" '
    'action="/webhooks/twilio/voicemail-done" method="POST" />\n'
    '  <Say voice="Polly.Joanna-Neural">No message detected. Goodbye.</Say>\n'
    '  <Hangup/>\n'
    '</Response>'
)

_WHISPER_STATUS_MAP = {
    "accepted": "accepted, preparing",
    "en_route_pickup": "driver en route to pickup",
    "on_scene": "driver on scene",
    "in_transit": "passenger in transit",
}


@app.post("/webhooks/twilio/whisper", include_in_schema=False)
async def twilio_whisper(request: Request):
    """Whisper endpoint: Twilio calls this when the dispatcher answers. Returns
    a short <Say> identifying the caller before connecting the parties."""
    form = await request.form()
    if not _verify_twilio_signature(request, dict(form)):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    params = request.query_params
    caller_kind = params.get("kind", "unknown")
    caller_name = params.get("name", "Unknown caller")
    trip_id = params.get("trip_id")
    trip_status = params.get("trip_status")

    parts = [f"Call from {caller_name}"]
    if caller_kind in ("driver", "subcontractor", "customer"):
        parts.append(caller_kind)
    if trip_id and trip_status:
        status_desc = _WHISPER_STATUS_MAP.get(trip_status, trip_status)
        parts.append(f"Trip {trip_id}, {status_desc}")

    whisper_text = " — ".join(parts) + "."

    return _twilio_xml(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Response>\n'
        f'  <Say voice="Polly.Joanna-Neural">{_xml_escape(whisper_text)}</Say>\n'
        f'</Response>'
    )


@app.post("/webhooks/twilio/dial-status", include_in_schema=False)
async def twilio_dial_status(request: Request, session: Session = Depends(get_session)):
    """Called when the <Dial> verb finishes. If the dispatcher didn't pick up,
    fall through to <Record> + transcription. If they did, mark the call as
    answered in the log."""
    form = await request.form()
    if not _verify_twilio_signature(request, dict(form)):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    call_sid = form.get("CallSid")
    dial_status = (form.get("DialCallStatus") or "").lower()
    # Twilio uses: completed, busy, no-answer, failed, canceled

    if call_sid:
        log = session.scalars(
            select(CallLog).where(CallLog.twilio_call_sid == call_sid)
        ).first()
        if log is not None:
            if dial_status == "completed":
                log.outcome = "answered"
                log.ended_at = datetime.now(timezone.utc)
            elif dial_status in ("no-answer", "busy", "canceled"):
                log.outcome = "voicemail"  # about to enter voicemail flow
            elif dial_status == "failed":
                log.outcome = "failed"
            session.commit()

    if dial_status == "completed":
        # Call connected — nothing more to do.
        return _twilio_xml('<?xml version="1.0" encoding="UTF-8"?><Response/>')
    # Otherwise fall through to voicemail.
    return _twilio_xml(_TWIML_VOICEMAIL_ONLY)


@app.post("/webhooks/twilio/voicemail", include_in_schema=False)
async def twilio_voicemail_webhook(request: Request, session: Session = Depends(get_session)):
    """Twilio's transcription-callback hook (configured via
    `transcribeCallback`). Fires once the voicemail recording is transcribed.
    Updates the CallLog with the recording URL + transcription text, sends
    the dispatcher an SMS alert, and creates a tentative Reservation row
    with the transcript in notes so it lands in the review queue."""
    form = await request.form()
    if not _verify_twilio_signature(request, dict(form)):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    call_sid = form.get("CallSid")
    recording_url = form.get("RecordingUrl")
    recording_duration = form.get("RecordingDuration")
    transcription = (form.get("TranscriptionText") or "").strip()

    if not call_sid:
        return Response(status_code=204)

    log = session.scalars(
        select(CallLog).where(CallLog.twilio_call_sid == call_sid)
    ).first()
    if log is None:
        # Fallback: create one from scratch if the initial webhook somehow missed.
        log = CallLog(
            twilio_call_sid=call_sid,
            from_phone=(form.get("From") or "(unknown)").strip(),
            to_phone=(form.get("To") or "(unknown)").strip(),
            direction="inbound",
            outcome="voicemail",
        )
        session.add(log)
        session.flush()

    log.outcome = "voicemail"
    log.recording_url = recording_url
    if recording_duration:
        try:
            log.recording_duration_seconds = int(recording_duration)
        except (TypeError, ValueError):
            pass
    if transcription:
        log.transcription = transcription[:4000]

    # Auto-create a Reservation row so the dispatcher's review queue sees it.
    # The transcription becomes the notes; the dispatcher calls back, confirms
    # details, then promotes the reservation to a Trip in the normal flow.
    res_id: int | None = None
    if transcription:
        parsed = core.parse_voicemail_transcript(transcription)
        r = Reservation(
            name=parsed.get("name") or f"Voicemail from {log.from_phone}",
            email="(voicemail — no email)",
            phone=log.from_phone if log.from_phone.startswith("+") else "+10000000000",
            pickup_label=parsed.get("pickup_label") or "(see transcription — phone callback required)",
            dropoff_label=parsed.get("dropoff_label") or "(see transcription — phone callback required)",
            pickup_date=parsed.get("pickup_date"),
            pickup_time_str=parsed.get("pickup_time_str"),
            notes=f"Voicemail transcription:\n\n{transcription}",
            source=f"twilio-voicemail:{call_sid}",
            status=ReservationStatus.PENDING_REVIEW,
        )
        session.add(r)
        session.flush()
        res_id = r.id
        log.linked_reservation_id = res_id
    session.commit()

    # Notify the dispatcher.
    if transcription:
        preview = transcription[:140] + ("…" if len(transcription) > 140 else "")
        _notify_dispatcher(
            f"VOICEMAIL from {log.from_phone}: \"{preview}\". "
            f"Reservation #{res_id} created for review."
        )

    return Response(status_code=204)


@app.post("/webhooks/twilio/voicemail-done", include_in_schema=False)
async def twilio_voicemail_done(request: Request, session: Session = Depends(get_session)):
    """The <Record>'s action callback — fires when recording completes (before
    transcription). We just confirm hangup; the transcription-callback above
    is the one that does the real work once Twilio's transcription is ready."""
    form = await request.form()
    if not _verify_twilio_signature(request, dict(form)):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return _twilio_xml(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        '  <Say voice="Polly.Joanna-Neural">Thank you. We will call you back.</Say>\n'
        '  <Hangup/>\n'
        '</Response>'
    )


def _urlencode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


@app.post("/drivers", response_model=DriverOut, status_code=201)
def create_driver(payload: DriverCreate, session: Session = Depends(get_session)):
    if session.scalars(select(Driver).where(Driver.phone == payload.phone)).first():
        raise HTTPException(status_code=409, detail=f"Driver with phone {payload.phone} already exists")
    driver = Driver(
        name=payload.name,
        phone=payload.phone,
        vehicle_classes=",".join(c.value for c in payload.vehicle_classes),
    )
    # Mint a bearer token at creation time so the dispatcher can hand it to
    # the chauffeur immediately. Rotatable later via the admin surface.
    driver.ensure_auth_token()
    session.add(driver)
    session.commit()
    session.refresh(driver)
    return driver


@app.get("/drivers", response_model=list[DriverOut])
def list_drivers(session: Session = Depends(get_session)):
    return list(session.scalars(select(Driver)).all())


@app.get("/drivers/{driver_id}", response_model=DriverOut)
def get_driver(driver_id: int, session: Session = Depends(get_session)):
    driver = session.get(Driver, driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail="Driver not found")
    return driver


@app.patch("/drivers/{driver_id}/status", response_model=DriverOut)
def update_driver_status(driver_id: int, payload: DriverStatusUpdate, session: Session = Depends(get_session)):
    driver = session.get(Driver, driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail="Driver not found")
    driver.status = payload.status
    if payload.lat is not None:
        driver.lat = payload.lat
    if payload.lon is not None:
        driver.lon = payload.lon
    session.commit()
    session.refresh(driver)
    return driver


@app.post("/drivers/{driver_id}/location", response_model=DriverOut)
def update_driver_location(driver_id: int, payload: DriverLocationUpdate, session: Session = Depends(get_session)):
    """Dispatcher-side location-only update (admin-gated, CSRF-checked).
    The driver PWA's ~30s GPS heartbeat does NOT use this route — it POSTs to
    the driver-authenticated ``/driver/location`` instead. Does not touch the
    driver's status enum; use PATCH /status to change that. Returns the
    updated driver so the client can sanity-check coordinates."""
    driver = session.get(Driver, driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail="Driver not found")
    driver.lat = payload.lat
    driver.lon = payload.lon
    session.commit()
    session.refresh(driver)
    return driver


# ---------------------------------------------------------------------------
# Subcontractors
# ---------------------------------------------------------------------------


@app.post("/subcontractors", response_model=SubcontractorOut, status_code=201)
def create_subcontractor(payload: SubcontractorCreate, session: Session = Depends(get_session)):
    if session.scalars(select(Subcontractor).where(Subcontractor.dispatch_phone == payload.dispatch_phone)).first():
        raise HTTPException(
            status_code=409, detail=f"Subcontractor with dispatch_phone {payload.dispatch_phone} already exists"
        )
    sub = Subcontractor(
        name=payload.name,
        dispatch_phone=payload.dispatch_phone,
        dispatch_email=payload.dispatch_email,
        vehicle_classes=",".join(c.value for c in payload.vehicle_classes),
        tier=payload.tier,
        markup_pct=payload.markup_pct,
        base_lat=payload.base_lat,
        base_lon=payload.base_lon,
        service_radius_mi=payload.service_radius_mi,
        notes=payload.notes,
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return sub


@app.get("/subcontractors", response_model=list[SubcontractorOut])
def list_subcontractors(active_only: bool = False, session: Session = Depends(get_session)):
    stmt = select(Subcontractor)
    if active_only:
        stmt = stmt.where(Subcontractor.active.is_(True))
    return list(session.scalars(stmt).all())


@app.get("/subcontractors/{sub_id}", response_model=SubcontractorOut)
def get_subcontractor(sub_id: int, session: Session = Depends(get_session)):
    sub = session.get(Subcontractor, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Subcontractor not found")
    return sub


@app.patch("/subcontractors/{sub_id}", response_model=SubcontractorOut)
def update_subcontractor(sub_id: int, payload: SubcontractorUpdate, session: Session = Depends(get_session)):
    sub = session.get(Subcontractor, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Subcontractor not found")
    data = payload.model_dump(exclude_unset=True)
    if "vehicle_classes" in data and data["vehicle_classes"] is not None:
        data["vehicle_classes"] = ",".join(c.value for c in data["vehicle_classes"])
    for k, v in data.items():
        setattr(sub, k, v)
    session.commit()
    session.refresh(sub)
    return sub


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


@app.post("/trips", response_model=TripOut, status_code=201)
def create_trip(payload: TripCreate, session: Session = Depends(get_session)):
    """Create a trip. Does NOT auto-broadcast — that's a separate explicit step."""
    trip = Trip(**payload.model_dump())
    session.add(trip)
    session.commit()
    session.refresh(trip)
    return trip


@app.get("/trips", response_model=list[TripOut])
def list_trips(status: TripStatus | None = None, session: Session = Depends(get_session)):
    stmt = select(Trip)
    if status is not None:
        stmt = stmt.where(Trip.status == status)
    return list(session.scalars(stmt.order_by(Trip.id.desc())).all())


@app.get("/trips/{trip_id}", response_model=TripOut)
def get_trip(trip_id: int, session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return trip


@app.post("/trips/{trip_id}/broadcast", response_model=list[TripBroadcastOut])
def broadcast(trip_id: int, session: Session = Depends(get_session)):
    """Broadcast the trip to nearest available own-fleet drivers and notify them."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    try:
        broadcasts = core.broadcast_trip(session, trip)
    except core.TripNotBroadcastable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not broadcasts:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No available drivers within radius matching vehicle_class={trip.vehicle_class.value}; "
                f"trip status is {trip.status.value}"
                + (" (earlier offers are still open)" if core.has_pending_offers(trip) else "")
            ),
        )
    return broadcasts


@app.post("/trips/{trip_id}/broadcast-overflow", response_model=list[TripBroadcastOut])
def broadcast_overflow(trip_id: int, session: Session = Depends(get_session)):
    """Hand off the trip to active subcontractors (tier 1 first, then 2, then 3).

    Explicit dispatcher action — typically taken after own-fleet broadcast yields
    no acceptance, or pre-emptively for known peak-week capacity gaps (RSM
    Sunday departure, large wedding cluster).
    """
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    try:
        broadcasts = core.broadcast_to_subcontractors(session, trip)
    except core.TripNotBroadcastable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not broadcasts:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No active subcontractors within radius matching vehicle_class={trip.vehicle_class.value}; "
                f"trip status is {trip.status.value}"
                + (" (earlier offers are still open)" if core.has_pending_offers(trip) else "")
            ),
        )
    return broadcasts


@app.post("/trips/{trip_id}/accept", response_model=TripOut)
def accept(trip_id: int, driver_id: int, session: Session = Depends(get_session)):
    try:
        return core.accept_trip(session, trip_id, driver_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/trips/{trip_id}/decline", response_model=TripBroadcastOut)
def decline(trip_id: int, driver_id: int, session: Session = Depends(get_session)):
    try:
        return core.decline_trip(session, trip_id, driver_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/trips/{trip_id}/sub-accept", response_model=TripOut)
def sub_accept(
    trip_id: int,
    subcontractor_id: int,
    payload: SubAcceptRequest | None = None,
    session: Session = Depends(get_session),
):
    """Subcontractor accepts an overflow trip. Optionally records their cost
    quote so the trip's fare = quote + markup.
    """
    quote = payload.quoted_cost_cents if payload is not None else None
    try:
        return core.accept_trip_subcontractor(session, trip_id, subcontractor_id, quoted_cost_cents=quote)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/trips/{trip_id}/sub-decline", response_model=TripBroadcastOut)
def sub_decline(trip_id: int, subcontractor_id: int, session: Session = Depends(get_session)):
    try:
        return core.decline_trip_subcontractor(session, trip_id, subcontractor_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/trips/{trip_id}/status", response_model=TripOut)
def update_trip_status_endpoint(trip_id: int, payload: TripStatusUpdate, session: Session = Depends(get_session)):
    try:
        return core.update_trip_status(session, trip_id, payload.status, fare_cents=payload.fare_cents)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/trips/{trip_id}/broadcasts", response_model=list[TripBroadcastOut])
def list_trip_broadcasts(trip_id: int, session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return trip.broadcasts


# ---------------------------------------------------------------------------
# Reservations (customer-side inquiries from the marketing site form)
# ---------------------------------------------------------------------------


def _notify_dispatcher(message: str) -> None:
    """Fire-and-forget SMS to the dispatcher cell (env-gated, fail-soft). The
    console provider in dev/tests just prints; the Twilio provider sends a real
    SMS. Notification failures must never 500 the caller."""
    dispatcher_phone = os.getenv("DISPATCHER_PHONE", "").strip()
    if not dispatcher_phone:
        return
    try:
        get_provider().send_sms(dispatcher_phone, message)
    except Exception:  # pragma: no cover — notification failures shouldn't 500 the request
        pass


def _format_dispatcher_alert(r: Reservation) -> str:
    when = r.pickup_at.strftime("%Y-%m-%d %H:%M") if r.pickup_at is not None else (
        f"{r.pickup_date or '?'} {r.pickup_time_str or '?'}"
    )
    pax = f" ({r.passengers} pax)" if r.passengers else ""
    pref = f" [{r.vehicle_pref}]" if r.vehicle_pref else ""
    flight = f" Flight {r.flight}." if r.flight else ""
    return (
        f"NEW RESERVATION #{r.id} — {r.name} ({r.phone}). "
        f"{r.pickup_label} -> {r.dropoff_label} at {when}{pax}{pref}.{flight} "
        f"{r.email}"
    )


@app.post("/reservations", response_model=ReservationOut, status_code=201)
def create_reservation(
    request: Request,
    payload: ReservationCreate,
    session: Session = Depends(get_session),
):
    """Public endpoint — accepts inquiries from the marketing site reserve form.

    No authentication (the form is public). Honeypot-positive submissions are
    recorded with status SPAM and silently 201'd — telling a bot it tripped
    a spam filter just teaches it how to bypass.

    Rate-limited per IP via `_public_form_rate_limited` (env-tunable).
    """
    if _public_form_rate_limited(request):
        raise HTTPException(status_code=429, detail="Rate limit exceeded; try again shortly")

    is_spam = bool(payload.bot_field and payload.bot_field.strip())

    vehicle_class: VehicleClass | None = None
    if payload.vehicle_pref:
        vehicle_class = _VEHICLE_PREF_TO_CLASS.get(payload.vehicle_pref.strip().lower())

    reservation = Reservation(
        name=payload.name.strip(),
        email=payload.email.strip(),
        phone=payload.phone.strip(),
        pickup_label=payload.pickup_label.strip(),
        dropoff_label=payload.dropoff_label.strip(),
        pickup_date=payload.pickup_date,
        pickup_time_str=payload.pickup_time_str,
        pickup_at=payload.pickup_at,
        passengers=payload.passengers,
        vehicle_pref=payload.vehicle_pref,
        vehicle_class=vehicle_class,
        flight=payload.flight,
        notes=payload.notes,
        source=payload.source,
        status=ReservationStatus.SPAM if is_spam else ReservationStatus.PENDING_REVIEW,
    )
    session.add(reservation)
    session.commit()
    session.refresh(reservation)

    # Notify the dispatcher (env-gated, fail-soft).
    if not is_spam:
        _notify_dispatcher(_format_dispatcher_alert(reservation))

    return reservation


@app.get("/reservations", response_model=list[ReservationOut])
def list_reservations(
    status: ReservationStatus | None = None,
    session: Session = Depends(get_session),
):
    stmt = select(Reservation)
    if status is not None:
        stmt = stmt.where(Reservation.status == status)
    return list(session.scalars(stmt.order_by(Reservation.id.desc())).all())


@app.get("/reservations/{reservation_id}", response_model=ReservationOut)
def get_reservation(reservation_id: int, session: Session = Depends(get_session)):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return r


@app.patch("/reservations/{reservation_id}", response_model=ReservationOut)
def update_reservation(
    reservation_id: int,
    payload: ReservationUpdate,
    session: Session = Depends(get_session),
):
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if payload.linked_trip_id is not None:
        if session.get(Trip, payload.linked_trip_id) is None:
            raise HTTPException(status_code=409, detail=f"Trip {payload.linked_trip_id} not found")
    data = payload.model_dump(exclude_unset=True)
    # CONVERTED is reachable only via the promote action (one reservation → at
    # most one trip). Reject a manual flip to CONVERTED with no backing trip.
    effective_link = data.get("linked_trip_id", r.linked_trip_id)
    if (
        data.get("status") == ReservationStatus.CONVERTED
        and r.status != ReservationStatus.CONVERTED
        and effective_link is None
    ):
        raise HTTPException(
            status_code=409,
            detail="Use the promote action to convert a reservation to a trip.",
        )
    for k, v in data.items():
        setattr(r, k, v)
    session.commit()
    session.refresh(r)
    return r


# ---------------------------------------------------------------------------
# Inquiries (wedding-inquiry / vendor-onboarding / early-access forms)
# ---------------------------------------------------------------------------


def _inquiry_to_out(inq: Inquiry) -> dict:
    """Convert an Inquiry row to the API-facing dict, parsing details_json back
    into a dict for the response."""
    details: dict | None = None
    if inq.details_json:
        try:
            parsed = _json.loads(inq.details_json)
            details = parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            details = None
    return {
        "id": inq.id,
        "kind": inq.kind,
        "name": inq.name,
        "email": inq.email,
        "phone": inq.phone,
        "notes": inq.notes,
        "details": details,
        "source": inq.source,
        "status": inq.status,
        "dispatcher_notes": inq.dispatcher_notes,
        "created_at": inq.created_at,
    }


def _format_inquiry_alert(inq: Inquiry) -> str:
    kind_label = inq.kind.value.replace("_", "-")
    return (
        f"NEW INQUIRY #{inq.id} ({kind_label}) — {inq.name} <{inq.email}> "
        f"{inq.phone or '(no phone)'}. "
        f"{(inq.notes or '')[:120]}"
    )


@app.post("/inquiries", response_model=InquiryOut, status_code=201)
def create_inquiry(
    request: Request,
    payload: InquiryCreate,
    session: Session = Depends(get_session),
):
    """Public endpoint — accepts the three non-reservation marketing-site forms.

    Same posture as `/reservations`: no auth, honeypot-aware spam quarantine
    (silently 201'd so a bot can't fingerprint the spam filter), per-IP rate
    limit shared with /reservations, optional dispatcher SMS alert.
    """
    if _public_form_rate_limited(request):
        raise HTTPException(status_code=429, detail="Rate limit exceeded; try again shortly")

    is_spam = bool(payload.bot_field and payload.bot_field.strip())

    details_json = _json.dumps(payload.details) if payload.details else None
    inquiry = Inquiry(
        kind=payload.kind,
        name=payload.name.strip(),
        email=payload.email.strip(),
        phone=payload.phone,
        notes=payload.notes,
        details_json=details_json,
        source=payload.source,
        status=InquiryStatus.SPAM if is_spam else InquiryStatus.PENDING_REVIEW,
    )
    session.add(inquiry)
    session.commit()
    session.refresh(inquiry)

    if not is_spam:
        _notify_dispatcher(_format_inquiry_alert(inquiry))

    return _inquiry_to_out(inquiry)


@app.get("/inquiries", response_model=list[InquiryOut])
def list_inquiries(
    status: InquiryStatus | None = None,
    kind: str | None = None,
    session: Session = Depends(get_session),
):
    stmt = select(Inquiry)
    if status is not None:
        stmt = stmt.where(Inquiry.status == status)
    if kind is not None:
        # Accept either the enum value or the bare string. Invalid kinds just
        # match nothing rather than 422 — list endpoints stay forgiving.
        stmt = stmt.where(Inquiry.kind == kind)
    rows = list(session.scalars(stmt.order_by(Inquiry.id.desc())).all())
    return [_inquiry_to_out(r) for r in rows]


@app.get("/inquiries/{inquiry_id}", response_model=InquiryOut)
def get_inquiry(inquiry_id: int, session: Session = Depends(get_session)):
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    return _inquiry_to_out(inq)


@app.patch("/inquiries/{inquiry_id}", response_model=InquiryOut)
def update_inquiry(
    inquiry_id: int,
    payload: InquiryUpdate,
    session: Session = Depends(get_session),
):
    inq = session.get(Inquiry, inquiry_id)
    if inq is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(inq, k, v)
    session.commit()
    session.refresh(inq)
    return _inquiry_to_out(inq)


# ---------------------------------------------------------------------------
# Quote (public — pre-submission estimate for the reserve form)
# ---------------------------------------------------------------------------
#
# Phase 4.6b scaffold. The reserve form fetches /quote when pickup, dropoff,
# and vehicle are filled; the response renders as a "From $XXX — estimate,
# dispatcher confirms by phone" line above the submit button. Anything we
# can't place (free-text addresses, novel destinations) returns resolved=False
# with a human-readable message — the dispatcher's confirmation call is
# always the binding step, so the soft-fail is a real UX, not a degraded one.
#
# Same posture as /reservations and /inquiries: no auth, shared per-IP rate
# limit, no DB writes (this is a read-only quote). Numbers come from active
# RateCard rows seeded in seed.py — replace the sample values with real rates
# before going live.


@app.post("/quote", response_model=QuoteResponse)
def create_quote(
    request: Request,
    payload: QuoteRequest,
    session: Session = Depends(get_session),
):
    """Public endpoint — non-binding pricing estimate for the reserve form.

    Resolves text labels via the corridor anchor table, then runs the same
    pure fare calculator the admin uses. Returns a resolved estimate when
    everything places; soft-fails with a "dispatcher will confirm" message
    when any input can't be resolved. Always 200 — the form expects a
    response and the soft-fail message is the useful payload in that case.
    """
    if _public_form_rate_limited(request):
        raise HTTPException(status_code=429, detail="Rate limit exceeded; try again shortly")

    pickup = _anchors.resolve_anchor(payload.pickup_label)
    dropoff = _anchors.resolve_anchor(payload.dropoff_label)

    vehicle_class: VehicleClass | None = None
    if payload.vehicle_pref:
        vehicle_class = _VEHICLE_PREF_TO_CLASS.get(payload.vehicle_pref.strip().lower())

    if pickup is None or dropoff is None:
        return QuoteResponse(
            resolved=False,
            pickup_resolved=pickup[0] if pickup else None,
            dropoff_resolved=dropoff[0] if dropoff else None,
            vehicle_class=vehicle_class,
            message=(
                "Estimate unavailable for this route — the dispatcher will "
                "confirm pricing when they call to confirm your reservation."
            ),
        )

    if vehicle_class is None:
        return QuoteResponse(
            resolved=False,
            pickup_resolved=pickup[0],
            dropoff_resolved=dropoff[0],
            vehicle_class=None,
            message=(
                "Estimate unavailable until a single vehicle class is chosen "
                "— the dispatcher will confirm pricing by phone."
            ),
        )

    card = session.scalars(
        select(RateCard)
        .where(RateCard.vehicle_class == vehicle_class, RateCard.active.is_(True))
        .order_by(RateCard.id.asc())
    ).first()
    if card is None:
        return QuoteResponse(
            resolved=False,
            pickup_resolved=pickup[0],
            dropoff_resolved=dropoff[0],
            vehicle_class=vehicle_class,
            message=(
                f"Estimate unavailable for {vehicle_class.value} — the "
                "dispatcher will confirm pricing by phone."
            ),
        )

    is_airport_meet = _anchors.is_airport(pickup[0]) or _anchors.is_airport(dropoff[0])
    breakdown = _fare.quote_trip(
        pickup_lat=pickup[1],
        pickup_lon=pickup[2],
        dropoff_lat=dropoff[1],
        dropoff_lon=dropoff[2],
        pickup_time=payload.pickup_at,
        base_fare_cents=card.base_fare_cents,
        per_mile_cents=card.per_mile_cents,
        per_hour_cents=card.per_hour_cents,
        minimum_fare_cents=card.minimum_fare_cents,
        airport_meet_surcharge_cents=card.airport_meet_surcharge_cents,
        after_hours_surcharge_cents=card.after_hours_surcharge_cents,
        peak_week_multiplier=card.peak_week_multiplier,
        is_airport_meet=is_airport_meet,
        is_peak_week=payload.is_peak_week,
        rate_card_id=card.id,
        rate_card_name=card.name,
    )

    # User-facing copy. The "From $X" framing + "dispatcher confirms" tail
    # is load-bearing — keeps the estimate non-binding while still giving
    # the guest a useful expectation. Round to nearest dollar so the
    # message reads cleanly ("From $458" not "From $458.27").
    dollars = round(breakdown.total_cents / 100)
    message = f"From ${dollars} — estimate, dispatcher confirms by phone."

    return QuoteResponse(
        resolved=True,
        pickup_resolved=pickup[0],
        dropoff_resolved=dropoff[0],
        vehicle_class=vehicle_class,
        total_cents=breakdown.total_cents,
        total_dollars=breakdown.total_dollars,
        distance_miles=round(breakdown.distance_miles, 1),
        duration_hours=round(breakdown.duration_hours, 2),
        rate_card_id=breakdown.rate_card_id,
        rate_card_name=breakdown.rate_card_name,
        lines=[QuoteLine(label=l, cents=c) for l, c in breakdown.lines],
        message=message,
    )


# ---------------------------------------------------------------------------
# OSRM route proxy (v1.1.15) — one OSRM_BASE_URL for maps + fares
# ---------------------------------------------------------------------------


@app.get("/osrm/route/v1/driving/{coords}", include_in_schema=False)
def osrm_route_proxy(coords: str, request: Request):
    """Same-origin proxy for OSRM's route service, used by the dispatcher
    ``/map`` and (via ``data-osrm-url``) the marketing-site maps. Validation,
    corridor bounding, caching and failure mapping live in ``osrm_proxy.py``.
    Public + per-IP rate limited; non-2xx tells clients to keep their
    straight-line fallback."""
    if _osrm_proxy_rate_limited(request):
        return JSONResponse(
            {"detail": "Too many routing requests"}, status_code=429,
            headers={"Cache-Control": "no-store", "Retry-After": "60"},
        )
    result = _osrm.proxy_route(coords, request.query_params.multi_items())
    return Response(
        content=result.body, status_code=result.status,
        media_type="application/json", headers=result.headers,
    )


# ---------------------------------------------------------------------------
# Inbound webhooks (Twilio SMS reply)
# ---------------------------------------------------------------------------

# Matches "ACCEPT 1", "decline   42", "Accept-1", " accept1 " — the keyword
# (accept/decline) optionally followed by punctuation/whitespace then the trip id.
_REPLY_RE = re.compile(
    r"^\s*(accept|decline)\W*(\d+)(?:\s+(?:at\s+)?\$?((?:\d{1,2},\d{3}|\d{1,5})(?:\.\d{1,2})?))?\s*$",
    re.IGNORECASE,
)
# E9 (v1.1.18): an optional trailing price — "ACCEPT 12 450", "accept 12 $450.50",
# "ACCEPT 12 at 450" — is a subcontractor's quoted cost in dollars. It's recorded
# with the cost-plus markup at accept time. Drivers don't quote, so a price on a
# driver reply is ignored, and a price on DECLINE is ignored.


def _verify_twilio_signature(request: Request, params: dict[str, str]) -> bool:
    """Validate Twilio's X-Twilio-Signature header against the request URL + form params.

    Hardened default: if TWILIO_AUTH_TOKEN is set in env, signature validation
    is required (no signature → reject). If the token is not set (dev / tests),
    validation is skipped — there's no shared secret to verify against. An
    explicit TWILIO_VALIDATE_SIGNATURE=false override is still honored so a
    real-Twilio deployment can be temporarily diagnostically opened.
    """
    if os.getenv("TWILIO_VALIDATE_SIGNATURE", "").lower() == "false":
        return True  # explicit override — diagnostics only
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not token:
        return True  # no shared secret configured — dev / test path

    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        return False  # token set but library missing — fail closed

    signature = request.headers.get("X-Twilio-Signature", "")
    # E5 (v1.1.17): Twilio signs the public URL it called. Behind a TLS proxy
    # str(request.url) is http://internal-host/..., so rebuild it from the
    # configured public origin when PUBLIC_BASE_URL is set (required in
    # production). Path + query come from the request; scheme + host don't.
    public_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base:
        url = public_base + request.url.path
        if request.url.query:
            url += "?" + request.url.query
    else:
        url = str(request.url)
    return RequestValidator(token).validate(url, params, signature)


@app.post("/webhooks/twilio/sms", response_model=WebhookAck)
async def twilio_sms_webhook(
    request: Request,
    session: Session = Depends(get_session),
):
    """Inbound Twilio SMS.

    Drivers and subcontractors both reply to broadcast SMSes with `ACCEPT N`
    or `DECLINE N` where N is the trip id. We look up the sender by phone
    (Driver.phone or Subcontractor.dispatch_phone), parse the body, and route
    to the correct accept/decline function.

    Returns a JSON ack (useful for tests + ops dashboards). A TwiML reply
    isn't required by Twilio — the HTTP status is what they care about — but
    the JSON body lets the dispatcher CLI / a future ops UI confirm what
    happened.
    """
    form = await request.form()

    if not _verify_twilio_signature(request, dict(form)):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    From = form.get("From")
    Body = form.get("Body")
    if not From or not Body:
        raise HTTPException(status_code=422, detail="Missing required form fields: From, Body")

    match = _REPLY_RE.match(Body or "")
    if match is None:
        return WebhookAck(matched=False, detail=f"Unrecognized reply body: {Body!r}")

    action = match.group(1).lower()
    trip_id = int(match.group(2))
    quoted_cost_cents = (
        int(round(float(match.group(3).replace(",", "")) * 100)) if match.group(3) else None
    )
    if quoted_cost_cents is not None and quoted_cost_cents <= 0:
        quoted_cost_cents = None

    # Match sender to a driver first, then a subcontractor.
    driver = session.scalars(select(Driver).where(Driver.phone == From)).first()
    sub = (
        None
        if driver is not None
        else session.scalars(select(Subcontractor).where(Subcontractor.dispatch_phone == From)).first()
    )

    if driver is None and sub is None:
        return WebhookAck(
            matched=False,
            detail="Sender not recognized",
        )

    try:
        if driver is not None:
            if action == "accept":
                core.accept_trip(session, trip_id, driver.id)
            else:
                core.decline_trip(session, trip_id, driver.id)
            return WebhookAck(
                matched=True,
                recipient_kind="driver",
                recipient_id=driver.id,
                trip_id=trip_id,
                action=action,
                detail=f"Driver {driver.id} {action} trip {trip_id}",
            )
        else:
            assert sub is not None
            fare_note = ""
            if action == "accept":
                accepted = core.accept_trip_subcontractor(
                    session, trip_id, sub.id, quoted_cost_cents=quoted_cost_cents,
                )
                fare_note = (
                    f"; cost ${quoted_cost_cents / 100:.2f} + markup → fare ${accepted.fare_cents / 100:.2f}"
                    if quoted_cost_cents is not None
                    else "; no price in reply — fare pending, record the sub's cost on the trip page"
                )
            else:
                core.decline_trip_subcontractor(session, trip_id, sub.id)
            return WebhookAck(
                matched=True,
                recipient_kind="subcontractor",
                recipient_id=sub.id,
                trip_id=trip_id,
                action=action,
                detail=f"Subcontractor {sub.id} {action} trip {trip_id}{fare_note}",
            )
    except ValueError as exc:
        # Soft-fail webhook errors — return 200 with matched=False so Twilio
        # doesn't retry. The dispatcher can see the detail via the ops log.
        return WebhookAck(
            matched=False,
            recipient_kind="driver" if driver is not None else "subcontractor",
            recipient_id=(driver.id if driver is not None else sub.id),
            trip_id=trip_id,
            action=action,
            detail=str(exc),
        )
