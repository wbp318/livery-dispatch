"""Tests for /inquiries — the three non-reservation marketing-site forms.

Three kinds:
    - wedding (concierge.html wedding-inquiry form)
    - vendor (concierge.html vendor-onboarding form)
    - early_access (account.html early-access form)

All share one Inquiry table with kind-specific overflow stashed in a JSON
details column. Lifecycle is lighter than Reservation — no conversion to a
Trip; the dispatcher replies and closes.
"""

from __future__ import annotations


def test_wedding_inquiry_minimum_payload(client):
    r = client.post(
        "/inquiries",
        json={
            "kind": "wedding",
            "name": "Eleanor Brand",
            "email": "eleanor@brandevents.com",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "wedding"
    assert body["status"] == "pending_review"
    assert body["details"] is None
    assert body["phone"] is None


def test_wedding_inquiry_full_payload(client):
    r = client.post(
        "/inquiries",
        json={
            "kind": "wedding",
            "name": "Eleanor Brand",
            "email": "eleanor@brandevents.com",
            "phone": "+1 (912) 555-2000",
            "notes": "Friday rehearsal at the Cloister, Saturday ceremony at Sea Island Chapel.",
            "source": "site/concierge.html#wedding",
            "details": {
                "firm": "Brand Events Co.",
                "weekend_date": "2027-04-17",
                "guests": 180,
                "resort": "The Cloister, Sea Island",
            },
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["phone"] == "+19125552000"  # E.164 normalized
    assert body["details"]["firm"] == "Brand Events Co."
    assert body["details"]["guests"] == 180
    assert body["source"] == "site/concierge.html#wedding"


def test_vendor_onboarding_payload(client):
    r = client.post(
        "/inquiries",
        json={
            "kind": "vendor",
            "name": "James Whitfield",
            "email": "concierge@jekyllislandclub.com",
            "phone": "+19126351000",
            "details": {
                "title": "Director of Rooms",
                "organization": "Jekyll Island Club Resort",
                "needed": "COI + named-insured endorsement",
            },
            "source": "site/concierge.html#vendor",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "vendor"
    assert body["details"]["organization"] == "Jekyll Island Club Resort"


def test_early_access_payload(client):
    r = client.post(
        "/inquiries",
        json={
            "kind": "early_access",
            "name": "Mr Holm",
            "email": "holm@familyoffice.example",
            "details": {"account_type": "family-office"},
            "source": "site/account.html",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "early_access"
    assert body["details"]["account_type"] == "family-office"


def test_invalid_kind_rejected(client):
    r = client.post(
        "/inquiries",
        json={"kind": "purchase_yacht", "name": "X", "email": "x@y.com"},
    )
    assert r.status_code == 422


def test_missing_required_field_422(client):
    # No email
    r = client.post("/inquiries", json={"kind": "wedding", "name": "X"})
    assert r.status_code == 422


def test_honeypot_quarantines_as_spam(client):
    r = client.post(
        "/inquiries",
        json={
            "kind": "early_access",
            "name": "Bot",
            "email": "bot@spammer.example",
            "bot_field": "I am a bot",
        },
    )
    # Bot gets 201 just like a real submission — no fingerprinting of the spam filter.
    assert r.status_code == 201
    iid = r.json()["id"]

    # But the row is quarantined.
    detail = client.get(f"/inquiries/{iid}").json()
    assert detail["status"] == "spam"

    # And the default review queue doesn't include it.
    pending = client.get("/inquiries?status=pending_review").json()
    assert all(i["id"] != iid for i in pending)


def test_phone_optional_blank_string_accepted(client):
    # The site forms render `phone` even when optional; an empty string
    # should be accepted (and stored as None), not rejected as bad E.164.
    r = client.post(
        "/inquiries",
        json={"kind": "wedding", "name": "X", "email": "x@y.com", "phone": ""},
    )
    assert r.status_code == 201
    assert r.json()["phone"] is None


def test_phone_invalid_format_rejected(client):
    r = client.post(
        "/inquiries",
        json={"kind": "wedding", "name": "X", "email": "x@y.com", "phone": "555"},
    )
    assert r.status_code == 422


def test_list_filter_by_kind(client):
    for kind in ["wedding", "vendor", "early_access"]:
        client.post(
            "/inquiries",
            json={"kind": kind, "name": f"Test {kind}", "email": f"{kind}@x.com"},
        )

    weddings = client.get("/inquiries?kind=wedding").json()
    vendors = client.get("/inquiries?kind=vendor").json()
    early = client.get("/inquiries?kind=early_access").json()
    assert len(weddings) == 1 and weddings[0]["kind"] == "wedding"
    assert len(vendors) == 1 and vendors[0]["kind"] == "vendor"
    assert len(early) == 1 and early[0]["kind"] == "early_access"


def test_list_filter_by_status(client):
    iid = client.post(
        "/inquiries",
        json={"kind": "wedding", "name": "A", "email": "a@x.com"},
    ).json()["id"]
    client.patch(f"/inquiries/{iid}", json={"status": "responded"})

    pending = client.get("/inquiries?status=pending_review").json()
    responded = client.get("/inquiries?status=responded").json()
    assert all(i["status"] == "pending_review" for i in pending)
    assert any(i["id"] == iid for i in responded)


def test_dispatcher_update_status_and_notes(client):
    iid = client.post(
        "/inquiries",
        json={"kind": "vendor", "name": "X", "email": "x@y.com"},
    ).json()["id"]

    r = client.patch(
        f"/inquiries/{iid}",
        json={"status": "closed", "dispatcher_notes": "Sent COI + W-9 via email 2026-05-12."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "closed"
    assert "Sent COI" in body["dispatcher_notes"]


def test_get_nonexistent_returns_404(client):
    assert client.get("/inquiries/99999").status_code == 404
    assert client.patch("/inquiries/99999", json={"status": "closed"}).status_code == 404


def test_oversized_notes_rejected(client):
    r = client.post(
        "/inquiries",
        json={
            "kind": "wedding",
            "name": "X",
            "email": "x@y.com",
            "notes": "A" * 5000,  # max 2000
        },
    )
    assert r.status_code == 422


def test_list_ordering_newest_first(client):
    ids = []
    for i in range(3):
        ids.append(
            client.post(
                "/inquiries",
                json={"kind": "wedding", "name": f"Test {i}", "email": f"t{i}@x.com"},
            ).json()["id"]
        )
    listed = [i["id"] for i in client.get("/inquiries").json()]
    assert listed == list(reversed(ids))
