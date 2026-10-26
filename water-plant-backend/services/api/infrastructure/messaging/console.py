"""Development adapter: logs messages instead of sending them."""

import hashlib
import logging

from .base import MessagingAdapter, NormalizedCallback, SubmitResult

log = logging.getLogger(__name__)


class ConsoleAdapter(MessagingAdapter):
    name = "console"

    def send_template(self, *, recipient, template_name, language, variables, idempotency_key, body):
        message_id = "console-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
        log.info("MESSAGE to=%s template=%s lang=%s id=%s body=%s", recipient[-4:].rjust(len(recipient), "*"),
                 template_name, language, message_id, body)
        return SubmitResult(provider_message_id=message_id)

    def query_status(self, provider_message_id):
        return "ACCEPTED_BY_PROVIDER"

    def normalize_callback(self, payload):
        return [NormalizedCallback(event_identity=str(e["id"]), provider_message_id=e["message_id"],
                                   status=e["status"]) for e in payload.get("events", [])]
