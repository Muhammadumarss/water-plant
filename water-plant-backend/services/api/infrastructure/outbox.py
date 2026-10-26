"""Outbox consumers and the per-delivery processor."""

import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from modules.jobs.models import EventDelivery

log = logging.getLogger(__name__)


def _notifications(event):
    from . import notify

    ids = notify.create_intents(event)
    return {"enqueue_notifications": [str(i) for i in ids]}


def _invoice_document(event):
    from .documents import render_invoice_document

    render_invoice_document(event.payload["invoice_id"])
    return {}


def _report_export(event):
    from .reports import run_export

    run_export(event.payload["job_id"])
    return {}


HANDLERS = {
    "notifications": _notifications,
    "documents.invoice": _invoice_document,
    "reports.export": _report_export,
}


def process_delivery(delivery_id) -> str:
    with transaction.atomic():
        delivery = EventDelivery.objects.select_for_update().select_related("event").filter(id=delivery_id).first()
        if delivery is None or delivery.status == EventDelivery.Status.COMPLETED:
            return "SKIPPED"
        delivery.attempts += 1
        delivery.save(update_fields=["attempts"])
    handler = HANDLERS.get(delivery.consumer)
    try:
        if handler is None:
            raise RuntimeError(f"no handler for consumer {delivery.consumer}")
        followups = handler(delivery.event) or {}
    except Exception as exc:
        log.exception("outbox consumer failed delivery=%s", delivery_id)
        with transaction.atomic():
            d = EventDelivery.objects.select_for_update().get(id=delivery_id)
            d.last_error = str(exc)[:2000]
            if d.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
                d.status = EventDelivery.Status.FAILED  # terminal; reported for operator recovery
            else:
                d.status = EventDelivery.Status.PENDING
                d.next_attempt_at = timezone.now() + timedelta(
                    seconds=min(3600, 15 * 2 ** d.attempts) + random.randint(0, 15))
            d.lease_until = None
            d.save()
        return d.status
    with transaction.atomic():
        EventDelivery.objects.filter(id=delivery_id).update(status=EventDelivery.Status.COMPLETED,
                                                            completed_at=timezone.now(), lease_until=None)
    for notification_id in followups.get("enqueue_notifications", []):
        try:
            from .tasks import submit_notification

            submit_notification.delay(notification_id)
        except Exception:
            log.warning("could not enqueue notification %s; scan will recover", notification_id)
    return "COMPLETED"
