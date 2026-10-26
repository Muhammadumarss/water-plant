from django.conf import settings
from django.db import models
from django.db.models import Q

from common.models import BusinessOwned, MasterRecord


class Equipment(MasterRecord):
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    location_description = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "code"], name="equipment_unique_code")]


class MaintenanceTask(MasterRecord):
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED"
        DONE = "DONE"
        CANCELLED = "CANCELLED"

    equipment = models.ForeignKey(Equipment, on_delete=models.PROTECT, related_name="tasks")
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, default="MAINTENANCE",
                            choices=[("MAINTENANCE", "Maintenance"), ("CLEANING", "Cleaning")])
    interval_days = models.PositiveIntegerField(null=True, blank=True)
    due_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED)
    last_alerted_for = models.DateField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["business", "status", "due_date"])]


class MaintenanceLog(BusinessOwned):
    task = models.ForeignKey(MaintenanceTask, on_delete=models.PROTECT, related_name="logs")
    work_done = models.TextField()
    parts_consumed = models.JSONField(default=list, blank=True)
    technician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    performed_at = models.DateTimeField()


class QualitySample(BusinessOwned):
    batch = models.ForeignKey("production.ProductionBatch", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="quality_samples")
    equipment = models.ForeignKey(Equipment, null=True, blank=True, on_delete=models.PROTECT)
    location_description = models.CharField(max_length=200, blank=True)
    sampled_at = models.DateTimeField()
    sampled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(batch__isnull=False) | Q(equipment__isnull=False) | ~Q(location_description=""),
                name="sample_has_subject",
            )
        ]


class TestResult(BusinessOwned):
    sample = models.ForeignKey(QualitySample, on_delete=models.PROTECT, related_name="results")
    parameter = models.CharField(max_length=60)  # e.g. TDS, pH, residual chlorine
    value = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.CharField(max_length=20)
    within_limits = models.BooleanField(null=True)
    lab_attachment = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.PROTECT)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")


class BatchHold(BusinessOwned):
    batch = models.ForeignKey("production.ProductionBatch", on_delete=models.PROTECT, related_name="holds")
    reason = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    released_at = models.DateTimeField(null=True, blank=True)
    release_evidence = models.TextField(blank=True)
    evidence_document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(released_at__isnull=True) | (Q(released_by__isnull=False) & ~Q(release_evidence="")),
                name="hold_release_needs_evidence",
            )
        ]
