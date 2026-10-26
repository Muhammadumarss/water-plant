"""Master-data commands. Each publishes reference-data changes for desktop sync
as the LAST step of its transaction (sync clock is acquired after domain locks)."""

from django.db import transaction

from common.db import update_versioned
from common.errors import ValidationFailed
from common.money import money
from modules.audit.services import record as audit
from modules.catalog.models import ContainerType, Product
from modules.catalog.services import publish_deposit_policy, publish_price
from modules.customers.models import Customer
from modules.customers.services import create_customer
from modules.synchronization.services import publish_changes


def customer_payload(customer: Customer) -> dict:
    return {
        "id": str(customer.id), "display_name": customer.display_name, "customer_type": customer.customer_type,
        "is_active": customer.is_active, "version": customer.version,
        "phones": [c.phone_normalized for c in customer.contacts.all()],
    }


def product_payload(product: Product) -> dict:
    return {
        "id": str(product.id), "code": product.code, "name": product.name, "name_ur": product.name_ur,
        "sale_mode": product.sale_mode, "unit": product.unit, "is_active": product.is_active,
        "container_type_id": str(product.container_type_id) if product.container_type_id else None,
        "version": product.version,
    }


def create_customer_cmd(ctx, data) -> Customer:
    ctx.require("customer.manage")
    with transaction.atomic():
        customer = create_customer(
            ctx, display_name=data["display_name"], customer_type=data.get("customer_type", "INDIVIDUAL"),
            phones=data.get("phones", []), address=data.get("address"), credit_limit=data.get("credit_limit", "0"),
            credit_terms_days=data.get("credit_terms_days", 0), notes=data.get("notes", ""),
        )
        audit(ctx, "customer.created", "customer", customer.id)
        publish_changes(ctx.business, [("customers", customer.id, "UPSERT", customer_payload(customer))])
    return customer


def update_customer_cmd(ctx, customer: Customer, expected_version: int, data) -> Customer:
    ctx.require("customer.manage")
    allowed = {"display_name", "customer_type", "notes", "credit_terms_days", "credit_limit", "is_active"}
    changes = {k: v for k, v in data.items() if k in allowed}
    if "credit_limit" in changes:
        ctx.require("credit.approve")
        changes["credit_limit"] = money(changes["credit_limit"])
    if not changes:
        raise ValidationFailed("No editable fields supplied.")
    with transaction.atomic():
        customer = update_versioned(customer, expected_version, **changes)
        audit(ctx, "customer.updated", "customer", customer.id, changes=changes)
        op = "UPSERT" if customer.is_active else "DELETE"
        publish_changes(ctx.business, [("customers", customer.id, op, customer_payload(customer))])
    return customer


def create_product_cmd(ctx, data) -> Product:
    ctx.require("settings.manage")
    with transaction.atomic():
        product = Product.objects.create(
            business=ctx.business, code=data["code"], name=data["name"], name_ur=data.get("name_ur", ""),
            sale_mode=data["sale_mode"], unit=data.get("unit", "bottle"),
            container_type_id=data.get("container_type_id"), water_litres_per_unit=data.get("water_litres_per_unit", 0),
            stock_item_id=data.get("stock_item_id"), is_public=data.get("is_public", True), created_by=ctx.user,
        )
        if data.get("price") is not None:
            publish_price(ctx, product=product, amount=data["price"])
        audit(ctx, "product.created", "product", product.id)
        publish_changes(ctx.business, [("products", product.id, "UPSERT", product_payload(product))])
    return product


def publish_price_cmd(ctx, product: Product, data):
    with transaction.atomic():
        customer = None
        if data.get("customer_id"):
            customer = Customer.objects.get(business=ctx.business, id=data["customer_id"])
        rule = publish_price(ctx, product=product, amount=data["amount"], customer=customer,
                             effective_from=data.get("effective_from"))
        audit(ctx, "price.published", "product", product.id, changes={"amount": rule.amount, "version": rule.version})
        if customer is None:
            publish_changes(ctx.business, [("prices", product.id, "UPSERT", {
                "product_id": str(product.id), "amount": str(rule.amount), "version": rule.version,
                "effective_from": rule.effective_from.isoformat()})])
    return rule


def publish_deposit_cmd(ctx, container_type: ContainerType, data):
    with transaction.atomic():
        policy = publish_deposit_policy(ctx, container_type=container_type, amount_per_unit=data["amount_per_unit"],
                                        effective_from=data.get("effective_from"))
        audit(ctx, "deposit_policy.published", "container_type", container_type.id,
              changes={"amount": policy.amount_per_unit, "version": policy.version})
        publish_changes(ctx.business, [("deposit_policies", container_type.id, "UPSERT", {
            "container_type_id": str(container_type.id), "amount_per_unit": str(policy.amount_per_unit),
            "version": policy.version})])
    return policy
