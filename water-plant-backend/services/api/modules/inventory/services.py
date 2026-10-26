"""Inventory module services. The only code that writes stock tables.

Locking rule: callers resolve every bucket they need first, then lock them in
one query sorted by UUID (lock_buckets), then post."""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import F

from common.errors import BatchNotReleased, BusinessConflict, InsufficientStock, ValidationFailed
from common.money import quantity, require_whole

from .models import (
    Condition,
    Reservation,
    StockBucket,
    StockItem,
    StockLocation,
    StockMovement,
    StockPostingGroup,
)

RELEASED = "RELEASED"


@dataclass(frozen=True)
class BucketKey:
    item_id: object
    location_id: object
    condition: str
    batch_id: object = None


@dataclass
class MovementLine:
    item: StockItem
    source: StockBucket
    target: StockBucket
    quantity: Decimal
    movement_type: str
    consume_reservation: Reservation | None = None


@dataclass
class Allocation:
    bucket: StockBucket
    quantity: Decimal
    reservation: Reservation | None = None


@dataclass
class LockedBuckets:
    by_id: dict = field(default_factory=dict)

    def get(self, bucket):
        return self.by_id[bucket.id]


# ---------------------------------------------------------------- locations

def virtual_location(business, kind: str) -> StockLocation:
    location, _ = StockLocation.objects.get_or_create(
        business=business, kind=StockLocation.Kind.VIRTUAL, virtual_kind=kind,
        defaults={"name": f"Virtual: {kind.title()}"},
    )
    return location


def plant_location(business) -> StockLocation:
    location = StockLocation.objects.filter(business=business, kind=StockLocation.Kind.PLANT, is_active=True) \
        .order_by("created_at").first()
    if location is None:
        raise BusinessConflict("No plant stock location configured.", code="CONFIGURATION_MISSING")
    return location


def treated_water_item(business) -> StockItem:
    item = StockItem.objects.filter(business=business, kind=StockItem.Kind.TREATED_WATER, is_active=True) \
        .order_by("created_at").first()
    if item is None:
        raise BusinessConflict("No treated-water stock item configured.", code="CONFIGURATION_MISSING")
    return item


def customer_location(customer) -> StockLocation:
    location, _ = StockLocation.objects.get_or_create(
        business_id=customer.business_id, kind=StockLocation.Kind.CUSTOMER, customer=customer,
        defaults={"name": f"Customer custody: {customer.display_name}"},
    )
    return location


def rider_location(business, rider) -> StockLocation:
    location, _ = StockLocation.objects.get_or_create(
        business=business, kind=StockLocation.Kind.RIDER, rider=rider,
        defaults={"name": f"Rider: {rider}"},
    )
    return location


# ---------------------------------------------------------------- buckets

def bucket_for(business, key: BucketKey) -> StockBucket:
    location = StockLocation.objects.only("kind").get(id=key.location_id)
    bucket, _ = StockBucket.objects.get_or_create(
        business=business, item_id=key.item_id, location_id=key.location_id,
        condition=key.condition, batch_id=key.batch_id,
        defaults={"is_virtual": location.kind == StockLocation.Kind.VIRTUAL},
    )
    return bucket


def lock_buckets(bucket_ids) -> LockedBuckets:
    """Lock non-virtual buckets in UUID order. Virtual buckets are never locked."""
    ids = sorted({b for b in bucket_ids if b is not None}, key=str)
    rows = (StockBucket.objects.select_for_update(of=("self",)).select_related("batch", "item", "location")
            .filter(id__in=ids, is_virtual=False).order_by("id"))
    locked = LockedBuckets({row.id: row for row in rows})
    for row in StockBucket.objects.select_related("batch", "item", "location").filter(id__in=ids, is_virtual=True):
        locked.by_id[row.id] = row
    return locked


def candidate_buckets(business, *, item, locations, condition):
    """Non-virtual buckets that might supply this item (not yet locked)."""
    return list(StockBucket.objects.filter(
        business=business, item=item, location__in=locations, condition=condition, is_virtual=False,
        on_hand__gt=F("reserved"),
    ).values_list("id", flat=True))


def is_bucket_eligible(bucket: StockBucket) -> bool:
    return bucket.batch_id is None or bucket.batch.status == RELEASED


def allocate_from(locked: LockedBuckets, candidate_ids, qty: Decimal, *, location_priority=None,
                  allow_held=False, label="item", used: dict | None = None) -> list[Allocation]:
    """Pick available stock from already-locked buckets.
    Priority: preferred location order, then oldest batch first (FIFO).
    `used` tracks quantities already allocated earlier in the same command."""
    used = used if used is not None else {}
    qty = quantity(qty)
    rows = [locked.by_id[i] for i in candidate_ids if i in locked.by_id]
    rank = {loc_id: n for n, loc_id in enumerate(location_priority or [])}
    rows.sort(key=lambda b: (rank.get(b.location_id, 99),
                             b.batch.production_date.isoformat() if b.batch_id else "0000",
                             str(b.id)))
    allocations, remaining = [], qty
    held_available = Decimal(0)
    for bucket in rows:
        if remaining <= 0:
            break
        free = bucket.on_hand - bucket.reserved - used.get(bucket.id, Decimal(0))
        if free <= 0:
            continue
        if not allow_held and not is_bucket_eligible(bucket):
            held_available += free
            continue
        take = min(free, remaining)
        allocations.append(Allocation(bucket=bucket, quantity=take))
        used[bucket.id] = used.get(bucket.id, Decimal(0)) + take
        remaining -= take
    if remaining > 0:
        available = qty - remaining
        if held_available > 0 and available + held_available >= qty:
            raise BatchNotReleased(f"Stock of {label} exists only in held or unreleased batches.")
        raise InsufficientStock(
            f"Only {available.normalize()} {label} available.",
            details={"requested": str(qty.normalize()), "available": str(available.normalize())},
        )
    return allocations


# ---------------------------------------------------------------- posting

def post_movements(ctx, *, operation_id, action: str, lines: list[MovementLine], source_type: str,
                   source_id=None, reason: str = "", reversal_of=None, allow_held: bool = False,
                   enforce_batch_release: bool = True) -> StockPostingGroup:
    """Append one posting group and update bucket projections atomically.

    Buckets must already be locked by the caller (lock_buckets) and passed as
    the same Python objects so all deltas accumulate on one instance."""
    if not lines:
        raise ValidationFailed("A stock posting needs at least one movement.")
    touched: dict = {}
    for line in lines:
        q = quantity(line.quantity)
        if q <= 0:
            raise ValidationFailed("Stock quantities must be positive.")
        if line.item.whole_units:
            require_whole(q)
        src, tgt = line.source, line.target
        if src.item_id != line.item.id or tgt.item_id != line.item.id:
            raise ValidationFailed("A movement keeps the same item and base unit.")
        if enforce_batch_release and not allow_held and src.batch_id and not src.is_virtual \
                and line.movement_type in {StockMovement.Type.CUSTOMER_ISSUE, StockMovement.Type.SALE,
                                           StockMovement.Type.DISPATCH} \
                and src.batch.status != RELEASED:
            raise BatchNotReleased()
        if not src.is_virtual:
            if line.consume_reservation is not None:
                res = line.consume_reservation
                if res.bucket_id != src.id or res.status != Reservation.Status.ACTIVE or q > res.quantity:
                    raise BusinessConflict("Reservation does not cover this movement.", code="RESERVATION_MISMATCH")
                src.reserved -= q
            elif src.on_hand - src.reserved < q:
                raise InsufficientStock(
                    f"Only {(src.on_hand - src.reserved).normalize()} {line.item.name} available.",
                    details={"bucket_id": str(src.id)},
                )
            src.on_hand -= q
            touched[src.id] = src
        if not tgt.is_virtual:
            tgt.on_hand += q
            touched[tgt.id] = tgt

    for bucket in touched.values():
        if bucket.on_hand < 0 or bucket.reserved < 0 or bucket.reserved > bucket.on_hand:
            raise InsufficientStock("Stock balance cannot become negative.")
        bucket.save(update_fields=["on_hand", "reserved", "updated_at"])

    group = StockPostingGroup.objects.create(
        business=ctx.business, operation_id=operation_id, action=action, source_type=source_type,
        source_id=source_id, actor=ctx.user, reason=reason, reversal_of=reversal_of,
    )
    StockMovement.objects.bulk_create([
        StockMovement(business=ctx.business, group=group, item=line.item, source_bucket=line.source,
                      target_bucket=line.target, quantity=quantity(line.quantity), movement_type=line.movement_type)
        for line in lines
    ])
    consumed = {}
    for line in lines:
        if line.consume_reservation is not None:
            consumed.setdefault(line.consume_reservation.id, [line.consume_reservation, Decimal(0)])
            consumed[line.consume_reservation.id][1] += quantity(line.quantity)
    for res, used in consumed.values():
        if used >= res.quantity:
            res.status = Reservation.Status.CONSUMED
            res.save(update_fields=["status", "updated_at"])
        else:
            res.quantity -= used
            res.save(update_fields=["quantity", "updated_at"])
    return group


def reserve(ctx, *, allocations: list[Allocation], order_line=None, lease=None, expires_at=None) -> list[Reservation]:
    """Reserve physical eligible stock from locked buckets."""
    reservations = []
    for alloc in allocations:
        bucket = alloc.bucket
        if bucket.on_hand - bucket.reserved < alloc.quantity:
            raise InsufficientStock()
        bucket.reserved += alloc.quantity
        bucket.save(update_fields=["reserved", "updated_at"])
        reservations.append(Reservation.objects.create(
            business=ctx.business, order_line=order_line, lease=lease, bucket=bucket,
            quantity=alloc.quantity, expires_at=expires_at,
        ))
    return reservations


def release_reservation(reservation: Reservation, bucket: StockBucket) -> None:
    """Caller must hold the reservation's bucket lock."""
    if reservation.status != Reservation.Status.ACTIVE:
        return
    bucket.reserved -= reservation.quantity
    if bucket.reserved < 0:
        bucket.reserved = Decimal(0)
    bucket.save(update_fields=["reserved", "updated_at"])
    reservation.status = Reservation.Status.RELEASED
    reservation.save(update_fields=["status", "updated_at"])


# ---------------------------------------------------------------- reads

def customer_bottle_holdings(customer) -> dict:
    """Plant-owned containers in this customer's custody, by stock item id."""
    location = StockLocation.objects.filter(customer=customer).first()
    if location is None:
        return {}
    rows = StockBucket.objects.filter(location=location, condition=Condition.CUSTOMER_HELD) \
        .values("item_id", "on_hand")
    out: dict = {}
    for row in rows:
        out[row["item_id"]] = out.get(row["item_id"], Decimal(0)) + row["on_hand"]
    return out
