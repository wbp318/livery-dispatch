"""Tests for Web Push notification integration (v1.1.0).

PUSH_NOTIFICATIONS_ENABLED is the master feature flag. Default false →
broadcast falls through to SMS regardless of subscription state (current
behavior preserved). When true, drivers with an active subscription
receive push instead of SMS.

The actual push delivery (pywebpush → real push services) is mocked at
the `WebPushProvider.send_push` boundary so tests don't hit FCM/Mozilla.

These tests cover:
  1. Subscribe endpoint — auth, CSRF, upsert behavior, schema validation
  2. Unsubscribe endpoint — removes the row, idempotent
  3. VAPID public-key endpoint — auth-gated, reports feature-flag state
  4. Broadcast integration — push when subscribed + flag on, SMS otherwise
  5. Expired-subscription cleanup — 410/404 from push service deletes row
  6. Token rotation — cascade-deletes push subscriptions
  7. WebPushProvider — flag-gate refuses send when off; missing VAPID
     credentials refused; happy path with mocked pywebpush
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import select

from models import (
    Driver,
    DriverPushSubscription,
    DriverStatus,
    Trip,
    TripStatus,
    VehicleClass,
)
from notifications import WebPushProvider, WebPushResult, push_notifications_enabled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_auth(client):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    client.headers.pop("Authorization", None)


def _make_driver(session, *, name="Test", phone="+19045550100", token="t" * 43) -> Driver:
    d = Driver(
        name=name,
        phone=phone,
        vehicle_classes="suv",
        status=DriverStatus.AVAILABLE,
        lat=31.2,
        lon=-81.5,
        auth_token=token,
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def _bearer(client, driver):
    _strip_auth(client)
    client.headers["Authorization"] = f"Bearer {driver.auth_token}"


# Test fixture: deliberately low-entropy placeholder values so static
# scanners (GitGuardian, etc.) don't false-positive on realistic-looking
# base64url strings. Lengths match the real-world shape (88 char p256dh,
# 22 char auth) so Pydantic min_length validation still exercises the
# right path. See .gitguardian.yaml for the suppression that pins this.
_SAMPLE_SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/test-endpoint-abc123",
    "keys": {
        "p256dh": "TEST_P256DH_" + "X" * 76,  # 88 chars total, zero-entropy
        "auth":   "TEST_AUTH_" + "Y" * 12,    # 22 chars total, zero-entropy
    },
}


# ---------------------------------------------------------------------------
# 1. Subscribe endpoint
# ---------------------------------------------------------------------------


class TestPushSubscribeEndpoint:

    def test_subscribe_requires_auth(self, client):
        _strip_auth(client)
        r = client.post("/driver/push/subscribe", json=_SAMPLE_SUB)
        assert r.status_code == 401

    def test_subscribe_persists_row(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        r = client.post("/driver/push/subscribe", json=_SAMPLE_SUB)
        assert r.status_code == 200, r.text
        rows = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).all())
        assert len(rows) == 1
        assert rows[0].endpoint == _SAMPLE_SUB["endpoint"]
        assert rows[0].p256dh == _SAMPLE_SUB["keys"]["p256dh"]
        assert rows[0].auth == _SAMPLE_SUB["keys"]["auth"]

    def test_re_subscribing_upserts_not_duplicates(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        r1 = client.post("/driver/push/subscribe", json=_SAMPLE_SUB)
        assert r1.status_code == 200
        # Second subscribe from "same device" (same endpoint) with refreshed
        # keys — should update the row, not create a second.
        refreshed = {**_SAMPLE_SUB, "keys": {"p256dh": "X" * 88, "auth": "Y" * 22}}
        r2 = client.post("/driver/push/subscribe", json=refreshed)
        assert r2.status_code == 200
        rows = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).all())
        assert len(rows) == 1
        # The second response gives the same id (upsert, not insert).
        assert r1.json()["id"] == r2.json()["id"]

    def test_two_devices_same_driver_two_rows(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        client.post("/driver/push/subscribe", json=_SAMPLE_SUB)
        second = {**_SAMPLE_SUB, "endpoint": "https://fcm.googleapis.com/fcm/send/second-device"}
        client.post("/driver/push/subscribe", json=second)
        rows = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).all())
        assert len(rows) == 2, f"phone + tablet should be two rows, got {len(rows)}"

    def test_subscribe_validates_schema(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        # Missing keys field.
        r = client.post("/driver/push/subscribe", json={"endpoint": "https://x.com/e"})
        assert r.status_code == 422

    def test_subscribe_rejects_oversize_endpoint(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        bad = {**_SAMPLE_SUB, "endpoint": "https://x.com/" + ("A" * 600)}
        r = client.post("/driver/push/subscribe", json=bad)
        assert r.status_code == 422

    def test_subscribe_records_user_agent(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        r = client.post(
            "/driver/push/subscribe",
            json=_SAMPLE_SUB,
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4)"},
        )
        assert r.status_code == 200
        row = session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).first()
        assert row.user_agent is not None and "iPhone" in row.user_agent

    def test_subscribe_isolated_between_drivers(self, client, session):
        a = _make_driver(session, name="A", phone="+19045550201", token="a" * 43)
        b = _make_driver(session, name="B", phone="+19045550202", token="b" * 43)
        _bearer(client, a)
        client.post("/driver/push/subscribe", json=_SAMPLE_SUB)
        # Driver B has no subscriptions.
        rows = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == b.id)
        ).all())
        assert rows == []


# ---------------------------------------------------------------------------
# 2. Unsubscribe endpoint
# ---------------------------------------------------------------------------


class TestPushUnsubscribeEndpoint:

    def test_unsubscribe_removes_row(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        client.post("/driver/push/subscribe", json=_SAMPLE_SUB)
        r = client.request(
            "DELETE",
            "/driver/push/subscribe",
            json={"endpoint": _SAMPLE_SUB["endpoint"]},
        )
        assert r.status_code == 200
        rows = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).all())
        assert rows == []

    def test_unsubscribe_unknown_endpoint_is_idempotent(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        r = client.request(
            "DELETE",
            "/driver/push/subscribe",
            json={"endpoint": "https://nothing.example.com/never-subscribed"},
        )
        # No row to delete — still 200, not 404.
        assert r.status_code == 200

    def test_unsubscribe_requires_auth(self, client):
        _strip_auth(client)
        r = client.request(
            "DELETE",
            "/driver/push/subscribe",
            json={"endpoint": _SAMPLE_SUB["endpoint"]},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. VAPID public-key endpoint
# ---------------------------------------------------------------------------


class TestPushVAPIDKeyEndpoint:

    def test_vapid_key_requires_auth(self, client):
        _strip_auth(client)
        r = client.get("/driver/push/vapid-key")
        assert r.status_code == 401

    def test_vapid_key_reports_flag_off_by_default(self, client, session, monkeypatch):
        monkeypatch.delenv("PUSH_NOTIFICATIONS_ENABLED", raising=False)
        d = _make_driver(session)
        _bearer(client, d)
        r = client.get("/driver/push/vapid-key")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False

    def test_vapid_key_reports_flag_on(self, client, session, monkeypatch):
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("VAPID_PUBLIC_KEY", "BAtest-public-key-for-tests")
        d = _make_driver(session)
        _bearer(client, d)
        r = client.get("/driver/push/vapid-key")
        body = r.json()
        assert body["enabled"] is True
        assert body["publicKey"] == "BAtest-public-key-for-tests"


# ---------------------------------------------------------------------------
# 4. Broadcast integration — push when subscribed + flag on, SMS otherwise
# ---------------------------------------------------------------------------


def _trip_payload(vehicle_class: str = "suv") -> dict:
    return {
        "passenger_name": "Mr Smith",
        "passenger_phone": "+19045556789",
        "pickup_label": "JAX",
        "pickup_lat": 30.49,
        "pickup_lon": -81.69,
        "dropoff_label": "Cloister",
        "dropoff_lat": 31.20,
        "dropoff_lon": -81.34,
        "pickup_time": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
        "vehicle_class": vehicle_class,
    }


class TestBroadcastPushIntegration:

    def test_broadcast_uses_sms_when_flag_off(self, client, session, monkeypatch):
        # Flag default is "false" — even with a subscription present, SMS is used.
        monkeypatch.delenv("PUSH_NOTIFICATIONS_ENABLED", raising=False)
        d = _make_driver(session)
        _bearer(client, d)
        client.post("/driver/push/subscribe", json=_SAMPLE_SUB)

        # Reset client auth to admin (broadcasting is an admin op).
        from admin_auth import create_session_cookie, make_admin_csrf_token, COOKIE_NAME
        client.cookies.clear()
        client.headers.pop("Authorization", None)
        sess = create_session_cookie("admin")
        client.cookies.set(COOKIE_NAME, sess)
        client.headers["X-CSRF-Token"] = make_admin_csrf_token(sess)

        # Patch the SMS provider to record calls.
        sms_calls = []
        push_calls = []

        class _RecordingProvider:
            def send_sms(self, to, body):
                sms_calls.append((to, body))
                return {"status": "logged"}

        with patch("core.get_provider", return_value=_RecordingProvider()), \
             patch("notifications.WebPushProvider.send_push") as send_push_mock:
            trip = client.post("/trips", json=_trip_payload()).json()
            r = client.post(f"/trips/{trip['id']}/broadcast")
            assert r.status_code == 200, r.text

        assert len(sms_calls) == 1, f"expected 1 SMS, got {len(sms_calls)} (push: {send_push_mock.call_count})"
        send_push_mock.assert_not_called()

    def test_broadcast_uses_push_when_flag_on_and_subscribed(self, client, session, monkeypatch):
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-pem")
        monkeypatch.setenv("VAPID_CONTACT_EMAIL", "mailto:test@example.com")
        d = _make_driver(session)
        _bearer(client, d)
        client.post("/driver/push/subscribe", json=_SAMPLE_SUB)

        # Reset to admin auth.
        from admin_auth import create_session_cookie, make_admin_csrf_token, COOKIE_NAME
        client.cookies.clear()
        client.headers.pop("Authorization", None)
        sess = create_session_cookie("admin")
        client.cookies.set(COOKIE_NAME, sess)
        client.headers["X-CSRF-Token"] = make_admin_csrf_token(sess)

        sms_calls = []

        class _RecordingProvider:
            def send_sms(self, to, body):
                sms_calls.append((to, body))
                return {"status": "logged"}

        with patch("core.get_provider", return_value=_RecordingProvider()), \
             patch("notifications.WebPushProvider.send_push",
                   return_value=WebPushResult(delivered=True)) as send_push_mock:
            trip = client.post("/trips", json=_trip_payload()).json()
            r = client.post(f"/trips/{trip['id']}/broadcast")
            assert r.status_code == 200, r.text

        assert send_push_mock.call_count == 1, \
            f"expected 1 push call, got {send_push_mock.call_count}"
        assert sms_calls == [], f"SMS should not have been sent, got {sms_calls}"

    def test_broadcast_falls_back_to_sms_when_subscribed_but_push_fails(self, client, session, monkeypatch):
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-pem")
        monkeypatch.setenv("VAPID_CONTACT_EMAIL", "mailto:test@example.com")
        d = _make_driver(session)
        _bearer(client, d)
        client.post("/driver/push/subscribe", json=_SAMPLE_SUB)

        from admin_auth import create_session_cookie, make_admin_csrf_token, COOKIE_NAME
        client.cookies.clear()
        client.headers.pop("Authorization", None)
        sess = create_session_cookie("admin")
        client.cookies.set(COOKIE_NAME, sess)
        client.headers["X-CSRF-Token"] = make_admin_csrf_token(sess)

        sms_calls = []

        class _RecordingProvider:
            def send_sms(self, to, body):
                sms_calls.append((to, body))
                return {"status": "logged"}

        with patch("core.get_provider", return_value=_RecordingProvider()), \
             patch("notifications.WebPushProvider.send_push",
                   return_value=WebPushResult(delivered=False, error="network error")):
            trip = client.post("/trips", json=_trip_payload()).json()
            client.post(f"/trips/{trip['id']}/broadcast")

        # Push failed → SMS fallback.
        assert len(sms_calls) == 1, "push-failed → SMS should fire"

    def test_broadcast_expired_subscription_deletes_row(self, client, session, monkeypatch):
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-pem")
        monkeypatch.setenv("VAPID_CONTACT_EMAIL", "mailto:test@example.com")
        d = _make_driver(session)
        _bearer(client, d)
        client.post("/driver/push/subscribe", json=_SAMPLE_SUB)

        # Confirm the row exists pre-broadcast.
        pre = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).all())
        assert len(pre) == 1

        from admin_auth import create_session_cookie, make_admin_csrf_token, COOKIE_NAME
        client.cookies.clear()
        client.headers.pop("Authorization", None)
        sess = create_session_cookie("admin")
        client.cookies.set(COOKIE_NAME, sess)
        client.headers["X-CSRF-Token"] = make_admin_csrf_token(sess)

        # Push service returns 410 (subscription gone) — broadcast must
        # delete the row and fall back to SMS.
        with patch("core.get_provider"), \
             patch("notifications.WebPushProvider.send_push",
                   return_value=WebPushResult(delivered=False, expired=True, error="410 Gone")):
            trip = client.post("/trips", json=_trip_payload()).json()
            client.post(f"/trips/{trip['id']}/broadcast")

        # Stale row deleted.
        post = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).all())
        assert post == [], "expired subscription must be deleted from the broadcast loop"

    def test_broadcast_uses_sms_when_no_subscription(self, client, session, monkeypatch):
        # Flag on but driver hasn't subscribed → SMS as fallback.
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-private-pem")
        monkeypatch.setenv("VAPID_CONTACT_EMAIL", "mailto:test@example.com")
        d = _make_driver(session)
        # Skip the subscribe call.

        from admin_auth import create_session_cookie, make_admin_csrf_token, COOKIE_NAME
        client.cookies.clear()
        sess = create_session_cookie("admin")
        client.cookies.set(COOKIE_NAME, sess)
        client.headers["X-CSRF-Token"] = make_admin_csrf_token(sess)

        sms_calls = []

        class _RecordingProvider:
            def send_sms(self, to, body):
                sms_calls.append((to, body))
                return {"status": "logged"}

        with patch("core.get_provider", return_value=_RecordingProvider()), \
             patch("notifications.WebPushProvider.send_push") as send_push_mock:
            trip = client.post("/trips", json=_trip_payload()).json()
            client.post(f"/trips/{trip['id']}/broadcast")

        assert len(sms_calls) == 1, "no subscription → SMS"
        send_push_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Token rotation cascade-deletes push subscriptions
# ---------------------------------------------------------------------------


class TestPushTokenRotationCascade:

    def test_rotate_token_deletes_push_subscriptions(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        # Two devices subscribed.
        client.post("/driver/push/subscribe", json=_SAMPLE_SUB)
        second = {**_SAMPLE_SUB, "endpoint": "https://fcm.googleapis.com/fcm/send/second-device"}
        client.post("/driver/push/subscribe", json=second)
        pre = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).all())
        assert len(pre) == 2

        # Reset to admin auth and rotate.
        from admin_auth import create_session_cookie, make_admin_csrf_token, COOKIE_NAME
        client.cookies.clear()
        client.headers.pop("Authorization", None)
        sess = create_session_cookie("admin")
        client.cookies.set(COOKIE_NAME, sess)
        client.headers["X-CSRF-Token"] = make_admin_csrf_token(sess)

        r = client.post(
            f"/admin/drivers/{d.id}/action/rotate-token",
            follow_redirects=False,
        )
        assert r.status_code == 303

        # All subscriptions for this driver gone.
        session.expire_all()
        post = list(session.scalars(
            select(DriverPushSubscription).where(DriverPushSubscription.driver_id == d.id)
        ).all())
        assert post == [], "rotating token must cascade-delete push subscriptions"


# ---------------------------------------------------------------------------
# 6. WebPushProvider unit tests
# ---------------------------------------------------------------------------


class TestWebPushProvider:

    def test_provider_refuses_send_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("PUSH_NOTIFICATIONS_ENABLED", raising=False)
        p = WebPushProvider()
        result = p.send_push(
            endpoint="https://x.com/e", p256dh="x" * 88, auth="x" * 22, payload={"hi": 1},
        )
        assert result.delivered is False
        assert "flag" in (result.error or "").lower()

    def test_provider_refuses_send_without_vapid(self, monkeypatch):
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "true")
        monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
        p = WebPushProvider()
        result = p.send_push(
            endpoint="https://x.com/e", p256dh="x" * 88, auth="x" * 22, payload={"hi": 1},
        )
        assert result.delivered is False
        assert "vapid" in (result.error or "").lower()

    def test_provider_translates_410_to_expired(self, monkeypatch):
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "true")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-key")
        monkeypatch.setenv("VAPID_CONTACT_EMAIL", "mailto:test@example.com")

        # Mock pywebpush.webpush to raise WebPushException with 410 status.
        from pywebpush import WebPushException
        mock_response = MagicMock()
        mock_response.status_code = 410

        def _raise_410(**kwargs):
            raise WebPushException("Subscription gone", response=mock_response)

        with patch("pywebpush.webpush", side_effect=_raise_410):
            p = WebPushProvider()
            result = p.send_push(
                endpoint="https://x.com/e", p256dh="x" * 88, auth="x" * 22, payload={"hi": 1},
            )
        assert result.delivered is False
        assert result.expired is True

    def test_provider_contact_email_gets_mailto_prefix(self, monkeypatch):
        # Bare email (no mailto:) should be normalized.
        monkeypatch.setenv("VAPID_CONTACT_EMAIL", "ops@example.com")
        p = WebPushProvider()
        assert p.vapid_contact == "mailto:ops@example.com"

    def test_push_notifications_enabled_helper(self, monkeypatch):
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "true")
        assert push_notifications_enabled() is True
        monkeypatch.setenv("PUSH_NOTIFICATIONS_ENABLED", "false")
        assert push_notifications_enabled() is False
        monkeypatch.delenv("PUSH_NOTIFICATIONS_ENABLED", raising=False)
        assert push_notifications_enabled() is False  # default off


# ---------------------------------------------------------------------------
# 7. Service worker push handler renders
# ---------------------------------------------------------------------------


class TestServiceWorkerPushHandler:

    def test_sw_includes_push_event_handler(self, client):
        _strip_auth(client)
        r = client.get("/driver/sw.js")
        assert r.status_code == 200
        body = r.text
        assert "addEventListener('push'" in body, "SW must register push listener"
        assert "showNotification" in body, "SW push handler must call showNotification"
        assert "notificationclick" in body, "SW must handle notification taps"

    def test_dashboard_renders_push_block(self, client, session):
        d = _make_driver(session)
        _bearer(client, d)
        r = client.get("/driver")
        assert r.status_code == 200
        assert 'id="push-block"' in r.text
        assert 'id="push-enable-btn"' in r.text
