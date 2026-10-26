"""Sales, payments, invoices, credit notes, refunds, cash shifts, expenses."""

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from application.commands import money as money_cmd
from application.commands.sales import complete_sale
from common.errors import NotFound
from common.ids import parse_uuid
from modules.billing.models import Invoice
from modules.customers.services import can_view_customer

from .base import BaseView, ListMixin, command_response, paginate
from .serializers import CompleteSaleIn, InvoiceOut, ReceivePaymentIn

IDEMPOTENCY_HEADER = OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True,
                                      description="Must equal operation_id.")


class CompleteSaleView(BaseView):
    request_serializer = CompleteSaleIn

    @extend_schema(request=CompleteSaleIn, parameters=[IDEMPOTENCY_HEADER],
                   description="Complete an immediate counter sale as ONE transaction.")
    def post(self, request):
        result, status = complete_sale(self.ctx(request), self.data(request),
                                       idempotency_key=self.idempotency_key(request))
        return command_response(result, status)


class ReceivePaymentView(BaseView):
    request_serializer = ReceivePaymentIn

    @extend_schema(request=ReceivePaymentIn, parameters=[IDEMPOTENCY_HEADER])
    def post(self, request):
        result, status = money_cmd.receive_payment(self.ctx(request), self.data(request),
                                                   idempotency_key=self.idempotency_key(request))
        return command_response(result, status)


class InvoicesView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        qs = Invoice.objects.filter(business=ctx.business).select_related("balance").prefetch_related("lines")
        if ctx.is_customer:
            qs = qs.filter(customer__account_links__user=ctx.user, customer__account_links__revoked_at__isnull=True)
        else:
            ctx.require("customer.view", "report.financial")
        if request.query_params.get("customer_id"):
            qs = qs.filter(customer_id=parse_uuid(request.query_params["customer_id"], "customer_id"))
        return paginate(self, request, qs.order_by("-issued_at"), InvoiceOut)


class InvoiceDetailView(BaseView):
    def get(self, request, invoice_id):
        ctx = self.ctx(request)
        invoice = Invoice.objects.filter(business=ctx.business, id=parse_uuid(invoice_id)).first()
        if invoice is None or not can_view_customer(ctx, invoice.customer):
            raise NotFound()
        return Response(InvoiceOut(invoice).data)


class CreditNoteView(BaseView):
    @extend_schema(parameters=[IDEMPOTENCY_HEADER])
    def post(self, request, invoice_id):
        result, status = money_cmd.issue_credit_note(self.ctx(request), invoice_id, self.data(request),
                                                     idempotency_key=self.idempotency_key(request))
        return command_response(result, status)


class RefundView(BaseView):
    @extend_schema(parameters=[IDEMPOTENCY_HEADER])
    def post(self, request):
        result, status = money_cmd.refund_advance(self.ctx(request), self.data(request),
                                                  idempotency_key=self.idempotency_key(request))
        return command_response(result, status)


class DepositRefundView(BaseView):
    @extend_schema(parameters=[IDEMPOTENCY_HEADER])
    def post(self, request):
        result, status = money_cmd.refund_deposit(self.ctx(request), self.data(request),
                                                  idempotency_key=self.idempotency_key(request))
        return command_response(result, status)


def _shift_payload(shift):
    return {"id": str(shift.id), "cash_account_id": str(shift.cash_account_id), "status": shift.status,
            "opened_at": shift.opened_at.isoformat(), "opening_float": str(shift.opening_float),
            "expected_close": str(shift.expected_close) if shift.expected_close is not None else None,
            "counted_close": str(shift.counted_close) if shift.counted_close is not None else None,
            "variance": str(shift.variance) if shift.variance is not None else None}


class OpenShiftView(BaseView):
    def post(self, request):
        return Response(_shift_payload(money_cmd.open_shift(self.ctx(request), self.data(request))), status=201)


class CloseShiftView(BaseView):
    def post(self, request, shift_id):
        return Response(_shift_payload(money_cmd.close_shift(self.ctx(request), shift_id, self.data(request))))


class ApproveShiftView(BaseView):
    def post(self, request, shift_id):
        return Response(_shift_payload(money_cmd.approve_shift(self.ctx(request), shift_id)))


class ExpenseView(BaseView):
    @extend_schema(parameters=[IDEMPOTENCY_HEADER])
    def post(self, request):
        result, status = money_cmd.record_expense(self.ctx(request), self.data(request),
                                                  idempotency_key=self.idempotency_key(request))
        return command_response(result, status)
