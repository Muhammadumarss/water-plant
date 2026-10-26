from django.conf import settings
from django.db import models

from common.models import BusinessOwned
from common.money import MoneyField


class SyncClock(models.Model):
    """Incremented at the END of relevant write transactions and locked until
    commit, so change cursors never skip rows committed out of sequence order."""

    business = models.OneToOneField("organization.Business", on_delete=models.PROTECT, related_name="sync_clock")
    revision = models.PositiveBigIntegerField(default=0)


class ChangeRecord(models.Model):
    business = models.ForeignKey("organization.Business", on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveBigIntegerField()
    seq = models.PositiveIntegerField()
    dataset = models.CharField(max_length=30)  # products | prices | customers | deposit_policies
    entity_id = models.UUIDField()
    op = models.CharField(max_length=8, choices=[("UPSERT", "Upsert"), ("DELETE", "Tombstone")])
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "revision", "seq"], name="change_unique_position"),
        ]
        indexes = [models.Index(fields=["business", "dataset", "revision"])]


class OfflineLease(BusinessOwned):
    """Bounded offline autonomy: device/actor, stock allocation, allowed
    operations, signed policy snapshot, expiry and budgets."""

    device = models.ForeignKey("identity.Device", on_delete=models.PROTECT, related_name="leases")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    cash_account = models.ForeignKey("finance.CashAccount", on_delete=models.PROTECT, related_name="+")
    stock_location = models.ForeignKey("inventory.StockLocation", on_delete=models.PROTECT, related_name="+")
    allowed_operations = models.JSONField(default=list)
    policy_snapshot = models.JSONField(default=dict)
    signature = models.CharField(max_length=128)
    schema_version = models.PositiveSmallIntegerField(default=1)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    max_operations = models.PositiveIntegerField(default=500)
    max_amount = MoneyField(default=0)
    used_operations = models.PositiveIntegerField(default=0)
    used_amount = MoneyField(default=0)
    revoked_at = models.DateTimeField(null=True, blank=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)
    last_device_sequence = models.PositiveBigIntegerField(default=0)


class SyncOperation(BusinessOwned):
    class Outcome(models.TextChoices):
        ACCEPTED = "ACCEPTED"
        ALREADY_APPLIED = "ALREADY_APPLIED"
        REVIEW_REQUIRED = "REVIEW_REQUIRED"
        INVALID = "INVALID"

    device = models.ForeignKey("identity.Device", on_delete=models.PROTECT, related_name="sync_operations")
    lease = models.ForeignKey(OfflineLease, null=True, blank=True, on_delete=models.PROTECT,
                              related_name="operations")
    operation_id = models.UUIDField()
    device_sequence = models.PositiveBigIntegerField()
    command = models.CharField(max_length=60)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    local_occurred_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    result = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    resolution = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["device", "operation_id"], name="sync_op_unique_id"),
            models.UniqueConstraint(fields=["device", "device_sequence"], name="sync_op_unique_sequence"),
        ]
