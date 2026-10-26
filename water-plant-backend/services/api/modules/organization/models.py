import uuid

from django.db import models

from common.models import MasterRecord
from common.money import MoneyField, currency_check, currency_field


class Business(models.Model):
    """V1 provisions exactly one business. Not a multi-tenant SaaS boundary."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    currency = currency_field()
    timezone = models.CharField(max_length=64, default="Asia/Karachi")
    default_language = models.CharField(max_length=5, default="en", choices=[("en", "English"), ("ur", "Urdu")])
    # Default notification channel when a customer has no preference. V1 fallback OFF.
    default_message_channel = models.CharField(
        max_length=16, default="NONE", choices=[("WHATSAPP", "WhatsApp"), ("SMS", "SMS"), ("NONE", "None")]
    )
    invoice_prefix = models.CharField(max_length=10, default="INV")
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [currency_check("business_valid_currency")]

    def __str__(self):
        return self.name


class Branch(MasterRecord):
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20)
    address = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "code"], name="branch_unique_code")]

    def __str__(self):
        return self.name


class ServiceArea(MasterRecord):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    delivery_fee = MoneyField(default=0)
    is_public = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "name"], name="service_area_unique_name"),
            models.CheckConstraint(condition=models.Q(delivery_fee__gte=0), name="service_area_fee_nonneg"),
        ]


class DeliveryWindow(MasterRecord):
    WEEKDAYS = [(i, d) for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])]

    service_area = models.ForeignKey(ServiceArea, null=True, blank=True, on_delete=models.PROTECT)
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    capacity_orders = models.PositiveIntegerField(default=50)
    label = models.CharField(max_length=60, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(end_time__gt=models.F("start_time")), name="window_time_order"),
        ]
