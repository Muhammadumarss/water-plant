"""Counter sale: the architecture's worked example, idempotency, rollback, credit and quality rules."""

from decimal import Decimal

import pytest

from modules.billing.models import Invoice
from modules.finance.models import PaymentReceipt
from modules.fulfillment.models import Handover
from modules.inventory.models import StockBucket, StockMovement
from modules.jobs.models import OutboxEvent

from .conftest import op_id, post_sale, sale_payload

pytestmark = pytest.mark.django_db


def test_architecture_worked_example(demo, cashier_client, shift, customer):
    """Opening: 2 bottles held, deposit 1000. Deliver 3, collect 2, receive 800 (tendered 1000)."""
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="2", apply="1200.00"))
    assert r.status_code == 201, r.content
    summary = r.json()["account_summary"]
    assert summary["deposit_balance"]["amount"] == "1000.00"
    assert summary["bottle_holdings"][0]["quantity"] == "2"

    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="3", returns=2, apply="800.00",
                                               tendered="1000.00"))
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["change_due"]["amount"] == "200.00"
    assert body["payment"]["received"]["amount"] == "800.00"  # recorded once, not the tendered 1000
    assert body["payment"]["allocated_to_invoice"]["amount"] == "300.00"  # water revenue
    assert body["payment"]["allocated_to_deposit"]["amount"] == "500.00"
    s = body["account_summary"]
    assert s["dues"]["amount"] == "0.00"
    assert s["deposit_balance"]["amount"] == "1500.00"
    assert s["advance_credit"]["amount"] == "0.00"
    assert s["bottle_holdings"][0]["quantity"] == "3"
    dirty = StockBucket.objects.get(location=demo.till, item=demo.bottle.stock_item, condition="DIRTY_RETURNED")
    assert dirty.on_hand == 2
    assert OutboxEvent.objects.filter(event_type="invoice.issued").count() == 2


def test_replay_returns_original_and_conflict_on_changed_payload(demo, cashier_client, shift, customer):
    body = sale_payload(demo, customer, shift, qty="1", apply="600.00")
    first = post_sale(cashier_client, body)
    assert first.status_code == 201
    again = post_sale(cashier_client, body)
    assert again.status_code == 200
    assert again.json()["handover_id"] == first.json()["handover_id"]
    assert Handover.objects.count() == 1 and PaymentReceipt.objects.count() == 1

    changed = {**body, "lines": [{"product_id": str(demo.product_bottle.id), "quantity": "2"}]}
    conflict = post_sale(cashier_client, changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_idempotency_header_must_match(demo, cashier_client, shift, customer):
    body = sale_payload(demo, customer, shift, qty="1", apply="600.00")
    r = cashier_client.post("/api/v1/sales/complete", body, format="json", HTTP_IDEMPOTENCY_KEY=op_id())
    assert r.status_code == 400


def test_insufficient_stock_has_no_partial_effects(demo, cashier_client, shift, customer):
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1000", apply="0"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    assert not Invoice.objects.exists() and not Handover.objects.exists()
    assert not StockMovement.objects.filter(movement_type="CUSTOMER_ISSUE").exists()


def test_deposit_must_be_funded_and_credit_limit_enforced(demo, cashier_client, shift, customer):
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="100.00"))
    assert r.json()["error"]["code"] == "DEPOSIT_NOT_FUNDED"
    # Deposit paid, water charge unpaid, but customer is cash-only (credit limit 0).
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="500.00"))
    assert r.json()["error"]["code"] == "CREDIT_LIMIT_EXCEEDED"
    customer.credit_limit = Decimal("150.00")
    customer.save()
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="500.00"))
    assert r.status_code == 201
    assert r.json()["account_summary"]["dues"]["amount"] == "100.00"


def test_stale_price_version_requires_reconfirmation(demo, cashier_client, shift, customer, owner_ctx):
    from modules.catalog.services import publish_price

    publish_price(owner_ctx, product=demo.product_bottle, amount="120.00")
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="620.00", version=1))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PRICE_CHANGED"
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="620.00", version=2))
    assert r.status_code == 201


def test_return_cannot_exceed_customer_holding(demo, cashier_client, shift, customer):
    body = sale_payload(demo, customer, shift, qty="1", returns=3, apply="100.00")
    r = post_sale(cashier_client, body)
    assert r.json()["error"]["code"] == "RETURN_EXCEEDS_HOLDING"


def test_held_batch_cannot_be_sold(demo, cashier_client, shift, customer, owner_ctx):
    from application.commands.plant import hold_batch
    from modules.production.models import ProductionBatch

    hold_batch(owner_ctx, ProductionBatch.objects.get().id, {"reason": "Complaint about taste"})
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NOT_RELEASED"


def test_refill_does_not_touch_bottle_custody(demo, cashier_client, shift, customer):
    body = sale_payload(demo, customer, shift, apply="80.00")
    body["lines"] = [{"product_id": str(demo.product_refill.id), "quantity": "1"}]
    r = post_sale(cashier_client, body)
    assert r.status_code == 201, r.content
    assert r.json()["account_summary"]["bottle_holdings"] == []  # customer-owned containers are excluded


def test_failure_mid_posting_rolls_back_everything(demo, cashier_client, shift, customer, monkeypatch):
    import application.commands.handover as handover

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash after stock posting")

    monkeypatch.setattr(handover, "emit", boom)
    with pytest.raises(RuntimeError):
        post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    assert not Handover.objects.exists() and not Invoice.objects.exists()
    assert not PaymentReceipt.objects.exists()
    full = StockBucket.objects.get(item=demo.bottle.stock_item, condition="FULL", location__kind="PLANT")
    assert full.on_hand == 100


def test_cashier_cannot_issue_credit_notes(demo, cashier_client, shift, customer):
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    invoice_id = r.json()["invoice"]["id"]
    body = {"operation_id": op_id(), "amount": "50.00", "reason": "test"}
    r = cashier_client.post(f"/api/v1/invoices/{invoice_id}/credit-notes", body, format="json",
                            HTTP_IDEMPOTENCY_KEY=body["operation_id"])
    assert r.status_code == 403
