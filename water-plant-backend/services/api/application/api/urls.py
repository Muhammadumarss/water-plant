"""/api/v1 routes (architecture section 12)."""

from django.urls import path

from . import views_auth as a
from . import views_core as c
from . import views_misc as x
from . import views_money as m
from . import views_ops as o

urlpatterns = [
    # Public
    path("public/products", c.PublicProductsView.as_view()),
    path("public/service-areas", c.PublicServiceAreasView.as_view()),
    path("public/order-requests", c.PublicOrderRequestView.as_view()),
    # Auth
    path("auth/web/csrf", a.CsrfView.as_view()),
    path("auth/web/login", a.WebLoginView.as_view()),
    path("auth/web/logout", a.WebLogoutView.as_view()),
    path("auth/token", a.TokenView.as_view()),
    path("auth/token/refresh", a.TokenRefreshView.as_view()),
    path("auth/token/revoke", a.TokenRevokeView.as_view()),
    path("me", a.MeView.as_view()),
    path("me/customers", a.MyCustomersView.as_view()),
    path("me/orders", c.MyOrdersView.as_view()),
    path("me/notification-preference", x.NotificationPreferenceView.as_view()),
    # Customers
    path("customers", c.CustomersView.as_view()),
    path("customers/<uuid:customer_id>", c.CustomerDetailView.as_view()),
    path("customers/<uuid:customer_id>/account-summary", c.AccountSummaryView.as_view()),
    path("customers/<uuid:customer_id>/account-links", c.AccountLinkView.as_view()),
    # Catalog
    path("products", c.ProductsView.as_view()),
    path("products/<uuid:product_id>/prices", c.ProductPriceView.as_view()),
    path("container-types/<uuid:container_type_id>/deposit-policies", c.DepositPolicyView.as_view()),
    # Orders
    path("orders", c.OrdersView.as_view()),
    path("orders/<uuid:order_id>/accept", c.OrderActionView.as_view(action="accept")),
    path("orders/<uuid:order_id>/reject", c.OrderActionView.as_view(action="reject")),
    path("orders/<uuid:order_id>/reserve", c.OrderActionView.as_view(action="reserve")),
    path("orders/<uuid:order_id>/cancel", c.OrderActionView.as_view(action="cancel")),
    path("recurring-schedules", c.SchedulesView.as_view()),
    path("recurring-schedules/<uuid:schedule_id>/pause", c.SchedulePauseView.as_view()),
    # Money
    path("sales/complete", m.CompleteSaleView.as_view()),
    path("payments/receive", m.ReceivePaymentView.as_view()),
    path("invoices", m.InvoicesView.as_view()),
    path("invoices/<uuid:invoice_id>", m.InvoiceDetailView.as_view()),
    path("invoices/<uuid:invoice_id>/credit-notes", m.CreditNoteView.as_view()),
    path("refunds", m.RefundView.as_view()),
    path("deposit-refunds", m.DepositRefundView.as_view()),
    path("cash-shifts/open", m.OpenShiftView.as_view()),
    path("cash-shifts/<uuid:shift_id>/close", m.CloseShiftView.as_view()),
    path("cash-shifts/<uuid:shift_id>/approve", m.ApproveShiftView.as_view()),
    path("expenses", m.ExpenseView.as_view()),
    # Inventory & plant
    path("inventory/balances", o.InventoryBalancesView.as_view()),
    path("inventory/movements", o.InventoryMovementsView.as_view()),
    path("inventory/transfers", o.TransferView.as_view()),
    path("inventory/counts", o.CountsView.as_view()),
    path("inventory/counts/<uuid:count_id>/approve", o.CountApproveView.as_view()),
    path("purchases", o.PurchasesView.as_view()),
    path("purchases/<uuid:purchase_id>/receive", o.PurchaseReceiveView.as_view()),
    path("production/batches", o.BatchesView.as_view()),
    path("production/batches/<uuid:batch_id>/hold", o.BatchHoldView.as_view()),
    path("production/batches/<uuid:batch_id>/release", o.BatchReleaseView.as_view()),
    path("production/filling-runs", o.FillingRunsView.as_view()),
    path("quality/samples", o.QualitySamplesView.as_view()),
    path("quality/samples/<uuid:sample_id>/results", o.QualityResultsView.as_view()),
    path("maintenance/tasks", o.MaintenanceTasksView.as_view()),
    path("maintenance/tasks/<uuid:task_id>/complete", o.MaintenanceCompleteView.as_view()),
    # Delivery
    path("delivery/runs", o.DeliveryRunsView.as_view()),
    path("delivery/me/stops", o.MyStopsView.as_view()),
    path("delivery/runs/<uuid:run_id>/dispatch", o.RunDispatchView.as_view()),
    path("delivery/runs/<uuid:run_id>/settle", o.RunSettleView.as_view()),
    path("delivery/stops/<uuid:stop_id>/complete", o.StopCompleteView.as_view()),
    path("delivery/stops/<uuid:stop_id>/fail", o.StopFailView.as_view()),
    # Notifications
    path("notifications", x.NotificationsView.as_view()),
    path("notifications/<uuid:notification_id>/retry", x.NotificationRetryView.as_view()),
    path("webhooks/messaging/<str:provider>", x.MessagingWebhookView.as_view()),
    # Documents
    path("documents/upload-intents", x.UploadIntentView.as_view()),
    path("documents/<uuid:document_id>/content", x.DocumentContentView.as_view()),
    path("documents/<uuid:document_id>/finalize", x.DocumentFinalizeView.as_view()),
    path("documents/<uuid:document_id>/download", x.DocumentDownloadView.as_view()),
    path("documents/<uuid:document_id>/blob", x.DocumentBlobView.as_view()),
    # Support & reports
    path("complaints", x.ComplaintsView.as_view()),
    path("complaints/<uuid:complaint_id>/resolve", x.ComplaintResolveView.as_view()),
    path("reports/exports", x.ExportsView.as_view()),
    path("reports/exports/<uuid:job_id>", x.ExportDetailView.as_view()),
    # Sync
    path("sync/leases", x.SyncLeaseView.as_view()),
    path("sync/bootstrap", x.SyncBootstrapView.as_view()),
    path("sync/changes", x.SyncChangesView.as_view()),
    path("sync/operations", x.SyncOperationsView.as_view()),
    path("sync/operations/<uuid:operation_id>", x.SyncOperationDetailView.as_view()),
    # Health
    path("health/live", x.HealthLiveView.as_view()),
    path("health/ready", x.HealthReadyView.as_view()),
]
