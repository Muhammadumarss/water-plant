"""complete_sale: immediate counter sale as ONE transaction (architecture section 10)."""

from common.errors import NotFound, PriceChanged, ValidationFailed
from common.ids import parse_uuid
from common.money import money, money_str, quantity, require_currency
from modules.catalog.models import ContainerType
from modules.catalog.services import current_price, get_product
from modules.customers.models import Customer
from modules.finance.services import get_open_shift
from modules.fulfillment.models import Handover
from modules.idempotency.services import execute
from modules.inventory.services import plant_location

from .handover import HandoverRequest, PaymentInput, ReturnLine, SaleLine, post_handover


def _customer(ctx, customer_id) -> Customer:
    customer = Customer.objects.filter(business=ctx.business, id=parse_uuid(customer_id, "customer_id"),
                                       is_active=True).first()
    if customer is None:
        raise NotFound("Customer not found.")
    return customer


def build_sale_lines(ctx, customer, lines_data, *, price_snapshot=None) -> list[SaleLine]:
    """Prices are server decisions. A stale accepted_price_version returns PRICE_CHANGED."""
    lines, stale = [], []
    for index, raw in enumerate(lines_data):
        product = get_product(ctx, parse_uuid(raw["product_id"], f"lines.{index}.product_id"))
        qty = quantity(raw["quantity"])
        if qty <= 0:
            raise ValidationFailed(fields={f"lines.{index}.quantity": ["Must be greater than zero."]})
        if price_snapshot is not None:
            snap = price_snapshot.get(str(product.id))
            if snap is None:
                raise ValidationFailed(fields={f"lines.{index}.product_id": ["Not in the offline price snapshot."]})
            unit_price, version = money(snap["amount"]), snap["version"]
        else:
            quote = current_price(product, customer)
            unit_price, version = quote.unit_price, quote.version
            accepted = raw.get("accepted_price_version")
            if accepted is not None and int(accepted) != version:
                stale.append({"line": index, "product_id": str(product.id), "accepted_version": int(accepted),
                              "current_version": version, "current_unit_price": money_str(unit_price)})
        lines.append(SaleLine(product=product, quantity=qty, unit_price=unit_price, price_version=version))
    if stale:
        raise PriceChanged(details={"lines": stale})
    return lines


def build_returns(ctx, returns_data) -> list[ReturnLine]:
    out = []
    for index, raw in enumerate(returns_data or []):
        ct = ContainerType.objects.filter(business=ctx.business, id=parse_uuid(
            raw["container_type_id"], f"bottle_returns.{index}.container_type_id")).select_related("stock_item").first()
        if ct is None:
            raise NotFound("Container type not found.")
        out.append(ReturnLine(container_type=ct, quantity=quantity(raw["quantity"]),
                              damaged=quantity(raw.get("damaged_quantity", 0))))
    return out


def build_payment(raw) -> PaymentInput | None:
    if not raw:
        return None
    require_currency(raw.get("currency", "PKR"), "payment.currency")
    tendered = raw.get("tendered_amount")
    return PaymentInput(method=raw.get("method", "CASH").upper(), apply_amount=money(raw.get("apply_amount", "0")),
                        tendered_amount=money(tendered) if tendered not in (None, "") else None,
                        reference=raw.get("reference", "") or "")


def complete_sale(ctx, data: dict, *, idempotency_key=None):
    ctx.require("sale.counter")

    def handler(operation_id):
        shift = get_open_shift(ctx, parse_uuid(data["cash_shift_id"], "cash_shift_id"))
        customer = _customer(ctx, data["customer_id"])
        till = shift.cash_account.stock_location
        plant = plant_location(ctx.business)
        sources = [loc for loc in (till, plant) if loc is not None]
        req = HandoverRequest(
            customer=customer, channel=Handover.Channel.COUNTER,
            lines=build_sale_lines(ctx, customer, data.get("lines", [])),
            returns=build_returns(ctx, data.get("bottle_returns")),
            payment=build_payment(data.get("payment")),
            cash_account_id=shift.cash_account_id, shift=shift,
            source_locations=sources, return_location=till or plant,
            unsecured_loan=bool(data.get("unsecured_bottle_loan", False)),
        )
        return post_handover(ctx, operation_id, req)

    return execute(ctx, command_name="complete_sale", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)
