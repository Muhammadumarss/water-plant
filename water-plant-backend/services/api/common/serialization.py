"""Canonical JSON for payload hashing and response snapshots."""

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal


def _default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Not serializable: {type(value)!r}")


def canonical_json(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_default)


def payload_hash(data) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def to_jsonable(data):
    return json.loads(canonical_json(data))
