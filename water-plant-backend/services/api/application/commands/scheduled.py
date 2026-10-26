"""Scheduled, duplicate-safe jobs. Run by Celery Beat (single scheduler) but
correct even if a run happens twice."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone

from common.money import ZERO
from common.timeutil import business_today
from modules.billing.models import InvoiceBalance
from modules.finance.models import ReceiptAllocation, ReceiptBalance
from modules.identity.context import system_context
from modules.inventory.models import Reservation, StockBucket, StockLocation, StockMovement
from modules.inventory.services import lock_buckets, release_reservation
from modules.jobs.services import emit
from modules.orders.models import Order, OrderLine, RecurringSchedule, ScheduleOccurrence
from modules.organization.models import Business
from modules.plant_care.models import MaintenanceTask


def _next_date(schedule: RecurringSchedule, after):
    if schedule.recurrence == RecurringSchedule.Recurrence.EVERY_N_DAYS:
        return after + timedelta(days=schedule.interval_days or 7)
    days = sorted(schedule.weekdays or [after.weekday()])
    for offset in range(1, 8):
        candidate = after + timedelta(days=offset)
        if candidate.weekday() in days:
            return candidate
    return after + timedelta(days=7)


def generate_recurring_orders(horizon_days: int = 2) -> int:
    """Unique schedule/local service date/slot. Respects pause periods."""
    created = 0
    today = business_today()
    horizon = today + timedelta(days=horizon_days)
    for schedule in RecurringSchedule.objects.filter(is_active=True, next_occurrence_date__lte=horizon) \
            .select_related("business", "customer"):
        ctx = system_context(schedule.business, request_id="job:recurring")
        while schedule.next_occurrence_date <= horizon:
            service_date = schedule.next_occurrence_date
            paused = schedule.pause_from and schedule.pause_from <= service_date <= (schedule.pause_until or service_date)
            try:
                with transaction.atomic():
                    occurrence = ScheduleOccurrence.objects.create(
                        business=schedule.business, schedule=schedule, service_date=service_date,
                        delivery_window=schedule.delivery_window, skipped_reason="PAUSED" if paused else "")
                    if not paused:
                        order = Order.objects.create(
                            business=schedule.business, branch=ctx.branch, customer=schedule.customer,
                            channel=Order.Channel.RECURRING, requested_date=service_date,
                            delivery_window=schedule.delivery_window,
                            address_snapshot=schedule.address.as_snapshot() if schedule.address else "")
                        OrderLine.objects.bulk_create([
                            OrderLine(business=schedule.business, order=order, product=line.product,
                                      quantity=line.quantity) for line in schedule.lines.all()])
                        occurrence.order = order
                        occurrence.save(update_fields=["order"])
                        created += 1
            except IntegrityError:
                pass  # another run already generated this occurrence
            schedule.next_occurrence_date = _next_date(schedule, service_date)
            RecurringSchedule.objects.filter(id=schedule.id).update(next_occurrence_date=schedule.next_occurrence_date)
    return created


def expire_reservations() -> int:
    """Release expired PLANT reservations only. Never release dispatched or offline-custody goods."""
    released = 0
    now = timezone.now()
    for res in Reservation.objects.filter(status=Reservation.Status.ACTIVE, expires_at__lt=now,
                                          bucket__location__kind=StockLocation.Kind.PLANT, lease__isnull=True):
        with transaction.atomic():
            if res.order_line_id:
                Order.objects.select_for_update().filter(id=res.order_line.order_id).first()
            locked = lock_buckets([res.bucket_id])
            res.refresh_from_db()
            if res.status == Reservation.Status.ACTIVE:
                release_reservation(res, locked.by_id[res.bucket_id])
                released += 1
    return released


def maintenance_alerts() -> int:
    """Deduplicated per task/due-window. Never releases quality holds."""
    today = business_today()
    count = 0
    for task in MaintenanceTask.objects.filter(status=MaintenanceTask.Status.SCHEDULED, due_date__lte=today) \
            .exclude(last_alerted_for=F("due_date")):
        updated = MaintenanceTask.objects.filter(id=task.id).exclude(last_alerted_for=F("due_date")) \
            .update(last_alerted_for=F("due_date"))
        count += updated
    return count


def overdue_reminders() -> int:
    """One reminder per invoice per reminder window (unique logical outbox event)."""
    today = business_today()
    window = today.isocalendar().week + today.year * 100
    count = 0
    for balance in InvoiceBalance.objects.filter(due_date__lt=today).exclude(status="PAID") \
            .select_related("invoice", "customer__business"):
        ctx = system_context(balance.customer.business, request_id="job:overdue")
        try:
            with transaction.atomic():
                emit(ctx, "invoice.overdue", "invoice", balance.invoice_id,
                     {"invoice_id": balance.invoice_id, "customer_id": balance.customer_id},
                     aggregate_version=window)
                count += 1
        except IntegrityError:
            pass
    return count


def reconcile_projections(business=None, repair: bool = False) -> dict:
    """Compare projections with ledgers; report differences before any repair."""
    report = {"stock_buckets": [], "invoice_balances": [], "receipt_balances": []}
    businesses = [business] if business else list(Business.objects.all())
    for biz in businesses:
        incoming = dict(StockMovement.objects.filter(business=biz).values("target_bucket")
                        .annotate(s=Sum("quantity")).values_list("target_bucket", "s"))
        outgoing = dict(StockMovement.objects.filter(business=biz).values("source_bucket")
                        .annotate(s=Sum("quantity")).values_list("source_bucket", "s"))
        for bucket in StockBucket.objects.filter(business=biz, is_virtual=False):
            ledger = (incoming.get(bucket.id) or ZERO) - (outgoing.get(bucket.id) or ZERO)
            if ledger != bucket.on_hand:
                report["stock_buckets"].append({"bucket_id": str(bucket.id), "projection": str(bucket.on_hand),
                                                "ledger": str(ledger)})
                if repair:
                    StockBucket.objects.filter(id=bucket.id).update(on_hand=ledger)
        for balance in InvoiceBalance.objects.filter(business=biz):
            allocs = ReceiptAllocation.objects.filter(invoice_id=balance.invoice_id)
            gross = allocs.filter(reversal_of__isnull=True).aggregate(s=Sum("amount"))["s"] or ZERO
            rev = allocs.filter(reversal_of__isnull=False).aggregate(s=Sum("amount"))["s"] or ZERO
            if gross - rev != balance.allocated:
                report["invoice_balances"].append({"invoice_id": str(balance.invoice_id),
                                                   "projection": str(balance.allocated), "ledger": str(gross - rev)})
        for rb in ReceiptBalance.objects.filter(business=biz):
            allocs = ReceiptAllocation.objects.filter(receipt_id=rb.receipt_id)
            gross = allocs.filter(reversal_of__isnull=True).aggregate(s=Sum("amount"))["s"] or ZERO
            rev = allocs.filter(reversal_of__isnull=False).aggregate(s=Sum("amount"))["s"] or ZERO
            if gross - rev != rb.allocated:
                report["receipt_balances"].append({"receipt_id": str(rb.receipt_id),
                                                   "projection": str(rb.allocated), "ledger": str(gross - rev)})
    return report
