from django.conf import settings
from django.db import models

from common.models import BusinessOwned


class OutboxEvent(BusinessOwned):
    """Created in the same commit as the business record (transactional outbox)."""

    aggregate_type = models.CharField(max_length=40)
    aggregate_id = models.UUIDField()
    aggregate_version = models.PositiveIntegerField(default=1)
    event_type = models.CharField(max_length=60)
    schema_version = models.PositiveSmallIntegerField(default=1)
    payload = models.JSONField(default=dict)
    request_id = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event_type", "aggregate_type", "aggregate_id", "aggregate_version"],
                                    name="outbox_unique_logical_event"),
        ]


class EventDelivery(models.Model):
    """Per-consumer delivery record. Consumer completion is authoritative;
    'published to broker' is not 'completed'."""

    class Status(models.TextChoices):
        PENDING = "PENDING"
        LEASED = "LEASED"
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"

    event = models.ForeignKey(OutboxEvent, on_delete=models.PROTECT, related_name="deliveries")
    consumer = models.CharField(max_length=60)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(db_index=True)
    last_error = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "consumer"], name="delivery_unique_consumer")]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class ExportJob(BusinessOwned):
    class Status(models.TextChoices):
        QUEUED = "QUEUED"
        RUNNING = "RUNNING"
        DONE = "DONE"
        FAILED = "FAILED"

    report_type = models.CharField(max_length=40)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.QUEUED)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    as_of = models.DateTimeField(null=True, blank=True)
    document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.PROTECT)
    error = models.TextField(blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
