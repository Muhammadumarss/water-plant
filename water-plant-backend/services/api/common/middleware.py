import contextvars
import logging
import re
import uuid

_request_id = contextvars.ContextVar("request_id", default="-")
_VALID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def current_request_id() -> str:
    return _request_id.get()


class RequestIdMiddleware:
    """Assigns a correlation id to every request (X-Request-ID)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _VALID.match(incoming or "") else f"req_{uuid.uuid4().hex[:20]}"
        request.request_id = request_id
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response["X-Request-ID"] = request_id
        return response


class RequestIdLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = _request_id.get()
        return True
