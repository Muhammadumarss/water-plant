"""Delivery runs: plan -> dispatch (stock to rider custody) -> complete/fail stops -> settle (stock + cash)."""

import uuid

from django.db import transaction
from django.utils import timezone

from common.errors import BusinessConflict, NotFound, ValidationFailed
from common.ids import parse_uuid
from common.money import ZERO, money, money_payload, quantity
from modules.audit.services import record as audit
from modules.customers.services import lock_credit_account
from modules.finance.models import CashAccount
from modules.finance.services import _cash_move, lock_cash_account, transfer_cash
from modules.fulfillment.models import DeliveryAttempt, DeliveryRun, DeliveryStop, Handover
from modules.idempotency.services import execute
from modules.identity.models import Membership
from modules.inventory.models import Reservation, StockBucket, StockMovement
from modules.inventory.services import (
    Allocation,
    BucketKey,
    MovementLine,
    bucket_for,
    lock_buckets,
    plant_location,
    post_movements,
    reserve,
    rider_location,
)
from modules.jobs.services import emit
from modules.orders.models import Order, OrderStatus

from .handover import HandoverRequest, SaleLine, post_handover
from .sales import build_payment, build_returns


def rider_cash_account(business, rider) -> CashAccount:
    account, _ = CashAccount.objects.get_or_create(business=business, kind=CashAccount.Kind.RIDER, rider=rider,
                                                   defaults={"name": f"Rider cash: {rider}"})
    return account


def create_run(ctx, data) -> DeliveryRun:
    ctx.require("delivery.dispatch")
    rider_id = parse_uuid(data["rider_id"], "rider_id")
    if not Membership.objects.filter(business=ctx.business, user_id=rider_id, role="RIDER", is_active=True).exists():
        raise ValidationFailed(fields={"rider_id": ["Not an active rider."]})
    order_ids = [parse_uuid(o, "order_ids") for o in data.get("order_ids", [])]
    with transaction.atomic():
        orders = {o.id: o for o in Order.objects.select_for_update().filter(business=ctx.business, id__in=order_ids)}
        if len(orders) != len(set(order_ids)):
            raise NotFound("Some orders were not found.")
        if any(o.status not in {OrderStatus.READY, OrderStatus.PARTIALLY_FULFILLED} for o in orders.values()):
            raise BusinessConflict("Only orders with reserved stock can be scheduled.", code="ORDER_NOT_READY")
        run = DeliveryRun.objects.create(business=ctx.business, rider_id=rider_id,
                                         run_date=data.get("run_date") or timezone.localdate())
        for seq, oid in enumerate(order_ids, start=1):
            DeliveryStop.objects.create(business=ctx.business, run=run, order=orders[oid], sequence=seq)
        audit(ctx, "delivery_run.created", "delivery_run", run.id, changes={"orders": order_ids})
    return run


def _lock_run(ctx, run_id) -> DeliveryRun:
    run = DeliveryRun.objects.select_for_update().filter(business=ctx.business, id=parse_uuid(run_id)).first()
    if run is None:
        raise NotFound("Delivery run not found.")
    return run


def dispatch_run(ctx, run_id, data, *, idempotency_key=None):
    """Move reserved stock from the plant into rider custody. Held batches cannot be dispatched."""
    ctx.require("delivery.dispatch")

    def handler(operation_id):
        run = _lock_run(ctx, run_id)
        if run.status != DeliveryRun.Status.PLANNED:
            raise BusinessConflict("Run is not in PLANNED state.", code="INVALID_RUN_STATE")
        stops = list(run.stops.select_related("order").order_by("sequence"))
        list(Order.objects.select_for_update().filter(id__in=[s.order_id for s in stops]).order_by("id"))
        reservations = list(Reservation.objects.filter(order_line__order__in=[s.order_id for s in stops],
                                                       status=Reservation.Status.ACTIVE)
                            .select_related("bucket", "order_line"))
        if not reservations:
            raise BusinessConflict("Nothing reserved to dispatch.", code="NOTHING_TO_DISPATCH")
        rider_loc = rider_location(ctx.business, run.rider)
        targets = {}
        for res in reservations:
            b = res.bucket
            key = BucketKey(b.item_id, rider_loc.id, b.condition, b.batch_id)
            if key not in targets:
                targets[key] = bucket_for(ctx.business, key)
        locked = lock_buckets([r.bucket_id for r in reservations] + [b.id for b in targets.values()])
        lines, new_reservations = [], []
        for res in reservations:
            src = locked.get(res.bucket)
            tgt = locked.get(targets[BucketKey(src.item_id, rider_loc.id, src.condition, src.batch_id)])
            lines.append(MovementLine(src.item, src, tgt, res.quantity, StockMovement.Type.DISPATCH,
                                      consume_reservation=res))
            new_reservations.append((res.order_line, tgt, res.quantity))
        group = post_movements(ctx, operation_id=operation_id, action="DISPATCH", lines=lines,
                               source_type="delivery_run", source_id=run.id)
        for order_line, tgt, qty in new_reservations:
            reserve(ctx, allocations=[Allocation(bucket=tgt, quantity=qty)], order_line=order_line)
        run.status, run.dispatched_at, run.dispatched_by = DeliveryRun.Status.DISPATCHED, timezone.now(), ctx.user
        run.version += 1
        run.save()
        rider_cash_account(ctx.business, run.rider)
        audit(ctx, "delivery_run.dispatched", "delivery_run", run.id, operation_id=operation_id)
        for stop in stops:
            emit(ctx, "delivery.dispatched", "order", stop.order_id,
                 {"order_id": stop.order_id, "customer_id": stop.order.customer_id, "run_id": run.id},
                 aggregate_version=stop.order.version)
        return {"_resource": ("delivery_run", run.id), "run_id": str(run.id), "status": run.status,
                "stock_posting_id": str(group.id)}

    return execute(ctx, command_name="dispatch_run", operation_id=data.get("operation_id"),
                   payload={"run_id": str(run_id), **data}, handler=handler, header_key=idempotency_key)


def _stop_for_actor(ctx, stop_id) -> DeliveryStop:
    stop = DeliveryStop.objects.select_related("run", "order").filter(business=ctx.business,
                                                                      id=parse_uuid(stop_id)).first()
    if stop is None:
        raise NotFound()
    if ctx.role == "RIDER" and stop.run.rider_id != ctx.user_id:
        raise NotFound()  # riders see assigned stops only
    if ctx.role != "RIDER":
        ctx.require("delivery.dispatch")
    return stop


def complete_stop(ctx, stop_id, data, *, idempotency_key=None):
    """Confirmed delivery: actual quantities, bottle exchange and collection, one transaction."""
    stop = _stop_for_actor(ctx, stop_id)

    def handler(operation_id):
        lock_credit_account(stop.order.customer)
        order = Order.objects.select_for_update().get(id=stop.order_id)
        run = DeliveryRun.objects.select_for_update().get(id=stop.run_id)
        locked_stop = DeliveryStop.objects.select_for_update().get(id=stop.id)
        if run.status != DeliveryRun.Status.DISPATCHED:
            raise BusinessConflict("Run is not out for delivery.", code="INVALID_RUN_STATE")
        if locked_stop.status == DeliveryStop.Status.COMPLETED:
            raise BusinessConflict("Stop already completed.", code="STOP_ALREADY_COMPLETED")
        if order.status not in {OrderStatus.READY, OrderStatus.PARTIALLY_FULFILLED}:
            raise BusinessConflict("Order cannot be handed over in its current state.", code="INVALID_ORDER_STATE")
        rider_loc = rider_location(ctx.business, run.rider)
        order_lines = {ol.id: ol for ol in order.lines.select_related(
            "product", "product__container_type", "product__container_type__stock_item", "product__stock_item")}
        sale_lines = []
        for index, raw in enumerate(data.get("lines", [])):
            ol = order_lines.get(parse_uuid(raw["order_line_id"], f"lines.{index}.order_line_id"))
            if ol is None:
                raise ValidationFailed(fields={f"lines.{index}.order_line_id": ["Not on this order."]})
            qty = quantity(raw["quantity"])
            if qty <= 0 or qty > ol.open_quantity:
                raise ValidationFailed(fields={f"lines.{index}.quantity": [f"Must be 1..{ol.open_quantity}."]})
            reservations = list(Reservation.objects.select_related("bucket").filter(
                order_line=ol, status=Reservation.Status.ACTIVE, bucket__location=rider_loc).order_by("id"))
            sale_lines.append(SaleLine(product=ol.product, quantity=qty, unit_price=ol.quote_unit_price,
                                       price_version=ol.quote_price_version, order_line=ol,
                                       reservations=reservations))
        payment = build_payment(data.get("payment"))
        if payment and payment.method != "CASH":
            ctx.require("payment.verify_digital")
        req = HandoverRequest(
            customer=order.customer, channel=Handover.Channel.DELIVERY, lines=sale_lines,
            returns=build_returns(ctx, data.get("bottle_returns")), payment=payment,
            cash_account_id=rider_cash_account(ctx.business, run.rider).id, source_locations=[rider_loc],
            return_location=rider_loc, order=order, delivery_stop=locked_stop,
        )
        result = post_handover(ctx, operation_id, req)
        for line in sale_lines:
            ol = line.order_line
            ol.fulfilled_quantity = ol.fulfilled_quantity + line.quantity
            ol.save(update_fields=["fulfilled_quantity"])
        complete = all(ol.open_quantity <= 0 for ol in order_lines.values())
        order.status = OrderStatus.FULFILLED if complete else OrderStatus.PARTIALLY_FULFILLED
        order.version += 1
        order.save(update_fields=["status", "version", "updated_at"])
        locked_stop.status = DeliveryStop.Status.COMPLETED
        locked_stop.save(update_fields=["status", "updated_at"])
        DeliveryAttempt.objects.create(business=ctx.business, stop=locked_stop, attempted_at=timezone.now(),
                                       outcome="DELIVERED" if complete else "PARTIAL", operation_id=operation_id,
                                       handover_id=result["handover_id"])
        emit(ctx, "delivery.completed", "order", order.id,
             {"order_id": order.id, "customer_id": order.customer_id, "handover_id": result["handover_id"]},
             aggregate_version=order.version)
        result["order_status"] = order.status
        return result

    return execute(ctx, command_name="complete_stop", operation_id=data.get("operation_id"),
                   payload={"stop_id": str(stop_id), **data}, handler=handler, header_key=idempotency_key)


def fail_stop(ctx, stop_id, data, *, idempotency_key=None):
    """Failed attempt: no delivered quantity, no invoice. Goods stay in rider custody until settlement."""
    stop = _stop_for_actor(ctx, stop_id)
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise ValidationFailed(fields={"reason": ["A reason is required."]})

    def handler(operation_id):
        locked_stop = DeliveryStop.objects.select_for_update().get(id=stop.id)
        if locked_stop.status == DeliveryStop.Status.COMPLETED:
            raise BusinessConflict("Stop already completed.", code="STOP_ALREADY_COMPLETED")
        locked_stop.status = DeliveryStop.Status.FAILED
        locked_stop.save(update_fields=["status", "updated_at"])
        attempt = DeliveryAttempt.objects.create(business=ctx.business, stop=locked_stop, outcome="FAILED",
                                                 reason=reason[:200], attempted_at=timezone.now(),
                                                 operation_id=operation_id)
        audit(ctx, "delivery_stop.failed", "delivery_stop", stop.id, reason=reason, operation_id=operation_id)
        return {"_resource": ("delivery_attempt", attempt.id), "stop_id": str(stop.id), "status": "FAILED"}

    return execute(ctx, command_name="fail_stop", operation_id=data.get("operation_id"),
                   payload={"stop_id": str(stop_id), **data}, handler=handler, header_key=idempotency_key)


def settle_run(ctx, run_id, data, *, idempotency_key=None):
    """Return all rider stock to the plant (re-reserving undelivered orders), and reconcile cash."""
    ctx.require("delivery.dispatch")

    def handler(operation_id):
        run = _lock_run(ctx, run_id)
        if run.status != DeliveryRun.Status.DISPATCHED:
            raise BusinessConflict("Run is not out for delivery.", code="INVALID_RUN_STATE")
        counted = money(data["counted_cash"])
        rider_account = rider_cash_account(ctx.business, run.rider)
        target_id = parse_uuid(data["deposit_to_cash_account_id"], "deposit_to_cash_account_id")
        accounts = sorted([rider_account.id, target_id], key=str)
        locked_accounts = {acc_id: lock_cash_account(ctx.business, acc_id) for acc_id in accounts}
        rider_acc, target_acc = locked_accounts[rider_account.id], locked_accounts[target_id]
        expected = money(rider_acc.balance)

        rider_loc = rider_location(ctx.business, run.rider)
        plant = plant_location(ctx.business)
        rider_buckets = list(StockBucket.objects.filter(location=rider_loc, on_hand__gt=0))
        targets = {}
        for b in rider_buckets:
            key = BucketKey(b.item_id, plant.id, b.condition, b.batch_id)
            targets[b.id] = bucket_for(ctx.business, key)
        active = list(Reservation.objects.filter(bucket__location=rider_loc, status=Reservation.Status.ACTIVE)
                      .select_related("order_line__order"))
        locked = lock_buckets([b.id for b in rider_buckets] + [t.id for t in targets.values()])
        rereserve = []
        reserved_by_bucket: dict = {}
        for res in active:
            reserved_by_bucket.setdefault(res.bucket_id, []).append(res)
        lines = []
        for b in rider_buckets:
            src = locked.get(b)
            tgt = locked.get(targets[b.id])
            remaining = src.on_hand
            for res in reserved_by_bucket.get(src.id, []):
                lines.append(MovementLine(src.item, src, tgt, res.quantity, StockMovement.Type.RETURN_TO_PLANT,
                                          consume_reservation=res))
                rereserve.append((res.order_line, tgt, res.quantity))
                remaining -= res.quantity
            if remaining > 0:
                lines.append(MovementLine(src.item, src, tgt, remaining, StockMovement.Type.RETURN_TO_PLANT))
        group = None
        if lines:
            group = post_movements(ctx, operation_id=operation_id, action="RUN_SETTLEMENT", lines=lines,
                                   source_type="delivery_run", source_id=run.id)
        for order_line, tgt, qty in rereserve:
            if order_line.order.status in {OrderStatus.READY, OrderStatus.PARTIALLY_FULFILLED}:
                reserve(ctx, allocations=[Allocation(bucket=tgt, quantity=qty)], order_line=order_line)
        if counted > 0:
            # The physically counted cash moves to the plant; any difference is recorded as variance.
            transfer_cash(ctx, source=rider_acc, target=target_acc, amount=counted,
                          transfer_group=uuid.uuid4(), reason=f"Run settlement {run.id}")
        variance = money(counted - expected)
        if rider_acc.balance != ZERO:
            direction = "IN" if rider_acc.balance < 0 else "OUT"
            _cash_move(ctx, rider_acc, direction, "ADJUSTMENT", abs(rider_acc.balance),
                       reason=f"Settlement variance run {run.id}")
        run.status, run.settled_at, run.settled_by = DeliveryRun.Status.SETTLED, timezone.now(), ctx.user
        run.expected_cash, run.counted_cash, run.cash_variance = expected, counted, variance
        run.stock_report = {"returned_lines": len(lines)}
        run.version += 1
        run.save()
        audit(ctx, "delivery_run.settled", "delivery_run", run.id, operation_id=operation_id,
              changes={"expected_cash": expected, "counted_cash": counted, "variance": variance})
        return {"_resource": ("delivery_run", run.id), "run_id": str(run.id), "status": run.status,
                "expected_cash": money_payload(expected), "counted_cash": money_payload(counted),
                "cash_variance": money_payload(variance), "stock_posting_id": str(group.id) if group else None}

    return execute(ctx, command_name="settle_run", operation_id=data.get("operation_id"),
                   payload={"run_id": str(run_id), **data}, handler=handler, header_key=idempotency_key)
