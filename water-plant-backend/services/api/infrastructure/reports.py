"""Asynchronous report exports with an as-of marker and formula-safe CSV."""

import csv
import hashlib
import io

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from modules.billing.models import InvoiceBalance, InvoiceLine
from modules.documents.models import Document
from modules.finance.models import DepositAccount, ReceiptBalance
from modules.inventory.models import StockBucket
from modules.jobs.models import ExportJob

from .storage import get_storage

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def _customer_balances(business_id, params):
    yield ["customer_id", "customer", "dues", "deposit", "advance"]
    dues = {r["customer_id"]: r for r in InvoiceBalance.objects.filter(business_id=business_id)
            .values("customer_id", "customer__display_name").annotate(d=Sum(F("net_due") - F("allocated")))}
    deposits = dict(DepositAccount.objects.filter(business_id=business_id).values_list("customer_id", "balance"))
    advances = {r["customer_id"]: r["a"] for r in ReceiptBalance.objects.filter(business_id=business_id)
                .values("customer_id").annotate(a=Sum(F("amount") - F("allocated") - F("refunded")))}
    for cid in set(dues) | set(deposits) | set(advances):
        row = dues.get(cid, {})
        yield [cid, row.get("customer__display_name", ""), row.get("d", 0) or 0, deposits.get(cid, 0),
               advances.get(cid, 0) or 0]


def _sales_by_product(business_id, params):
    yield ["product", "quantity", "amount"]
    qs = InvoiceLine.objects.filter(business_id=business_id, invoice__is_opening_balance=False)
    if params.get("from"):
        qs = qs.filter(invoice__issued_at__date__gte=params["from"])
    if params.get("to"):
        qs = qs.filter(invoice__issued_at__date__lte=params["to"])
    for row in qs.values("product_name_snapshot").annotate(q=Sum("quantity"), a=Sum("line_total")):
        yield [row["product_name_snapshot"], row["q"], row["a"]]


def _stock_custody(business_id, params):
    yield ["item", "location_kind", "location", "condition", "batch", "on_hand", "reserved"]
    for b in StockBucket.objects.filter(business_id=business_id, is_virtual=False, on_hand__gt=0) \
            .select_related("item", "location", "batch"):
        yield [b.item.name, b.location.kind, b.location.name, b.condition, b.batch.batch_code if b.batch else "",
               b.on_hand, b.reserved]


REPORTS = {"customer_balances": _customer_balances, "sales_by_product": _sales_by_product,
           "stock_custody": _stock_custody}


def run_export(job_id) -> None:
    with transaction.atomic():
        job = ExportJob.objects.select_for_update().get(id=job_id)
        if job.status == ExportJob.Status.DONE:
            return
        job.status, job.as_of = ExportJob.Status.RUNNING, timezone.now()
        job.save(update_fields=["status", "as_of"])
    try:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        # REPEATABLE READ snapshot so the whole export reflects one instant.
        with transaction.atomic():
            from django.db import connection

            from common.db import is_postgres

            if is_postgres():
                with connection.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            for row in REPORTS[job.report_type](job.business_id, job.params):
                writer.writerow([safe_cell(v) for v in row])
        data = buffer.getvalue().encode("utf-8-sig")
        doc = Document.objects.create(business_id=job.business_id, kind=Document.Kind.EXPORT,
                                      display_filename=f"{job.report_type}-{job.as_of:%Y%m%d%H%M}.csv",
                                      content_type="text/csv", declared_size=len(data), uploaded_by=job.requested_by)
        get_storage().put(doc.storage_key, data, "text/csv")
        Document.objects.filter(id=doc.id).update(status=Document.Status.AVAILABLE, actual_size=len(data),
                                                  sha256=hashlib.sha256(data).hexdigest())
        ExportJob.objects.filter(id=job.id).update(status=ExportJob.Status.DONE, document=doc,
                                                   finished_at=timezone.now())
    except Exception as exc:
        ExportJob.objects.filter(id=job.id).update(status=ExportJob.Status.FAILED, error=str(exc)[:2000],
                                                   finished_at=timezone.now())
        raise
