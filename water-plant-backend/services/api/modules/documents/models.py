import uuid

from django.conf import settings
from django.db import models

from common.models import BusinessOwned


def random_key():
    return f"{uuid.uuid4().hex[:2]}/{uuid.uuid4().hex}"


class Document(BusinessOwned):
    """Attachment metadata. Storage keys are random; filenames are display only."""

    class Kind(models.TextChoices):
        ATTACHMENT = "ATTACHMENT"
        INVOICE = "INVOICE"
        EXPORT = "EXPORT"
        LAB_RESULT = "LAB_RESULT"

    class Status(models.TextChoices):
        PENDING_UPLOAD = "PENDING_UPLOAD"
        QUARANTINED = "QUARANTINED"
        AVAILABLE = "AVAILABLE"
        REJECTED = "REJECTED"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    storage_key = models.CharField(max_length=200, unique=True, default=random_key)
    display_filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    declared_size = models.PositiveIntegerField()
    actual_size = models.PositiveIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING_UPLOAD)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey("customers.Customer", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="documents")
    invoice = models.ForeignKey("billing.Invoice", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="documents")
    source_version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["invoice", "kind", "source_version"], condition=models.Q(kind="INVOICE"),
                                    name="invoice_document_unique_version"),
        ]
