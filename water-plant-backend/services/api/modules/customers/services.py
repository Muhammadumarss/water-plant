from django.db.models import Q

from common.errors import NotFound, ValidationFailed
from common.money import money
from common.phone import normalize_phone

from .models import Customer, CustomerAccountLink, CustomerAddress, CustomerContact, CustomerCreditAccount


def create_customer(ctx, *, display_name, customer_type="INDIVIDUAL", phones=(), address=None,
                    credit_limit="0", credit_terms_days=0, notes="") -> Customer:
    if credit_limit and money(credit_limit) > 0:
        ctx.require("credit.approve")
    customer = Customer.objects.create(
        business=ctx.business, display_name=display_name.strip(), customer_type=customer_type,
        credit_limit=money(credit_limit or 0), credit_terms_days=credit_terms_days, notes=notes,
        created_by=ctx.user,
    )
    CustomerCreditAccount.objects.create(business=ctx.business, customer=customer)
    for index, phone in enumerate(phones):
        normalized = normalize_phone(phone)
        if not normalized:
            raise ValidationFailed(fields={f"phones.{index}": ["Invalid phone number."]})
        CustomerContact.objects.create(business=ctx.business, customer=customer, phone_raw=phone,
                                       phone_normalized=normalized, is_primary=index == 0)
    if address:
        CustomerAddress.objects.create(business=ctx.business, customer=customer, is_default=True, **address)
    return customer


def get_customer(ctx, customer_id) -> Customer:
    customer = Customer.objects.filter(business=ctx.business, id=customer_id).first()
    if customer is None or not can_view_customer(ctx, customer):
        raise NotFound()
    return customer


def can_view_customer(ctx, customer) -> bool:
    if customer.business_id != ctx.business_id:
        return False
    if ctx.is_customer:
        return CustomerAccountLink.objects.filter(
            user=ctx.user, customer=customer, revoked_at__isnull=True
        ).exists()
    return ctx.has("customer.view") or ctx.has("sale.counter")


def scoped_customers(ctx):
    qs = Customer.objects.filter(business=ctx.business)
    if ctx.is_customer:
        return qs.filter(account_links__user=ctx.user, account_links__revoked_at__isnull=True).distinct()
    ctx.require("customer.view", "sale.counter", "delivery.assigned")
    if ctx.role == "RIDER":
        # Riders only see customers on their assigned stops.
        return qs.filter(orders__delivery_stops__run__rider=ctx.user).distinct()
    return qs


def search_customers(ctx, query: str | None):
    qs = scoped_customers(ctx)
    if query:
        normalized = normalize_phone(query)
        condition = Q(display_name__icontains=query)
        if len(normalized) >= 6:
            condition |= Q(contacts__phone_normalized__startswith=normalized)
        qs = qs.filter(condition).distinct()
    return qs.order_by("display_name")


def link_account(ctx, *, customer, user, relationship="account_holder") -> CustomerAccountLink:
    ctx.require("customer.manage")
    if user.contact_verified_at is None:
        raise ValidationFailed("The portal user must verify their contact before linking.",
                               fields={"user_id": ["Contact not verified."]})
    link, _ = CustomerAccountLink.objects.get_or_create(
        business=ctx.business, user=user, customer=customer, revoked_at=None,
        defaults={"relationship": relationship, "linked_by": ctx.user},
    )
    return link


def lock_credit_account(customer) -> CustomerCreditAccount:
    account = CustomerCreditAccount.objects.select_for_update().filter(customer=customer).first()
    if account is None:
        CustomerCreditAccount.objects.get_or_create(business_id=customer.business_id, customer=customer)
        account = CustomerCreditAccount.objects.select_for_update().get(customer=customer)
    return account
