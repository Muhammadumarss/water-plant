"""Order lifecycle: request -> accept (quote snapshot, capacity) -> reserve (physical stock) -> handover."""

from django.db import IntegrityError, transaction
from django.db.models import Count

from common.errors import BusinessConflict, Forbidden, NotFound, StaleVersion, ValidationFailed
from common.ids import parse_uuid
from common.money import quantity
from common.phone import normalize_phone
from common.timeutil import business_today
from modules.audit.services import record as audit
from modules.catalog.services import current_price, get_product
from modules.customers.models import Customer, CustomerAccountLink
from modules.inventory.models import Reservation, StockLocation
from modules.inventory.services import (
    allocate_from,
    candidate_buckets,
    lock_buckets,
    plant_location,
    release_reservation,
    reserve,
)
from modules.jobs.services import emit
from modules.orders.models import TRANSITIONS, Order, OrderLine, OrderStatus
from modules.organization.models import DeliveryWindow, ServiceArea

from .handover import stock_requirement


def _transition(order: Order, target: str):
    if target not in TRANSITIONS.get(order.status, set()):
        raise BusinessConflict(f"Cannot move order from {order.status} to {target}.", code="INVALID_ORDER_STATE")
    order.status = target
    order.version += 1


def lock_order(ctx, order_id, expected_version=None) -> Order:
    order = Order.objects.select_for_update().filter(business=ctx.business, id=parse_uuid(order_id)).first()
    if order is None:
        raise NotFound("Order not found.")
    if expected_version is not None and int(expected_version) != order.version:
        raise StaleVersion()
    return order


def _validate_lines(ctx, lines_data):
    if not lines_data:
        raise ValidationFailed(fields={"lines": ["At least one line is required."]})
    out = []
    for index, raw in enumerate(lines_data):
        product = get_product(ctx, parse_uuid(raw["product_id"], f"lines.{index}.product_id"))
        qty = quantity(raw["quantity"])
        if qty <= 0:
            raise ValidationFailed(fields={f"lines.{index}.quantity": ["Must be greater than zero."]})
        out.append((product, qty))
    return out


def _create_order(ctx, *, customer, guest_name="", guest_phone="", channel, data, placed_by=None):
    lines = _validate_lines(ctx, data.get("lines"))
    window = None
    if data.get("delivery_window_id"):
        window = DeliveryWindow.objects.filter(business=ctx.business, id=parse_uuid(data["delivery_window_id"]),
                                               is_active=True).first()
        if window is None:
            raise ValidationFailed(fields={"delivery_window_id": ["Unknown delivery window."]})
    area = None
    if data.get("service_area_id"):
        area = ServiceArea.objects.filter(business=ctx.business, id=parse_uuid(data["service_area_id"]),
                                          is_active=True).first()
        if area is None:
            raise ValidationFailed(fields={"service_area_id": ["We do not serve this area."]})
    op = parse_uuid(data["operation_id"], "operation_id") if data.get("operation_id") else None
    try:
        with transaction.atomic():
            order = Order.objects.create(
                business=ctx.business, branch=ctx.branch, customer=customer, guest_name=guest_name,
                guest_phone_normalized=normalize_phone(guest_phone) if guest_phone else "", channel=channel,
                requested_date=data.get("requested_date") or business_today(), delivery_window=window,
                service_area=area, address_snapshot=data.get("address", ""), placed_by=placed_by,
                notes=data.get("notes", "")[:1000], operation_id=op,
            )
            OrderLine.objects.bulk_create([OrderLine(business=ctx.business, order=order, product=p, quantity=q)
                                           for p, q in lines])
    except IntegrityError:
        existing = Order.objects.filter(business=ctx.business, operation_id=op).first()
        if existing is None:
            raise
        return existing
    return order


def create_public_order_request(ctx, data) -> Order:
    """Guest request: creates no invoice, payment or stock posting. Stays unlinked."""
    phone = data.get("phone", "")
    if not normalize_phone(phone) or not data.get("name"):
        raise ValidationFailed(fields={"phone": ["Required."], "name": ["Required."]})
    return _create_order(ctx, customer=None, guest_name=data["name"][:200], guest_phone=phone,
                         channel=Order.Channel.WEB, data=data)


def create_customer_order(ctx, data) -> Order:
    customer = Customer.objects.filter(business=ctx.business, id=parse_uuid(data.get("customer_id"), "customer_id"),
                                       is_active=True).first()
    if customer is None:
        raise NotFound("Customer not found.")
    if ctx.is_customer:
        if not CustomerAccountLink.objects.filter(user=ctx.user, customer=customer, revoked_at__isnull=True).exists():
            raise NotFound("Customer not found.")
        channel = Order.Channel.APP if ctx.device else Order.Channel.WEB
    else:
        ctx.require("order.manage")
        channel = data.get("channel", Order.Channel.PHONE)
    return _create_order(ctx, customer=customer, channel=channel, data=data, placed_by=ctx.user)


def accept_order(ctx, order_id, data) -> Order:
    ctx.require("order.manage")
    with transaction.atomic():
        order = lock_order(ctx, order_id, data.get("expected_version"))
        if order.customer_id is None:
            # Convert guest to identified customer before accepting (staff decision, not phone auto-link).
            if not data.get("customer_id"):
                raise ValidationFailed("Identify the customer before accepting a guest request.",
                                       fields={"customer_id": ["Required for guest orders."]})
            customer = Customer.objects.filter(business=ctx.business, id=parse_uuid(data["customer_id"])).first()
            if customer is None:
                raise NotFound("Customer not found.")
            order.customer = customer
        if order.delivery_window_id:
            taken = Order.objects.filter(business=ctx.business, delivery_window_id=order.delivery_window_id,
                                         requested_date=order.requested_date,
                                         status__in=[OrderStatus.ACCEPTED, OrderStatus.READY,
                                                     OrderStatus.PARTIALLY_FULFILLED]).aggregate(n=Count("id"))["n"]
            if taken >= order.delivery_window.capacity_orders:
                raise BusinessConflict("The delivery window is full.", code="WINDOW_FULL")
        for line in order.lines.select_related("product"):
            quote = current_price(line.product, order.customer)
            line.quote_unit_price, line.quote_price_version = quote.unit_price, quote.version
            line.save(update_fields=["quote_unit_price", "quote_price_version"])
        _transition(order, OrderStatus.ACCEPTED)
        order.save(update_fields=["status", "version", "customer", "updated_at"])
        audit(ctx, "order.accepted", "order", order.id)
        emit(ctx, "order.accepted", "order", order.id, {"order_id": order.id, "customer_id": order.customer_id},
             aggregate_version=order.version)
    return order


def reject_order(ctx, order_id, data) -> Order:
    ctx.require("order.manage")
    with transaction.atomic():
        order = lock_order(ctx, order_id, data.get("expected_version"))
        _transition(order, OrderStatus.REJECTED)
        order.rejection_reason = (data.get("reason") or "")[:200]
        order.save(update_fields=["status", "version", "rejection_reason", "updated_at"])
        audit(ctx, "order.rejected", "order", order.id, reason=order.rejection_reason)
    return order


def reserve_order(ctx, order_id, data) -> Order:
    """Ready = physical eligible stock reserved. Held/unreleased batches are never reserved."""
    ctx.require("order.manage")
    with transaction.atomic():
        order = lock_order(ctx, order_id, data.get("expected_version"))
        if order.status != OrderStatus.ACCEPTED:
            raise BusinessConflict("Only accepted orders can be reserved.", code="INVALID_ORDER_STATE")
        plant = plant_location(ctx.business)
        lines = list(order.lines.select_related("product", "product__container_type",
                                                "product__container_type__stock_item", "product__stock_item"))
        specs = [stock_requirement(ctx, line.product, line.open_quantity) for line in lines]
        candidates = set()
        for item, condition, *_ in specs:
            candidates.update(candidate_buckets(ctx.business, item=item, locations=[plant.id], condition=condition))
        locked = lock_buckets(candidates)
        used = {}
        for line, (item, condition, stock_qty, *_rest) in zip(lines, specs, strict=True):
            ids = [i for i in candidates if locked.by_id.get(i) and locked.by_id[i].item_id == item.id
                   and locked.by_id[i].condition == condition]
            allocations = allocate_from(locked, ids, stock_qty, location_priority=[plant.id],
                                        label=line.product.name, used=used)
            for alloc in allocations:
                used[alloc.bucket.id] -= alloc.quantity  # reserve() increments bucket.reserved itself
            reserve(ctx, allocations=allocations, order_line=line)
        _transition(order, OrderStatus.READY)
        order.save(update_fields=["status", "version", "updated_at"])
        audit(ctx, "order.reserved", "order", order.id)
    return order


def cancel_order(ctx, order_id, data) -> Order:
    if ctx.is_customer:
        with transaction.atomic():
            order = lock_order(ctx, order_id)
            if order.customer is None or not CustomerAccountLink.objects.filter(
                    user=ctx.user, customer=order.customer, revoked_at__isnull=True).exists():
                raise NotFound()
            if order.status not in {OrderStatus.REQUESTED, OrderStatus.ACCEPTED}:
                raise Forbidden("Please contact the plant to cancel this order.")
            return _cancel_locked(ctx, order, data)
    ctx.require("order.manage")
    with transaction.atomic():
        order = lock_order(ctx, order_id, data.get("expected_version"))
        return _cancel_locked(ctx, order, data)


def _cancel_locked(ctx, order, data) -> Order:
    reservations = list(Reservation.objects.filter(order_line__order=order, status=Reservation.Status.ACTIVE)
                        .select_related("bucket__location"))
    if any(r.bucket.location.kind == StockLocation.Kind.RIDER for r in reservations):
        raise BusinessConflict("Goods are out for delivery; settle the run to return them first.",
                               code="GOODS_DISPATCHED")
    locked = lock_buckets([r.bucket_id for r in reservations])
    for res in reservations:
        release_reservation(res, locked.get(res.bucket))
    if order.status == OrderStatus.PARTIALLY_FULFILLED:
        for line in order.lines.all():
            line.cancelled_quantity = line.quantity - line.fulfilled_quantity
            line.save(update_fields=["cancelled_quantity"])
        _transition(order, OrderStatus.CLOSED_PARTIAL)
    else:
        _transition(order, OrderStatus.CANCELLED)
    order.save(update_fields=["status", "version", "updated_at"])
    audit(ctx, "order.cancelled", "order", order.id, reason=data.get("reason", ""))
    return order
