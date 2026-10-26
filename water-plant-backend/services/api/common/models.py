"""Base model conventions: UUID ids, UTC timestamps, business ownership,
versioned master records and append-only ledger records."""

import uuid

from django.conf import settings
from django.db import models

from .errors import ImmutableRecordError


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class BusinessOwned(UUIDModel):
    business = models.ForeignKey("organization.Business", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        abstract = True


class MasterRecord(BusinessOwned):
    """Mutable master data with optimistic concurrency (expected_version / If-Match)."""

    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        abstract = True


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRecordError(f"{self.model.__name__} rows are append-only")

    def delete(self):
        raise ImmutableRecordError(f"{self.model.__name__} rows are append-only")


class AppendOnlyModel(BusinessOwned):
    """Posted business documents and movement entries. Correct with reversals,
    never by UPDATE/DELETE. A database trigger enforces this on PostgreSQL too."""

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError(f"{type(self).__name__} is append-only")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError(f"{type(self).__name__} is append-only")
