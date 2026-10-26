from django.conf import settings
from django.db import models

from common.models import AppendOnlyModel, MasterRecord
from common.money import MoneyField, QuantityField


class ContainerType(MasterRecord):
    """A kind of plant-owned returnable container, e.g. 19L bottle."""

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    capacity_litres = QuantityField()
    stock_item = models.OneToOneField("inventory.StockItem", on_delete=models.PROTECT, related_name="container_type")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "code"], name="container_type_unique_code")]

    def __str__(self):
        return self.name


class Product(MasterRecord):
    class SaleMode(models.TextChoices):
        RETURNABLE_BOTTLE = "RETURNABLE_BOTTLE", "Water in plant-owned returnable bottle"
        REFILL = "REFILL", "Refill into customer-owned container"
        MERCHANDISE = "MERCHANDISE", "Nonreturnable merchandise"

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    name_ur = models.CharField(max_length=120, blank=True)
    sale_mode = models.CharField(max_length=20, choices=SaleMode.choices)
    unit = models.CharField(max_length=20, default="bottle")
    container_type = models.ForeignKey(ContainerType, null=True, blank=True, on_delete=models.PROTECT)
    water_litres_per_unit = QuantityField(default=0)
    # Merchandise only. Bottled water uses the container type's stock item; refill uses treated water.
    stock_item = models.ForeignKey("inventory.StockItem", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    is_public = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "code"], name="product_unique_code"),
            models.CheckConstraint(
                condition=~models.Q(sale_mode="RETURNABLE_BOTTLE") | models.Q(container_type__isnull=False),
                name="product_bottle_needs_container",
            ),
            models.CheckConstraint(
                condition=~models.Q(sale_mode="MERCHANDISE") | models.Q(stock_item__isnull=False),
                name="product_merch_needs_item",
            ),
            models.CheckConstraint(condition=models.Q(water_litres_per_unit__gte=0), name="product_litres_nonneg"),
            models.CheckConstraint(condition=models.Q(tax_rate__gte=0), name="product_tax_nonneg"),
        ]

    def __str__(self):
        return self.name


class PriceRule(AppendOnlyModel):
    """Effective-dated price. Publishing a new price appends a new version;
    existing quotes and invoices keep their snapshots."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="price_rules")
    customer = models.ForeignKey("customers.Customer", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="price_overrides")
    amount = MoneyField()
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "customer", "version"], nulls_distinct=False,
                                    name="price_rule_unique_version"),
            models.CheckConstraint(condition=models.Q(amount__gte=0), name="price_rule_amount_nonneg"),
        ]
        indexes = [models.Index(fields=["product", "customer", "-effective_from"])]


class DepositPolicy(AppendOnlyModel):
    container_type = models.ForeignKey(ContainerType, on_delete=models.PROTECT, related_name="deposit_policies")
    amount_per_unit = MoneyField()
    effective_from = models.DateTimeField()
    version = models.PositiveIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["container_type", "version"], name="deposit_policy_unique_version"),
            models.CheckConstraint(condition=models.Q(amount_per_unit__gte=0), name="deposit_policy_nonneg"),
        ]
