from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Max, Q
from django.utils import timezone

from common.errors import BusinessConflict, NotFound
from common.money import money

from .models import ContainerType, DepositPolicy, PriceRule, Product


@dataclass(frozen=True)
class Quote:
    product: Product
    unit_price: Decimal
    price_rule_id: object
    version: int


def get_product(ctx, product_id, *, active_only=True) -> Product:
    qs = Product.objects.filter(business=ctx.business, id=product_id).select_related("container_type")
    if active_only:
        qs = qs.filter(is_active=True)
    product = qs.first()
    if product is None:
        raise NotFound(f"Product {product_id} not found.")
    return product


def current_price(product: Product, customer=None, at=None) -> Quote:
    at = at or timezone.now()
    base = PriceRule.objects.filter(product=product, effective_from__lte=at).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gt=at)
    )
    rule = None
    if customer is not None:
        rule = base.filter(customer=customer).order_by("-effective_from", "-version").first()
    if rule is None:
        rule = base.filter(customer__isnull=True).order_by("-effective_from", "-version").first()
    if rule is None:
        raise BusinessConflict(f"No active price for {product.name}.", code="PRICE_NOT_CONFIGURED")
    return Quote(product=product, unit_price=rule.amount, price_rule_id=rule.id, version=rule.version)


def publish_price(ctx, *, product: Product, amount, customer=None, effective_from=None) -> PriceRule:
    ctx.require("price.publish")
    last = PriceRule.objects.filter(product=product, customer=customer).aggregate(v=Max("version"))["v"] or 0
    return PriceRule.objects.create(
        business=ctx.business, product=product, customer=customer, amount=money(amount),
        effective_from=effective_from or timezone.now(), version=last + 1, created_by=ctx.user,
    )


def current_deposit_policy(container_type: ContainerType, at=None) -> DepositPolicy | None:
    at = at or timezone.now()
    return (DepositPolicy.objects.filter(container_type=container_type, effective_from__lte=at)
            .order_by("-effective_from", "-version").first())


def publish_deposit_policy(ctx, *, container_type, amount_per_unit, effective_from=None) -> DepositPolicy:
    ctx.require("price.publish")
    last = DepositPolicy.objects.filter(container_type=container_type).aggregate(v=Max("version"))["v"] or 0
    return DepositPolicy.objects.create(
        business=ctx.business, container_type=container_type, amount_per_unit=money(amount_per_unit),
        effective_from=effective_from or timezone.now(), version=last + 1, created_by=ctx.user,
    )
