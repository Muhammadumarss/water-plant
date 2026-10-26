"""PostgreSQL triggers that reject UPDATE/DELETE on posted ledger tables.

Corrections are made with linked reversals, credit notes or adjustments.
TRUNCATE (used only by test database flushing) is not affected."""

from django.db import migrations

from common.db import REJECT_FUNCTION_SQL, append_only_trigger_sql

LEDGER_TABLES = [
    "audit_auditevent",
    "billing_invoice",
    "billing_invoiceline",
    "billing_creditnote",
    "billing_creditnoteline",
    "catalog_pricerule",
    "catalog_depositpolicy",
    "finance_paymentreceipt",
    "finance_receiptallocation",
    "finance_depositentry",
    "finance_refund",
    "finance_refundsource",
    "finance_expense",
    "finance_cashmovement",
    "fulfillment_handover",
    "fulfillment_handoverline",
    "fulfillment_bottlereturn",
    "fulfillment_deliveryattempt",
    "inventory_stockpostinggroup",
    "inventory_stockmovement",
    "inventory_goodsreceipt",
]


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(REJECT_FUNCTION_SQL)
    for table in LEDGER_TABLES:
        schema_editor.execute(append_only_trigger_sql(table)[0])


def backward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in LEDGER_TABLES:
        schema_editor.execute(append_only_trigger_sql(table)[1])
    schema_editor.execute("DROP FUNCTION IF EXISTS wp_reject_ledger_change();")


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0002_initial"),
        ("billing", "0002_initial"),
        ("catalog", "0003_initial"),
        ("finance", "0003_initial"),
        ("fulfillment", "0003_initial"),
        ("inventory", "0002_initial"),
    ]

    operations = [migrations.RunPython(forward, backward)]
