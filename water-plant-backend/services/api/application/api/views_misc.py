"""Notifications, provider webhooks, documents, complaints, reports, sync and health."""

import hashlib
import json

from django.conf import settings
from django.core import signing
from django.db import connection, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.parsers import FileUploadParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from application.commands import sync
from common.db import update_versioned
from common.errors import BusinessConflict, NotFound, Unauthenticated, ValidationFailed
from common.ids import parse_uuid
from infrastructure import notify
from infrastructure.messaging.base import adapter_by_name
from infrastructure.storage import get_storage, read_download_token
from modules.audit.services import record as audit
from modules.customers.services import can_view_customer, get_customer
from modules.documents.models import Document
from modules.jobs.models import ExportJob
from modules.jobs.services import emit
from modules.notifications.models import Channel, Notification, NotificationPreference
from modules.support.models import Complaint

from .base import BaseView, ListMixin, paginate
from .serializers import ComplaintOut, NotificationOut

# ---------------------------------------------------------------- notifications


class NotificationPreferenceView(BaseView):
    def patch(self, request):
        ctx = self.ctx(request)
        data = self.data(request)
        customer = get_customer(ctx, parse_uuid(data["customer_id"], "customer_id"))
        if not ctx.is_customer:
            ctx.require("customer.manage")
        channel = data.get("selected_channel", Channel.NONE)
        if channel not in Channel.values:
            raise ValidationFailed(fields={"selected_channel": ["WHATSAPP, SMS or NONE."]})
        pref, created = NotificationPreference.objects.get_or_create(
            customer=customer, defaults={"business": ctx.business})
        changes = {"selected_channel": channel, "language": data.get("language", pref.language),
                   "fallback_enabled": False}  # V1: fallback OFF
        if data.get("consent") is True:
            changes.update(consent_given_at=timezone.now(), consent_withdrawn_at=None,
                           consent_source=f"{'customer' if ctx.is_customer else 'staff'}:{ctx.request_id}")
        elif data.get("consent") is False:
            changes["consent_withdrawn_at"] = timezone.now()
        with transaction.atomic():
            pref = update_versioned(pref, pref.version if created else self.expected_version(request, data), **changes)
            # Re-evaluate unsent messages: never send through both channels automatically.
            Notification.objects.filter(customer=customer, state="PENDING").update(channel=channel)
            audit(ctx, "notification_preference.updated", "customer", customer.id, changes=changes)
        return Response({"customer_id": str(customer.id), "selected_channel": pref.selected_channel,
                         "language": pref.language, "consent": pref.has_consent, "version": pref.version})


class NotificationsView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        qs = Notification.objects.filter(business=ctx.business).order_by("-created_at")
        if ctx.is_customer:
            qs = qs.filter(customer__account_links__user=ctx.user, customer__account_links__revoked_at__isnull=True)
        else:
            ctx.require("notification.view", "notification.retry")
        return paginate(self, request, qs, NotificationOut)


class NotificationRetryView(BaseView):
    def post(self, request, notification_id):
        ctx = self.ctx(request)
        n = Notification.objects.filter(business=ctx.business, id=parse_uuid(notification_id)).first()
        if n is None:
            raise NotFound()
        n = notify.request_retry(ctx, n)
        try:
            from infrastructure.tasks import submit_notification

            submit_notification.delay(str(n.id))
        except Exception:
            pass  # recovery scan picks up PENDING notifications
        return Response(NotificationOut(n).data, status=202)


class MessagingWebhookView(BaseView):
    """Provider-signature authentication, not a user JWT."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "webhook"

    def post(self, request, provider):
        adapter = adapter_by_name(provider)
        body = request.body
        if adapter is None or not adapter.verify_webhook(body=body, headers=request.headers):
            raise Unauthenticated("Invalid signature.")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise ValidationFailed("Invalid JSON.") from None
        applied = notify.apply_callbacks(adapter.name, adapter.normalize_callback(payload), body)
        return Response({"applied": applied})


# ---------------------------------------------------------------- documents

def _can_access_document(ctx, doc: Document) -> bool:
    if doc.business_id != ctx.business_id:
        return False
    if doc.customer_id:
        return can_view_customer(ctx, doc.customer)
    if doc.kind == Document.Kind.EXPORT:
        return ctx.has("report.financial") or doc.uploaded_by_id == ctx.user_id
    return not ctx.is_customer and (doc.uploaded_by_id == ctx.user_id or ctx.is_owner)


class UploadIntentView(BaseView):
    def post(self, request):
        ctx = self.ctx(request)
        ctx.require("document.upload", "self.service")
        data = self.data(request)
        content_type = data.get("content_type", "")
        size = int(data.get("size", 0))
        if content_type not in settings.DOCUMENT_ALLOWED_TYPES:
            raise ValidationFailed(fields={"content_type": ["File type not allowed."]})
        if size <= 0 or size > settings.DOCUMENT_MAX_UPLOAD_BYTES:
            raise ValidationFailed(fields={"size": ["File too large or empty."]})
        customer = None
        if data.get("customer_id"):
            customer = get_customer(ctx, parse_uuid(data["customer_id"], "customer_id"))
        doc = Document.objects.create(business=ctx.business, kind=data.get("kind", Document.Kind.ATTACHMENT),
                                      display_filename=str(data.get("filename", "upload"))[:200],
                                      content_type=content_type, declared_size=size, uploaded_by=ctx.user,
                                      customer=customer)
        return Response({"document_id": str(doc.id), "upload": get_storage().upload_target(doc)}, status=201)


class DocumentContentView(BaseView):
    """Local-storage upload target (S3 deployments upload to a presigned URL instead)."""

    parser_classes = [FileUploadParser]

    def put(self, request, document_id):
        ctx = self.ctx(request)
        doc = Document.objects.filter(business=ctx.business, id=parse_uuid(document_id),
                                      uploaded_by=ctx.user, status=Document.Status.PENDING_UPLOAD).first()
        if doc is None:
            raise NotFound()
        data = request.body
        if len(data) > doc.declared_size or len(data) > settings.DOCUMENT_MAX_UPLOAD_BYTES:
            raise ValidationFailed("Upload exceeds the declared size.")
        get_storage().put(doc.storage_key, data, doc.content_type)
        return Response(status=204)


MAGIC = {"application/pdf": b"%PDF", "image/png": b"\x89PNG", "image/jpeg": b"\xff\xd8\xff"}


class DocumentFinalizeView(BaseView):
    def post(self, request, document_id):
        ctx = self.ctx(request)
        with transaction.atomic():
            doc = Document.objects.select_for_update().filter(business=ctx.business, id=parse_uuid(document_id),
                                                              uploaded_by=ctx.user).first()
            if doc is None:
                raise NotFound()
            if doc.status != Document.Status.PENDING_UPLOAD:
                raise BusinessConflict("Document already finalized.", code="DOCUMENT_FINALIZED")
            storage = get_storage()
            if not storage.exists(doc.storage_key):
                raise BusinessConflict("Upload not found.", code="UPLOAD_MISSING")
            content = storage.get(doc.storage_key)
            magic = MAGIC.get(doc.content_type)
            ok = len(content) <= doc.declared_size and (magic is None or content.startswith(magic))
            # Quarantine until validation/scanning passes. A malware scanner hook belongs here.
            doc.status = Document.Status.AVAILABLE if ok else Document.Status.REJECTED
            doc.actual_size, doc.sha256 = len(content), hashlib.sha256(content).hexdigest()
            doc.save()
        return Response({"document_id": str(doc.id), "status": doc.status})


class DocumentDownloadView(BaseView):
    def get(self, request, document_id):
        ctx = self.ctx(request)
        doc = Document.objects.filter(business=ctx.business, id=parse_uuid(document_id),
                                      status=Document.Status.AVAILABLE).first()
        if doc is None or not _can_access_document(ctx, doc):
            raise NotFound()
        return Response({"url": get_storage().download_url(doc), "expires_in": settings.DOCUMENT_DOWNLOAD_TTL_SECONDS,
                         "filename": doc.display_filename})


class DocumentBlobView(BaseView):
    """Serves a local-storage file behind a short-lived signed token."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, document_id):
        try:
            token_doc = read_download_token(request.query_params.get("token", ""))
        except signing.BadSignature:
            raise NotFound() from None
        if token_doc != str(document_id):
            raise NotFound()
        doc = Document.objects.filter(id=document_id, status=Document.Status.AVAILABLE).first()
        if doc is None:
            raise NotFound()
        response = HttpResponse(get_storage().get(doc.storage_key), content_type=doc.content_type)
        safe_name = "".join(ch for ch in doc.display_filename if ch.isalnum() or ch in "._-") or "download"
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


# ---------------------------------------------------------------- complaints

class ComplaintsView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        qs = Complaint.objects.filter(business=ctx.business).order_by("-created_at")
        if ctx.is_customer:
            qs = qs.filter(customer__account_links__user=ctx.user, customer__account_links__revoked_at__isnull=True)
        else:
            ctx.require("complaint.manage")
        return paginate(self, request, qs, ComplaintOut)

    def post(self, request):
        ctx = self.ctx(request)
        data = self.data(request)
        customer = get_customer(ctx, parse_uuid(data["customer_id"], "customer_id"))
        if not ctx.is_customer:
            ctx.require("complaint.manage")
        complaint = Complaint.objects.create(business=ctx.business, customer=customer,
                                             order_id=data.get("order_id"), category=data.get("category", "GENERAL"),
                                             description=data["description"][:4000], opened_by=ctx.user)
        return Response(ComplaintOut(complaint).data, status=201)


class ComplaintResolveView(BaseView):
    def post(self, request, complaint_id):
        ctx = self.ctx(request)
        ctx.require("complaint.manage")
        data = self.data(request)
        complaint = Complaint.objects.filter(business=ctx.business, id=parse_uuid(complaint_id)).first()
        if complaint is None:
            raise NotFound()
        if not data.get("resolution"):
            raise ValidationFailed(fields={"resolution": ["Required."]})
        complaint.status, complaint.resolution = Complaint.Status.RESOLVED, data["resolution"]
        complaint.resolved_by, complaint.resolved_at = ctx.user, timezone.now()
        complaint.save()
        audit(ctx, "complaint.resolved", "complaint", complaint.id)
        return Response(ComplaintOut(complaint).data)


# ---------------------------------------------------------------- reports

class ExportsView(BaseView):
    def post(self, request):
        from infrastructure.reports import REPORTS

        ctx = self.ctx(request)
        data = self.data(request)
        report_type = data.get("report_type")
        if report_type not in REPORTS:
            raise ValidationFailed(fields={"report_type": [f"One of: {', '.join(REPORTS)}"]})
        if report_type == "stock_custody":
            ctx.require("report.financial", "report.operational_stock")
        else:
            ctx.require("report.financial")
        with transaction.atomic():
            job = ExportJob.objects.create(business=ctx.business, report_type=report_type,
                                           params=data.get("params", {}), requested_by=ctx.user)
            emit(ctx, "report.export_requested", "export_job", job.id, {"job_id": job.id})
        return Response({"job_id": str(job.id), "status": job.status}, status=202)


class ExportDetailView(BaseView):
    def get(self, request, job_id):
        ctx = self.ctx(request)
        job = ExportJob.objects.filter(business=ctx.business, id=parse_uuid(job_id)).first()
        if job is None or (job.requested_by_id != ctx.user_id and not ctx.has("report.financial")):
            raise NotFound()
        return Response({"job_id": str(job.id), "status": job.status, "report_type": job.report_type,
                         "as_of": job.as_of.isoformat() if job.as_of else None,
                         "document_id": str(job.document_id) if job.document_id else None, "error": job.error or None})


# ---------------------------------------------------------------- sync

class SyncLeaseView(BaseView):
    def post(self, request):
        result, status = sync.issue_lease(self.ctx(request), self.data(request),
                                          idempotency_key=self.idempotency_key(request))
        return Response(result, status=201 if status == 201 else 200)


class SyncBootstrapView(BaseView):
    def post(self, request):
        return Response(sync.bootstrap(self.ctx(request)))


class SyncChangesView(BaseView):
    def get(self, request):
        cursor = request.query_params.get("cursor")
        if not cursor:
            raise ValidationFailed(fields={"cursor": ["Required."]})
        return Response(sync.changes(self.ctx(request), cursor))


class SyncOperationsView(BaseView):
    def post(self, request):
        return Response(sync.submit_operations(self.ctx(request), self.data(request)))


class SyncOperationDetailView(BaseView):
    def get(self, request, operation_id):
        return Response(sync.operation_status(self.ctx(request), operation_id))


# ---------------------------------------------------------------- health

class HealthLiveView(BaseView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "ok"})


class HealthReadyView(BaseView):
    """Database is required; messaging/broker outage is reported as degraded only."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        checks = {}
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            checks["database"] = "ok"
        except Exception:
            return Response({"status": "unavailable", "checks": {"database": "down"}}, status=503)
        try:
            import redis

            redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=1).ping()
            checks["broker"] = "ok"
        except Exception:
            checks["broker"] = "degraded"
        return Response({"status": "ok" if checks["broker"] == "ok" else "degraded", "checks": checks})
