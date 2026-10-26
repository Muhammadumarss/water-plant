"""Money in, money allocated, money out.

Unallocated receipt money is customer advance credit; there is no separate
editable wallet. Deposits are a liability, never water revenue."""

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from common.models import AppendOnlyModel, BusinessOwned
from common.money import MoneyField, QuantityField, currency_check, currency_field


class PaymentMethod(models.TextChoices):
    CASH = "CASH"
    BANK_TRANSFER = "BANK_TRANSFER"
    MOBILE_WALLET = "MOBILE_WALLET"


class CashAccount(BusinessOwned):
    class Kind(models.TextChoices):
        TILL = "TILL"
        RIDER = "RIDER"
        BANK = "BANK"
        SAFE = "SAFE"

    kind = models.CharField(max_length=8, choices=Kind.choices)
    name = models.CharField(max_length=120)
    stock_location = models.ForeignKey("inventory.StockLocation", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+", help_text="Till stock custody for counter sales.")
    rider = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="+")
    balance = MoneyField(default=0)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "rider"], condition=Q(kind="RIDER"),
                                    name="cash_account_unique_rider"),
        ]


class CashShift(BusinessOwned):
    class Status(models.TextChoices):
        OPEN = "OPEN"
        CLOSED = "CLOSED"
        APPROVED = "APPROVED"

    cash_account = models.ForeignKey(CashAccount, on_delete=models.PROTECT, related_name="shifts")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField()
    opening_float = MoneyField(default=0)
    closed_at = models.DateTimeField(null=True, blank=True)
    expected_close = MoneyField(null=True, blank=True)
    counted_close = MoneyField(null=True, blank=True)
    variance = MoneyField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["cash_account"], condition=Q(status="OPEN"), name="shift_one_open"),
        ]


class PaymentReceipt(AppendOnlyModel):
    class Verification(models.TextChoices):
        VERIFIED = "VERIFIED"
        CASH = "CASH"

    branch = models.ForeignKey("organization.Branch", null=True, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="receipts")
    amount = MoneyField()
    currency = currency_field()
    method = models.CharField(max_length=16, choices=PaymentMethod.choices)
    received_at = models.DateTimeField()
    custodian = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    cash_account = models.ForeignKey(CashAccount, on_delete=models.PROTECT, related_name="receipts")
    shift = models.ForeignKey(CashShift, null=True, blank=True, on_delete=models.PROTECT, related_name="receipts")
    verification = models.CharField(max_length=10, choices=Verification.choices)
    verified_reference = models.CharField(max_length=100, blank=True)
    receipt_number = models.CharField(max_length=30)
    operation_id = models.UUIDField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "operation_id"], name="receipt_unique_op"),
            models.UniqueConstraint(fields=["business", "receipt_number"], name="receipt_unique_number"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="receipt_amount_positive"),
            models.CheckConstraint(condition=Q(method="CASH") | ~Q(verified_reference=""),
                                   name="receipt_digital_needs_reference"),
            currency_check("receipt_valid_currency"),
        ]
        indexes = [models.Index(fields=["business", "customer", "created_at"])]


class ReceiptBalance(BusinessOwned):
    """Parent lock row and projection for one receipt:
    allocations + refunds funded from it <= received amount."""

    receipt = models.OneToOneField(PaymentReceipt, on_delete=models.PROTECT, related_name="funds")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="+")
    amount = MoneyField()
    allocated = MoneyField(default=0)
    refunded = MoneyField(default=0)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(allocated__gte=0) & Q(refunded__gte=0), name="receipt_funds_nonneg"),
            models.CheckConstraint(condition=Q(allocated__lte=F("amount") - F("refunded")),
                                   name="receipt_funds_conserved"),
        ]

    @property
    def unallocated(self):
        return self.amount - self.allocated - self.refunded


class DepositAccount(BusinessOwned):
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="deposit_accounts")
    currency = currency_field()
    balance = MoneyField(default=0)  # projection of sum(signed entries)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["customer", "currency"], name="deposit_account_unique"),
            models.CheckConstraint(condition=Q(balance__gte=0), name="deposit_balance_nonneg"),
        ]


class ReceiptAllocation(AppendOnlyModel):
    class Target(models.TextChoices):
        INVOICE = "INVOICE"
        DEPOSIT = "DEPOSIT"

    receipt = models.ForeignKey(PaymentReceipt, on_delete=models.PROTECT, related_name="allocations")
    target_kind = models.CharField(max_length=8, choices=Target.choices)
    invoice = models.ForeignKey("billing.Invoice", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="allocations")
    deposit_account = models.ForeignKey(DepositAccount, null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="allocations")
    amount = MoneyField()
    # A reversal row releases (part of) an earlier allocation. Effective = original - reversals.
    reversal_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="reversals")
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="allocation_amount_positive"),
            models.CheckConstraint(
                condition=(Q(target_kind="INVOICE", invoice__isnull=False, deposit_account__isnull=True)
                           | Q(target_kind="DEPOSIT", deposit_account__isnull=False, invoice__isnull=True)),
                name="allocation_exactly_one_target",
            ),
        ]


class ImportBatch(BusinessOwned):
    class Status(models.TextChoices):
        DRY_RUN = "DRY_RUN"
        APPROVED = "APPROVED"
        REJECTED = "REJECTED"

    source_filename = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRY_RUN)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    report = models.JSONField(default=dict, blank=True)


class OpeningEntry(BusinessOwned):
    """Approved opening balance from an import. Old debt is excluded from new-period revenue."""

    class BalanceType(models.TextChoices):
        RECEIVABLE = "RECEIVABLE"
        DEPOSIT = "DEPOSIT"
        ADVANCE = "ADVANCE"
        BOTTLES = "BOTTLES"

    import_batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="entries")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="opening_entries")
    balance_type = models.CharField(max_length=10, choices=BalanceType.choices)
    amount = MoneyField(default=0)
    quantity = QuantityField(default=0)
    container_type = models.ForeignKey("catalog.ContainerType", null=True, blank=True, on_delete=models.PROTECT)
    source_row = models.PositiveIntegerField()
    dedup_key = models.CharField(max_length=120)
    consumed_amount = MoneyField(default=0)  # refunded or released funding (DEPOSIT/ADVANCE)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "dedup_key"], name="opening_entry_unique_dedup"),
            models.CheckConstraint(condition=Q(amount__gte=0) & Q(consumed_amount__gte=0),
                                   name="opening_entry_nonneg"),
            models.CheckConstraint(condition=Q(consumed_amount__lte=F("amount")), name="opening_entry_conserved"),
        ]


class Refund(AppendOnlyModel):
    class Kind(models.TextChoices):
        ADVANCE = "ADVANCE"
        DEPOSIT = "DEPOSIT"

    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="refunds")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    amount = MoneyField()
    currency = currency_field()
    method = models.CharField(max_length=16, choices=PaymentMethod.choices)
    cash_account = models.ForeignKey(CashAccount, on_delete=models.PROTECT, related_name="refunds")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    paid_at = models.DateTimeField()
    operation_id = models.UUIDField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "operation_id"], name="refund_unique_op"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_positive"),
        ]


class RefundSource(AppendOnlyModel):
    refund = models.ForeignKey(Refund, on_delete=models.PROTECT, related_name="sources")
    receipt = models.ForeignKey(PaymentReceipt, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="refund_sources")
    opening_entry = models.ForeignKey(OpeningEntry, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="refund_sources")
    amount = MoneyField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_source_positive"),
            models.CheckConstraint(
                condition=(Q(receipt__isnull=False, opening_entry__isnull=True)
                           | Q(receipt__isnull=True, opening_entry__isnull=False)),
                name="refund_source_exactly_one",
            ),
        ]


class DepositEntry(AppendOnlyModel):
    deposit_account = models.ForeignKey(DepositAccount, on_delete=models.PROTECT, related_name="entries")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="deposit_entries")
    amount = MoneyField()  # signed
    funding_allocation = models.ForeignKey(ReceiptAllocation, null=True, blank=True, on_delete=models.PROTECT,
                                           related_name="deposit_entries")
    opening_entry = models.ForeignKey(OpeningEntry, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="deposit_entries")
    refund = models.ForeignKey(Refund, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="deposit_entries")
    container_type = models.ForeignKey("catalog.ContainerType", null=True, blank=True, on_delete=models.PROTECT)
    bottles = models.IntegerField(default=0)
    agreement_version = models.PositiveIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=200)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(amount=0), name="deposit_entry_nonzero"),
            models.CheckConstraint(
                condition=Q(funding_allocation__isnull=False) | Q(opening_entry__isnull=False)
                | Q(refund__isnull=False),
                name="deposit_entry_has_source",
            ),
        ]


class Expense(AppendOnlyModel):
    category = models.CharField(max_length=60)
    payee = models.CharField(max_length=200)
    amount = MoneyField()
    currency = currency_field()
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    paid_at = models.DateTimeField()
    cash_account = models.ForeignKey(CashAccount, on_delete=models.PROTECT, related_name="expenses")
    receipt_document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.PROTECT)
    notes = models.TextField(blank=True)
    operation_id = models.UUIDField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "operation_id"], name="expense_unique_op"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="expense_amount_positive"),
        ]


class CashMovement(AppendOnlyModel):
    class Direction(models.TextChoices):
        IN = "IN"
        OUT = "OUT"

    class Kind(models.TextChoices):
        RECEIPT = "RECEIPT"
        REFUND = "REFUND"
        EXPENSE = "EXPENSE"
        TRANSFER = "TRANSFER"
        ADJUSTMENT = "ADJUSTMENT"

    cash_account = models.ForeignKey(CashAccount, on_delete=models.PROTECT, related_name="movements")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = MoneyField()
    receipt = models.ForeignKey(PaymentReceipt, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    refund = models.ForeignKey(Refund, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    expense = models.ForeignKey(Expense, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    shift = models.ForeignKey(CashShift, null=True, blank=True, on_delete=models.PROTECT, related_name="movements")
    transfer_group = models.UUIDField(null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="cash_movement_positive"),
            models.CheckConstraint(
                condition=(Q(kind="RECEIPT", receipt__isnull=False) | Q(kind="REFUND", refund__isnull=False)
                           | Q(kind="EXPENSE", expense__isnull=False) | Q(kind="TRANSFER", transfer_group__isnull=False)
                           | Q(kind="ADJUSTMENT")),
                name="cash_movement_source",
            ),
        ]
        indexes = [models.Index(fields=["cash_account", "created_at"])]
