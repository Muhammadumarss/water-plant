"""HTTP interface helpers: parse input, call a command/query, shape the response.
No business rules live here."""

from rest_framework import status as http
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from common.errors import ValidationFailed
from modules.identity.context import context_from_request


class BaseView(APIView):
    request_serializer = None

    def ctx(self, request):
        return context_from_request(request)

    def data(self, request) -> dict:
        if self.request_serializer is not None:
            serializer = self.request_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            return _plain(serializer.validated_data)
        if not isinstance(request.data, dict):
            raise ValidationFailed("Expected a JSON object.")
        return dict(request.data)

    @staticmethod
    def idempotency_key(request):
        return request.headers.get("Idempotency-Key")

    @staticmethod
    def expected_version(request, data=None):
        raw = request.headers.get("If-Match") or (data or {}).get("expected_version")
        if raw in (None, ""):
            raise ValidationFailed("expected_version or If-Match is required.",
                                   fields={"expected_version": ["Required for edits."]})
        try:
            return int(str(raw).strip('"W/ '))
        except ValueError:
            raise ValidationFailed(fields={"expected_version": ["Must be an integer."]}) from None

    def handle_exception(self, exc):
        # Commands read required request fields with data["name"]; a missing one is a 400, not a 500.
        if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
            exc = ValidationFailed(fields={exc.args[0]: ["This field is required."]})
        return super().handle_exception(exc)


class PublicView(BaseView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "public"


def command_response(result, status_code):
    return Response(result, status=http.HTTP_201_CREATED if status_code == 201 else http.HTTP_200_OK)


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def paginate(view, request, queryset, serializer_cls):
    page = view.paginator.paginate_queryset(queryset, request, view=view)
    return view.paginator.get_paginated_response(serializer_cls(page, many=True).data)


class ListMixin:
    @property
    def paginator(self):
        if not hasattr(self, "_paginator"):
            from common.pagination import DefaultPagination

            self._paginator = DefaultPagination()
        return self._paginator
