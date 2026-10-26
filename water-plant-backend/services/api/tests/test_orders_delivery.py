"""Order -> reserve -> dispatch -> partial/failed delivery -> settlement."""

import pytest

from modules.billing.models import Invoice
from modules.inventory.models import Reservation, StockBucket

from .conftest import client_for, op_id

pytestmark = pytest.mark.django_db


def _post(client, url, body=None, idem=False):
    body = body or {}
    if idem:
        body.setdefault("operation_id", op_id())
        return client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY=body["operation_id"])
    return client.post(url, body, format="json")


@pytest.fixture
def ready_order(demo, owner_client, customer):
    r = _post(owner_client, "/api/v1/orders", {"customer_id": str(customer.id), "lines": [
        {"product_id": str(demo.product_bottle.id), "quantity": "3"}]})
    assert r.status_code == 201, r.content
    order = r.json()
    r = _post(owner_client, f"/api/v1/orders/{order['id']}/accept", {"expected_version": order["version"]})
    assert r.status_code == 200, r.content
    r = _post(owner_client, f"/api/v1/orders/{order['id']}/reserve", {"expected_version": r.json()["version"]})
    assert r.status_code == 200, r.content
    assert r.json()["status"] == "READY"
    return r.json()


def test_public_order_request_creates_no_postings(demo):
    from rest_framework.test import APIClient

    r = APIClient().post("/api/v1/public/order-requests", {"name": "Guest", "phone": "03001112222", "lines": [
        {"product_id": str(demo.product_bottle.id), "quantity": "2"}]}, format="json")
    assert r.status_code == 201
    assert not Invoice.objects.exists() and not Reservation.objects.exists()


def test_partial_failed_and_settled_delivery(demo, owner_client, ready_order, customer):
    customer.credit_limit = 1000
    customer.save()
    rider = client_for(demo.rider)
    r = _post(owner_client, "/api/v1/delivery/runs", {"rider_id": str(demo.rider.id),
                                                      "order_ids": [ready_order["id"]]})
    run = r.json()
    r = _post(owner_client, f"/api/v1/delivery/runs/{run['id']}/dispatch", idem=True)
    assert r.status_code == 201, r.content

    stops = rider.get("/api/v1/delivery/me/stops").json()["results"]
    stop_id = stops[0]["id"]
    line_id = ready_order["lines"][0]["id"]
    # Failed attempt first: no invoice.
    r = _post(rider, f"/api/v1/delivery/stops/{stop_id}/fail", {"reason": "Customer not home"}, idem=True)
    assert r.status_code == 201
    assert not Invoice.objects.exists()
    # Partial handover of 2 of 3; deposit for 2 bottles paid.
    r = _post(rider, f"/api/v1/delivery/stops/{stop_id}/complete", {
        "lines": [{"order_line_id": line_id, "quantity": "2"}],
        "payment": {"method": "cash", "apply_amount": "1200.00"}}, idem=True)
    assert r.status_code == 201, r.content
    assert r.json()["order_status"] == "PARTIALLY_FULFILLED"
    assert r.json()["invoice"]["total"]["amount"] == "200.00"  # only actual quantity invoiced

    r = _post(owner_client, f"/api/v1/delivery/runs/{run['id']}/settle", {
        "counted_cash": "1200.00", "deposit_to_cash_account_id": str(demo.till_cash.id)}, idem=True)
    assert r.status_code == 201, r.content
    assert r.json()["cash_variance"]["amount"] == "0.00"
    rider_stock = StockBucket.objects.filter(location__kind="RIDER", on_hand__gt=0)
    assert not rider_stock.exists()
    # The undelivered bottle is re-reserved at the plant for the still-open order.
    assert Reservation.objects.filter(status="ACTIVE", bucket__location__kind="PLANT").count() == 1

    r = _post(owner_client, f"/api/v1/orders/{ready_order['id']}/cancel", {})
    assert r.json()["status"] == "CLOSED_PARTIAL"
    assert Invoice.objects.count() == 1


def test_rider_cannot_see_other_riders_stops(demo, owner_client, ready_order):
    from application.commands.setup import owner_context
    from modules.identity.models import Membership, User

    other = User.objects.create_user("rider2", "pw-123456789")
    Membership.objects.create(business=demo.business, user=other, role="RIDER")
    r = _post(owner_client, "/api/v1/delivery/runs", {"rider_id": str(demo.rider.id),
                                                      "order_ids": [ready_order["id"]]})
    _post(owner_client, f"/api/v1/delivery/runs/{r.json()['id']}/dispatch", idem=True)
    assert client_for(other).get("/api/v1/delivery/me/stops").json()["results"] == []
    assert owner_context(demo.business, demo.owner).is_owner
