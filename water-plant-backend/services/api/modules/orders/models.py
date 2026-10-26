from django.conf import settings
from django.db import models
from django.db.models import F, Q

from common.models import BusinessOwned, MasterRecord
from common.money import MoneyField, QuantityField


class OrderStatus(models.TextChoices):
    REQUESTED = "REQUESTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    READY = "READY"
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    CLOSED_PARTIAL = "CLOSED_PARTIAL"


# Allowed transitions (architecture section 9).
TRANSITIONS = {
    OrderStatus.REQUESTED: {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.ACCEPTED: {OrderStatus.READY, OrderStatus.CANCELLED},
    OrderStatus.READY: {OrderStatus.PARTIALLY_FULFILLED, OrderStatus.FULFILLED, OrderStatus.CANCELLED},
    OrderStatus.PARTIALLY_FULFILLED: {OrderStatus.FULFILLED, OrderStatus.CLOSED_PARTIAL},
}


class Order(BusinessOwned):
    class Channel(models.TextChoices):
        WEB = "WEB"
        APP = "APP"
        PHONE = "PHONE"
        COUNTER = "COUNTER"
        RECURRING = "RECURRING"

    branch = models.ForeignKey("organization.Branch", null=True, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey("customers.Customer", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="orders")
    guest_name = models.CharField(max_length=200, blank=True)
    guest_phone_normalized = models.CharField(max_length=20, blank=True)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    requested_date = models.DateField()
    delivery_window = models.ForeignKey("organization.DeliveryWindow", null=True, blank=True,
                                        on_delete=models.PROTECT)
    service_area = models.ForeignKey("organization.ServiceArea", null=True, blank=True, on_delete=models.PROTECT)
    address_snapshot = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.REQUESTED)
    version = models.PositiveIntegerField(default=1)
    placed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    notes = models.TextField(blank=True)
    rejection_reason = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    operation_id = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(customer__isnull=False) | ~Q(guest_phone_normalized=""),
                                   name="order_customer_or_guest"),
            models.UniqueConstraint(fields=["business", "operation_id"], name="order_unique_op"),
        ]
        indexes = [models.Index(fields=["business", "status", "requested_date"])]


class OrderLine(BusinessOwned):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    quantity = QuantityField()
    quote_unit_price = MoneyField(null=True, blank=True)
    quote_price_version = models.PositiveIntegerField(null=True, blank=True)
    fulfilled_quantity = QuantityField(default=0)
    cancelled_quantity = QuantityField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="order_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(fulfilled_quantity__gte=0, cancelled_quantity__gte=0)
                & Q(fulfilled_quantity__lte=F("quantity") - F("cancelled_quantity")),
                name="order_line_qty_conserved",
            ),
        ]

    @property
    def open_quantity(self):
        return self.quantity - self.fulfilled_quantity - self.cancelled_quantity


class RecurringSchedule(MasterRecord):
    class Recurrence(models.TextChoices):
        WEEKLY = "WEEKLY"
        EVERY_N_DAYS = "EVERY_N_DAYS"

    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="schedules")
    recurrence = models.CharField(max_length=14, choices=Recurrence.choices)
    weekdays = models.JSONField(default=list, blank=True)  # 0=Mon .. 6=Sun for WEEKLY
    interval_days = models.PositiveSmallIntegerField(null=True, blank=True)
    delivery_window = models.ForeignKey("organization.DeliveryWindow", null=True, blank=True,
                                        on_delete=models.PROTECT)
    timezone = models.CharField(max_length=64, default="Asia/Karachi")
    start_date = models.DateField()
    pause_from = models.DateField(null=True, blank=True)
    pause_until = models.DateField(null=True, blank=True)
    next_occurrence_date = models.DateField()
    address = models.ForeignKey("customers.CustomerAddress", null=True, blank=True, on_delete=models.PROTECT)


class RecurringScheduleLine(BusinessOwned):
    schedule = models.ForeignKey(RecurringSchedule, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    quantity = QuantityField()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="schedule_line_qty_positive")]


class ScheduleOccurrence(BusinessOwned):
    schedule = models.ForeignKey(RecurringSchedule, on_delete=models.PROTECT, related_name="occurrences")
    service_date = models.DateField()
    delivery_window = models.ForeignKey("organization.DeliveryWindow", null=True, blank=True,
                                        on_delete=models.PROTECT)
    order = models.OneToOneField(Order, null=True, blank=True, on_delete=models.PROTECT, related_name="occurrence")
    skipped_reason = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["schedule", "service_date", "delivery_window"], nulls_distinct=False,
                                    name="occurrence_unique_slot"),
        ]
