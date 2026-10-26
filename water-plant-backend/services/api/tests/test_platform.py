"""Auth sessions, scope isolation, outbox/messaging, offline sync and projections."""

import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from infrastructure.messaging.base import ProviderError
from infrastructure.messaging.fake import FakeAdapter
from modules.identity.models import Membership, User
from modules.jobs.models import EventDelivery
from modules.notifications.models import MessageAttempt, Notification, NotificationPreference

from .conftest import client_for, op_id, post_sale, sale_payload

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- auth & scope

def test_refresh_rotation_and_replay_revokes_session(demo):
    client = APIClient()
    r = client.post("/api/v1/auth/token", {"username": "cashier", "password": "demo-password-123"}, format="json")
    assert r.status_code == 201
    first = r.json()
    r = client.post("/api/v1/auth/token/refresh", {"refresh": first["refresh"]}, format="json")
    assert r.status_code == 200
    second = r.json()
    # Replaying the superseded refresh token revokes the whole session.
    r = client.post("/api/v1/auth/token/refresh", {"refresh": first["refresh"]}, format="json")
    assert r.status_code == 401
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {second['access']}")
    assert client.get("/api/v1/me").status_code == 401


def test_logout_takes_effect_before_token_expiry(demo):
    client = APIClient()
    tokens = client.post("/api/v1/auth/token", {"username": "owner", "password": "demo-password-123"},
                         format="json").json()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    assert client.get("/api/v1/me").status_code == 200
    assert client.post("/api/v1/auth/token/revoke").status_code == 204
    assert client.get("/api/v1/me").status_code == 401


def test_customer_sees_only_linked_accounts(demo, customer, owner_ctx):
    from application.commands.master import create_customer_cmd
    from modules.customers.services import link_account

    other = create_customer_cmd(owner_ctx, {"display_name": "Neighbour", "phones": ["03001234567"]})  # same phone
    portal = User.objects.create_user("portal", "pw-123456789", contact_verified_at=timezone.now())
    Membership.objects.create(business=demo.business, user=portal, role="CUSTOMER")
    link_account(owner_ctx, customer=customer, user=portal)
    client = client_for(portal)
    ids = {c["customer_id"] for c in client.get("/api/v1/me/customers").json()}
    assert ids == {str(customer.id)}
    assert client.get(f"/api/v1/customers/{other.id}/account-summary").status_code == 404
    assert client.get(f"/api/v1/customers/{customer.id}/account-summary").status_code == 200


def test_stale_master_edit_returns_412(demo, owner_client, customer):
    url = f"/api/v1/customers/{customer.id}"
    assert owner_client.patch(url, {"display_name": "A", "expected_version": 1}, format="json").status_code == 200
    r = owner_client.patch(url, {"display_name": "B", "expected_version": 1}, format="json")
    assert r.status_code == 412


# ---------------------------------------------------------------- outbox & messaging

def _drain():
    from infrastructure.outbox import process_delivery
    from modules.jobs.services import lease_due_deliveries

    for delivery_id in lease_due_deliveries():
        process_delivery(delivery_id)


def _consent(customer, channel="WHATSAPP"):
    NotificationPreference.objects.create(business=customer.business, customer=customer, selected_channel=channel,
                                          consent_given_at=timezone.now())


def test_outbox_survives_and_deduplicates(demo, cashier_client, shift, customer):
    from infrastructure.notify import submit

    _consent(customer)
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    _drain()
    _drain()  # duplicate dispatcher run: nothing new
    notifications = Notification.objects.filter(purpose="PAYMENT_RECEIVED")
    assert notifications.count() == 1
    assert EventDelivery.objects.exclude(status="COMPLETED").count() == 0
    submit(notifications.get().id)
    submit(notifications.get().id)  # duplicate task delivery
    assert len(FakeAdapter.sent) == 1


def test_uncertain_provider_outcome_is_not_resent(demo, cashier_client, shift, customer):
    from infrastructure.notify import submit

    _consent(customer)
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    _drain()
    n = Notification.objects.get(purpose="PAYMENT_RECEIVED")
    FakeAdapter.next_error = ProviderError("timeout after send", retryable=True, uncertain=True)
    assert submit(n.id) == "UNKNOWN"
    assert submit(n.id) == "UNKNOWN"  # no blind resend
    assert MessageAttempt.objects.filter(notification=n).count() == 1


def test_no_consent_means_no_message(demo, cashier_client, shift, customer):
    from infrastructure.notify import submit

    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    _drain()
    n = Notification.objects.get(purpose="PAYMENT_RECEIVED")
    assert submit(n.id) == "CANCELLED"
    assert FakeAdapter.sent == []


def test_signed_callbacks_are_deduplicated_and_monotonic(demo, cashier_client, shift, customer):
    from infrastructure.notify import submit

    _consent(customer)
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    _drain()
    n = Notification.objects.get(purpose="PAYMENT_RECEIVED")
    submit(n.id)
    message_id = MessageAttempt.objects.get(notification=n).provider_message_id

    def send(events):
        body = json.dumps({"events": events}).encode()
        sig = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
        return APIClient().post("/api/v1/webhooks/messaging/fake", body, content_type="application/json",
                                HTTP_X_SIGNATURE=sig)

    assert send([{"id": "e2", "message_id": message_id, "status": "DELIVERED"}]).json()["applied"] == 1
    assert send([{"id": "e2", "message_id": message_id, "status": "DELIVERED"}]).json()["applied"] == 0
    send([{"id": "e1", "message_id": message_id, "status": "ACCEPTED_BY_PROVIDER"}])  # late, older status
    n.refresh_from_db()
    assert n.state == "DELIVERED"
    bad = APIClient().post("/api/v1/webhooks/messaging/fake", b"{}", content_type="application/json",
                           HTTP_X_SIGNATURE="nope")
    assert bad.status_code == 401


# ---------------------------------------------------------------- offline sync

def _device_client(demo):
    from modules.identity.services import issue_session

    tokens = issue_session(demo.cashier, demo.device)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    return client


def test_offline_sale_replay_gap_and_expiry(demo, owner_client, customer):
    r = owner_client.post("/api/v1/sync/leases", {
        "operation_id": op_id(), "device_id": str(demo.device.id), "actor_id": str(demo.cashier.id),
        "stock_allocations": [{"container_type_id": str(demo.bottle.id), "quantity": "10"}]},
        format="json")
    assert r.status_code == 201, r.content
    lease = r.json()
    device = _device_client(demo)
    boot = device.post("/api/v1/sync/bootstrap").json()
    assert boot["cursor"] and boot["products"]

    now = timezone.now()

    def op(seq, when=now, qty="1"):
        return {"operation_id": op_id(), "device_sequence": seq, "lease_id": lease["lease_id"],
                "command": "CASH_SALE_WITH_OPTIONAL_BOTTLE_EXCHANGE", "local_occurred_at": when.isoformat(),
                "sale": {"customer_id": str(customer.id),
                         "lines": [{"product_id": str(demo.product_bottle.id), "quantity": qty}],
                         "payment": {"method": "cash", "apply_amount": "600.00"}}}

    first, gap = op(1), op(3)
    r = device.post("/api/v1/sync/operations", {"operations": [first, gap]}, format="json").json()
    assert [x["outcome"] for x in r["results"]] == ["ACCEPTED", "DEFERRED"]
    r = device.post("/api/v1/sync/operations", {"operations": [first]}, format="json").json()
    assert r["results"][0]["outcome"] == "ALREADY_APPLIED"
    late = op(2, when=now + timedelta(days=2))
    r = device.post("/api/v1/sync/operations", {"operations": [late]}, format="json").json()
    assert r["results"][0]["outcome"] == "REVIEW_REQUIRED"  # preserved, not dropped


def test_projections_match_ledgers_after_activity(demo, cashier_client, shift, customer):
    from application.commands.scheduled import reconcile_projections

    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="2", apply="1200.00"))
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", returns=2, apply="100.00"))
    report = reconcile_projections()
    assert report == {"stock_buckets": [], "invoice_balances": [], "receipt_balances": []}


def test_ledger_rows_are_append_only_in_database(demo, cashier_client, shift, customer):
    from django.db import DatabaseError, connection, transaction

    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    with pytest.raises(DatabaseError):
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE billing_invoice SET total = 0")


def test_health(db):
    assert APIClient().get("/api/v1/health/live").json() == {"status": "ok"}
