"""Desktop offline synchronization (architecture section 14).

V1 offline command: CASH_SALE_WITH_OPTIONAL_BOTTLE_EXCHANGE only. No offline
credit, refunds, privileged adjustments, price changes or account linking."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.errors import BusinessConflict, DomainError, Forbidden, NotFound, ValidationFailed
from common.ids import parse_uuid
from common.money import money, quantity
from common.serialization import payload_hash, to_jsonable
from modules.audit.services import record as audit
from modules.catalog.models import ContainerType, Product
from modules.catalog.services import current_deposit_policy, current_price
from modules.customers.models import Customer
from modules.finance.models import CashAccount
from modules.fulfillment.models import Handover
from modules.idempotency.services import execute
from modules.identity.models import Device
from modules.inventory.models import Condition, Reservation, StockBucket, StockMovement
from modules.inventory.services import (
    Allocation,
    BucketKey,
    MovementLine,
    allocate_from,
    bucket_for,
    candidate_buckets,
    lock_buckets,
    plant_location,
    post_movements,
    reserve,
)
from modules.synchronization.models import OfflineLease, SyncClock, SyncOperation
from modules.synchronization.services import changes_since, make_cursor, read_cursor, sign_snapshot

from .handover import HandoverRequest, PaymentInput, SaleLine, post_handover
from .master import customer_payload, product_payload
from .sales import build_returns

OFFLINE_SALE = "CASH_SALE_WITH_OPTIONAL_BOTTLE_EXCHANGE"
DATASETS = ["customers", "products", "prices", "deposit_policies"]


def _offline_device(ctx) -> Device:
    device = ctx.device
    if device is None or not device.offline_enabled or device.revoked_at is not None:
        raise Forbidden("This device is not enrolled for offline operation.")
    return device


# ------------------------------------------------------------ lease

def policy_snapshot(ctx) -> dict:
    prices, products = {}, []
    for product in Product.objects.filter(business=ctx.business, is_active=True):
        try:
            quote = current_price(product)
        except DomainError:
            continue
        prices[str(product.id)] = {"amount": str(quote.unit_price), "version": quote.version}
        products.append(product_payload(product))
    deposits = {}
    for ct in ContainerType.objects.filter(business=ctx.business, is_active=True):
        policy = current_deposit_policy(ct)
        if policy:
            deposits[str(ct.id)] = {"amount": str(policy.amount_per_unit), "version": policy.version}
    return {"prices": prices, "deposits": deposits, "products": products, "currency": ctx.business.currency}


def issue_lease(ctx, data, *, idempotency_key=None):
    """Allocate full bottles physically to the till and sign a policy snapshot."""
    ctx.require("device.manage")

    def handler(operation_id):
        device = Device.objects.select_related("assigned_till").filter(
            business=ctx.business, id=parse_uuid(data["device_id"]), revoked_at__isnull=True).first()
        if device is None or not device.offline_enabled or device.assigned_till is None:
            raise BusinessConflict("Device must be offline-enabled with an assigned till.", code="DEVICE_NOT_READY")
        if OfflineLease.objects.filter(device=device, revoked_at__isnull=True, reconciled_at__isnull=True,
                                       expires_at__gt=timezone.now()).exists():
            raise BusinessConflict("Device already has an active lease.", code="LEASE_ACTIVE")
        cash_account = CashAccount.objects.filter(business=ctx.business, stock_location=device.assigned_till,
                                                  kind=CashAccount.Kind.TILL).first()
        if cash_account is None:
            raise BusinessConflict("No till cash account for the device's till.", code="CONFIGURATION_MISSING")
        till = device.assigned_till
        hours = int(data.get("hours") or settings.OFFLINE_LEASE_DEFAULT_HOURS)
        snapshot = policy_snapshot(ctx)
        now = timezone.now()
        lease = OfflineLease.objects.create(
            business=ctx.business, device=device, actor_id=parse_uuid(data["actor_id"], "actor_id"),
            cash_account=cash_account, stock_location=till, allowed_operations=[OFFLINE_SALE],
            policy_snapshot=snapshot, signature="pending", issued_at=now, expires_at=now + timedelta(hours=hours),
            max_operations=int(data.get("max_operations", 500)), max_amount=money(data.get("max_amount", "100000")),
            last_device_sequence=SyncOperation.objects.filter(device=device).aggregate(
                m=Max("device_sequence"))["m"] or 0,
        )
        lease.signature = sign_snapshot({"lease": str(lease.id), "device": str(device.id),
                                         "expires_at": lease.expires_at.isoformat(), **snapshot})
        lease.save(update_fields=["signature"])

        plant = plant_location(ctx.business)
        allocations_out = []
        for index, raw in enumerate(data.get("stock_allocations", [])):
            ct = ContainerType.objects.select_related("stock_item").filter(
                business=ctx.business, id=parse_uuid(raw["container_type_id"])).first()
            if ct is None:
                raise NotFound("Container type not found.")
            qty = quantity(raw["quantity"])
            cand = candidate_buckets(ctx.business, item=ct.stock_item, locations=[plant.id], condition=Condition.FULL)
            # Resolve till targets for every candidate first, then lock everything once in UUID order.
            batch_ids = dict(StockBucket.objects.filter(id__in=cand).values_list("id", "batch_id"))
            targets = {bid: bucket_for(ctx.business, BucketKey(ct.stock_item_id, till.id, Condition.FULL, batch_id))
                       for bid, batch_id in batch_ids.items()}
            locked = lock_buckets(list(cand) + [t.id for t in targets.values()])
            picks = allocate_from(locked, cand, qty, label=ct.name)
            lines = [MovementLine(ct.stock_item, p.bucket, locked.get(targets[p.bucket.id]), p.quantity,
                                  StockMovement.Type.TRANSFER) for p in picks]
            post_movements(ctx, operation_id=operation_id, action=f"LEASE_ALLOCATION_{index}", lines=lines,
                           source_type="offline_lease", source_id=lease.id)
            for p in picks:
                reserve(ctx, allocations=[Allocation(bucket=locked.get(targets[p.bucket.id]), quantity=p.quantity)],
                        lease=lease)
            allocations_out.append({"container_type_id": str(ct.id), "quantity": str(qty)})
        audit(ctx, "offline_lease.issued", "offline_lease", lease.id, operation_id=operation_id,
              changes={"device": str(device.id), "allocations": allocations_out})
        return {"_resource": ("offline_lease", lease.id), "lease_id": str(lease.id),
                "expires_at": lease.expires_at.isoformat(), "signature": lease.signature,
                "policy_snapshot": snapshot, "allocations": allocations_out,
                "max_operations": lease.max_operations, "max_amount": str(lease.max_amount)}

    return execute(ctx, command_name="issue_lease", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)


# ------------------------------------------------------------ reference data

def bootstrap(ctx) -> dict:
    device = _offline_device(ctx)
    clock, _ = SyncClock.objects.get_or_create(business=ctx.business)
    revision = clock.revision  # read BEFORE the snapshot; later changes are re-sent (upserts are idempotent)
    customers = [customer_payload(c) for c in Customer.objects.filter(business=ctx.business, is_active=True)
                 .prefetch_related("contacts")]
    snapshot = policy_snapshot(ctx)
    return {"cursor": make_cursor(device, revision, 10**9, DATASETS), "schema_version": 1,
            "customers": customers, "products": snapshot["products"], "prices": snapshot["prices"],
            "deposit_policies": snapshot["deposits"]}


def changes(ctx, cursor: str) -> dict:
    device = _offline_device(ctx)
    revision, seq = read_cursor(device, cursor, DATASETS)
    rows = changes_since(ctx.business, revision, seq, DATASETS)
    if rows:
        cursor = make_cursor(device, rows[-1].revision, rows[-1].seq, DATASETS)
    return {"cursor": cursor, "has_more": len(rows) >= 500, "changes": [
        {"revision": r.revision, "dataset": r.dataset, "entity_id": str(r.entity_id), "op": r.op, "payload": r.payload}
        for r in rows]}


# ------------------------------------------------------------ operations

def _record(ctx, device, lease, op, outcome, result=None, error_code=""):
    SyncOperation.objects.create(
        business=ctx.business, device=device, lease=lease, operation_id=op["operation_id"],
        device_sequence=op["device_sequence"], command=op.get("command", ""), payload=to_jsonable(op),
        payload_hash=payload_hash(op), local_occurred_at=parse_datetime(op.get("local_occurred_at") or "") or None,
        outcome=outcome, result=to_jsonable(result or {}), error_code=error_code,
    )


def _apply_offline_sale(ctx, device, lease, op):
    sale = op.get("sale") or {}
    snapshot = lease.policy_snapshot

    def handler(operation_id):
        lease_locked = OfflineLease.objects.select_for_update().get(id=lease.id)
        customer = Customer.objects.filter(business=ctx.business, id=parse_uuid(sale.get("customer_id"))).first()
        if customer is None:
            raise NotFound("Customer not found.", code="CUSTOMER_UNKNOWN")
        lines = []
        for index, raw in enumerate(sale.get("lines", [])):
            product = Product.objects.select_related("container_type", "container_type__stock_item", "stock_item") \
                .filter(business=ctx.business, id=parse_uuid(raw["product_id"])).first()
            price = snapshot["prices"].get(str(raw["product_id"]))
            if product is None or price is None:
                raise ValidationFailed(fields={f"lines.{index}.product_id": ["Not in the lease snapshot."]})
            line = SaleLine(product=product, quantity=quantity(raw["quantity"]), unit_price=money(price["amount"]),
                            price_version=price["version"])
            if product.sale_mode == Product.SaleMode.RETURNABLE_BOTTLE:
                line.reservations = list(Reservation.objects.select_related("bucket").filter(
                    lease=lease, status=Reservation.Status.ACTIVE,
                    bucket__item=product.container_type.stock_item).order_by("id"))
            lines.append(line)
        pay = sale.get("payment") or {}
        if pay and pay.get("method", "CASH").upper() != "CASH":
            raise ValidationFailed("Offline sales are cash only.", code="OFFLINE_CASH_ONLY")
        payment = PaymentInput(method="CASH", apply_amount=money(pay.get("apply_amount", "0")),
                               tendered_amount=money(pay["tendered_amount"]) if pay.get("tendered_amount") else None)
        req = HandoverRequest(
            customer=customer, channel=Handover.Channel.OFFLINE_COUNTER, lines=lines,
            returns=build_returns(ctx, sale.get("bottle_returns")), payment=payment,
            cash_account_id=lease.cash_account_id, source_locations=[lease.stock_location],
            return_location=lease.stock_location, allow_credit=False,
            # Historical record of a real handover: a later batch hold must not drop it.
            allow_held_batches=True,
            device_occurred_at=parse_datetime(op.get("local_occurred_at") or "") or None,
            deposit_rates={k: (money(v["amount"]), v["version"]) for k, v in snapshot["deposits"].items()},
            flags=["OFFLINE"],
        )
        result = post_handover(ctx, operation_id, req)
        amount = money(payment.apply_amount)
        if lease_locked.used_operations + 1 > lease_locked.max_operations \
                or lease_locked.used_amount + amount > lease_locked.max_amount:
            raise BusinessConflict("Lease budget exceeded.", code="LEASE_BUDGET_EXCEEDED")
        lease_locked.used_operations += 1
        lease_locked.used_amount = money(lease_locked.used_amount + amount)
        lease_locked.last_device_sequence = int(op["device_sequence"])
        lease_locked.save(update_fields=["used_operations", "used_amount", "last_device_sequence"])
        _record(ctx, device, lease, op, SyncOperation.Outcome.ACCEPTED, result)
        return result

    return execute(ctx, command_name="offline_sale", operation_id=op["operation_id"], payload=op, handler=handler)


def submit_operations(ctx, data) -> dict:
    device = _offline_device(ctx)
    ops = data.get("operations") or []
    if len(ops) > settings.SYNC_MAX_BATCH:
        raise ValidationFailed(f"At most {settings.SYNC_MAX_BATCH} operations per batch.")
    results = []
    last_seq = SyncOperation.objects.filter(device=device).aggregate(m=Max("device_sequence"))["m"] or 0
    for op in sorted(ops, key=lambda o: int(o.get("device_sequence", 0))):
        op_id = str(parse_uuid(op.get("operation_id"), "operation_id"))
        op["operation_id"] = op_id
        seq = int(op.get("device_sequence", 0))
        existing = SyncOperation.objects.filter(device=device, operation_id=op_id).first()
        if existing is not None:
            if existing.payload_hash != payload_hash(op):
                results.append({"operation_id": op_id, "outcome": "INVALID", "error_code": "IDEMPOTENCY_CONFLICT"})
            else:
                results.append({"operation_id": op_id, "outcome": "ALREADY_APPLIED", "result": existing.result,
                                "original_outcome": existing.outcome})
            continue
        if seq != last_seq + 1:
            # Missing sequence gap: pause dependent records instead of inventing an order.
            results.append({"operation_id": op_id, "outcome": "DEFERRED", "error_code": "SEQUENCE_GAP",
                            "expected_sequence": last_seq + 1})
            break
        lease = OfflineLease.objects.filter(business=ctx.business, device=device,
                                            id=parse_uuid(op.get("lease_id"), "lease_id")).first()
        outcome, result, code = None, None, ""
        if op.get("command") != OFFLINE_SALE:
            outcome, code = SyncOperation.Outcome.INVALID, "UNSUPPORTED_OFFLINE_COMMAND"
        elif lease is None:
            outcome, code = SyncOperation.Outcome.REVIEW_REQUIRED, "LEASE_UNKNOWN"
        else:
            occurred = parse_datetime(op.get("local_occurred_at") or "")
            if lease.revoked_at is not None and (occurred is None or occurred >= lease.revoked_at):
                outcome, code = SyncOperation.Outcome.REVIEW_REQUIRED, "LEASE_REVOKED"
            elif occurred is None or occurred > lease.expires_at or occurred < lease.issued_at:
                outcome, code = SyncOperation.Outcome.REVIEW_REQUIRED, "LEASE_EXPIRED_OR_CLOCK_DRIFT"
        if outcome is None:
            try:
                result, _status = _apply_offline_sale(ctx, device, lease, op)
                outcome = SyncOperation.Outcome.ACCEPTED
            except DomainError as exc:
                outcome, code = SyncOperation.Outcome.REVIEW_REQUIRED, exc.code
        if outcome != SyncOperation.Outcome.ACCEPTED:
            # Never silently drop a real cash sale: persist original evidence for owner reconciliation.
            with transaction.atomic():
                _record(ctx, device, lease, op, outcome, error_code=code)
                audit(ctx, "sync.review_required" if outcome == "REVIEW_REQUIRED" else "sync.invalid",
                      "sync_operation", op_id, reason=code)
        last_seq = seq
        results.append({"operation_id": op_id, "outcome": outcome, "error_code": code or None, "result": result})
    return {"results": results, "received_at": timezone.now().isoformat()}


def operation_status(ctx, operation_id) -> dict:
    device = _offline_device(ctx)
    op = SyncOperation.objects.filter(device=device, operation_id=parse_uuid(operation_id)).first()
    if op is None:
        raise NotFound()
    return {"operation_id": str(op.operation_id), "outcome": op.outcome, "error_code": op.error_code or None,
            "result": op.result, "received_at": op.received_at.isoformat(),
            "resolved_at": op.resolved_at.isoformat() if op.resolved_at else None}
