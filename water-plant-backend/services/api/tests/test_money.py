"""Credit notes on paid invoices, advance and deposit refunds, receipt fund conservation."""

import pytest

from modules.finance.models import ReceiptBalance

from .conftest import op_id, post_sale, sale_payload

pytestmark = pytest.mark.django_db


def _post(client, url, body):
    return client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY=body["operation_id"])


def test_credit_note_on_paid_invoice_releases_allocation_then_refund(demo, cashier_client, owner_client, shift,
                                                                      customer):
    r = post_sale(cashier_client, sale_payload(demo, customer, shift, qty="2", apply="1200.00"))
    invoice_id = r.json()["invoice"]["id"]
    r = _post(owner_client, f"/api/v1/invoices/{invoice_id}/credit-notes",
              {"operation_id": op_id(), "amount": "100.00", "reason": "One bottle was leaking"})
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["released_allocations"][0]["amount"] == "100.00"
    assert body["account_summary"]["advance_credit"]["amount"] == "100.00"
    assert body["account_summary"]["dues"]["amount"] == "0.00"

    r = _post(owner_client, "/api/v1/refunds", {"operation_id": op_id(), "customer_id": str(customer.id),
                                                "amount": "100.00", "reason": "Credit refund",
                                                "cash_account_id": str(demo.till_cash.id)})
    assert r.status_code == 201, r.content
    assert r.json()["account_summary"]["advance_credit"]["amount"] == "0.00"
    # Cannot spend the same funds twice.
    r = _post(owner_client, "/api/v1/refunds", {"operation_id": op_id(), "customer_id": str(customer.id),
                                                "amount": "1.00", "reason": "again",
                                                "cash_account_id": str(demo.till_cash.id)})
    assert r.json()["error"]["code"] == "INSUFFICIENT_FUNDS"
    for rb in ReceiptBalance.objects.all():
        assert rb.allocated + rb.refunded <= rb.amount


def test_deposit_refund_after_bottles_returned(demo, cashier_client, owner_client, shift, customer):
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="2", apply="1200.00"))
    body = sale_payload(demo, customer, shift, returns=2, apply="0")
    body["lines"], body["payment"] = [], None
    r = post_sale(cashier_client, body)  # return-only handover: no invoice
    assert r.status_code == 201, r.content
    assert r.json()["invoice"] is None
    assert r.json()["account_summary"]["bottle_holdings"][0]["quantity"] == "0"

    r = _post(owner_client, "/api/v1/deposit-refunds", {
        "operation_id": op_id(), "customer_id": str(customer.id), "amount": "1000.00",
        "reason": "Customer stopped service", "cash_account_id": str(demo.till_cash.id)})
    assert r.status_code == 201, r.content
    s = r.json()["account_summary"]
    assert s["deposit_balance"]["amount"] == "0.00"
    assert s["advance_credit"]["amount"] == "0.00"


def test_deposit_refund_blocked_while_bottles_held(demo, cashier_client, owner_client, shift, customer):
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    r = _post(owner_client, "/api/v1/deposit-refunds", {
        "operation_id": op_id(), "customer_id": str(customer.id), "amount": "500.00", "reason": "x",
        "cash_account_id": str(demo.till_cash.id)})
    assert r.json()["error"]["code"] == "BOTTLES_STILL_HELD"


def test_receive_payment_allocates_oldest_first(demo, cashier_client, shift, customer):
    customer.credit_limit = 1000
    customer.save()
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="500.00"))
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="500.00"))
    body = {"operation_id": op_id(), "customer_id": str(customer.id), "amount": "150.00",
            "cash_shift_id": shift["id"]}
    r = _post(cashier_client, "/api/v1/payments/receive", body)
    assert r.status_code == 201, r.content
    data = r.json()
    assert [a["amount"] for a in data["allocations"]] == ["100.00", "50.00"]
    assert data["account_summary"]["dues"]["amount"] == "50.00"


def test_shift_close_reports_variance(demo, cashier_client, shift, customer):
    post_sale(cashier_client, sale_payload(demo, customer, shift, qty="1", apply="600.00"))
    r = cashier_client.post(f"/api/v1/cash-shifts/{shift['id']}/close", {"counted_close": "590.00"}, format="json")
    assert r.status_code == 200
    assert r.json()["expected_close"] == "600.00"
    assert r.json()["variance"] == "-10.00"
