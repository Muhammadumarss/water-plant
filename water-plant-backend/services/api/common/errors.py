"""Stable machine error codes and the API error envelope.

{"error": {"code", "message", "fields", "request_id", "retryable"}}
"""

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions as drf_exc
from rest_framework.response import Response

security_log = logging.getLogger("security")


class DomainError(Exception):
    code = "DOMAIN_ERROR"
    status = 409
    retryable = False
    default_message = "The request could not be completed."

    def __init__(self, message=None, *, code=None, fields=None, status=None, retryable=None,
                 details=None, retry_after=None):
        self.message = message or self.default_message
        if code:
            self.code = code
        if status:
            self.status = status
        if retryable is not None:
            self.retryable = retryable
        self.fields = fields or {}
        self.details = details
        self.retry_after = retry_after
        super().__init__(self.message)

    def as_payload(self, request_id=None):
        body = {
            "code": self.code,
            "message": self.message,
            "fields": self.fields,
            "request_id": request_id,
            "retryable": self.retryable,
        }
        if self.details is not None:
            body["details"] = self.details
        return {"error": body}


class ValidationFailed(DomainError):
    code = "VALIDATION_ERROR"
    status = 400
    default_message = "Some fields are invalid."


class Unauthenticated(DomainError):
    code = "UNAUTHENTICATED"
    status = 401
    default_message = "Authentication is required."


class Forbidden(DomainError):
    code = "FORBIDDEN"
    status = 403
    default_message = "You do not have permission for this action."


class NotFound(DomainError):
    code = "NOT_FOUND"
    status = 404
    default_message = "Not found."


class BusinessConflict(DomainError):
    code = "BUSINESS_STATE_CONFLICT"
    status = 409


class InsufficientStock(BusinessConflict):
    code = "INSUFFICIENT_STOCK"
    default_message = "Not enough eligible stock."


class BatchNotReleased(BusinessConflict):
    code = "BATCH_NOT_RELEASED"
    default_message = "Stock from a held or unreleased batch cannot be used."


class PriceChanged(BusinessConflict):
    code = "PRICE_CHANGED"
    default_message = "Prices changed. Please reconfirm."


class CreditLimitExceeded(BusinessConflict):
    code = "CREDIT_LIMIT_EXCEEDED"
    default_message = "This sale exceeds the customer's approved credit."


class DepositNotFunded(BusinessConflict):
    code = "DEPOSIT_NOT_FUNDED"
    default_message = "The required bottle deposit must be paid at handover."


class InsufficientFunds(BusinessConflict):
    code = "INSUFFICIENT_FUNDS"
    default_message = "The requested amount exceeds the available eligible funds."


class IdempotencyConflict(DomainError):
    code = "IDEMPOTENCY_CONFLICT"
    status = 409
    default_message = "This operation ID was already used with different input."


class OperationInProgress(DomainError):
    code = "OPERATION_IN_PROGRESS"
    status = 409
    retryable = True
    default_message = "The same operation is still being processed. Retry shortly."


class StaleVersion(DomainError):
    code = "STALE_VERSION"
    status = 412
    default_message = "This record was changed by someone else. Reload and try again."


class ImmutableRecordError(RuntimeError):
    """Programming error: posted ledger rows are append-only."""


def _flatten(detail, prefix=""):
    out = {}
    if isinstance(detail, dict):
        for key, value in detail.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(detail, list):
        if all(not isinstance(item, (dict, list)) for item in detail):
            out[prefix or "non_field_errors"] = [str(item) for item in detail]
        else:
            for index, value in enumerate(detail):
                if value:
                    out.update(_flatten(value, f"{prefix}.{index}" if prefix else str(index)))
    else:
        out[prefix or "non_field_errors"] = [str(detail)]
    return out


def _response(error: DomainError, request_id):
    response = Response(error.as_payload(request_id), status=error.status)
    if error.retry_after:
        response["Retry-After"] = str(error.retry_after)
    return response


def api_exception_handler(exc, context):
    request = context.get("request")
    request_id = getattr(request, "request_id", None)

    if isinstance(exc, DomainError):
        return _response(exc, request_id)
    if isinstance(exc, drf_exc.ValidationError):
        return _response(ValidationFailed(fields=_flatten(exc.detail)), request_id)
    if isinstance(exc, (drf_exc.NotAuthenticated, drf_exc.AuthenticationFailed)):
        security_log.info("authentication failure path=%s", getattr(request, "path", "?"))
        response = _response(Unauthenticated(str(exc.detail) if exc.detail else None), request_id)
        response["WWW-Authenticate"] = 'Bearer realm="api"'
        return response
    if isinstance(exc, (drf_exc.PermissionDenied, DjangoPermissionDenied)):
        return _response(Forbidden(), request_id)
    if isinstance(exc, (drf_exc.NotFound, Http404)):
        return _response(NotFound(), request_id)
    if isinstance(exc, drf_exc.Throttled):
        err = DomainError("Too many requests. Try again later.", code="THROTTLED", status=429,
                          retryable=True, retry_after=int(exc.wait or 1))
        return _response(err, request_id)
    if isinstance(exc, drf_exc.MethodNotAllowed):
        return _response(DomainError(code="METHOD_NOT_ALLOWED", status=405, message="Method not allowed."), request_id)
    if isinstance(exc, drf_exc.UnsupportedMediaType | drf_exc.ParseError):
        return _response(ValidationFailed(str(exc.detail)), request_id)
    if isinstance(exc, drf_exc.APIException):
        return _response(DomainError(str(exc.detail), code=exc.default_code.upper(), status=exc.status_code), request_id)
    return None
