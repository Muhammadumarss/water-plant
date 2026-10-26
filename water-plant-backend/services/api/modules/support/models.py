from django.conf import settings
from django.db import models

from common.models import BusinessOwned


class Complaint(BusinessOwned):
    class Status(models.TextChoices):
        OPEN = "OPEN"
        ASSIGNED = "ASSIGNED"
        RESOLVED = "RESOLVED"

    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="complaints")
    order = models.ForeignKey("orders.Order", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    category = models.CharField(max_length=40, default="GENERAL")
    description = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    resolution = models.TextField(blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
