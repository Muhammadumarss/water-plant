from django.conf import settings
from django.db import models
from django.db.models import Q

from common.models import AppendOnlyModel, BusinessOwned
from common.money import MoneyField, QuantityField


class Handover(AppendOnlyModel):
    """The physical handover of goods (counter or delivery). One handover maps to one invoice."""

    class Channel(models.TextChoices):
        COUNTER = "COUNTER"
        DELIVERY = "DELIVERY"
        OFFLINE_COUNTER = "OFFLINE_COUNTER"

    branch = models.ForeignKey("organization.Branch", null=True, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="handovers")
    order = models.ForeignKey("orders.Order", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="handovers")
    delivery_stop = models.ForeignKey("fulfillment.DeliveryStop", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="handovers")
    channel = models.CharField(max_length=16, choices=Channel.choices)
    handed_over_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    device = models.ForeignKey("identity.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField()
    device_occurred_at = models.DateTimeField(null=True, blank=True)
    operation_id = models.UUIDField()
    stock_posting = models.OneToOneField("inventory.StockPostingGroup", null=True, blank=True,
                                         on_delete=models.PROTECT, related_name="handover")
    receipt_reference = models.CharField(max_length=40)
    flags = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "operation_id"], name="handover_unique_op")]


class HandoverLine(AppendOnlyModel):
    handover = models.ForeignKey(Handover, on_delete=models.PROTECT, related_name="lines")
    order_line = models.ForeignKey("orders.OrderLine", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="handover_lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    quantity = QuantityField()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="handover_line_qty_positive")]


class BottleReturn(AppendOnlyModel):
    handover = models.ForeignKey(Handover, on_delete=models.PROTECT, related_name="bottle_returns")
    container_type = models.ForeignKey("catalog.ContainerType", on_delete=models.PROTECT)
    quantity = QuantityField()
    damaged_quantity = QuantityField(default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="bottle_return_qty_positive")]


class DeliveryRun(BusinessOwned):
    class Status(models.TextChoices):
        PLANNED = "PLANNED"
        DISPATCHED = "DISPATCHED"
        SETTLED = "SETTLED"
        CANCELLED = "CANCELLED"

    rider = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="delivery_runs")
    run_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    dispatched_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="+")
    settled_at = models.DateTimeField(null=True, blank=True)
    settled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    expected_cash = MoneyField(null=True, blank=True)
    counted_cash = MoneyField(null=True, blank=True)
    cash_variance = MoneyField(null=True, blank=True)
    stock_report = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)


class DeliveryStop(BusinessOwned):
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED"
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"

    run = models.ForeignKey(DeliveryRun, on_delete=models.PROTECT, related_name="stops")
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="delivery_stops")
    sequence = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "sequence"], name="stop_unique_sequence"),
            models.UniqueConstraint(fields=["run", "order"], name="stop_unique_order"),
        ]


class DeliveryAttempt(AppendOnlyModel):
    class Outcome(models.TextChoices):
        DELIVERED = "DELIVERED"
        PARTIAL = "PARTIAL"
        FAILED = "FAILED"

    stop = models.ForeignKey(DeliveryStop, on_delete=models.PROTECT, related_name="attempts")
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    reason = models.CharField(max_length=200, blank=True)
    attempted_at = models.DateTimeField()
    operation_id = models.UUIDField()
    handover = models.OneToOneField(Handover, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "operation_id"], name="attempt_unique_op")]
