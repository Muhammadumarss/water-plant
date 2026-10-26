"""Public catalog, customers, catalog admin, orders and recurring schedules."""

from django.db import transaction
from rest_framework.response import Response

from application.commands import master, orders
from application.queries.account_summary import account_summary
from common.context import RequestContext
from common.errors import NotFound, ValidationFailed
from common.ids import parse_uuid
from common.money import quantity
from modules.catalog.models import ContainerType, Product
from modules.customers.models import CustomerAccountLink
from modules.customers.services import get_customer, link_account, search_customers
from modules.identity.models import User
from modules.orders.models import Order, RecurringSchedule, RecurringScheduleLine
from modules.organization.models import Business, ServiceArea

from .base import BaseView, ListMixin, PublicView, paginate
from .serializers import (
    CustomerOut,
    OrderOut,
    ProductOut,
    PublicProductOut,
    RecurringScheduleOut,
    ServiceAreaOut,
)


def public_context(request) -> RequestContext:
    business = Business.objects.filter(is_active=True).order_by("created_at").first()
    if business is None:
        raise NotFound()
    from modules.organization.models import Branch

    branch = Branch.objects.filter(business=business, is_active=True).order_by("created_at").first()
    return RequestContext(business=business, branch=branch, user=None, role="PUBLIC",
                          request_id=getattr(request, "request_id", "-"))


# ---------------------------------------------------------------- public

class PublicProductsView(PublicView):
    def get(self, request):
        ctx = public_context(request)
        products = Product.objects.filter(business=ctx.business, is_active=True, is_public=True).order_by("sort_order")
        return Response(PublicProductOut(products, many=True).data)


class PublicServiceAreasView(PublicView):
    def get(self, request):
        ctx = public_context(request)
        areas = ServiceArea.objects.filter(business=ctx.business, is_active=True, is_public=True).order_by("name")
        return Response(ServiceAreaOut(areas, many=True).data)


class PublicOrderRequestView(PublicView):
    def post(self, request):
        order = orders.create_public_order_request(public_context(request), self.data(request))
        # Generic response: no customer data is disclosed to an anonymous caller.
        return Response({"order_request_id": str(order.id), "status": order.status}, status=201)


# ---------------------------------------------------------------- customers

class CustomersView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        qs = search_customers(ctx, request.query_params.get("q")).prefetch_related("contacts", "addresses")
        return paginate(self, request, qs, CustomerOut)

    def post(self, request):
        customer = master.create_customer_cmd(self.ctx(request), self.data(request))
        return Response(CustomerOut(customer).data, status=201)


class CustomerDetailView(BaseView):
    def get(self, request, customer_id):
        customer = get_customer(self.ctx(request), parse_uuid(customer_id))
        response = Response(CustomerOut(customer).data)
        response["ETag"] = f'"{customer.version}"'
        return response

    def patch(self, request, customer_id):
        ctx = self.ctx(request)
        data = self.data(request)
        customer = get_customer(ctx, parse_uuid(customer_id))
        customer = master.update_customer_cmd(ctx, customer, self.expected_version(request, data), data)
        return Response(CustomerOut(customer).data)


class AccountSummaryView(BaseView):
    def get(self, request, customer_id):
        return Response(account_summary(get_customer(self.ctx(request), parse_uuid(customer_id))))


class AccountLinkView(BaseView):
    def post(self, request, customer_id):
        ctx = self.ctx(request)
        customer = get_customer(ctx, parse_uuid(customer_id))
        user = User.objects.filter(id=parse_uuid(request.data.get("user_id"), "user_id")).first()
        if user is None:
            raise NotFound("User not found.")
        link = link_account(ctx, customer=customer, user=user, relationship=request.data.get("relationship",
                                                                                             "account_holder"))
        return Response({"link_id": str(link.id)}, status=201)


# ---------------------------------------------------------------- catalog admin

class ProductsView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        return paginate(self, request, Product.objects.filter(business=ctx.business).order_by("sort_order"),
                        ProductOut)

    def post(self, request):
        return Response(ProductOut(master.create_product_cmd(self.ctx(request), self.data(request))).data, status=201)


class ProductPriceView(BaseView):
    def post(self, request, product_id):
        ctx = self.ctx(request)
        product = Product.objects.filter(business=ctx.business, id=parse_uuid(product_id)).first()
        if product is None:
            raise NotFound()
        rule = master.publish_price_cmd(ctx, product, self.data(request))
        return Response({"price_rule_id": str(rule.id), "version": rule.version, "amount": str(rule.amount)},
                        status=201)


class DepositPolicyView(BaseView):
    def post(self, request, container_type_id):
        ctx = self.ctx(request)
        ct = ContainerType.objects.filter(business=ctx.business, id=parse_uuid(container_type_id)).first()
        if ct is None:
            raise NotFound()
        policy = master.publish_deposit_cmd(ctx, ct, self.data(request))
        return Response({"deposit_policy_id": str(policy.id), "version": policy.version}, status=201)


# ---------------------------------------------------------------- orders

def scoped_orders(ctx):
    qs = Order.objects.filter(business=ctx.business).prefetch_related("lines")
    if ctx.is_customer:
        return qs.filter(customer__account_links__user=ctx.user, customer__account_links__revoked_at__isnull=True)
    ctx.require("order.manage", "order.view")
    return qs


class OrdersView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        qs = scoped_orders(ctx)
        status = request.query_params.get("status")
        if status:
            qs = qs.filter(status=status.upper())
        return paginate(self, request, qs.order_by("-created_at"), OrderOut)

    def post(self, request):
        order = orders.create_customer_order(self.ctx(request), self.data(request))
        return Response(OrderOut(order).data, status=201)


class MyOrdersView(OrdersView):
    def get(self, request):
        ctx = self.ctx(request)
        if not ctx.is_customer:
            raise NotFound()
        return super().get(request)


class OrderActionView(BaseView):
    action = None
    handlers = {"accept": orders.accept_order, "reject": orders.reject_order, "reserve": orders.reserve_order,
                "cancel": orders.cancel_order}

    def post(self, request, order_id):
        order = self.handlers[self.action](self.ctx(request), order_id, self.data(request))
        return Response(OrderOut(order).data)


# ---------------------------------------------------------------- recurring schedules

class SchedulesView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        qs = RecurringSchedule.objects.filter(business=ctx.business)
        if ctx.is_customer:
            qs = qs.filter(customer__account_links__user=ctx.user, customer__account_links__revoked_at__isnull=True)
        else:
            ctx.require("order.manage")
        return paginate(self, request, qs.order_by("next_occurrence_date"), RecurringScheduleOut)

    def post(self, request):
        ctx = self.ctx(request)
        data = self.data(request)
        customer = get_customer(ctx, parse_uuid(data["customer_id"], "customer_id"))
        if not ctx.is_customer:
            ctx.require("order.manage")
        if data.get("recurrence") not in RecurringSchedule.Recurrence.values:
            raise ValidationFailed(fields={"recurrence": ["WEEKLY or EVERY_N_DAYS."]})
        with transaction.atomic():
            schedule = RecurringSchedule.objects.create(
                business=ctx.business, customer=customer, recurrence=data["recurrence"],
                weekdays=data.get("weekdays", []), interval_days=data.get("interval_days"),
                delivery_window_id=data.get("delivery_window_id"), start_date=data["start_date"],
                next_occurrence_date=data["start_date"], created_by=ctx.user)
            for raw in data.get("lines", []):
                product = Product.objects.get(business=ctx.business, id=parse_uuid(raw["product_id"]))
                RecurringScheduleLine.objects.create(business=ctx.business, schedule=schedule, product=product,
                                                     quantity=quantity(raw["quantity"]))
        return Response(RecurringScheduleOut(schedule).data, status=201)


class SchedulePauseView(BaseView):
    def post(self, request, schedule_id):
        ctx = self.ctx(request)
        schedule = RecurringSchedule.objects.filter(business=ctx.business, id=parse_uuid(schedule_id)).first()
        if schedule is None:
            raise NotFound()
        if ctx.is_customer:
            if not CustomerAccountLink.objects.filter(user=ctx.user, customer=schedule.customer,
                                                      revoked_at__isnull=True).exists():
                raise NotFound()
        else:
            ctx.require("order.manage")
        data = self.data(request)
        if not data.get("pause_from"):
            raise ValidationFailed(fields={"pause_from": ["Required."]})
        from common.db import update_versioned

        schedule = update_versioned(schedule, self.expected_version(request, data), pause_from=data["pause_from"],
                                    pause_until=data.get("pause_until"))
        return Response(RecurringScheduleOut(schedule).data)
