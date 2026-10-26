"""Request/response serializers. Requests validate SHAPE only; prices, stock
eligibility and permissions are decided by commands on the server."""

from rest_framework import serializers

from modules.billing.models import Invoice, InvoiceLine
from modules.catalog.models import Product
from modules.customers.models import Customer, CustomerAddress, CustomerContact
from modules.fulfillment.models import DeliveryRun, DeliveryStop
from modules.inventory.models import StockBucket, StockMovement
from modules.notifications.models import Notification
from modules.orders.models import Order, OrderLine, RecurringSchedule
from modules.organization.models import ServiceArea
from modules.plant_care.models import MaintenanceTask
from modules.production.models import ProductionBatch
from modules.support.models import Complaint

# ---------------------------------------------------------------- requests

class SaleLineIn(serializers.Serializer):
    product_id = serializers.UUIDField()
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=0)
    accepted_price_version = serializers.IntegerField(required=False, allow_null=True)


class BottleReturnIn(serializers.Serializer):
    container_type_id = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)
    damaged_quantity = serializers.IntegerField(min_value=0, required=False, default=0)


class PaymentIn(serializers.Serializer):
    method = serializers.ChoiceField(choices=["cash", "CASH", "bank_transfer", "BANK_TRANSFER",
                                              "mobile_wallet", "MOBILE_WALLET"])
    tendered_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    apply_amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    currency = serializers.CharField(default="PKR")
    reference = serializers.CharField(required=False, allow_blank=True, default="")


class CompleteSaleIn(serializers.Serializer):
    operation_id = serializers.UUIDField()
    device_id = serializers.UUIDField(required=False, allow_null=True)
    customer_id = serializers.UUIDField()
    cash_shift_id = serializers.UUIDField()
    lines = SaleLineIn(many=True, required=False, default=list)
    bottle_returns = BottleReturnIn(many=True, required=False, default=list)
    payment = PaymentIn(required=False, allow_null=True)
    unsecured_bottle_loan = serializers.BooleanField(required=False, default=False)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        return _stringify(value)


class ReceivePaymentIn(serializers.Serializer):
    operation_id = serializers.UUIDField()
    customer_id = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    method = serializers.CharField(default="CASH")
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    cash_shift_id = serializers.UUIDField(required=False, allow_null=True)
    cash_account_id = serializers.UUIDField(required=False, allow_null=True)
    allocations = serializers.ListField(child=serializers.DictField(), required=False, default=list)

    def to_internal_value(self, data):
        return _stringify(super().to_internal_value(data))


def _stringify(value):
    """Normalize Decimals/UUIDs to strings so idempotency hashing is canonical."""
    if isinstance(value, dict):
        return {k: _stringify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


# ---------------------------------------------------------------- responses

class ContactOut(serializers.ModelSerializer):
    class Meta:
        model = CustomerContact
        fields = ["id", "phone_normalized", "label", "is_primary", "verified_at"]


class AddressOut(serializers.ModelSerializer):
    class Meta:
        model = CustomerAddress
        fields = ["id", "line1", "area", "landmark", "service_area", "is_default"]


class CustomerOut(serializers.ModelSerializer):
    contacts = ContactOut(many=True, read_only=True)
    addresses = AddressOut(many=True, read_only=True)

    class Meta:
        model = Customer
        fields = ["id", "display_name", "customer_type", "credit_limit", "credit_terms_days", "is_active",
                  "version", "contacts", "addresses", "created_at", "updated_at"]


class ProductOut(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "code", "name", "name_ur", "sale_mode", "unit", "container_type", "water_litres_per_unit",
                  "is_active", "version"]


class PublicProductOut(serializers.ModelSerializer):
    price = serializers.SerializerMethodField()
    price_version = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ["id", "name", "name_ur", "sale_mode", "unit", "price", "price_version"]

    def _quote(self, obj):
        from common.errors import DomainError
        from modules.catalog.services import current_price

        try:
            return current_price(obj)
        except DomainError:
            return None

    def get_price(self, obj):
        quote = self._quote(obj)
        return {"amount": str(quote.unit_price), "currency": "PKR"} if quote else None

    def get_price_version(self, obj):
        quote = self._quote(obj)
        return quote.version if quote else None


class ServiceAreaOut(serializers.ModelSerializer):
    class Meta:
        model = ServiceArea
        fields = ["id", "name", "description", "delivery_fee"]


class OrderLineOut(serializers.ModelSerializer):
    class Meta:
        model = OrderLine
        fields = ["id", "product", "quantity", "quote_unit_price", "quote_price_version", "fulfilled_quantity",
                  "cancelled_quantity"]


class OrderOut(serializers.ModelSerializer):
    lines = OrderLineOut(many=True, read_only=True)

    class Meta:
        model = Order
        fields = ["id", "customer", "channel", "requested_date", "delivery_window", "status", "version",
                  "address_snapshot", "notes", "lines", "created_at", "updated_at"]


class RecurringScheduleOut(serializers.ModelSerializer):
    class Meta:
        model = RecurringSchedule
        fields = ["id", "customer", "recurrence", "weekdays", "interval_days", "delivery_window", "start_date",
                  "pause_from", "pause_until", "next_occurrence_date", "is_active", "version"]


class InvoiceLineOut(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ["id", "product_name_snapshot", "unit_snapshot", "quantity", "unit_price", "discount", "tax_amount",
                  "line_total"]


class InvoiceOut(serializers.ModelSerializer):
    lines = InvoiceLineOut(many=True, read_only=True)
    status = serializers.CharField(source="balance.status", read_only=True)
    net_due = serializers.DecimalField(source="balance.net_due", max_digits=14, decimal_places=2, read_only=True)
    allocated = serializers.DecimalField(source="balance.allocated", max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = ["id", "document_number", "issued_at", "customer", "currency", "subtotal", "discount_total",
                  "tax_total", "total", "net_due", "allocated", "status", "lines"]


class BucketOut(serializers.ModelSerializer):
    item_code = serializers.CharField(source="item.code")
    location_name = serializers.CharField(source="location.name")
    location_kind = serializers.CharField(source="location.kind")
    batch_code = serializers.CharField(source="batch.batch_code", default=None)
    available = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)

    class Meta:
        model = StockBucket
        fields = ["id", "item", "item_code", "location", "location_name", "location_kind", "condition", "batch",
                  "batch_code", "on_hand", "reserved", "available"]


class MovementOut(serializers.ModelSerializer):
    action = serializers.CharField(source="group.action")

    class Meta:
        model = StockMovement
        fields = ["id", "group", "action", "item", "source_bucket", "target_bucket", "quantity", "movement_type",
                  "created_at"]


class BatchOut(serializers.ModelSerializer):
    class Meta:
        model = ProductionBatch
        fields = ["id", "batch_code", "production_date", "input_litres", "output_litres", "status", "released_at"]


class MaintenanceTaskOut(serializers.ModelSerializer):
    class Meta:
        model = MaintenanceTask
        fields = ["id", "equipment", "title", "kind", "interval_days", "due_date", "status", "version"]


class StopOut(serializers.ModelSerializer):
    order = OrderOut(read_only=True)

    class Meta:
        model = DeliveryStop
        fields = ["id", "run", "sequence", "status", "order"]


class RunOut(serializers.ModelSerializer):
    class Meta:
        model = DeliveryRun
        fields = ["id", "rider", "run_date", "status", "expected_cash", "counted_cash", "cash_variance", "version"]


class NotificationOut(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "customer", "purpose", "channel", "language", "state", "state_reason", "created_at",
                  "updated_at"]


class ComplaintOut(serializers.ModelSerializer):
    class Meta:
        model = Complaint
        fields = ["id", "customer", "order", "category", "description", "status", "assigned_to", "resolution",
                  "resolved_at", "created_at"]
