"""Transactional outbox: emit() in the business transaction; the dispatcher
leases deliveries after commit."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from common.serialization import to_jsonable

from .models import EventDelivery, OutboxEvent

log = logging.getLogger(__name__)

# event type -> consumers. Adding a consumer later creates deliveries only for new events.
CONSUMERS = {
    "invoice.issued": ["documents.invoice"],
    "payment.received": ["notifications"],
    "order.accepted": ["notifications"],
    "delivery.dispatched": ["notifications"],
    "delivery.completed": ["notifications"],
    "invoice.overdue": ["notifications"],
    "report.export_requested": ["reports.export"],
}


def emit(ctx, event_type: str, aggregate_type: str, aggregate_id, payload: dict, aggregate_version: int = 1):
    event = OutboxEvent.objects.create(
        business=ctx.business, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
        aggregate_version=aggregate_version, event_type=event_type, payload=to_jsonable(payload),
        request_id=ctx.request_id,
    )
    consumers = CONSUMERS.get(event_type, [])
    now = timezone.now()
    EventDelivery.objects.bulk_create([EventDelivery(event=event, consumer=c, next_attempt_at=now) for c in consumers])
    if consumers and settings.OUTBOX_WAKE_ON_COMMIT:
        transaction.on_commit(_wake_dispatcher)
    return event


def _wake_dispatcher():
    # Best effort only: if this never runs, the periodic scan recovers the event.
    try:
        from infrastructure.tasks import dispatch_outbox

        dispatch_outbox.delay()
    except Exception:  # broker down must never fail a committed sale
        log.warning("outbox wake failed; periodic scan will recover", exc_info=True)


def lease_due_deliveries(limit: int = 100) -> list:
    now = timezone.now()
    lease_until = now + timedelta(seconds=settings.OUTBOX_LEASE_SECONDS)
    with transaction.atomic():
        rows = list(
            EventDelivery.objects.select_for_update(skip_locked=True)
            .filter(Q(status=EventDelivery.Status.PENDING, next_attempt_at__lte=now)
                    | Q(status=EventDelivery.Status.LEASED, lease_until__lt=now))
            .order_by("next_attempt_at")[:limit]
        )
        ids = [row.id for row in rows]
        EventDelivery.objects.filter(id__in=ids).update(status=EventDelivery.Status.LEASED, lease_until=lease_until)
    return ids
