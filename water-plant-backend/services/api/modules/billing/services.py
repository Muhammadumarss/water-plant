from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from common.errors import BusinessConflict
from common.money import ZERO, money, quantity
from common.timeutil import business_today

from .models import CreditNote, CreditNoteLine, DocumentSequence, Invoice, InvoiceBalance, InvoiceLine


@dataclass
class InvoiceLineInput:
    product: object
    quantity: Decimal
    unit_price: Decimal
    price_rule_version: int | None = None
    handover_line: object = None
    discount: Decimal = ZERO

    @property
    def tax_rate(self):
        return self.product.tax_rate or Decimal(0)


def lock_sequence(business, doc_type: str, prefix: str) -> DocumentSequence:
    seq, _ = DocumentSequence.objects.get_or_create(business=business, doc_type=doc_type,
                                                    defaults={"prefix": prefix})
    return DocumentSequence.objects.select_for_update().get(id=seq.id)


def next_number(seq: DocumentSequence) -> str:
    number = seq.next_number
    seq.next_number = number + 1
    seq.save(update_fields=["next_number"])
    return f"{seq.prefix}-{number:07d}"


def price_lines(lines: list[InvoiceLineInput]):
    """Compute line totals; tax is added on top of the discounted amount."""
    out = []
    for line in lines:
        gross = money(quantity(line.quantity) * line.unit_price)
        net = money(gross - money(line.discount))
        if net < 0:
            raise BusinessConflict("Discount exceeds line amount.", code="INVALID_DISCOUNT")
        tax = money(net * line.tax_rate / Decimal(100))
        out.append((line, gross, tax, money(net + tax)))
    return out


def issue_invoice(ctx, *, seq: DocumentSequence, customer, lines: list[InvoiceLineInput], handover=None,
                  address_snapshot="", language="en", is_opening_balance=False) -> Invoice:
    """Caller holds the locked sequence row and the customer credit row."""
    priced = price_lines(lines)
    subtotal = money(sum((g for _, g, _, _ in priced), ZERO))
    discount_total = money(sum((money(line.discount) for line, *_ in priced), ZERO))
    tax_total = money(sum((t for _, _, t, _ in priced), ZERO))
    total = money(sum((lt for *_, lt in priced), ZERO))
    invoice = Invoice.objects.create(
        business=ctx.business, branch=ctx.branch, customer=customer, document_number=next_number(seq),
        issued_at=timezone.now(), currency=ctx.business.currency, customer_name_snapshot=customer.display_name,
        address_snapshot=address_snapshot, subtotal=subtotal, discount_total=discount_total, tax_total=tax_total,
        total=total, handover=handover, issued_by=ctx.user, language=language,
        is_opening_balance=is_opening_balance,
    )
    InvoiceLine.objects.bulk_create([
        InvoiceLine(
            business=ctx.business, invoice=invoice, product=line.product, handover_line=line.handover_line,
            product_name_snapshot=line.product.name, unit_snapshot=line.product.unit,
            quantity=quantity(line.quantity), unit_price=money(line.unit_price), discount=money(line.discount),
            tax_rate=line.tax_rate, tax_amount=tax, line_total=line_total,
            price_rule_version=line.price_rule_version,
        )
        for line, _gross, tax, line_total in priced
    ])
    due = business_today() + timedelta(days=customer.credit_terms_days or 0)
    balance = InvoiceBalance.objects.create(business=ctx.business, invoice=invoice, customer=customer,
                                            net_due=total, due_date=due)
    balance.recompute_status()
    balance.save(update_fields=["status"])
    return invoice


def lock_balances(invoice_ids) -> dict:
    rows = InvoiceBalance.objects.select_for_update().filter(invoice_id__in=list(invoice_ids)).order_by("id")
    return {row.invoice_id: row for row in rows}


def apply_allocation(balance: InvoiceBalance, amount: Decimal) -> None:
    amount = money(amount)
    if amount <= 0 or amount > balance.outstanding:
        raise BusinessConflict("Allocation exceeds the invoice's outstanding amount.", code="OVER_ALLOCATION")
    balance.allocated = money(balance.allocated + amount)
    balance.version += 1
    balance.recompute_status()
    balance.save(update_fields=["allocated", "status", "version", "updated_at"])


def release_allocation(balance: InvoiceBalance, amount: Decimal) -> None:
    amount = money(amount)
    if amount <= 0 or amount > balance.allocated:
        raise BusinessConflict("Cannot release more than was allocated.", code="OVER_RELEASE")
    balance.allocated = money(balance.allocated - amount)
    balance.version += 1
    balance.recompute_status()
    balance.save(update_fields=["allocated", "status", "version", "updated_at"])


def create_credit_note(ctx, *, seq, invoice: Invoice, balance: InvoiceBalance, amount: Decimal, reason: str,
                       operation_id, line_amounts: list[tuple] | None = None) -> CreditNote:
    """Reduce net_due. Caller must first release allocations above the new net due."""
    amount = money(amount)
    if amount <= 0 or amount > balance.net_due:
        raise BusinessConflict("Credit exceeds the invoice's remaining net amount.", code="CREDIT_EXCEEDS_INVOICE")
    if balance.allocated > balance.net_due - amount:
        raise BusinessConflict("Release payment allocations before crediting.", code="ALLOCATIONS_EXCEED_NET")
    note = CreditNote.objects.create(business=ctx.business, invoice=invoice, document_number=next_number(seq),
                                     reason=reason, amount=amount, approved_by=ctx.user, operation_id=operation_id)
    if not line_amounts:
        # Spread across lines proportionally to remaining creditable value, last line takes rounding.
        lines = list(invoice.lines.all().order_by("id"))
        remaining = amount
        line_amounts = []
        for index, line in enumerate(lines):
            already = line.credit_lines.aggregate(s=Sum("amount"))["s"] or ZERO
            room = line.line_total - already
            share = room if index == len(lines) - 1 else min(room, money(amount * line.line_total / invoice.total))
            share = min(share, remaining)
            if share > 0:
                line_amounts.append((line, ZERO, share))
                remaining -= share
        if remaining > 0:
            raise BusinessConflict("Credit exceeds creditable line amounts.", code="CREDIT_EXCEEDS_INVOICE")
    for line, qty, line_amount in line_amounts:
        CreditNoteLine.objects.create(business=ctx.business, credit_note=note, invoice_line=line,
                                      quantity=quantity(qty or 0), amount=money(line_amount))
    balance.net_due = money(balance.net_due - amount)
    balance.version += 1
    balance.recompute_status()
    balance.save(update_fields=["net_due", "status", "version", "updated_at"])
    return note


def customer_dues(customer) -> Decimal:
    agg = InvoiceBalance.objects.filter(customer=customer).aggregate(due=Sum("net_due"), alloc=Sum("allocated"))
    return money((agg["due"] or ZERO) - (agg["alloc"] or ZERO))
