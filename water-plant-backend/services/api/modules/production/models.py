from django.conf import settings
from django.db import models
from django.db.models import Q

from common.models import BusinessOwned
from common.money import QuantityField


class ProductionBatch(BusinessOwned):
    class Status(models.TextChoices):
        PENDING_RELEASE = "PENDING_RELEASE"  # produced, awaiting quality release
        RELEASED = "RELEASED"
        HOLD = "HOLD"
        BLOCKED = "BLOCKED"  # rejected; never released

    batch_code = models.CharField(max_length=40)
    production_date = models.DateField()
    input_litres = QuantityField()
    output_litres = QuantityField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING_RELEASE)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    treatment_notes = models.TextField(blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    operation_id = models.UUIDField(unique=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "batch_code"], name="batch_unique_code"),
            models.CheckConstraint(condition=Q(input_litres__gte=0) & Q(output_litres__gte=0),
                                   name="batch_litres_nonneg"),
            models.CheckConstraint(condition=Q(output_litres__lte=models.F("input_litres")),
                                   name="batch_output_le_input"),
        ]
        indexes = [models.Index(fields=["business", "status"])]

    def __str__(self):
        return self.batch_code


class FillingRun(BusinessOwned):
    batch = models.ForeignKey(ProductionBatch, on_delete=models.PROTECT, related_name="filling_runs")
    container_type = models.ForeignKey("catalog.ContainerType", on_delete=models.PROTECT)
    filled_quantity = QuantityField()
    water_litres_consumed = QuantityField()
    wastage_litres = QuantityField(default=0)
    containers_rejected = QuantityField(default=0)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    posting_group = models.OneToOneField("inventory.StockPostingGroup", on_delete=models.PROTECT)
    operation_id = models.UUIDField(unique=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(filled_quantity__gt=0), name="filling_qty_positive"),
            models.CheckConstraint(condition=Q(wastage_litres__gte=0), name="filling_waste_nonneg"),
        ]


class FillingConsumption(BusinessOwned):
    filling_run = models.ForeignKey(FillingRun, on_delete=models.PROTECT, related_name="consumptions")
    item = models.ForeignKey("inventory.StockItem", on_delete=models.PROTECT)
    quantity = QuantityField()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="filling_consumption_positive")]
