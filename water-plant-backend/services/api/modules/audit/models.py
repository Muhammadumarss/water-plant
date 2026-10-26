from django.conf import settings
from django.db import models

from common.models import AppendOnlyModel


class AuditEvent(AppendOnlyModel):
    """Who/what/when/why. Commits with the business transaction.
    Never stores raw passwords, tokens or provider secrets."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="+")
    device = models.ForeignKey("identity.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=60)
    target_type = models.CharField(max_length=40)
    target_id = models.CharField(max_length=64)
    outcome = models.CharField(max_length=20, default="SUCCESS")
    reason = models.TextField(blank=True)
    changes = models.JSONField(default=dict, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    operation_id = models.UUIDField(null=True, blank=True)
    occurred_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["business", "target_type", "target_id"]),
            models.Index(fields=["business", "created_at"]),
        ]
