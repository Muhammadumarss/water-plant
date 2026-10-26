"""Real PostgreSQL concurrency: independent connections, no test-wrapping transaction."""

import threading

import pytest
from django.db import connection

from modules.fulfillment.models import Handover
from modules.inventory.models import StockBucket

from .conftest import client_for, post_sale, sale_payload

pytestmark = pytest.mark.django_db(transaction=True)


def _run_parallel(fns):
    results, barrier = [None] * len(fns), threading.Barrier(len(fns))

    def worker(i, fn):
        try:
            barrier.wait()
            results[i] = fn()
        except Exception as exc:  # surface to the main thread
            results[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_same_operation_id_posts_once(demo, shift, customer):
    body = sale_payload(demo, customer, shift, qty="1", apply="600.00")
    results = _run_parallel([lambda: post_sale(client_for(demo.cashier), body) for _ in range(4)])
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(201) == 1, statuses
    assert all(s in (200, 201, 409) for s in statuses)
    assert Handover.objects.count() == 1


def test_two_clients_cannot_both_take_the_last_bottle(demo, shift, customer, owner_ctx):
    # Leave exactly one full bottle at the plant.
    full = StockBucket.objects.get(item=demo.bottle.stock_item, condition="FULL", location__kind="PLANT")
    StockBucket.objects.filter(id=full.id).update(reserved=full.on_hand - 1)
    bodies = [sale_payload(demo, customer, shift, qty="1", apply="600.00") for _ in range(2)]
    results = _run_parallel([lambda b=b: post_sale(client_for(demo.cashier), b) for b in bodies])
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409], codes
    assert Handover.objects.count() == 1


def test_concurrent_credit_sales_obey_one_limit(demo, shift, customer):
    customer.credit_limit = 150
    customer.save()
    bodies = [sale_payload(demo, customer, shift, qty="1", apply="500.00") for _ in range(2)]  # 100 unpaid each
    results = _run_parallel([lambda b=b: post_sale(client_for(demo.cashier), b) for b in bodies])
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409], codes
