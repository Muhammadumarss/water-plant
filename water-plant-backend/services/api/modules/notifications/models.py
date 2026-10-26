from django.db import models

from common.models import BusinessOwned


class Channel(models.TextChoices):
    WHATSAPP = "WHATSAPP"
    SMS = "SMS"
    NONE = "NONE"


class NotificationState(models.TextChoices):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    ACCEPTED_BY_PROVIDER = "ACCEPTED_BY_PROVIDER"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


# Monotonic ordering for provider callbacks (callbacks may arrive out of order).
STATE_RANK = {
    NotificationState.PENDING: 0,
    NotificationState.SUBMITTING: 1,
    NotificationState.UNKNOWN: 2,
    NotificationState.ACCEPTED_BY_PROVIDER: 3,
    NotificationState.DELIVERED: 4,
    NotificationState.READ: 5,
    NotificationState.FAILED: 6,
    NotificationState.CANCELLED: 6,
}


class NotificationPreference(BusinessOwned):
    customer = models.OneToOneField("customers.Customer", on_delete=models.PROTECT,
                                    related_name="notification_preference")
    selected_channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.NONE)
    language = models.CharField(max_length=5, default="en", choices=[("en", "English"), ("ur", "Urdu")])
    fallback_enabled = models.BooleanField(default=False)  # V1: OFF
    consent_given_at = models.DateTimeField(null=True, blank=True)
    consent_source = models.CharField(max_length=60, blank=True)
    consent_withdrawn_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    @property
    def has_consent(self):
        return self.consent_given_at is not None and self.consent_withdrawn_at is None


class MessageTemplate(BusinessOwned):
    purpose = models.CharField(max_length=40)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    language = models.CharField(max_length=5)
    version = models.PositiveIntegerField(default=1)
    provider_template_name = models.CharField(max_length=100, blank=True)
    body = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "purpose", "channel", "language", "version"],
                                    name="template_unique_version"),
        ]


class Notification(BusinessOwned):
    event = models.ForeignKey("jobs.OutboxEvent", on_delete=models.PROTECT, related_name="notifications")
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="notifications")
    purpose = models.CharField(max_length=40)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    recipient = models.CharField(max_length=30)
    language = models.CharField(max_length=5)
    template = models.ForeignKey(MessageTemplate, null=True, blank=True, on_delete=models.PROTECT)
    template_version = models.PositiveIntegerField(null=True, blank=True)
    variables = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=24, choices=NotificationState.choices, default=NotificationState.PENDING)
    state_reason = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "customer", "purpose"], name="notification_unique_intent"),
        ]
        indexes = [models.Index(fields=["business", "state"])]


class MessageAttempt(BusinessOwned):
    class ErrorClass(models.TextChoices):
        NONE = "NONE"
        RETRYABLE = "RETRYABLE"
        PERMANENT = "PERMANENT"
        UNCERTAIN = "UNCERTAIN"

    notification = models.ForeignKey(Notification, on_delete=models.PROTECT, related_name="attempts")
    attempt_number = models.PositiveSmallIntegerField()
    provider = models.CharField(max_length=40)
    provider_message_id = models.CharField(max_length=120, blank=True, db_index=True)
    status = models.CharField(max_length=24, choices=NotificationState.choices,
                              default=NotificationState.SUBMITTING)
    error_class = models.CharField(max_length=10, choices=ErrorClass.choices, default=ErrorClass.NONE)
    error_detail = models.CharField(max_length=300, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["notification", "attempt_number"], name="attempt_unique_number"),
        ]


class ProviderCallback(models.Model):
    provider = models.CharField(max_length=40)
    event_identity = models.CharField(max_length=128)
    payload_hash = models.CharField(max_length=64)
    provider_message_id = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=24, blank=True)
    verified_at = models.DateTimeField()
    applied = models.BooleanField(default=False)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_identity"], name="callback_unique_identity"),
        ]
