"""Idempotent command execution (architecture section 11).

* Same key + same normalized payload -> stored result (HTTP 200).
* Same key + different payload -> 409 IDEMPOTENCY_CONFLICT.
* The key is claimed inside the posting transaction; concurrent duplicates
  serialize on the unique index. After a unique conflict we recover OUTSIDE
  the failed transaction and return the committed result.
* Bounded lock waits return a retryable response; in-progress requests are
  never "stolen" based on elapsed time."""

from django.db import IntegrityError, OperationalError

from common.db import command_transaction
from common.errors import (
    BusinessConflict,
    Forbidden,
    IdempotencyConflict,
    OperationInProgress,
    ValidationFailed,
)
from common.ids import parse_uuid
from common.serialization import payload_hash, to_jsonable

from .models import IdempotencyRecord

LOCK_TIMEOUT_CODES = {"55P03", "57014", "40P01", "40001"}


def _find(ctx, operation_id):
    return IdempotencyRecord.objects.filter(business=ctx.business, client_id=ctx.client_id,
                                            operation_id=operation_id).first()


def _replay(ctx, record, command_name, phash):
    if record.command_name != command_name or record.payload_hash != phash:
        raise IdempotencyConflict()
    # Re-check authorization before disclosing a previous response.
    if record.actor_id != ctx.user_id and not ctx.is_owner:
        raise Forbidden()
    return record.response_snapshot, 200


def _pgcode(exc) -> str | None:
    cause = getattr(exc, "__cause__", None)
    return getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)


def execute(ctx, *, command_name: str, operation_id, payload: dict, handler, header_key=None):
    """Run handler(operation_id) exactly once per (business, client, operation_id).

    handler runs inside the transaction and returns a JSON-able dict, optionally
    with "_resource": (type, id)."""
    op = parse_uuid(operation_id, "operation_id")
    if header_key is not None and str(header_key).strip() != str(op):
        raise ValidationFailed("Idempotency-Key header must equal operation_id.",
                               fields={"Idempotency-Key": ["Must equal operation_id."]})
    phash = payload_hash({"command": command_name, "payload": payload})

    existing = _find(ctx, op)
    if existing is not None:
        return _replay(ctx, existing, command_name, phash)

    try:
        with command_transaction():
            record = IdempotencyRecord.objects.create(
                business=ctx.business, client_id=ctx.client_id, operation_id=op, command_name=command_name,
                payload_hash=phash, actor=ctx.user,
            )
            result = handler(op)
            resource = result.pop("_resource", None) if isinstance(result, dict) else None
            record.response_snapshot = to_jsonable(result)
            if resource:
                record.resource_type, record.resource_id = resource[0], resource[1]
            record.save(update_fields=["response_snapshot", "resource_type", "resource_id"])
        return record.response_snapshot, 201
    except IntegrityError as exc:
        existing = _find(ctx, op)
        if existing is not None:
            return _replay(ctx, existing, command_name, phash)
        raise BusinessConflict("The request conflicts with current records.", code="CONSTRAINT_VIOLATION") from exc
    except OperationalError as exc:
        if _pgcode(exc) in LOCK_TIMEOUT_CODES:
            raise OperationInProgress(retry_after=2) from exc
        raise
