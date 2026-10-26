import uuid

import pytest
from django.db import connection
from rest_framework.test import APIClient

from application.commands.setup import owner_context, seed_demo
from infrastructure.messaging.fake import FakeAdapter


@pytest.fixture(autouse=True)
def _require_postgres(request):
    if connection.vendor != "postgresql":
        pytest.skip("Backend tests require PostgreSQL (SQLite cannot prove locking behaviour).")


@pytest.fixture(autouse=True)
def _reset_fake_provider():
    FakeAdapter.reset()
    yield
    FakeAdapter.reset()


@pytest.fixture
def demo(db):
    return seed_demo()


@pytest.fixture
def owner_ctx(demo):
    return owner_context(demo.business, demo.owner)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def cashier_client(demo):
    return client_for(demo.cashier)


@pytest.fixture
def owner_client(demo):
    return client_for(demo.owner)


@pytest.fixture
def shift(demo, cashier_client):
    response = cashier_client.post("/api/v1/cash-shifts/open", {"cash_account_id": str(demo.till_cash.id),
                                                                "opening_float": "0"}, format="json")
    assert response.status_code == 201, response.content
    return response.json()


@pytest.fixture
def customer(demo):
    from modules.customers.models import Customer

    return Customer.objects.get(business=demo.business, display_name="Ahmed Household")


def op_id():
    return str(uuid.uuid4())


def sale_payload(demo, customer, shift, *, qty="3", returns=0, apply="0", tendered=None, operation_id=None,
                 version=None):
    body = {
        "operation_id": operation_id or op_id(),
        "customer_id": str(customer.id),
        "cash_shift_id": shift["id"],
        "lines": [{"product_id": str(demo.product_bottle.id), "quantity": qty}],
        "bottle_returns": ([{"container_type_id": str(demo.bottle.id), "quantity": returns}] if returns else []),
        "payment": {"method": "cash", "apply_amount": apply, "tendered_amount": tendered or apply, "currency": "PKR"},
    }
    if version is not None:
        body["lines"][0]["accepted_price_version"] = version
    return body


def post_sale(client, body):
    return client.post("/api/v1/sales/complete", body, format="json", HTTP_IDEMPOTENCY_KEY=body["operation_id"])
