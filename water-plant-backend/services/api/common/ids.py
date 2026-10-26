import uuid

from .errors import ValidationFailed


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def parse_uuid(value, field: str = "id") -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationFailed(fields={field: ["Must be a valid UUID."]}) from exc
