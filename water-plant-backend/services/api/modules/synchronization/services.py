"""Reference-data change feed for enrolled desktop clients."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from common.errors import ValidationFailed
from common.serialization import canonical_json, to_jsonable

from .models import ChangeRecord, SyncClock

CURSOR_SCHEMA = 1


def publish_changes(business, changes: list[tuple]) -> int:
    """Call LAST in a write transaction, after all domain locks.
    changes: [(dataset, entity_id, op, payload)]. Takes no domain locks afterwards."""
    if not changes:
        return 0
    SyncClock.objects.get_or_create(business=business)
    clock = SyncClock.objects.select_for_update().get(business=business)
    clock.revision += 1
    clock.save(update_fields=["revision"])
    ChangeRecord.objects.bulk_create([
        ChangeRecord(business=business, revision=clock.revision, seq=i, dataset=ds, entity_id=eid, op=op,
                     payload=to_jsonable(payload))
        for i, (ds, eid, op, payload) in enumerate(changes)
    ])
    return clock.revision


def _sign(data: bytes) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), data, hashlib.sha256).hexdigest()


def sign_snapshot(snapshot: dict) -> str:
    return _sign(canonical_json(snapshot).encode())


def make_cursor(device, revision: int, seq: int, datasets: list[str]) -> str:
    body = json.dumps({"d": str(device.id), "r": revision, "s": seq, "ds": sorted(datasets), "v": CURSOR_SCHEMA},
                      separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode() + "." + _sign(body)[:32]


def read_cursor(device, cursor: str, datasets: list[str]) -> tuple[int, int]:
    try:
        encoded, sig = cursor.rsplit(".", 1)
        body = base64.urlsafe_b64decode(encoded.encode())
        if not hmac.compare_digest(_sign(body)[:32], sig):
            raise ValueError
        data = json.loads(body)
    except (ValueError, TypeError):
        raise ValidationFailed("Invalid cursor.", code="CURSOR_INVALID") from None
    if data.get("d") != str(device.id) or data.get("v") != CURSOR_SCHEMA or data.get("ds") != sorted(datasets):
        raise ValidationFailed("Cursor scope changed; bootstrap again.", code="CURSOR_SCOPE_CHANGED")
    return int(data["r"]), int(data["s"])


def changes_since(business, revision: int, seq: int, datasets: list[str], limit: int = 500):
    window_start = timezone.now() - timedelta(days=settings.SYNC_CHANGE_WINDOW_DAYS)
    oldest = ChangeRecord.objects.filter(business=business).order_by("revision").first()
    if revision > 0 and oldest and oldest.revision > revision and oldest.created_at < window_start:
        raise ValidationFailed("Cursor expired; take a fresh snapshot.", code="CURSOR_EXPIRED")
    from django.db.models import Q

    rows = (ChangeRecord.objects.filter(business=business, dataset__in=datasets)
            .filter(Q(revision__gt=revision) | Q(revision=revision, seq__gt=seq))
            .order_by("revision", "seq")[:limit])
    return list(rows)
