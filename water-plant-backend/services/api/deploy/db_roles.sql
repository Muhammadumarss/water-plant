-- Separate database roles (architecture section 5 and 18).
-- Run as a superuser after migrations. Adjust names/passwords for your environment.
-- wp_migrator: owns schema, runs migrations.  wp_app: runtime.  wp_readonly: inspection/reporting.
CREATE ROLE wp_app LOGIN PASSWORD 'change-me';
CREATE ROLE wp_readonly LOGIN PASSWORD 'change-me';
GRANT USAGE ON SCHEMA public TO wp_app, wp_readonly;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO wp_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO wp_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO wp_readonly;
-- Ledger tables: the runtime role may only append. (Triggers also reject UPDATE/DELETE.)
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'billing_invoice','billing_invoiceline','billing_creditnote','billing_creditnoteline',
    'finance_paymentreceipt','finance_receiptallocation','finance_depositentry','finance_refund',
    'finance_refundsource','finance_expense','finance_cashmovement','inventory_stockpostinggroup',
    'inventory_stockmovement','inventory_goodsreceipt','fulfillment_handover','fulfillment_handoverline',
    'fulfillment_bottlereturn','fulfillment_deliveryattempt','audit_auditevent','catalog_pricerule',
    'catalog_depositpolicy']
  LOOP
    EXECUTE format('REVOKE UPDATE, DELETE ON %I FROM wp_app', t);
  END LOOP;
END $$;
