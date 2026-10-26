"""Invoice documents generated from POSTED snapshots (never today's prices).

V1 renders a printable HTML document. A PDF engine with proper Urdu text
shaping is a deployment decision (see README); the deterministic-version rule
is the same: a retry never creates a second document for one invoice version."""

import hashlib
from html import escape

from django.db import IntegrityError, transaction

from modules.billing.models import Invoice
from modules.documents.models import Document

from .storage import get_storage


def _html(invoice: Invoice) -> str:
    rows = "".join(
        f"<tr><td>{escape(line.product_name_snapshot)}</td><td>{line.quantity.normalize()} {escape(line.unit_snapshot)}"
        f"</td><td>{line.unit_price}</td><td>{line.line_total}</td></tr>"
        for line in invoice.lines.order_by("id")
    )
    return f"""<!doctype html><html lang="{escape(invoice.language)}"><head><meta charset="utf-8">
<title>{escape(invoice.document_number)}</title>
<style>body{{font-family:sans-serif;max-width:640px;margin:auto}}table{{width:100%;border-collapse:collapse}}
td,th{{border-bottom:1px solid #ccc;padding:4px;text-align:left}}</style></head><body>
<h1>Invoice {escape(invoice.document_number)}</h1>
<p>Issued: {invoice.issued_at:%Y-%m-%d %H:%M} UTC<br>Customer: {escape(invoice.customer_name_snapshot)}<br>
{escape(invoice.address_snapshot)}</p>
<table><tr><th>Item</th><th>Qty</th><th>Rate</th><th>Amount</th></tr>{rows}</table>
<p>Subtotal: {invoice.subtotal} &nbsp; Discount: {invoice.discount_total} &nbsp; Tax: {invoice.tax_total}</p>
<p><strong>Total: {invoice.currency} {invoice.total}</strong></p></body></html>"""


def render_invoice_document(invoice_id, version: int = 1) -> Document:
    invoice = Invoice.objects.select_related("customer").get(id=invoice_id)
    existing = Document.objects.filter(invoice=invoice, kind=Document.Kind.INVOICE, source_version=version).first()
    if existing is not None and existing.status == Document.Status.AVAILABLE:
        return existing
    data = _html(invoice).encode()
    try:
        with transaction.atomic():
            doc, _ = Document.objects.get_or_create(
                invoice=invoice, kind=Document.Kind.INVOICE, source_version=version,
                defaults={"business_id": invoice.business_id, "customer": invoice.customer,
                          "display_filename": f"{invoice.document_number}.html", "content_type": "text/html",
                          "declared_size": len(data)})
    except IntegrityError:
        doc = Document.objects.get(invoice=invoice, kind=Document.Kind.INVOICE, source_version=version)
    get_storage().put(doc.storage_key, data, doc.content_type)
    Document.objects.filter(id=doc.id).update(status=Document.Status.AVAILABLE, actual_size=len(data),
                                              sha256=hashlib.sha256(data).hexdigest())
    doc.refresh_from_db()
    return doc
