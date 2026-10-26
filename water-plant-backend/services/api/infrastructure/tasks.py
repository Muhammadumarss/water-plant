"""Celery tasks. Each is idempotent: deduplication lives in PostgreSQL, not Redis."""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

log = logging.getLogger(__name__)


@shared_task(name="jobs.dispatch_outbox")
def dispatch_outbox():
    from modules.jobs.services import lease_due_deliveries

    ids = lease_due_deliveries()
    for delivery_id in ids:
        process_event_delivery.delay(delivery_id)
    return len(ids)


@shared_task(name="jobs.process_event_delivery", acks_late=True)
def process_event_delivery(delivery_id):
    from .outbox import process_delivery

    return process_delivery(delivery_id)


@shared_task(name="jobs.recover_stale_deliveries")
def recover_stale_deliveries():
    """Recovers after broker loss, lease expiry or worker failure."""
    from modules.notifications.models import Notification, NotificationState

    from .notify import mark_stale_submissions_unknown, reconcile_unknown

    dispatched = dispatch_outbox()
    unknown = mark_stale_submissions_unknown()
    reconciled = reconcile_unknown()
    cutoff = timezone.now() - timedelta(minutes=5)
    for nid in Notification.objects.filter(state=NotificationState.PENDING, updated_at__lt=cutoff) \
            .values_list("id", flat=True)[:200]:
        submit_notification.delay(str(nid))
    return {"dispatched": dispatched, "marked_unknown": unknown, "reconciled": reconciled}


@shared_task(name="notifications.submit")
def submit_notification(notification_id):
    from .notify import submit

    return submit(notification_id)


@shared_task(name="orders.generate_recurring_orders")
def generate_recurring_orders():
    from application.commands.scheduled import generate_recurring_orders as run

    return run()


@shared_task(name="orders.expire_reservations")
def expire_reservations():
    from application.commands.scheduled import expire_reservations as run

    return run()


@shared_task(name="plant_care.maintenance_alerts")
def maintenance_alerts():
    from application.commands.scheduled import maintenance_alerts as run

    return run()


@shared_task(name="billing.overdue_reminders")
def overdue_reminders():
    from application.commands.scheduled import overdue_reminders as run

    return run()


@shared_task(name="jobs.reconcile_projections")
def reconcile_projections():
    from application.commands.scheduled import reconcile_projections as run

    report = run(repair=False)
    if any(report.values()):
        log.error("ledger projection mismatches: %s", report)
    return report


@shared_task(name="reports.export")
def export_report(job_id):
    from .reports import run_export

    return run_export(job_id)
