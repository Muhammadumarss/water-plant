"""ONE stock-movement ledger. The bottle ledger is a filtered view of it."""

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from common.models import AppendOnlyModel, BusinessOwned, MasterRecord
from common.money import MoneyField, QuantityField


class StockItem(MasterRecord):
    class Kind(models.TextChoices):
        REUSABLE_CONTAINER = "REUSABLE_CONTAINER"
        CONSUMABLE = "CONSUMABLE"
        TREATED_WATER = "TREATED_WATER"
        MERCHANDISE = "MERCHANDISE"

    class Unit(models.TextChoices):
        BOTTLE = "BOTTLE"
        LITRE = "LITRE"
        PIECE = "PIECE"
        KG = "KG"

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=24, choices=Kind.choices)
    base_unit = models.CharField(max_length=10, choices=Unit.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "code"], name="stock_item_unique_code")]

    @property
    def whole_units(self) -> bool:
        return self.base_unit in {self.Unit.BOTTLE, self.Unit.PIECE}

    def __str__(self):
        return self.name


class StockLocation(BusinessOwned):
    class Kind(models.TextChoices):
        PLANT = "PLANT"
        TILL = "TILL"
        RIDER = "RIDER"
        CUSTOMER = "CUSTOMER"
        VIRTUAL = "VIRTUAL"

    class Virtual(models.TextChoices):
        SUPPLIER = "SUPPLIER"
        PRODUCTION = "PRODUCTION"
        CONSUMED = "CONSUMED"
        SOLD = "SOLD"
        WRITE_OFF = "WRITE_OFF"
        OPENING = "OPENING"
        ADJUSTMENT = "ADJUSTMENT"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    virtual_kind = models.CharField(max_length=12, choices=Virtual.choices, blank=True)
    name = models.CharField(max_length=120)
    branch = models.ForeignKey("organization.Branch", null=True, blank=True, on_delete=models.PROTECT)
    customer = models.OneToOneField("customers.Customer", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="stock_location")
    rider = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="+")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "virtual_kind"], condition=Q(kind="VIRTUAL"),
                                    name="location_unique_virtual"),
            models.UniqueConstraint(fields=["business", "rider"], condition=Q(kind="RIDER"),
                                    name="location_unique_rider"),
            models.CheckConstraint(condition=~Q(kind="VIRTUAL") | ~Q(virtual_kind=""), name="location_virtual_kind"),
            models.CheckConstraint(condition=~Q(kind="CUSTOMER") | Q(customer__isnull=False),
                                   name="location_customer_fk"),
        ]

    @property
    def is_virtual(self):
        return self.kind == self.Kind.VIRTUAL

    def __str__(self):
        return self.name


class Condition(models.TextChoices):
    CLEAN_EMPTY = "CLEAN_EMPTY"
    FULL = "FULL"
    DIRTY_RETURNED = "DIRTY_RETURNED"
    CUSTOMER_HELD = "CUSTOMER_HELD"
    DAMAGED = "DAMAGED"
    STANDARD = "STANDARD"  # non-container items (water litres, consumables, merchandise)


class StockBucket(BusinessOwned):
    """Transactional projection of a balance, updated in the SAME transaction
    as its movements. Virtual buckets are excluded from availability and
    their on_hand is not maintained (avoids a global hot row)."""

    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="buckets")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="buckets")
    condition = models.CharField(max_length=16, choices=Condition.choices)
    batch = models.ForeignKey("production.ProductionBatch", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="buckets")
    is_virtual = models.BooleanField(default=False)
    on_hand = QuantityField(default=0)
    reserved = QuantityField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "item", "location", "condition", "batch"],
                                    nulls_distinct=False, name="bucket_unique_identity"),
            models.CheckConstraint(condition=Q(is_virtual=True) | Q(on_hand__gte=0), name="bucket_on_hand_nonneg"),
            models.CheckConstraint(condition=Q(reserved__gte=0), name="bucket_reserved_nonneg"),
            models.CheckConstraint(condition=Q(is_virtual=True) | Q(reserved__lte=F("on_hand")),
                                   name="bucket_reserved_le_on_hand"),
        ]
        indexes = [models.Index(fields=["business", "item", "location", "condition"])]

    @property
    def available(self):
        return self.on_hand - self.reserved


class StockPostingGroup(AppendOnlyModel):
    """Immutable set of movements representing one business action."""

    operation_id = models.UUIDField()
    action = models.CharField(max_length=40)
    source_type = models.CharField(max_length=40)
    source_id = models.UUIDField(null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField(blank=True)
    reversal_of = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="reversed_by")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "operation_id", "action"], name="posting_group_unique_op"),
        ]
        indexes = [models.Index(fields=["source_type", "source_id"])]


class StockMovement(AppendOnlyModel):
    class Type(models.TextChoices):
        SUPPLIER_RECEIPT = "SUPPLIER_RECEIPT"
        PRODUCTION_OUTPUT = "PRODUCTION_OUTPUT"
        CLEANING = "CLEANING"
        FILLING_CONSUME = "FILLING_CONSUME"
        FILLING_OUTPUT = "FILLING_OUTPUT"
        TRANSFER = "TRANSFER"
        DISPATCH = "DISPATCH"
        RETURN_TO_PLANT = "RETURN_TO_PLANT"
        CUSTOMER_ISSUE = "CUSTOMER_ISSUE"
        SALE = "SALE"
        EMPTY_RETURN = "EMPTY_RETURN"
        DAMAGE = "DAMAGE"
        WRITE_OFF = "WRITE_OFF"
        COUNT_ADJUSTMENT = "COUNT_ADJUSTMENT"
        OPENING = "OPENING"
        REVERSAL = "REVERSAL"

    group = models.ForeignKey(StockPostingGroup, on_delete=models.PROTECT, related_name="movements")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="+")
    source_bucket = models.ForeignKey(StockBucket, on_delete=models.PROTECT, related_name="outgoing")
    target_bucket = models.ForeignKey(StockBucket, on_delete=models.PROTECT, related_name="incoming")
    quantity = QuantityField()
    movement_type = models.CharField(max_length=24, choices=Type.choices)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="movement_qty_positive"),
            models.CheckConstraint(condition=~Q(source_bucket=F("target_bucket")), name="movement_distinct_buckets"),
        ]
        indexes = [
            models.Index(fields=["source_bucket", "created_at"]),
            models.Index(fields=["target_bucket", "created_at"]),
        ]


class Reservation(BusinessOwned):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE"
        RELEASED = "RELEASED"
        CONSUMED = "CONSUMED"

    order_line = models.ForeignKey("orders.OrderLine", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="reservations")
    lease = models.ForeignKey("synchronization.OfflineLease", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="reservations")
    bucket = models.ForeignKey(StockBucket, on_delete=models.PROTECT, related_name="reservations")
    quantity = QuantityField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    flagged_reason = models.CharField(max_length=60, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="reservation_qty_positive")]
        indexes = [models.Index(fields=["status", "expires_at"])]


class StockCount(BusinessOwned):
    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED"
        APPROVED = "APPROVED"
        REJECTED = "REJECTED"

    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT)
    counted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED)
    notes = models.TextField(blank=True)
    posting_group = models.OneToOneField(StockPostingGroup, null=True, blank=True, on_delete=models.PROTECT)


class StockCountLine(BusinessOwned):
    count = models.ForeignKey(StockCount, on_delete=models.PROTECT, related_name="lines")
    bucket = models.ForeignKey(StockBucket, on_delete=models.PROTECT)
    expected_snapshot = QuantityField()
    counted_quantity = QuantityField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(counted_quantity__gte=0), name="count_line_nonneg"),
            models.UniqueConstraint(fields=["count", "bucket"], name="count_line_unique_bucket"),
        ]

    @property
    def variance(self):
        return self.counted_quantity - self.expected_snapshot


class Purchase(BusinessOwned):
    class Status(models.TextChoices):
        ORDERED = "ORDERED"
        PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
        RECEIVED = "RECEIVED"
        CANCELLED = "CANCELLED"

    supplier_name = models.CharField(max_length=200)
    reference = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ORDERED)
    ordered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    destination = models.ForeignKey(StockLocation, on_delete=models.PROTECT)


class PurchaseLine(BusinessOwned):
    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="lines")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT)
    condition = models.CharField(max_length=16, choices=Condition.choices, default=Condition.STANDARD)
    ordered_quantity = QuantityField()
    received_quantity = QuantityField(default=0)
    rate = MoneyField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(ordered_quantity__gt=0), name="purchase_line_qty_positive"),
            models.CheckConstraint(condition=Q(received_quantity__gte=0), name="purchase_line_received_nonneg"),
        ]


class GoodsReceipt(AppendOnlyModel):
    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="receipts")
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    posting_group = models.OneToOneField(StockPostingGroup, on_delete=models.PROTECT)
    operation_id = models.UUIDField(unique=True)
