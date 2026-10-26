"""Shared posting core for counter sales, deliveries and offline sales.

One handover produces, in ONE transaction: handover record, invoice (actual
quantities only), stock movements (single ledger), receipt with allocations
to invoice and deposit, deposit ledger entry, cash movement, audit event and
outbox events.

Lock order (architecture section 11):
  customer credit row -> orders (caller) -> invoice/receipt numbering ->
  deposit account -> cash account -> stock buckets sorted by UUID.
Every row is acquired before anything is posted."""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from application.queries.account_summary import account_summary
from common.errors import (
    BusinessConflict,
    CreditLimitExceeded,
    DepositNotFunded,
    InsufficientStock,
    ValidationFailed,
)
from common.money import ZERO, money, money_payload, money_str, qty_str, quantity, require_whole
from modules.audit.services import record as audit
from modules.billing.services import (
    InvoiceLineInput,
    customer_dues,
    issue_invoice,
    lock_balances,
    lock_sequence,
    price_lines,
)
from modules.catalog.models import Product
from modules.catalog.services import current_deposit_policy
from modules.customers.services import lock_credit_account
from modules.finance.models import PaymentMethod
from modules.finance.services import (
    allocate_to_deposit,
    allocate_to_invoice,
    lock_cash_account,
    lock_deposit_account,
    record_receipt,
)
from modules.fulfillment.models import BottleReturn, Handover, HandoverLine
from modules.inventory.models import Condition, StockLocation, StockMovement
from modules.inventory.services import (
    BucketKey,
    MovementLine,
    allocate_from,
    bucket_for,
    candidate_buckets,
    customer_location,
    lock_buckets,
    post_movements,
    treated_water_item,
    virtual_location,
)
from modules.jobs.services import emit


@dataclass
class SaleLine:
    product: Product
    quantity: Decimal
    unit_price: Decimal
    price_version: int | None
    order_line: object = None
    reservations: list = field(default_factory=list)


@dataclass
class ReturnLine:
    container_type: object
    quantity: Decimal
    damaged: Decimal = ZERO


@dataclass
class PaymentInput:
    method: str
    apply_amount: Decimal
    tendered_amount: Decimal | None = None
    reference: str = ""


@dataclass
class HandoverRequest:
    customer: object
    channel: str
    lines: list[SaleLine]
    returns: list[ReturnLine]
    payment: PaymentInput | None
    cash_account_id: object | None
    source_locations: list[StockLocation]
    return_location: StockLocation
    shift: object = None
    order: object = None
    delivery_stop: object = None
    unsecured_loan: bool = False
    allow_credit: bool = True
    allow_held_batches: bool = False
    device_occurred_at: object = None
    deposit_rates: dict | None = None  # container_type_id -> (amount, version); offline snapshot
    flags: list = field(default_factory=list)


def stock_requirement(ctx, product: Product, qty: Decimal):
    """(item, source condition, stock quantity, target kind, movement type) for selling qty of product."""
    if product.sale_mode == Product.SaleMode.RETURNABLE_BOTTLE:
        require_whole(qty)
        return (product.container_type.stock_item, Condition.FULL, quantity(qty), "customer",
                StockMovement.Type.CUSTOMER_ISSUE)
    if product.sale_mode == Product.SaleMode.REFILL:
        litres = quantity(qty * product.water_litres_per_unit)
        if litres <= 0:
            raise ValidationFailed(f"{product.name} has no litres per unit configured.")
        return treated_water_item(ctx.business), Condition.STANDARD, litres, "sold", StockMovement.Type.SALE
    if product.stock_item.whole_units:
        require_whole(qty)
    return product.stock_item, Condition.STANDARD, quantity(qty), "sold", StockMovement.Type.SALE


def _stock_spec(ctx, line: SaleLine):
    return stock_requirement(ctx, line.product, line.quantity)


def post_handover(ctx, operation_id, req: HandoverRequest) -> dict:
    business = ctx.business
    customer = req.customer
    if not req.lines and not req.returns:
        raise ValidationFailed("Nothing to hand over.", fields={"lines": ["Add a product line or a bottle return."]})
    if req.payment and money(req.payment.apply_amount) < 0:
        raise ValidationFailed(fields={"payment.apply_amount": ["Must not be negative."]})
    paying = req.payment is not None and money(req.payment.apply_amount) > 0

    # 1. Customer credit-control row (serializes credit decisions).
    lock_credit_account(customer)
    # 3. Document numbering.
    invoice_seq = lock_sequence(business, "INVOICE", business.invoice_prefix) if req.lines else None
    receipt_seq = lock_sequence(business, "RECEIPT", "RCP") if paying else None
    # 4. Deposit account.
    deposit_account = lock_deposit_account(customer, business.currency)
    # 5. Cash custody.
    cash_account = None
    if paying:
        if req.cash_account_id is None:
            raise ValidationFailed(fields={"cash_shift_id": ["A cash custody account is required to take payment."]})
        cash_account = lock_cash_account(business, req.cash_account_id)

    # 6. Resolve every bucket, then lock them all at once in UUID order.
    cust_loc = customer_location(customer)
    sold_loc = virtual_location(business, StockLocation.Virtual.SOLD)
    loc_ids = [loc.id for loc in req.source_locations]
    specs = [_stock_spec(ctx, line) for line in req.lines]

    target_buckets, candidate_ids = {}, set()
    for (item, condition, _qty, target_kind, _mt), line in zip(specs, req.lines, strict=True):
        key = (BucketKey(item.id, cust_loc.id, Condition.CUSTOMER_HELD) if target_kind == "customer"
               else BucketKey(item.id, sold_loc.id, Condition.STANDARD))
        target_buckets[key] = bucket_for(business, key)
        if line.reservations:
            candidate_ids.update(r.bucket_id for r in line.reservations)
        else:
            candidate_ids.update(candidate_buckets(business, item=item, locations=loc_ids, condition=condition))

    return_buckets = {}
    for ret in req.returns:
        require_whole(ret.quantity, "bottle_returns.quantity")
        require_whole(ret.damaged or ZERO, "bottle_returns.damaged")
        if ret.quantity <= 0 or ret.damaged < 0 or ret.damaged > ret.quantity:
            raise ValidationFailed(fields={"bottle_returns": ["Invalid return quantities."]})
        item_id = ret.container_type.stock_item_id
        for key in (BucketKey(item_id, cust_loc.id, Condition.CUSTOMER_HELD),
                    BucketKey(item_id, req.return_location.id, Condition.DIRTY_RETURNED),
                    BucketKey(item_id, req.return_location.id, Condition.DAMAGED)):
            return_buckets[key] = bucket_for(business, key)

    locked = lock_buckets(list(candidate_ids) + [b.id for b in target_buckets.values()]
                          + [b.id for b in return_buckets.values()])

    # 7. Recheck eligible stock and build movements (issues first, then returns).
    movements: list[MovementLine] = []
    used: dict = {}
    for (item, _condition, stock_qty, target_kind, mtype), line in zip(specs, req.lines, strict=True):
        key = (BucketKey(item.id, cust_loc.id, Condition.CUSTOMER_HELD) if target_kind == "customer"
               else BucketKey(item.id, sold_loc.id, Condition.STANDARD))
        target = locked.get(target_buckets[key])
        if line.reservations:
            remaining = stock_qty
            for res in line.reservations:
                if remaining <= 0:
                    break
                take = min(res.quantity, remaining)
                movements.append(MovementLine(item, locked.get(res.bucket), target, take, mtype,
                                              consume_reservation=res))
                remaining -= take
            if remaining > 0:
                raise InsufficientStock(f"Delivered quantity exceeds the reserved stock for {line.product.name}.")
        else:
            for alloc in allocate_from(locked, [i for i in candidate_ids if locked.by_id.get(i)
                                                and locked.by_id[i].item_id == item.id
                                                and locked.by_id[i].condition == _condition],
                                       stock_qty, location_priority=loc_ids, allow_held=req.allow_held_batches,
                                       label=line.product.name, used=used):
                movements.append(MovementLine(item, alloc.bucket, target, alloc.quantity, mtype))

    out_by_container, ret_by_container = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    containers = {}
    for line in req.lines:
        if line.product.sale_mode == Product.SaleMode.RETURNABLE_BOTTLE:
            ct = line.product.container_type
            containers[ct.id] = ct
            out_by_container[ct.id] += quantity(line.quantity)
    for ret in req.returns:
        ct = ret.container_type
        containers[ct.id] = ct
        ret_by_container[ct.id] += quantity(ret.quantity)
        item_id = ct.stock_item_id
        held = locked.get(return_buckets[BucketKey(item_id, cust_loc.id, Condition.CUSTOMER_HELD)])
        good = quantity(ret.quantity) - quantity(ret.damaged or 0)
        if good > 0:
            movements.append(MovementLine(ct.stock_item, held, locked.get(return_buckets[
                BucketKey(item_id, req.return_location.id, Condition.DIRTY_RETURNED)]), good,
                StockMovement.Type.EMPTY_RETURN))
        if ret.damaged and ret.damaged > 0:
            movements.append(MovementLine(ct.stock_item, held, locked.get(return_buckets[
                BucketKey(item_id, req.return_location.id, Condition.DAMAGED)]), quantity(ret.damaged),
                StockMovement.Type.DAMAGE))

    holdings_after = {}
    for ct_id, ct in containers.items():
        held_bucket = locked.by_id.get(next((b.id for k, b in {**target_buckets, **return_buckets}.items()
                                             if k.location_id == cust_loc.id and k.item_id == ct.stock_item_id), None))
        before = held_bucket.on_hand if held_bucket else ZERO
        after = before + out_by_container[ct_id] - ret_by_container[ct_id]
        if after < 0:
            # Staff cannot make customer holdings negative; unexplained returns need a supervised adjustment.
            raise BusinessConflict(f"Customer holds only {qty_str(before)} {ct.name}; return exceeds custody.",
                                   code="RETURN_EXCEEDS_HOLDING")
        holdings_after[ct_id] = after

    # 8. Deposit requirement for the net extra loan, at the current agreed rate.
    deposit_parts, deposit_required = [], ZERO
    for ct_id, ct in containers.items():
        net_extra = out_by_container[ct_id] - ret_by_container[ct_id]
        if net_extra <= 0:
            continue
        if req.deposit_rates is not None:
            rate, version = req.deposit_rates.get(str(ct_id), (None, None))
        else:
            policy = current_deposit_policy(ct)
            rate, version = (policy.amount_per_unit, policy.version) if policy else (None, None)
        if rate is None:
            raise BusinessConflict(f"No deposit policy for {ct.name}.", code="DEPOSIT_POLICY_MISSING")
        amount = money(net_extra * money(rate))
        if amount > 0:
            deposit_parts.append((ct, int(net_extra), amount, version))
            deposit_required += amount
    if deposit_required > 0 and req.unsecured_loan:
        ctx.require("deposit.waive")
        req.flags.append("UNSECURED_BOTTLE_LOAN")
        deposit_parts, deposit_required = [], ZERO

    # 9. Charges and payment application (server decisions).
    invoice_inputs = [InvoiceLineInput(product=ln.product, quantity=quantity(ln.quantity),
                                       unit_price=money(ln.unit_price), price_rule_version=ln.price_version)
                      for ln in req.lines]
    invoice_total = money(sum((lt for *_, lt in price_lines(invoice_inputs)), ZERO)) if invoice_inputs else ZERO
    apply_amount = money(req.payment.apply_amount) if req.payment else ZERO
    change_due = ZERO
    if req.payment:
        if req.payment.method == PaymentMethod.CASH:
            tendered = money(req.payment.tendered_amount if req.payment.tendered_amount is not None else apply_amount)
            if tendered < apply_amount:
                raise ValidationFailed(fields={"payment.tendered_amount": ["Must cover the applied amount."]})
            change_due = money(tendered - apply_amount)
    if apply_amount < deposit_required:
        raise DepositNotFunded(details={"deposit_required": money_str(deposit_required),
                                        "apply_amount": money_str(apply_amount)})
    invoice_paid = min(apply_amount - deposit_required, invoice_total)
    unpaid = money(invoice_total - invoice_paid)
    if unpaid > 0:
        if not req.allow_credit:
            raise CreditLimitExceeded("This sale must be fully paid.", details={"unpaid": money_str(unpaid)})
        dues = customer_dues(customer)
        if dues + unpaid > customer.credit_limit:
            raise CreditLimitExceeded(details={"current_dues": money_str(dues), "unpaid": money_str(unpaid),
                                               "credit_limit": money_str(customer.credit_limit)})

    # 10. Post everything.
    now = timezone.now()
    handover_id = uuid.uuid4()
    group = post_movements(ctx, operation_id=operation_id, action="HANDOVER", lines=movements,
                           source_type="handover", source_id=handover_id, allow_held=req.allow_held_batches) \
        if movements else None
    receipt_reference = f"H-{handover_id.hex[:10].upper()}"
    handover = Handover.objects.create(
        id=handover_id, business=business, branch=ctx.branch, customer=customer, order=req.order,
        delivery_stop=req.delivery_stop, channel=req.channel, handed_over_by=ctx.user, device=ctx.device,
        occurred_at=now, device_occurred_at=req.device_occurred_at, operation_id=operation_id,
        stock_posting=group, receipt_reference=receipt_reference, flags=req.flags,
    )
    for inv_line, line in zip(invoice_inputs, req.lines, strict=True):
        inv_line.handover_line = HandoverLine.objects.create(business=business, handover=handover,
                                                             order_line=line.order_line, product=line.product,
                                                             quantity=quantity(line.quantity))
    for ret in req.returns:
        BottleReturn.objects.create(business=business, handover=handover, container_type=ret.container_type,
                                    quantity=quantity(ret.quantity), damaged_quantity=quantity(ret.damaged or 0))

    invoice = None
    if invoice_inputs:
        address = customer.addresses.filter(is_default=True, is_active=True).first()
        invoice = issue_invoice(ctx, seq=invoice_seq, customer=customer, lines=invoice_inputs, handover=handover,
                                address_snapshot=address.as_snapshot() if address else "")

    receipt = None
    advance = ZERO
    if paying:
        receipt, rb = record_receipt(ctx, seq=receipt_seq, customer=customer, amount=apply_amount,
                                     method=req.payment.method, cash_account=cash_account,
                                     operation_id=operation_id, shift=req.shift,
                                     verified_reference=req.payment.reference)
        for ct, bottles, amount, version in deposit_parts:
            allocate_to_deposit(ctx, receipt_balance=rb, deposit_account=deposit_account, amount=amount,
                                container_type=ct, bottles=bottles, agreement_version=version,
                                reason=f"Deposit for {bottles} x {ct.name}")
        if invoice is not None and invoice_paid > 0:
            balance = lock_balances([invoice.id])[invoice.id]
            allocate_to_invoice(ctx, receipt_balance=rb, invoice=invoice, invoice_balance=balance,
                                amount=invoice_paid)
        advance = rb.unallocated

    # 11. Audit + outbox, committed with the posting.
    audit(ctx, "handover.completed", "handover", handover.id, operation_id=operation_id, changes={
        "channel": req.channel, "invoice": invoice.document_number if invoice else None,
        "invoice_total": invoice_total, "received": apply_amount, "deposit": deposit_required,
        "flags": req.flags,
    })
    if invoice is not None:
        emit(ctx, "invoice.issued", "invoice", invoice.id, {"invoice_id": invoice.id,
                                                            "document_number": invoice.document_number})
    if receipt is not None:
        emit(ctx, "payment.received", "receipt", receipt.id, {"receipt_id": receipt.id, "customer_id": customer.id,
                                                              "amount": apply_amount})

    return {
        "_resource": ("handover", handover.id),
        "handover_id": str(handover.id),
        "receipt_reference": invoice.document_number if invoice else receipt_reference,
        "invoice": None if invoice is None else {
            "id": str(invoice.id), "document_number": invoice.document_number,
            "total": money_payload(invoice.total), "paid": money_payload(invoice_paid),
            "outstanding": money_payload(unpaid),
        },
        "payment": None if receipt is None else {
            "id": str(receipt.id), "receipt_number": receipt.receipt_number, "method": receipt.method,
            "received": money_payload(apply_amount), "change_due": money_payload(change_due),
            "allocated_to_invoice": money_payload(invoice_paid), "allocated_to_deposit": money_payload(deposit_required),
            "unallocated_advance": money_payload(advance),
        },
        "change_due": money_payload(change_due),
        "deposit_collected": money_payload(deposit_required),
        "stock_posting_id": str(group.id) if group else None,
        "flags": req.flags,
        "account_summary": account_summary(customer),
    }
