from django.conf import settings
from django.db import models
from django.db.models import F, Q

from common.models import AppendOnlyModel, BusinessOwned
from common.money import MoneyField, QuantityField, currency_check, currency_field


class DocumentSequence(BusinessOwned):
    """Numbering row for financial documents. Gaps from rollbacks are acceptable."""

    doc_type = models.CharField(max_length=20)
    prefix = models.CharField(max_length=10)
    next_number = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "doc_type"], name="doc_sequence_unique")]


class Invoice(AppendOnlyModel):
    """Issued at actual handover. Immutable snapshots; correct with credit notes."""

    branch = models.ForeignKey("organization.Branch", null=True, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="invoices")
    document_number = models.CharField(max_length=30)
    issued_at = models.DateTimeField()
    currency = currency_field()
    customer_name_snapshot = models.CharField(max_length=200)
    address_snapshot = models.TextField(blank=True)
    subtotal = MoneyField()
    discount_total = MoneyField(default=0)
    tax_total = MoneyField(default=0)
    total = MoneyField()
    handover = models.OneToOneField("fulfillment.Handover", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="invoice")
    is_opening_balance = models.BooleanField(default=False)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    language = models.CharField(max_length=5, default="en")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "document_number"], name="invoice_unique_number"),
            models.CheckConstraint(condition=Q(total__gte=0), name="invoice_total_nonneg"),
            currency_check("invoice_valid_currency"),
        ]
        indexes = [models.Index(fields=["business", "customer", "created_at"])]


class InvoiceLine(AppendOnlyModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    handover_line = models.OneToOneField("fulfillment.HandoverLine", null=True, blank=True,
                                         on_delete=models.PROTECT, related_name="invoice_line")
    product_name_snapshot = models.CharField(max_length=120)
    unit_snapshot = models.CharField(max_length=20)
    quantity = QuantityField()
    unit_price = MoneyField()
    discount = MoneyField(default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_amount = MoneyField(default=0)
    line_total = MoneyField()
    price_rule_version = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="invoice_line_qty_positive"),
            models.CheckConstraint(condition=Q(unit_price__gte=0) & Q(line_total__gte=0),
                                   name="invoice_line_amounts_nonneg"),
        ]


class CreditNote(AppendOnlyModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="credit_notes")
    document_number = models.CharField(max_length=30)
    reason = models.TextField()
    amount = MoneyField()
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    operation_id = models.UUIDField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "document_number"], name="credit_note_unique_number"),
            models.UniqueConstraint(fields=["business", "operation_id"], name="credit_note_unique_op"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="credit_note_amount_positive"),
        ]


class CreditNoteLine(AppendOnlyModel):
    credit_note = models.ForeignKey(CreditNote, on_delete=models.PROTECT, related_name="lines")
    invoice_line = models.ForeignKey(InvoiceLine, on_delete=models.PROTECT, related_name="credit_lines")
    quantity = QuantityField(default=0)
    amount = MoneyField()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="credit_line_amount_positive")]


class InvoiceBalance(BusinessOwned):
    """Transactional projection of an invoice's dues. Rebuildable by reconciliation."""

    class Status(models.TextChoices):
        UNPAID = "UNPAID"
        PARTIAL = "PARTIAL"
        PAID = "PAID"

    invoice = models.OneToOneField(Invoice, on_delete=models.PROTECT, related_name="balance")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="invoice_balances")
    net_due = MoneyField()  # total minus credit notes
    allocated = MoneyField(default=0)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.UNPAID)
    due_date = models.DateField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(net_due__gte=0), name="balance_net_due_nonneg"),
            models.CheckConstraint(condition=Q(allocated__gte=0), name="balance_allocated_nonneg"),
            models.CheckConstraint(condition=Q(allocated__lte=F("net_due")), name="balance_allocated_le_due"),
        ]
        indexes = [models.Index(fields=["business", "customer", "status"])]

    @property
    def outstanding(self):
        return self.net_due - self.allocated

    def recompute_status(self):
        if self.allocated <= 0 and self.net_due > 0:
            self.status = self.Status.UNPAID
        elif self.allocated >= self.net_due:
            self.status = self.Status.PAID
        else:
            self.status = self.Status.PARTIAL
