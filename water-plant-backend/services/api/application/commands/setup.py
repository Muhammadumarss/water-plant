"""Provision the single v1 business, and optional demo data for development."""

import uuid
from dataclasses import dataclass

from django.db import transaction

from common.context import RequestContext
from modules.catalog.models import ContainerType, Product
from modules.finance.models import CashAccount
from modules.identity.models import Device, Membership, User
from modules.identity.policies import capabilities_for
from modules.inventory.models import StockItem, StockLocation
from modules.inventory.services import virtual_location
from modules.organization.models import Branch, Business, ServiceArea
from modules.synchronization.models import SyncClock


def owner_context(business, owner) -> RequestContext:
    branch = Branch.objects.filter(business=business).order_by("created_at").first()
    return RequestContext(business=business, branch=branch, user=owner, role="OWNER",
                          capabilities=capabilities_for("OWNER"), request_id="setup")


@transaction.atomic
def bootstrap_business(*, name: str, owner_username: str, owner_password: str, branch_name="Main Plant") -> Business:
    if Business.objects.exists():
        raise RuntimeError("V1 provisions exactly one business; one already exists.")
    business = Business.objects.create(name=name)
    Branch.objects.create(business=business, name=branch_name, code="MAIN")
    owner = User.objects.create_user(owner_username, owner_password, full_name="Owner")
    Membership.objects.create(business=business, user=owner, role="OWNER")
    StockLocation.objects.create(business=business, kind=StockLocation.Kind.PLANT, name="Plant store")
    for kind in StockLocation.Virtual.values:
        virtual_location(business, kind)
    StockItem.objects.create(business=business, code="WATER", name="Treated water", kind="TREATED_WATER",
                             base_unit="LITRE")
    SyncClock.objects.create(business=business)
    return business


@dataclass
class Demo:
    business: Business
    owner: User
    cashier: User
    rider: User
    operator: User
    till: StockLocation
    till_cash: CashAccount
    bottle: ContainerType
    product_bottle: Product
    product_refill: Product
    device: Device


def seed_demo(password="demo-password-123") -> Demo:
    """Development seed. Uses the real commands so stock enters through the ledger."""
    from application.commands import plant
    from application.commands.master import create_customer_cmd
    from modules.catalog.services import publish_deposit_policy, publish_price

    business = Business.objects.first() or bootstrap_business(name="Demo Water Plant", owner_username="owner",
                                                             owner_password=password)
    owner = Membership.objects.get(business=business, role="OWNER").user
    ctx = owner_context(business, owner)

    def staff(username, role, extra=()):
        user = User.objects.filter(username=username).first() or User.objects.create_user(
            username, password, full_name=username.title())
        Membership.objects.get_or_create(business=business, user=user,
                                         defaults={"role": role, "extra_capabilities": list(extra)})
        return user

    cashier = staff("cashier", "CASHIER")
    rider = staff("rider", "RIDER")
    operator = staff("operator", "OPERATOR", ["quality.release"])

    with transaction.atomic():
        till = StockLocation.objects.create(business=business, kind=StockLocation.Kind.TILL, name="Counter till 1")
        till_cash = CashAccount.objects.create(business=business, kind=CashAccount.Kind.TILL, name="Counter till 1",
                                               stock_location=till)
        CashAccount.objects.create(business=business, kind=CashAccount.Kind.SAFE, name="Plant safe")
        device = Device.objects.create(business=business, client_identifier="till-1", name="Counter PC",
                                       assigned_till=till, offline_enabled=True, enrolled_by=owner)
        bottle_item = StockItem.objects.create(business=business, code="BTL19", name="19L bottle",
                                               kind="REUSABLE_CONTAINER", base_unit="BOTTLE")
        StockItem.objects.create(business=business, code="CAP", name="Bottle cap", kind="CONSUMABLE",
                                 base_unit="PIECE")
        bottle = ContainerType.objects.create(business=business, code="19L", name="19L bottle", capacity_litres=19,
                                              stock_item=bottle_item)
        product_bottle = Product.objects.create(business=business, code="W19", name="19L water bottle",
                                                name_ur="19 لیٹر پانی", sale_mode="RETURNABLE_BOTTLE",
                                                container_type=bottle, water_litres_per_unit=19)
        product_refill = Product.objects.create(business=business, code="R19", name="19L refill (own bottle)",
                                                sale_mode="REFILL", unit="refill", water_litres_per_unit=19)
        publish_price(ctx, product=product_bottle, amount="100.00")
        publish_price(ctx, product=product_refill, amount="80.00")
        publish_deposit_policy(ctx, container_type=bottle, amount_per_unit="500.00")
        ServiceArea.objects.create(business=business, name="Model Town", delivery_fee=0)

    # Opening stock of clean empty bottles, through the ledger.
    opening = virtual_location(business, StockLocation.Virtual.OPENING)
    plant_loc = StockLocation.objects.get(business=business, kind=StockLocation.Kind.PLANT)
    from modules.inventory.services import BucketKey, MovementLine, bucket_for, lock_buckets, post_movements

    with transaction.atomic():
        src = bucket_for(business, BucketKey(bottle_item.id, opening.id, "CLEAN_EMPTY"))
        tgt = bucket_for(business, BucketKey(bottle_item.id, plant_loc.id, "CLEAN_EMPTY"))
        locked = lock_buckets([src.id, tgt.id])
        post_movements(ctx, operation_id=uuid.uuid4(), action="OPENING", source_type="opening",
                       lines=[MovementLine(bottle_item, locked.get(src), locked.get(tgt), 200, "OPENING")])

    batch, _ = plant.record_batch(ctx, {"operation_id": str(uuid.uuid4()), "batch_code": "B-0001",
                                        "input_litres": "5000", "output_litres": "4800"})
    plant.record_filling_run(ctx, {"operation_id": str(uuid.uuid4()), "batch_id": batch["batch_id"],
                                   "container_type_id": str(bottle.id), "filled_quantity": "100"})
    sample = plant.record_sample(ctx, {"batch_id": batch["batch_id"]})
    plant.record_results(ctx, sample.id, {"results": [
        {"parameter": "TDS", "value": "120", "unit": "mg/L", "within_limits": True},
        {"parameter": "pH", "value": "7.2", "unit": "pH", "within_limits": True}]})
    plant.release_batch(ctx, batch["batch_id"], {"evidence": "Demo lab results within limits"})
    create_customer_cmd(ctx, {"display_name": "Ahmed Household", "phones": ["0300-1234567"],
                              "address": {"line1": "House 12, Street 4", "area": "Model Town"}})
    return Demo(business, owner, cashier, rider, operator, till, till_cash, bottle, product_bottle, product_refill,
                device)


