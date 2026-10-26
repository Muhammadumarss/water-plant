"""Finance module services. Callers lock rows in the documented order:
deposit accounts, then receipts/opening funding, then cash accounts."""

from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone

from common.errors import BusinessConflict, InsufficientFunds, NotFound, ValidationFailed
from common.money import ZERO, money

from .models import (
    CashAccount,
    CashMovement,
    CashShift,
    DepositAccount,
    DepositEntry,
    OpeningEntry,
    PaymentMethod,
    PaymentReceipt,
    ReceiptAllocation,
    ReceiptBalance,
    Refund,
    RefundSource,
)

# ----------------------------------------------------------------- locks

def lock_deposit_account(customer, currency="PKR") -> DepositAccount:
    account, _ = DepositAccount.objects.get_or_create(business_id=customer.business_id, customer=customer,
                                                      currency=currency)
    return DepositAccount.objects.select_for_update().get(id=account.id)


def lock_cash_account(business, cash_account_id) -> CashAccount:
    account = CashAccount.objects.select_for_update().filter(business=business, id=cash_account_id,
                                                             is_active=True).first()
    if account is None:
        raise NotFound("Cash account not found.")
    return account


def lock_receipt_balances(receipt_ids) -> dict:
    rows = ReceiptBalance.objects.select_for_update().filter(receipt_id__in=list(receipt_ids)).order_by("id")
    return {row.receipt_id: row for row in rows}


def get_open_shift(ctx, shift_id) -> CashShift:
    shift = CashShift.objects.select_related("cash_account", "cash_account__stock_location").filter(
        business=ctx.business, id=shift_id).first()
    if shift is None:
        raise NotFound("Cash shift not found.")
    if shift.status != CashShift.Status.OPEN:
        raise BusinessConflict("The cash shift is not open.", code="SHIFT_NOT_OPEN")
    if shift.cashier_id != ctx.user_id and not ctx.is_owner:
        raise BusinessConflict("This shift belongs to another cashier.", code="SHIFT_NOT_YOURS")
    return shift


# ----------------------------------------------------------------- cash

def _cash_move(ctx, account: CashAccount, direction: str, kind: str, amount: Decimal, **refs) -> CashMovement:
    # Physical cash (including an unrecorded opening float) is reconciled at shift close,
    # so the recorded balance is not used to block an approved payout.
    amount = money(amount)
    account.balance = money(account.balance + amount if direction == "IN" else account.balance - amount)
    account.save(update_fields=["balance", "updated_at"])
    return CashMovement.objects.create(business=ctx.business, cash_account=account, direction=direction, kind=kind,
                                       amount=amount, actor=ctx.user, **refs)


def transfer_cash(ctx, *, source: CashAccount, target: CashAccount, amount, transfer_group, reason="") -> None:
    """Caller holds both cash account locks (sorted by id)."""
    _cash_move(ctx, source, "OUT", "TRANSFER", amount, transfer_group=transfer_group, reason=reason)
    _cash_move(ctx, target, "IN", "TRANSFER", amount, transfer_group=transfer_group, reason=reason)


# ----------------------------------------------------------------- receipts

def record_receipt(ctx, *, seq, customer, amount, method, cash_account: CashAccount, operation_id,
                   shift=None, verified_reference="") -> tuple[PaymentReceipt, ReceiptBalance]:
    from modules.billing.services import next_number

    amount = money(amount)
    if amount <= 0:
        raise ValidationFailed(fields={"payment.apply_amount": ["Must be greater than zero."]})
    if method not in PaymentMethod.values:
        raise ValidationFailed(fields={"payment.method": ["Unsupported method."]})
    if method != PaymentMethod.CASH:
        # Digital transfers need verified settlement evidence; a screenshot alone does not confirm payment.
        ctx.require("payment.verify_digital")
        if not verified_reference:
            raise ValidationFailed(fields={"payment.reference": ["Verified settlement reference is required."]})
    receipt = PaymentReceipt.objects.create(
        business=ctx.business, branch=ctx.branch, customer=customer, amount=amount, currency=ctx.business.currency,
        method=method, received_at=timezone.now(), custodian=ctx.user, cash_account=cash_account, shift=shift,
        verification=PaymentReceipt.Verification.CASH if method == PaymentMethod.CASH
        else PaymentReceipt.Verification.VERIFIED,
        verified_reference=verified_reference, receipt_number=next_number(seq), operation_id=operation_id,
    )
    balance = ReceiptBalance.objects.create(business=ctx.business, receipt=receipt, customer=customer, amount=amount)
    _cash_move(ctx, cash_account, "IN", "RECEIPT", amount, receipt=receipt, shift=shift)
    return receipt, balance


def _use_receipt_funds(rb: ReceiptBalance, amount: Decimal) -> None:
    if amount > rb.unallocated:
        raise InsufficientFunds("Allocation exceeds the receipt's unallocated amount.", code="OVER_ALLOCATION")
    rb.allocated = money(rb.allocated + amount)
    rb.version += 1
    rb.save(update_fields=["allocated", "version", "updated_at"])


def allocate_to_invoice(ctx, *, receipt_balance: ReceiptBalance, invoice, invoice_balance, amount) -> ReceiptAllocation:
    from modules.billing.services import apply_allocation

    amount = money(amount)
    _use_receipt_funds(receipt_balance, amount)
    apply_allocation(invoice_balance, amount)
    return ReceiptAllocation.objects.create(business=ctx.business, receipt_id=receipt_balance.receipt_id,
                                            target_kind="INVOICE", invoice=invoice, amount=amount)


def allocate_to_deposit(ctx, *, receipt_balance: ReceiptBalance, deposit_account: DepositAccount, amount,
                        container_type=None, bottles=0, agreement_version=None,
                        reason="Bottle deposit") -> ReceiptAllocation:
    amount = money(amount)
    _use_receipt_funds(receipt_balance, amount)
    allocation = ReceiptAllocation.objects.create(business=ctx.business, receipt_id=receipt_balance.receipt_id,
                                                  target_kind="DEPOSIT", deposit_account=deposit_account,
                                                  amount=amount)
    DepositEntry.objects.create(
        business=ctx.business, deposit_account=deposit_account, customer_id=deposit_account.customer_id,
        amount=amount, funding_allocation=allocation, container_type=container_type, bottles=bottles,
        agreement_version=agreement_version, reason=reason,
    )
    deposit_account.balance = money(deposit_account.balance + amount)
    deposit_account.version += 1
    deposit_account.save(update_fields=["balance", "version", "updated_at"])
    return allocation


def effective_amount(allocation: ReceiptAllocation) -> Decimal:
    reversed_total = allocation.reversals.aggregate(s=Sum("amount"))["s"] or ZERO
    return money(allocation.amount - reversed_total)


def reverse_allocation(ctx, *, allocation: ReceiptAllocation, receipt_balance: ReceiptBalance, amount,
                       reason: str) -> ReceiptAllocation:
    """Release (part of) an allocation back to the receipt's unallocated funds.
    Caller holds the receipt balance lock and the target's lock."""
    amount = money(amount)
    if allocation.reversal_of_id is not None:
        raise ValidationFailed("Cannot reverse a reversal.")
    if amount <= 0 or amount > effective_amount(allocation):
        raise BusinessConflict("Cannot release more than the effective allocation.", code="OVER_RELEASE")
    receipt_balance.allocated = money(receipt_balance.allocated - amount)
    receipt_balance.version += 1
    receipt_balance.save(update_fields=["allocated", "version", "updated_at"])
    return ReceiptAllocation.objects.create(
        business=ctx.business, receipt_id=allocation.receipt_id, target_kind=allocation.target_kind,
        invoice_id=allocation.invoice_id, deposit_account_id=allocation.deposit_account_id, amount=amount,
        reversal_of=allocation, reason=reason,
    )


# ----------------------------------------------------------------- refunds

def create_refund(ctx, *, customer, kind, amount, method, cash_account: CashAccount, reason, operation_id,
                  sources: list[tuple]) -> Refund:
    """sources: [(ReceiptBalance | OpeningEntry, amount)], all already locked. Sources sum to the refund."""
    amount = money(amount)
    if money(sum((money(a) for _, a in sources), ZERO)) != amount:
        raise BusinessConflict("Refund sources must sum to the refund amount.", code="REFUND_SOURCE_MISMATCH")
    refund = Refund.objects.create(
        business=ctx.business, customer=customer, kind=kind, amount=amount, currency=ctx.business.currency,
        method=method, cash_account=cash_account, approved_by=ctx.user, reason=reason, paid_at=timezone.now(),
        operation_id=operation_id,
    )
    for source, part in sources:
        part = money(part)
        if isinstance(source, ReceiptBalance):
            if part > source.unallocated:
                raise InsufficientFunds("Refund exceeds the receipt's remaining eligible funds.")
            source.refunded = money(source.refunded + part)
            source.version += 1
            source.save(update_fields=["refunded", "version", "updated_at"])
            RefundSource.objects.create(business=ctx.business, refund=refund, receipt_id=source.receipt_id, amount=part)
        elif isinstance(source, OpeningEntry):
            if part > source.amount - source.consumed_amount:
                raise InsufficientFunds("Refund exceeds the opening funding balance.")
            source.consumed_amount = money(source.consumed_amount + part)
            source.save(update_fields=["consumed_amount"])
            RefundSource.objects.create(business=ctx.business, refund=refund, opening_entry=source, amount=part)
        else:
            raise ValidationFailed("Invalid refund source.")
    _cash_move(ctx, cash_account, "OUT", "REFUND", amount, refund=refund)
    return refund


# ----------------------------------------------------------------- reads

def customer_advance(customer) -> Decimal:
    receipts = ReceiptBalance.objects.filter(customer=customer).aggregate(
        s=Sum(F("amount") - F("allocated") - F("refunded")))["s"] or ZERO
    opening = OpeningEntry.objects.filter(customer=customer, balance_type="ADVANCE").aggregate(
        s=Sum(F("amount") - F("consumed_amount")))["s"] or ZERO
    return money(receipts + opening)


def deposit_balance(customer, currency="PKR") -> Decimal:
    account = DepositAccount.objects.filter(customer=customer, currency=currency).first()
    return money(account.balance) if account else ZERO


def shift_expected_cash(shift: CashShift) -> Decimal:
    agg = CashMovement.objects.filter(shift=shift).values("direction").annotate(s=Sum("amount"))
    total = money(shift.opening_float)
    for row in agg:
        total += row["s"] if row["direction"] == "IN" else -row["s"]
    return money(total)
