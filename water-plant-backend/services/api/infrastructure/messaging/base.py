"""Messaging adapter interface. Provider details stay out of sales/billing logic."""

import hashlib
import hmac
from dataclasses import dataclass
from importlib import import_module

from django.conf import settings


class ProviderError(Exception):
    def __init__(self, message, *, retryable: bool, uncertain: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain  # request may have been accepted (timeout after send)


@dataclass
class SubmitResult:
    provider_message_id: str
    status: str = "ACCEPTED_BY_PROVIDER"


@dataclass
class NormalizedCallback:
    event_identity: str
    provider_message_id: str
    status: str  # DELIVERED | READ | FAILED | ACCEPTED_BY_PROVIDER


class MessagingAdapter:
    name = "base"

    def send_template(self, *, recipient: str, template_name: str, language: str, variables: dict,
                      idempotency_key: str, body: str) -> SubmitResult:
        raise NotImplementedError

    def query_status(self, provider_message_id: str) -> str | None:
        """Return a NotificationState value, or None when the provider cannot tell."""
        raise NotImplementedError

    def verify_webhook(self, *, body: bytes, headers) -> bool:
        secret = settings.MESSAGING_WEBHOOK_SECRETS.get(self.name, "")
        signature = headers.get("X-Signature", "")
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def normalize_callback(self, payload: dict) -> list[NormalizedCallback]:
        raise NotImplementedError


def adapter_for(channel: str) -> MessagingAdapter:
    path = settings.MESSAGING_PROVIDERS[channel.lower()]
    module, _, cls = path.rpartition(".")
    return getattr(import_module(module), cls)()


def adapter_by_name(name: str) -> MessagingAdapter | None:
    for path in settings.MESSAGING_PROVIDERS.values():
        module, _, cls = path.rpartition(".")
        adapter = getattr(import_module(module), cls)()
        if adapter.name == name:
            return adapter
    return None
