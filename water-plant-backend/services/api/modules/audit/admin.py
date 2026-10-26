"""Django Admin is for authorized operational support only.
Posted ledgers and projections are read-only; corrections go through commands."""

from django.contrib import admin

from modules.audit.models import AuditEvent
from modules.billing.models import CreditNote, Invoice, InvoiceBalance, InvoiceLine
from modules.catalog.models import ContainerType, DepositPolicy, PriceRule, Product
from modules.customers.models import Customer
from modules.finance.models import (
    CashAccount,
    CashMovement,
    CashShift,
    DepositAccount,
    DepositEntry,
    PaymentReceipt,
    ReceiptAllocation,
    Refund,
)
from modules.fulfillment.models import DeliveryRun, Handover
from modules.identity.models import Device, Membership, User
from modules.inventory.models import StockBucket, StockItem, StockLocation, StockMovement, StockPostingGroup
from modules.jobs.models import EventDelivery, OutboxEvent
from modules.notifications.models import Notification
from modules.orders.models import Order
from modules.organization.models import Branch, Business, ServiceArea
from modules.production.models import ProductionBatch
from modules.synchronization.models import SyncOperation


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


READ_ONLY = [
    AuditEvent, Invoice, InvoiceLine, InvoiceBalance, CreditNote, PriceRule, DepositPolicy, PaymentReceipt,
    ReceiptAllocation, DepositAccount, DepositEntry, Refund, CashMovement, CashShift, Handover, StockBucket,
    StockMovement, StockPostingGroup, OutboxEvent, EventDelivery, Notification, SyncOperation, Order, DeliveryRun,
    ProductionBatch,
]
for model in READ_ONLY:
    admin.site.register(model, ReadOnlyAdmin)


class NoDeleteAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return False


for model in [Business, Branch, ServiceArea, Customer, Product, ContainerType, StockItem, StockLocation,
              CashAccount, Device, Membership]:
    admin.site.register(model, NoDeleteAdmin)


@admin.register(User)
class UserAdmin(NoDeleteAdmin):
    list_display = ["username", "full_name", "is_active", "is_staff"]
    exclude = ["password"]
