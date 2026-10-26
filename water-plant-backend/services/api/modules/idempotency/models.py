from django.conf import settings
from django.db import models

from common.models import BusinessOwned


class IdempotencyRecord(BusinessOwned):
    """Claimed in the same transaction as the posting. The unique key
    serializes concurrent duplicates. Posted operations keep their identity
    permanently; old response bodies may be compacted to references."""

    client_id = models.CharField(max_length=120)
    operation_id = models.UUIDField()
    command_name = models.CharField(max_length=60)
    payload_hash = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    resource_type = models.CharField(max_length=40, blank=True)
    resource_id = models.UUIDField(null=True, blank=True)
    status_code = models.PositiveSmallIntegerField(default=201)
    response_snapshot = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "client_id", "operation_id"], name="idempotency_unique_key"),
        ]
