"""Notification provider abstraction.

Three providers:
    - ConsoleProvider: prints to stdout. Default. No external dependencies.
    - TwilioProvider: real SMS via Twilio. Activated by NOTIFICATION_PROVIDER=twilio.
    - WebPushProvider: per-subscription Web Push (v1.1.0). Not a stand-alone
      NotificationProvider — invoked directly by `core.broadcast_trip` for
      drivers who have opted in via `/driver/push/subscribe`. Has its own
      shape because the W3C Push spec is per-subscription, not per-phone.

The SMS provider interface is intentionally minimal — `send_sms(to, body)` —
so the Twilio path stays clean. Web Push lives alongside, not behind,
because it has different shape (subscription dict + JSON payload + expiry
handling).
"""

from __future__ import annotations

import json as _json
import os
from typing import Protocol

from dotenv import load_dotenv

load_dotenv()


class NotificationProvider(Protocol):
    def send_sms(self, to: str, body: str) -> dict: ...


class ConsoleProvider:
    """Logs SMS to stdout. Used when no real provider is configured.

    Defensive print: on Windows the default stdout codec is cp1252, which
    can't encode the `→` arrow we use in the formatted broadcast line.
    Letting the UnicodeEncodeError bubble up would 500 the
    `POST /trips/{id}/broadcast` endpoint that calls us. Replace
    un-encodable characters with `?` instead so the broadcast itself
    always succeeds; the SMS body the test/server actually sends is
    unaffected because that's a separate write path.
    """

    def send_sms(self, to: str, body: str) -> dict:
        try:
            print(f"[SMS → {to}] {body}")
        except UnicodeEncodeError:
            import sys
            encoding = (sys.stdout.encoding or "ascii")
            print(f"[SMS -> {to}] {body}".encode(encoding, errors="replace").decode(encoding, errors="replace"))
        return {"provider": "console", "to": to, "body": body, "status": "logged"}


class TwilioProvider:
    """Real SMS via Twilio's REST API."""

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        from twilio.rest import Client

        self.client = Client(account_sid, auth_token)
        self.from_number = from_number

    def send_sms(self, to: str, body: str) -> dict:
        message = self.client.messages.create(body=body, from_=self.from_number, to=to)
        return {"provider": "twilio", "to": to, "body": body, "sid": message.sid, "status": message.status}


def get_provider() -> NotificationProvider:
    """Resolve the configured provider from env vars."""
    provider = os.getenv("NOTIFICATION_PROVIDER", "console").lower()
    if provider == "twilio":
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        from_number = os.getenv("TWILIO_FROM_NUMBER")
        if not (sid and token and from_number):
            print("[notifications] TWILIO_* env vars not set; falling back to console.")
            return ConsoleProvider()
        return TwilioProvider(sid, token, from_number)
    return ConsoleProvider()


# ---------------------------------------------------------------------------
# Web Push (v1.1.0) — separate from the SMS-shaped NotificationProvider
# ---------------------------------------------------------------------------


class WebPushResult:
    """Outcome of a `send_push` call. Encapsulates the "subscription is
    expired, delete the row" signal so the caller doesn't have to inspect
    HTTP status codes."""

    __slots__ = ("delivered", "expired", "error")

    def __init__(self, *, delivered: bool, expired: bool = False, error: str | None = None):
        self.delivered = delivered
        self.expired = expired
        self.error = error

    def __repr__(self) -> str:
        return f"WebPushResult(delivered={self.delivered}, expired={self.expired}, error={self.error!r})"


def push_notifications_enabled() -> bool:
    """Feature flag (v1.1.0). When false, the WebPushProvider refuses to send
    and `core.broadcast_trip` falls through to SMS regardless of subscription
    state. Default false until the live phone smoke test confirms UX."""
    return os.getenv("PUSH_NOTIFICATIONS_ENABLED", "false").lower() == "true"


class WebPushProvider:
    """Sends a single push to a single (endpoint, p256dh, auth) subscription.

    The W3C Push spec returns 201 (sent), 410 (subscription gone — delete
    the row), or 5xx/4xx (transient/permanent error). pywebpush raises
    `WebPushException` carrying the response; we translate to `WebPushResult`
    so the broadcast loop never crashes mid-fanout when one driver's push
    service is unhappy.

    Construction reads VAPID credentials from env. Cheap — no network calls,
    no key-derivation work. Safe to instantiate per-request.
    """

    def __init__(self):
        self.vapid_private_key = os.getenv("VAPID_PRIVATE_KEY", "").strip()
        self.vapid_contact = os.getenv("VAPID_CONTACT_EMAIL", "").strip()
        # If the contact doesn't start with mailto: or https:, RFC 8292
        # implementations may reject it. Be forgiving in dev; prod boot
        # guard already requires a non-empty value.
        if self.vapid_contact and not (
            self.vapid_contact.startswith("mailto:")
            or self.vapid_contact.startswith("https:")
        ):
            self.vapid_contact = f"mailto:{self.vapid_contact}"

    def send_push(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        payload: dict,
        ttl: int = 60,
    ) -> WebPushResult:
        """Send a single push. `ttl` is how long the push service holds the
        message if the device is offline (60s default — broadcast windows are
        seconds, not hours; stale pushes are noise)."""
        if not push_notifications_enabled():
            return WebPushResult(delivered=False, error="push notifications feature flag is off")
        if not (self.vapid_private_key and self.vapid_contact):
            return WebPushResult(
                delivered=False,
                error="VAPID credentials not configured (check VAPID_PRIVATE_KEY / VAPID_CONTACT_EMAIL)",
            )

        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            return WebPushResult(delivered=False, error="pywebpush not installed")

        try:
            webpush(
                subscription_info={
                    "endpoint": endpoint,
                    "keys": {"p256dh": p256dh, "auth": auth},
                },
                data=_json.dumps(payload),
                vapid_private_key=self.vapid_private_key,
                vapid_claims={"sub": self.vapid_contact},
                ttl=ttl,
            )
            return WebPushResult(delivered=True)
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None) if exc.response else None
            expired = status in (404, 410)
            return WebPushResult(
                delivered=False,
                expired=expired,
                error=f"push service returned {status}: {exc}",
            )
        except Exception as exc:  # network / pywebpush internal — don't crash broadcast
            return WebPushResult(delivered=False, error=f"push send failed: {exc}")


def get_push_provider() -> WebPushProvider:
    """Convenience constructor — same pattern as get_provider()."""
    return WebPushProvider()
