"""Test adapter with scriptable outcomes."""

from .base import MessagingAdapter, NormalizedCallback, ProviderError, SubmitResult


class FakeAdapter(MessagingAdapter):
    name = "fake"
    sent: list = []
    next_error: ProviderError | None = None
    status_answer: str | None = None

    def send_template(self, *, recipient, template_name, language, variables, idempotency_key, body):
        if FakeAdapter.next_error is not None:
            error, FakeAdapter.next_error = FakeAdapter.next_error, None
            raise error
        FakeAdapter.sent.append({"recipient": recipient, "key": idempotency_key, "body": body})
        return SubmitResult(provider_message_id=f"fake-{idempotency_key}")

    def query_status(self, provider_message_id):
        return FakeAdapter.status_answer

    def normalize_callback(self, payload):
        return [NormalizedCallback(event_identity=str(e["id"]), provider_message_id=e["message_id"],
                                   status=e["status"]) for e in payload.get("events", [])]

    @classmethod
    def reset(cls):
        cls.sent = []
        cls.next_error = None
        cls.status_answer = None
