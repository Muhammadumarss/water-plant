"""Money commands: receive payment, credit note, advance refund, deposit refund, cash shifts, expenses."""

from django.utils import timezone

from application.queries.account_summary import account_summary
from common.errors import BusinessConflict, InsufficientFunds, NotFound, ValidationFailed
from common.ids import parse_uuid
from common.money import ZERO, money, money_payload, money_str
from modules.audit.services import record as audit
from modules.billing.models import Invoice, InvoiceBalance
from modules.billing.services import create_credit_note, lock_balances, lock_sequence, release_allocation
from modules.customers.models import Customer
from modules.customers.services import lock_credit_account
from modules.finance.models import (
    CashAccount,
    CashShift,
    DepositEntry,
    Expense,
    OpeningEntry,
    ReceiptAllocation,
    ReceiptBalance,
    Refund,
)
from modules.finance.services import (
    _cash_move,
    allocate_to_invoice,
    create_refund,
    effective_amount,
    get_open_shift,
    lock_cash_account,
    lock_deposit_account,
    lock_receipt_balances,
    record_receipt,
    reverse_allocation,
    shift_expected_cash,
)
from modules.idempotency.services import execute
from modules.jobs.services import emit


def _customer(ctx, customer_id) -> Customer:
    customer = Customer.objects.filter(business=ctx.business, id=parse_uuid(customer_id, "customer_id")).first()
    if customer is None:
        raise NotFound("Customer not found.")
    return customer


# ------------------------------------------------------------ receive payment

def receive_payment(ctx, data, *, idempotency_key=None):
    """Receive an incoming amount and allocate it to invoices (explicit or oldest first).
    Any remainder stays as customer advance credit."""
    ctx.require("payment.receive", "payment.collect_assigned")

    def handler(operation_id):
        customer = _customer(ctx, data["customer_id"])
        amount = money(data["amount"])
        if amount <= 0:
            raise ValidationFailed(fields={"amount": ["Must be greater than zero."]})
        shift = None
        if data.get("cash_shift_id"):
            shift = get_open_shift(ctx, parse_uuid(data["cash_shift_id"], "cash_shift_id"))
            cash_account_id = shift.cash_account_id
        else:
            cash_account_id = parse_uuid(data.get("cash_account_id"), "cash_account_id")
            if ctx.role == "RIDER":
                if not CashAccount.objects.filter(id=cash_account_id, rider=ctx.user).exists():
                    raise NotFound("Cash account not found.")

        lock_credit_account(customer)
        explicit = data.get("allocations") or []
        if explicit:
            invoice_ids = [parse_uuid(a["invoice_id"], f"allocations.{i}.invoice_id") for i, a in enumerate(explicit)]
            if Invoice.objects.filter(id__in=invoice_ids).exclude(customer=customer).exists():
                raise ValidationFailed(fields={"allocations": ["Invoices must belong to this customer."]})
            plan = [(iid, money(a["amount"])) for iid, a in zip(invoice_ids, explicit, strict=True)]
        else:
            open_balances = InvoiceBalance.objects.filter(customer=customer).exclude(status="PAID") \
                .order_by("invoice__issued_at").values_list("invoice_id", flat=True)
            plan = [(iid, None) for iid in open_balances]
        balances = lock_balances([iid for iid, _ in plan])
        seq = lock_sequence(ctx.business, "RECEIPT", "RCP")
        cash_account = lock_cash_account(ctx.business, cash_account_id)
        receipt, rb = record_receipt(ctx, seq=seq, customer=customer, amount=amount,
                                     method=data.get("method", "CASH").upper(), cash_account=cash_account,
                                     operation_id=operation_id, shift=shift,
                                     verified_reference=data.get("reference", ""))
        remaining = amount
        allocated = []
        for invoice_id, requested in plan:
            if remaining <= 0:
                break
            balance = balances.get(invoice_id)
            if balance is None:
                continue
            take = min(remaining, balance.outstanding) if requested is None else requested
            if take <= 0:
                continue
            if take > remaining:
                raise InsufficientFunds("Allocations exceed the received amount.", code="OVER_ALLOCATION")
            allocate_to_invoice(ctx, receipt_balance=rb, invoice=balance.invoice, invoice_balance=balance, amount=take)
            remaining -= take
            allocated.append({"invoice_id": str(invoice_id), "amount": money_str(take)})
        audit(ctx, "payment.received", "receipt", receipt.id, operation_id=operation_id,
              changes={"amount": amount, "allocations": allocated})
        emit(ctx, "payment.received", "receipt", receipt.id,
             {"receipt_id": receipt.id, "customer_id": customer.id, "amount": amount})
        return {"_resource": ("receipt", receipt.id), "receipt_id": str(receipt.id),
                "receipt_number": receipt.receipt_number, "received": money_payload(amount),
                "allocations": allocated, "unallocated_advance": money_payload(rb.unallocated),
                "account_summary": account_summary(customer)}

    return execute(ctx, command_name="receive_payment", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)


# ------------------------------------------------------------ credit note

def issue_credit_note(ctx, invoice_id, data, *, idempotency_key=None):
    """Owner-authorized adjustment. If the invoice is paid beyond its new net
    amount, the excess allocations are released atomically (becoming advance)."""
    ctx.require("credit.approve")

    def handler(operation_id):
        invoice = Invoice.objects.filter(business=ctx.business, id=parse_uuid(invoice_id)).first()
        if invoice is None:
            raise NotFound()
        amount = money(data["amount"])
        reason = (data.get("reason") or "").strip()
        if not reason:
            raise ValidationFailed(fields={"reason": ["A reason is required."]})
        lock_credit_account(invoice.customer)
        balance = lock_balances([invoice.id])[invoice.id]
        seq = lock_sequence(ctx.business, "CREDIT_NOTE", "CN")
        allocations = list(ReceiptAllocation.objects.filter(invoice=invoice, reversal_of__isnull=True)
                           .order_by("-created_at"))
        receipt_balances = lock_receipt_balances({a.receipt_id for a in allocations})
        excess = money(balance.allocated - (balance.net_due - amount))
        released = []
        for allocation in allocations:
            if excess <= 0:
                break
            eff = effective_amount(allocation)
            take = min(eff, excess)
            if take <= 0:
                continue
            reverse_allocation(ctx, allocation=allocation, receipt_balance=receipt_balances[allocation.receipt_id],
                               amount=take, reason=f"Credit note on {invoice.document_number}")
            release_allocation(balance, take)
            released.append({"receipt_id": str(allocation.receipt_id), "amount": money_str(take)})
            excess -= take
        note = create_credit_note(ctx, seq=seq, invoice=invoice, balance=balance, amount=amount, reason=reason,
                                  operation_id=operation_id)
        audit(ctx, "credit_note.issued", "invoice", invoice.id, operation_id=operation_id, reason=reason,
              changes={"amount": amount, "released_allocations": released})
        return {"_resource": ("credit_note", note.id), "credit_note_id": str(note.id),
                "document_number": note.document_number, "amount": money_payload(amount),
                "released_allocations": released, "invoice_net_due": money_payload(balance.net_due),
                "invoice_status": balance.status, "account_summary": account_summary(invoice.customer)}

    return execute(ctx, command_name="issue_credit_note", operation_id=data.get("operation_id"),
                   payload={"invoice_id": str(invoice_id), **data}, handler=handler, header_key=idempotency_key)


# ------------------------------------------------------------ refunds

def refund_advance(ctx, data, *, idempotency_key=None):
    """Refund unallocated customer credit, funded from eligible receipts/opening credit."""
    ctx.require("refund.approve")

    def handler(operation_id):
        customer = _customer(ctx, data["customer_id"])
        amount = money(data["amount"])
        reason = (data.get("reason") or "").strip()
        if amount <= 0 or not reason:
            raise ValidationFailed(fields={"amount": ["Must be > 0."], "reason": ["Required."]})
        lock_credit_account(customer)
        lock_deposit_account(customer, ctx.business.currency)
        candidates = ReceiptBalance.objects.filter(customer=customer).order_by("created_at") \
            .values_list("receipt_id", flat=True)
        balances = lock_receipt_balances(list(candidates))
        openings = list(OpeningEntry.objects.select_for_update().filter(customer=customer, balance_type="ADVANCE")
                        .order_by("id"))
        cash_account = lock_cash_account(ctx.business, parse_uuid(data["cash_account_id"], "cash_account_id"))
        sources, remaining = [], amount
        for rb in sorted(balances.values(), key=lambda b: b.created_at):
            if remaining <= 0:
                break
            take = min(rb.unallocated, remaining)
            if take > 0:
                sources.append((rb, take))
                remaining -= take
        for entry in openings:
            if remaining <= 0:
                break
            take = min(entry.amount - entry.consumed_amount, remaining)
            if take > 0:
                sources.append((entry, take))
                remaining -= take
        if remaining > 0:
            raise InsufficientFunds(details={"available": money_str(amount - remaining)})
        refund = create_refund(ctx, customer=customer, kind=Refund.Kind.ADVANCE, amount=amount,
                               method=data.get("method", "CASH").upper(), cash_account=cash_account,
                               reason=reason, operation_id=operation_id, sources=sources)
        audit(ctx, "refund.advance", "refund", refund.id, operation_id=operation_id, reason=reason,
              changes={"amount": amount})
        return {"_resource": ("refund", refund.id), "refund_id": str(refund.id), "amount": money_payload(amount),
                "account_summary": account_summary(customer)}

    return execute(ctx, command_name="refund_advance", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)


def refund_deposit(ctx, data, *, idempotency_key=None):
    """Release deposit funding (allocations or opening funding) and refund it,
    in one transaction. Holding bottles requires an explicit acknowledgement."""
    ctx.require("refund.approve")

    def handler(operation_id):
        from modules.inventory.services import customer_bottle_holdings

        customer = _customer(ctx, data["customer_id"])
        amount = money(data["amount"])
        reason = (data.get("reason") or "").strip()
        if amount <= 0 or not reason:
            raise ValidationFailed(fields={"amount": ["Must be > 0."], "reason": ["Required."]})
        held = sum(customer_bottle_holdings(customer).values(), ZERO)
        if held > 0 and not data.get("acknowledge_bottles_still_held"):
            raise BusinessConflict(f"Customer still holds {held.normalize()} bottles.", code="BOTTLES_STILL_HELD")
        lock_credit_account(customer)
        deposit_account = lock_deposit_account(customer, ctx.business.currency)
        if amount > deposit_account.balance:
            raise InsufficientFunds(details={"deposit_balance": money_str(deposit_account.balance)})
        allocations = list(ReceiptAllocation.objects.filter(deposit_account=deposit_account,
                                                            reversal_of__isnull=True).order_by("-created_at"))
        receipt_balances = lock_receipt_balances({a.receipt_id for a in allocations})
        openings = list(OpeningEntry.objects.select_for_update().filter(customer=customer, balance_type="DEPOSIT")
                        .order_by("id"))
        cash_account = lock_cash_account(ctx.business, parse_uuid(data["cash_account_id"], "cash_account_id"))

        sources, remaining = [], amount
        for allocation in allocations:
            if remaining <= 0:
                break
            take = min(effective_amount(allocation), remaining)
            if take <= 0:
                continue
            rb = receipt_balances[allocation.receipt_id]
            reverse_allocation(ctx, allocation=allocation, receipt_balance=rb, amount=take, reason="Deposit refund")
            sources.append((rb, take))
            remaining -= take
        for entry in openings:
            if remaining <= 0:
                break
            take = min(entry.amount - entry.consumed_amount, remaining)
            if take > 0:
                sources.append((entry, take))
                remaining -= take
        if remaining > 0:
            raise InsufficientFunds("Deposit funding trail does not cover this refund.")
        refund = create_refund(ctx, customer=customer, kind=Refund.Kind.DEPOSIT, amount=amount,
                               method=data.get("method", "CASH").upper(), cash_account=cash_account,
                               reason=reason, operation_id=operation_id, sources=sources)
        DepositEntry.objects.create(business=ctx.business, deposit_account=deposit_account, customer=customer,
                                    amount=-amount, refund=refund, reason=f"Deposit refund: {reason}"[:200])
        deposit_account.balance = money(deposit_account.balance - amount)
        deposit_account.version += 1
        deposit_account.save(update_fields=["balance", "version", "updated_at"])
        audit(ctx, "refund.deposit", "refund", refund.id, operation_id=operation_id, reason=reason,
              changes={"amount": amount, "bottles_held": held})
        return {"_resource": ("refund", refund.id), "refund_id": str(refund.id), "amount": money_payload(amount),
                "account_summary": account_summary(customer)}

    return execute(ctx, command_name="refund_deposit", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)


# ------------------------------------------------------------ cash shifts & expenses

def open_shift(ctx, data):
    ctx.require("shift.operate")
    account = CashAccount.objects.filter(business=ctx.business, id=parse_uuid(data["cash_account_id"]),
                                         kind=CashAccount.Kind.TILL, is_active=True).first()
    if account is None:
        raise NotFound("Till not found.")
    if CashShift.objects.filter(cash_account=account, status=CashShift.Status.OPEN).exists():
        raise BusinessConflict("This till already has an open shift.", code="SHIFT_ALREADY_OPEN")
    shift = CashShift.objects.create(business=ctx.business, cash_account=account, cashier=ctx.user,
                                     opened_at=timezone.now(), opening_float=money(data.get("opening_float", 0)))
    audit(ctx, "shift.opened", "cash_shift", shift.id, changes={"opening_float": shift.opening_float})
    return shift


def close_shift(ctx, shift_id, data):
    ctx.require("shift.operate")
    from django.db import transaction

    with transaction.atomic():
        shift = CashShift.objects.select_for_update().filter(business=ctx.business, id=parse_uuid(shift_id)).first()
        if shift is None:
            raise NotFound()
        if shift.status != CashShift.Status.OPEN:
            raise BusinessConflict("Shift is not open.", code="SHIFT_NOT_OPEN")
        if shift.cashier_id != ctx.user_id and not ctx.is_owner:
            raise BusinessConflict("This shift belongs to another cashier.", code="SHIFT_NOT_YOURS")
        expected = shift_expected_cash(shift)
        counted = money(data["counted_close"])
        shift.expected_close, shift.counted_close = expected, counted
        shift.variance = money(counted - expected)
        shift.closed_at = timezone.now()
        shift.status = CashShift.Status.CLOSED
        shift.notes = data.get("notes", "")
        shift.save()
        audit(ctx, "shift.closed", "cash_shift", shift.id,
              changes={"expected": expected, "counted": counted, "variance": shift.variance})
    return shift


def approve_shift(ctx, shift_id):
    ctx.require("shift.approve")
    shift = CashShift.objects.filter(business=ctx.business, id=parse_uuid(shift_id),
                                     status=CashShift.Status.CLOSED).first()
    if shift is None:
        raise NotFound()
    shift.status, shift.approved_by, shift.approved_at = CashShift.Status.APPROVED, ctx.user, timezone.now()
    shift.save(update_fields=["status", "approved_by", "approved_at"])
    audit(ctx, "shift.approved", "cash_shift", shift.id, changes={"variance": shift.variance})
    return shift


def record_expense(ctx, data, *, idempotency_key=None):
    ctx.require("refund.approve")

    def handler(operation_id):
        cash_account = lock_cash_account(ctx.business, parse_uuid(data["cash_account_id"], "cash_account_id"))
        expense = Expense.objects.create(business=ctx.business, category=data["category"], payee=data["payee"],
                                         amount=money(data["amount"]), paid_by=ctx.user, paid_at=timezone.now(),
                                         cash_account=cash_account, notes=data.get("notes", ""),
                                         operation_id=operation_id)
        _cash_move(ctx, cash_account, "OUT", "EXPENSE", expense.amount, expense=expense)
        audit(ctx, "expense.recorded", "expense", expense.id, operation_id=operation_id,
              changes={"amount": expense.amount, "category": expense.category})
        return {"_resource": ("expense", expense.id), "expense_id": str(expense.id),
                "amount": money_payload(expense.amount)}

    return execute(ctx, command_name="record_expense", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)
