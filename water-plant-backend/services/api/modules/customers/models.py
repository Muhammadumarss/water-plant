from django.conf import settings
from django.db import models

from common.models import BusinessOwned, MasterRecord
from common.money import MoneyField


class Customer(MasterRecord):
    class Type(models.TextChoices):
        INDIVIDUAL = "INDIVIDUAL"
        HOUSEHOLD = "HOUSEHOLD"
        COMMERCIAL = "COMMERCIAL"

    display_name = models.CharField(max_length=200)
    customer_type = models.CharField(max_length=16, choices=Type.choices, default=Type.INDIVIDUAL)
    # 0 means cash-only: no unpaid water charges allowed.
    credit_limit = MoneyField(default=0)
    credit_terms_days = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["business", "display_name"])]
        constraints = [models.CheckConstraint(condition=models.Q(credit_limit__gte=0), name="customer_credit_nonneg")]

    def __str__(self):
        return self.display_name


class CustomerContact(BusinessOwned):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="contacts")
    phone_raw = models.CharField(max_length=30)
    phone_normalized = models.CharField(max_length=20, db_index=True)
    label = models.CharField(max_length=40, blank=True)
    is_primary = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_method = models.CharField(max_length=30, blank=True)

    class Meta:
        indexes = [models.Index(fields=["business", "phone_normalized"])]


class CustomerAddress(BusinessOwned):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="addresses")
    line1 = models.CharField(max_length=255)
    area = models.CharField(max_length=120, blank=True)
    landmark = models.CharField(max_length=255, blank=True)
    service_area = models.ForeignKey("organization.ServiceArea", null=True, blank=True, on_delete=models.PROTECT)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def as_snapshot(self) -> str:
        return ", ".join(p for p in [self.line1, self.area, self.landmark] if p)


class CustomerAccountLink(BusinessOwned):
    """Explicit authorization for a verified portal user to see a customer.
    Never created automatically from a matching phone number."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="customer_links")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="account_links")
    relationship = models.CharField(max_length=40, default="account_holder")
    linked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "customer"], condition=models.Q(revoked_at__isnull=True), name="account_link_unique_active"
            )
        ]


class CustomerCreditAccount(BusinessOwned):
    """Serialization row: locked to make credit-limit decisions safe under concurrency."""

    customer = models.OneToOneField(Customer, on_delete=models.PROTECT, related_name="credit_account")
    updated_at = models.DateTimeField(auto_now=True)
