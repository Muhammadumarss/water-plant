"""Selected-channel messaging (WhatsApp OR SMS), never both automatically.

Submission protocol:
  1. Reserve a MessageAttempt and mark SUBMITTING (committed) before calling the provider.
  2. Call the provider OUTSIDE any transaction, with explicit timeouts.
  3. Save the provider id and known outcome.
  Uncertain acceptance -> UNKNOWN; reconcile via lookup/callback before any resend."""

import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from common.serialization import payload_hash
from modules.customers.models import Customer
from modules.notifications.models import (
    STATE_RANK,
    Channel,
    MessageAttempt,
    MessageTemplate,
    Notification,
    NotificationPreference,
    NotificationState,
    ProviderCallback,
)

from .messaging.base import ProviderError, adapter_by_name, adapter_for

log = logging.getLogger(__name__)

PURPOSE_BY_EVENT = {
    "order.accepted": "ORDER_ACCEPTED",
    "delivery.dispatched": "DELIVERY_DISPATCHED",
    "delivery.completed": "DELIVERY_COMPLETED",
    "payment.received": "PAYMENT_RECEIVED",
    "invoice.overdue": "INVOICE_OVERDUE",
}

DEFAULT_BODIES = {
    ("ORDER_ACCEPTED", "en"): "Your water order has been accepted.",
    ("ORDER_ACCEPTED", "ur"): "آپ کا پانی کا آرڈر قبول کر لیا گیا ہے۔",
    ("DELIVERY_DISPATCHED", "en"): "Your water delivery is on the way.",
    ("DELIVERY_DISPATCHED", "ur"): "آپ کی پانی کی ڈیلیوری روانہ ہو گئی ہے۔",
    ("DELIVERY_COMPLETED", "en"): "Your water delivery is complete. Thank you.",
    ("DELIVERY_COMPLETED", "ur"): "آپ کی ڈیلیوری مکمل ہو گئی۔ شکریہ۔",
    ("PAYMENT_RECEIVED", "en"): "We received your payment. Thank you.",
    ("PAYMENT_RECEIVED", "ur"): "آپ کی ادائیگی موصول ہو گئی۔ شکریہ۔",
    # No amounts on lock screens: a short authenticated statement link is enough.
    ("INVOICE_OVERDUE", "en"): "You have an outstanding balance. Please check your statement.",
    ("INVOICE_OVERDUE", "ur"): "آپ کا بقایا واجب الادا ہے۔ براہ کرم اپنا اسٹیٹمنٹ دیکھیں۔",
}


def _effective_channel(customer: Customer) -> tuple[str, str]:
    pref = NotificationPreference.objects.filter(customer=customer).first()
    if pref is not None:
        if not pref.has_consent:
            return Channel.NONE, pref.language
        return pref.selected_channel, pref.language
    business = customer.business
    return business.default_message_channel, business.default_language


def _recipient(customer: Customer) -> str:
    contact = customer.contacts.order_by("-is_primary", "created_at").first()
    return contact.phone_normalized if contact else ""


def create_intents(event) -> list:
    """Outbox consumer. Deduplicates against PostgreSQL (unique event/customer/purpose)."""
    purpose = PURPOSE_BY_EVENT.get(event.event_type)
    customer_id = event.payload.get("customer_id")
    if purpose is None or not customer_id:
        return []
    customer = Customer.objects.filter(id=customer_id).first()
    if customer is None:
        return []
    channel, language = _effective_channel(customer)
    try:
        notification, _ = Notification.objects.get_or_create(
            event=event, customer=customer, purpose=purpose,
            defaults={"business_id": event.business_id, "channel": channel, "language": language,
                      "recipient": _recipient(customer), "variables": event.payload},
        )
    except IntegrityError:
        notification = Notification.objects.get(event=event, customer=customer, purpose=purpose)
    return [notification.id]


def _render(notification: Notification) -> tuple[str, str, MessageTemplate | None]:
    template = MessageTemplate.objects.filter(
        business_id=notification.business_id, purpose=notification.purpose, channel=notification.channel,
        language=notification.language, is_active=True).order_by("-version").first()
    if template is not None:
        body = template.body.format_map(_Safe(notification.variables))
        return body, template.provider_template_name or notification.purpose.lower(), template
    body = DEFAULT_BODIES.get((notification.purpose, notification.language)) \
        or DEFAULT_BODIES.get((notification.purpose, "en"), notification.purpose)
    return body, notification.purpose.lower(), None


class _Safe(dict):
    def __missing__(self, key):
        return ""


def _still_relevant(notification: Notification) -> bool:
    if notification.purpose == "INVOICE_OVERDUE":
        from modules.billing.services import customer_dues

        return customer_dues(notification.customer) > 0
    return True


def submit(notification_id) -> str:
    # Phase 1: reserve the attempt (committed before the provider call).
    with transaction.atomic():
        n = Notification.objects.select_for_update().select_related("customer", "customer__business") \
            .filter(id=notification_id).first()
        if n is None or n.state != NotificationState.PENDING:
            return n.state if n else "MISSING"
        channel, language = _effective_channel(n.customer)  # re-check consent/selection at dispatch time
        if channel == Channel.NONE or not n.recipient:
            n.state, n.state_reason = NotificationState.CANCELLED, "no permitted channel"
            n.save(update_fields=["state", "state_reason", "updated_at"])
            return n.state
        if not _still_relevant(n):
            n.state, n.state_reason = NotificationState.CANCELLED, "no longer relevant"
            n.save(update_fields=["state", "state_reason", "updated_at"])
            return n.state
        n.channel, n.language = channel, language
        body, template_name, template = _render(n)
        n.template, n.template_version = template, template.version if template else None
        n.state = NotificationState.SUBMITTING
        n.save()
        adapter = adapter_for(channel)
        attempt = MessageAttempt.objects.create(
            business_id=n.business_id, notification=n, attempt_number=n.attempts.count() + 1, provider=adapter.name,
            status=NotificationState.SUBMITTING, submitted_at=timezone.now())

    # Phase 2: provider call, no transaction, no locks.
    try:
        result = adapter.send_template(recipient=n.recipient, template_name=template_name, language=language,
                                       variables=n.variables, idempotency_key=str(attempt.id), body=body)
        outcome, error = NotificationState.ACCEPTED_BY_PROVIDER, None
    except ProviderError as exc:
        result, error = None, exc
        outcome = NotificationState.UNKNOWN if exc.uncertain else NotificationState.FAILED
    except Exception as exc:  # unexpected: treat as uncertain, never blindly resend
        log.exception("provider call crashed")
        result, error = None, ProviderError(str(exc), retryable=False, uncertain=True)
        outcome = NotificationState.UNKNOWN

    # Phase 3: persist the known outcome.
    retry_in = None
    with transaction.atomic():
        n = Notification.objects.select_for_update().get(id=notification_id)
        attempt = MessageAttempt.objects.select_for_update().get(id=attempt.id)
        if result is not None:
            attempt.provider_message_id = result.provider_message_id
            attempt.status = NotificationState.ACCEPTED_BY_PROVIDER
        else:
            attempt.status = outcome
            attempt.error_detail = str(error)[:300]
            attempt.error_class = (MessageAttempt.ErrorClass.UNCERTAIN if error.uncertain
                                   else MessageAttempt.ErrorClass.RETRYABLE if error.retryable
                                   else MessageAttempt.ErrorClass.PERMANENT)
        attempt.save()
        if n.state == NotificationState.SUBMITTING:  # a fast callback may already have advanced it
            if result is not None:
                n.state = NotificationState.ACCEPTED_BY_PROVIDER
            elif error.retryable and not error.uncertain and attempt.attempt_number < settings.MESSAGING_MAX_ATTEMPTS:
                n.state = NotificationState.PENDING
                retry_in = min(3600, (2 ** attempt.attempt_number) * 30) + random.randint(0, 30)
            else:
                n.state = outcome
            n.state_reason = attempt.error_detail
            n.save(update_fields=["state", "state_reason", "updated_at"])
    if retry_in is not None:
        try:
            from .tasks import submit_notification

            submit_notification.apply_async(args=[str(notification_id)], countdown=retry_in)
        except Exception:
            log.warning("could not schedule retry; recovery scan will pick it up", exc_info=True)
    return n.state


def mark_stale_submissions_unknown(older_than_seconds: int = 600) -> int:
    """A crash mid-call leaves SUBMITTING. Never resend automatically: mark UNKNOWN."""
    cutoff = timezone.now() - timedelta(seconds=older_than_seconds)
    with transaction.atomic():
        stale = MessageAttempt.objects.select_for_update(skip_locked=True).filter(
            status=NotificationState.SUBMITTING, submitted_at__lt=cutoff)
        ids = list(stale.values_list("notification_id", flat=True))
        stale.update(status=NotificationState.UNKNOWN, error_class=MessageAttempt.ErrorClass.UNCERTAIN)
        Notification.objects.filter(id__in=ids, state=NotificationState.SUBMITTING).update(
            state=NotificationState.UNKNOWN, state_reason="submission outcome unknown")
    return len(ids)


def reconcile_unknown(limit: int = 100) -> int:
    fixed = 0
    for attempt in MessageAttempt.objects.filter(status=NotificationState.UNKNOWN).exclude(provider_message_id="")[:limit]:
        adapter = adapter_by_name(attempt.provider)
        status = adapter.query_status(attempt.provider_message_id) if adapter else None
        if status:
            _apply_status(attempt, status)
            fixed += 1
    return fixed


def _apply_status(attempt: MessageAttempt, status: str) -> bool:
    """Monotonic transitions only: callbacks may arrive out of order."""
    with transaction.atomic():
        attempt = MessageAttempt.objects.select_for_update().get(id=attempt.id)
        n = Notification.objects.select_for_update().get(id=attempt.notification_id)
        if STATE_RANK.get(status, -1) <= STATE_RANK.get(attempt.status, -1):
            return False
        attempt.status = status
        attempt.save(update_fields=["status", "updated_at"])
        if STATE_RANK.get(status, -1) > STATE_RANK.get(n.state, -1):
            n.state = status
            n.save(update_fields=["state", "updated_at"])
    return True


def apply_callbacks(provider: str, callbacks, raw_body: bytes) -> int:
    applied = 0
    for cb in callbacks:
        try:
            with transaction.atomic():
                record = ProviderCallback.objects.create(
                    provider=provider, event_identity=cb.event_identity[:128], payload_hash=payload_hash(
                        raw_body.decode(errors="replace")), provider_message_id=cb.provider_message_id,
                    status=cb.status, verified_at=timezone.now())
        except IntegrityError:
            continue  # duplicate provider event
        attempt = MessageAttempt.objects.filter(provider=provider, provider_message_id=cb.provider_message_id).first()
        if attempt is not None and _apply_status(attempt, cb.status):
            ProviderCallback.objects.filter(id=record.id).update(applied=True)
            applied += 1
    return applied


def request_retry(ctx, notification: Notification) -> Notification:
    """Controlled recovery: an owner explicitly accepts the risk of a duplicate after UNKNOWN."""
    ctx.require("notification.retry")
    with transaction.atomic():
        n = Notification.objects.select_for_update().get(id=notification.id)
        if n.state not in {NotificationState.FAILED, NotificationState.UNKNOWN}:
            from common.errors import BusinessConflict

            raise BusinessConflict("Only failed or unknown messages can be retried.", code="NOT_RETRYABLE")
        n.state, n.state_reason = NotificationState.PENDING, f"manual retry by {ctx.user}"
        n.save(update_fields=["state", "state_reason", "updated_at"])
    return n
