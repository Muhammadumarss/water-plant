"""Customer account summary: four separate positions, never one blended 'wallet'.

dues      = net issued invoice amounts after credit notes - effective invoice allocations
deposit   = sum of signed deposit entries
advance   = unallocated receipt/opening-credit funds remaining after refunds
bottles   = plant-owned container custody quantity (bottles, not rupees)"""

from django.utils import timezone

from common.money import money_payload, qty_str
from modules.billing.services import customer_dues
from modules.catalog.models import ContainerType
from modules.finance.services import customer_advance, deposit_balance
from modules.inventory.services import customer_bottle_holdings


def account_summary(customer) -> dict:
    currency = "PKR"
    holdings = customer_bottle_holdings(customer)
    containers = {ct.stock_item_id: ct for ct in ContainerType.objects.filter(business_id=customer.business_id)}
    bottles = [
        {"container_type_id": str(containers[item_id].id), "container_type": containers[item_id].name,
         "quantity": qty_str(qty), "unit": "bottle"}
        for item_id, qty in holdings.items() if item_id in containers
    ]
    return {
        "customer_id": str(customer.id),
        "as_of": timezone.now().isoformat(),
        "dues": money_payload(customer_dues(customer), currency),
        "deposit_balance": money_payload(deposit_balance(customer, currency), currency),
        "advance_credit": money_payload(customer_advance(customer), currency),
        "credit_limit": money_payload(customer.credit_limit, currency),
        "bottle_holdings": bottles,
    }
