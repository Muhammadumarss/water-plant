from django.utils import timezone

from common.serialization import to_jsonable

from .models import AuditEvent

_REDACT = {"password", "token", "refresh", "access", "secret", "otp", "code"}


def _safe(changes):
    if isinstance(changes, dict):
        return {k: ("[redacted]" if k.lower() in _REDACT else _safe(v)) for k, v in changes.items()}
    if isinstance(changes, list):
        return [_safe(v) for v in changes]
    return changes


def record(ctx, action: str, target_type: str, target_id, *, reason: str = "", changes=None,
           operation_id=None, outcome: str = "SUCCESS", occurred_at=None) -> AuditEvent:
    return AuditEvent.objects.create(
        business=ctx.business, actor=ctx.user, device=ctx.device, action=action, target_type=target_type,
        target_id=str(target_id), outcome=outcome, reason=reason, changes=to_jsonable(_safe(changes or {})),
        request_id=ctx.request_id, operation_id=operation_id, occurred_at=occurred_at or timezone.now(),
    )
