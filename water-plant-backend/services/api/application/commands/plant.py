"""Plant operations: stock transfers, counts, purchases, production, filling, quality, maintenance."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from common.errors import BusinessConflict, NotFound, ValidationFailed
from common.ids import parse_uuid
from common.money import money, qty_str, quantity
from modules.audit.services import record as audit
from modules.catalog.models import ContainerType
from modules.idempotency.services import execute
from modules.inventory.models import (
    Condition,
    GoodsReceipt,
    Purchase,
    PurchaseLine,
    Reservation,
    StockCount,
    StockCountLine,
    StockItem,
    StockLocation,
    StockMovement,
)
from modules.inventory.services import (
    BucketKey,
    MovementLine,
    bucket_for,
    lock_buckets,
    plant_location,
    post_movements,
    treated_water_item,
    virtual_location,
)
from modules.plant_care.models import BatchHold, MaintenanceLog, MaintenanceTask, QualitySample, TestResult
from modules.production.models import FillingConsumption, FillingRun, ProductionBatch


def _location(ctx, location_id) -> StockLocation:
    loc = StockLocation.objects.filter(business=ctx.business, id=parse_uuid(location_id), is_active=True).first()
    if loc is None:
        raise NotFound("Stock location not found.")
    return loc


def _item(ctx, item_id) -> StockItem:
    item = StockItem.objects.filter(business=ctx.business, id=parse_uuid(item_id), is_active=True).first()
    if item is None:
        raise NotFound("Stock item not found.")
    return item


# ------------------------------------------------------------ transfers & counts

ALLOWED_TRANSFER_CONDITIONS = {
    (Condition.DIRTY_RETURNED, Condition.CLEAN_EMPTY): StockMovement.Type.CLEANING,
    (Condition.DIRTY_RETURNED, Condition.DAMAGED): StockMovement.Type.DAMAGE,
    (Condition.CLEAN_EMPTY, Condition.DAMAGED): StockMovement.Type.DAMAGE,
}


def transfer_stock(ctx, data, *, idempotency_key=None):
    """Move stock between real custody locations, or clean/inspect containers (condition change)."""
    ctx.require("stock.transfer")

    def handler(operation_id):
        item = _item(ctx, data["item_id"])
        src_loc, tgt_loc = _location(ctx, data["from_location_id"]), _location(ctx, data["to_location_id"])
        if src_loc.is_virtual or tgt_loc.is_virtual or StockLocation.Kind.CUSTOMER in {src_loc.kind, tgt_loc.kind}:
            raise ValidationFailed("Use sale/return/adjustment commands for customer or virtual locations.")
        src_cond = data.get("from_condition", Condition.STANDARD)
        tgt_cond = data.get("to_condition", src_cond)
        mtype = StockMovement.Type.TRANSFER
        if src_cond != tgt_cond:
            mtype = ALLOWED_TRANSFER_CONDITIONS.get((src_cond, tgt_cond))
            if mtype is None:
                raise ValidationFailed(fields={"to_condition": ["This condition change is not allowed."]})
        batch_id = parse_uuid(data["batch_id"]) if data.get("batch_id") else None
        qty = quantity(data["quantity"])
        src = bucket_for(ctx.business, BucketKey(item.id, src_loc.id, src_cond, batch_id))
        tgt = bucket_for(ctx.business, BucketKey(item.id, tgt_loc.id, tgt_cond,
                                                 batch_id if tgt_cond == src_cond else None))
        locked = lock_buckets([src.id, tgt.id])
        group = post_movements(ctx, operation_id=operation_id, action="TRANSFER",
                               lines=[MovementLine(item, locked.get(src), locked.get(tgt), qty, mtype)],
                               source_type="transfer", reason=data.get("reason", ""))
        audit(ctx, "stock.transferred", "stock_posting", group.id, operation_id=operation_id,
              changes={"item": item.code, "quantity": qty, "from": str(src_loc.id), "to": str(tgt_loc.id)})
        return {"_resource": ("stock_posting", group.id), "posting_id": str(group.id)}

    return execute(ctx, command_name="transfer_stock", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)


def submit_count(ctx, data) -> StockCount:
    ctx.require("stock.transfer", "adjustment.request")
    location = _location(ctx, data["location_id"])
    with transaction.atomic():
        count = StockCount.objects.create(business=ctx.business, location=location, counted_by=ctx.user,
                                          notes=data.get("notes", ""))
        for index, raw in enumerate(data.get("lines", [])):
            item = _item(ctx, raw["item_id"])
            batch_id = parse_uuid(raw["batch_id"]) if raw.get("batch_id") else None
            bucket = bucket_for(ctx.business, BucketKey(item.id, location.id,
                                                        raw.get("condition", Condition.STANDARD), batch_id))
            counted = quantity(raw["counted_quantity"])
            if counted < 0:
                raise ValidationFailed(fields={f"lines.{index}.counted_quantity": ["Must not be negative."]})
            StockCountLine.objects.create(business=ctx.business, count=count, bucket=bucket,
                                          expected_snapshot=bucket.on_hand, counted_quantity=counted)
    return count


def approve_count(ctx, count_id, data, *, idempotency_key=None):
    """Supervised variance posting against the ADJUSTMENT virtual location."""
    ctx.require("stock.adjust.approve")

    def handler(operation_id):
        count = StockCount.objects.select_for_update().filter(business=ctx.business, id=parse_uuid(count_id)).first()
        if count is None:
            raise NotFound()
        if count.status != StockCount.Status.SUBMITTED:
            raise BusinessConflict("Count already decided.", code="COUNT_ALREADY_DECIDED")
        adjust = virtual_location(ctx.business, StockLocation.Virtual.ADJUSTMENT)
        lines = list(count.lines.select_related("bucket", "bucket__item"))
        sinks = {line.id: bucket_for(ctx.business, BucketKey(line.bucket.item_id, adjust.id, line.bucket.condition))
                 for line in lines}
        locked = lock_buckets([line.bucket_id for line in lines] + [s.id for s in sinks.values()])
        movements, report = [], []
        for line in lines:
            bucket = locked.get(line.bucket)
            # Variance = counted - expected at count time; it is applied to the current balance.
            variance = line.counted_quantity - line.expected_snapshot
            if variance == 0:
                continue
            sink = locked.get(sinks[line.id])
            if variance > 0:
                movements.append(MovementLine(bucket.item, sink, bucket, variance, StockMovement.Type.COUNT_ADJUSTMENT))
            else:
                movements.append(MovementLine(bucket.item, bucket, sink, -variance,
                                              StockMovement.Type.COUNT_ADJUSTMENT))
            report.append({"bucket_id": str(bucket.id), "variance": qty_str(variance)})
        group = None
        if movements:
            group = post_movements(ctx, operation_id=operation_id, action="COUNT_ADJUSTMENT", lines=movements,
                                   source_type="stock_count", source_id=count.id, reason=data.get("reason", ""))
        count.status, count.approved_by, count.approved_at = StockCount.Status.APPROVED, ctx.user, timezone.now()
        count.posting_group = group
        count.save()
        audit(ctx, "stock_count.approved", "stock_count", count.id, operation_id=operation_id,
              changes={"variances": report})
        return {"_resource": ("stock_count", count.id), "count_id": str(count.id), "variances": report}

    return execute(ctx, command_name="approve_count", operation_id=data.get("operation_id"),
                   payload={"count_id": str(count_id), **data}, handler=handler, header_key=idempotency_key)


# ------------------------------------------------------------ purchases

def create_purchase(ctx, data) -> Purchase:
    ctx.require("stock.transfer")
    with transaction.atomic():
        purchase = Purchase.objects.create(business=ctx.business, supplier_name=data["supplier_name"],
                                           reference=data.get("reference", ""), ordered_by=ctx.user,
                                           destination=_location(ctx, data.get("destination_location_id")
                                                                 or plant_location(ctx.business).id))
        for raw in data.get("lines", []):
            PurchaseLine.objects.create(business=ctx.business, purchase=purchase, item=_item(ctx, raw["item_id"]),
                                        condition=raw.get("condition", Condition.STANDARD),
                                        ordered_quantity=quantity(raw["quantity"]), rate=money(raw.get("rate", 0)))
    return purchase


def receive_purchase(ctx, purchase_id, data, *, idempotency_key=None):
    """Actual goods receipt: SUPPLIER virtual -> destination."""
    ctx.require("stock.transfer")

    def handler(operation_id):
        purchase = Purchase.objects.select_for_update().filter(business=ctx.business,
                                                               id=parse_uuid(purchase_id)).first()
        if purchase is None:
            raise NotFound()
        if purchase.status in {Purchase.Status.RECEIVED, Purchase.Status.CANCELLED}:
            raise BusinessConflict("Purchase is closed.", code="PURCHASE_CLOSED")
        supplier = virtual_location(ctx.business, StockLocation.Virtual.SUPPLIER)
        lines = {pl.id: pl for pl in purchase.lines.select_related("item")}
        received = []
        for index, raw in enumerate(data.get("lines", [])):
            pl = lines.get(parse_uuid(raw["purchase_line_id"]))
            if pl is None:
                raise ValidationFailed(fields={f"lines.{index}.purchase_line_id": ["Not on this purchase."]})
            received.append((pl, quantity(raw["quantity"])))
        buckets = {}
        for pl, _ in received:
            buckets[pl.id] = (bucket_for(ctx.business, BucketKey(pl.item_id, supplier.id, pl.condition)),
                              bucket_for(ctx.business, BucketKey(pl.item_id, purchase.destination_id, pl.condition)))
        locked = lock_buckets([b.id for pair in buckets.values() for b in pair])
        movements = []
        for pl, qty in received:
            src, tgt = buckets[pl.id]
            movements.append(MovementLine(pl.item, locked.get(src), locked.get(tgt), qty,
                                          StockMovement.Type.SUPPLIER_RECEIPT))
            pl.received_quantity += qty
            pl.save(update_fields=["received_quantity"])
        group = post_movements(ctx, operation_id=operation_id, action="GOODS_RECEIPT", lines=movements,
                               source_type="purchase", source_id=purchase.id)
        GoodsReceipt.objects.create(business=ctx.business, purchase=purchase, received_by=ctx.user,
                                    posting_group=group, operation_id=operation_id)
        done = all(pl.received_quantity >= pl.ordered_quantity for pl in lines.values())
        purchase.status = Purchase.Status.RECEIVED if done else Purchase.Status.PARTIALLY_RECEIVED
        purchase.save(update_fields=["status"])
        return {"_resource": ("purchase", purchase.id), "purchase_id": str(purchase.id), "status": purchase.status,
                "posting_id": str(group.id)}

    return execute(ctx, command_name="receive_purchase", operation_id=data.get("operation_id"),
                   payload={"purchase_id": str(purchase_id), **data}, handler=handler, header_key=idempotency_key)


# ------------------------------------------------------------ production

def record_batch(ctx, data, *, idempotency_key=None):
    """Record treated water output. The batch starts PENDING_RELEASE and cannot be sold until released."""
    ctx.require("production.write")

    def handler(operation_id):
        input_l, output_l = quantity(data["input_litres"]), quantity(data["output_litres"])
        if output_l <= 0 or output_l > input_l:
            raise ValidationFailed(fields={"output_litres": ["Must be > 0 and <= input litres."]})
        batch = ProductionBatch.objects.create(
            business=ctx.business, batch_code=data["batch_code"], production_date=data.get("production_date")
            or timezone.localdate(), input_litres=input_l, output_litres=output_l, operator=ctx.user,
            treatment_notes=data.get("treatment_notes", ""), operation_id=operation_id,
        )
        water = treated_water_item(ctx.business)
        source = bucket_for(ctx.business, BucketKey(water.id, virtual_location(
            ctx.business, StockLocation.Virtual.PRODUCTION).id, Condition.STANDARD))
        target = bucket_for(ctx.business, BucketKey(water.id, plant_location(ctx.business).id, Condition.STANDARD,
                                                    batch.id))
        locked = lock_buckets([source.id, target.id])
        post_movements(ctx, operation_id=operation_id, action="PRODUCTION_OUTPUT",
                       lines=[MovementLine(water, locked.get(source), locked.get(target), output_l,
                                           StockMovement.Type.PRODUCTION_OUTPUT)],
                       source_type="production_batch", source_id=batch.id)
        audit(ctx, "batch.recorded", "production_batch", batch.id, operation_id=operation_id,
              changes={"output_litres": output_l})
        return {"_resource": ("production_batch", batch.id), "batch_id": str(batch.id),
                "batch_code": batch.batch_code, "status": batch.status}

    return execute(ctx, command_name="record_batch", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)


def record_filling_run(ctx, data, *, idempotency_key=None):
    """Posting group: consume treated water + consumables, write off wastage,
    and transfer CLEAN_EMPTY containers to FULL containers linked to the batch."""
    ctx.require("production.write")

    def handler(operation_id):
        batch = ProductionBatch.objects.select_for_update().filter(business=ctx.business,
                                                                   id=parse_uuid(data["batch_id"])).first()
        if batch is None:
            raise NotFound("Batch not found.")
        if batch.status == ProductionBatch.Status.BLOCKED:
            raise BusinessConflict("Blocked batches cannot be used for filling.", code="BATCH_BLOCKED")
        ct = ContainerType.objects.select_related("stock_item").filter(
            business=ctx.business, id=parse_uuid(data["container_type_id"])).first()
        if ct is None:
            raise NotFound("Container type not found.")
        filled = quantity(data["filled_quantity"])
        water_used = quantity(data.get("water_litres_consumed") or filled * ct.capacity_litres)
        wastage = quantity(data.get("wastage_litres", 0))
        plant = plant_location(ctx.business)
        consumed = virtual_location(ctx.business, StockLocation.Virtual.CONSUMED)
        write_off = virtual_location(ctx.business, StockLocation.Virtual.WRITE_OFF)
        water = treated_water_item(ctx.business)

        keys = {
            "water_src": BucketKey(water.id, plant.id, Condition.STANDARD, batch.id),
            "water_sink": BucketKey(water.id, consumed.id, Condition.STANDARD),
            "waste_sink": BucketKey(water.id, write_off.id, Condition.STANDARD),
            "empty": BucketKey(ct.stock_item_id, plant.id, Condition.CLEAN_EMPTY),
            "full": BucketKey(ct.stock_item_id, plant.id, Condition.FULL, batch.id),
        }
        consumables = []
        for raw in data.get("consumables", []):
            item = _item(ctx, raw["item_id"])
            consumables.append((item, quantity(raw["quantity"]), BucketKey(item.id, plant.id, Condition.STANDARD),
                                BucketKey(item.id, consumed.id, Condition.STANDARD)))
        buckets = {name: bucket_for(ctx.business, key) for name, key in keys.items()}
        cons_buckets = [(item, qty, bucket_for(ctx.business, s), bucket_for(ctx.business, t))
                        for item, qty, s, t in consumables]
        locked = lock_buckets([b.id for b in buckets.values()] + [b.id for _, _, s, t in cons_buckets for b in (s, t)])
        g = {name: locked.get(b) for name, b in buckets.items()}
        lines = [
            MovementLine(water, g["water_src"], g["water_sink"], water_used, StockMovement.Type.FILLING_CONSUME),
            MovementLine(ct.stock_item, g["empty"], g["full"], filled, StockMovement.Type.FILLING_OUTPUT),
        ]
        if wastage > 0:
            lines.append(MovementLine(water, g["water_src"], g["waste_sink"], wastage, StockMovement.Type.WRITE_OFF))
        for item, qty, s, t in cons_buckets:
            lines.append(MovementLine(item, locked.get(s), locked.get(t), qty, StockMovement.Type.FILLING_CONSUME))
        group = post_movements(ctx, operation_id=operation_id, action="FILLING", lines=lines,
                               source_type="production_batch", source_id=batch.id)
        run = FillingRun.objects.create(business=ctx.business, batch=batch, container_type=ct, filled_quantity=filled,
                                        water_litres_consumed=water_used, wastage_litres=wastage, operator=ctx.user,
                                        posting_group=group, operation_id=operation_id)
        for item, qty, *_ in consumables:
            FillingConsumption.objects.create(business=ctx.business, filling_run=run, item=item, quantity=qty)
        audit(ctx, "filling.recorded", "filling_run", run.id, operation_id=operation_id,
              changes={"batch": batch.batch_code, "filled": filled, "wastage": wastage})
        return {"_resource": ("filling_run", run.id), "filling_run_id": str(run.id), "posting_id": str(group.id)}

    return execute(ctx, command_name="record_filling_run", operation_id=data.get("operation_id"), payload=data,
                   handler=handler, header_key=idempotency_key)


# ------------------------------------------------------------ quality

def record_sample(ctx, data) -> QualitySample:
    ctx.require("quality.sample", "production.write")
    batch = None
    if data.get("batch_id"):
        batch = ProductionBatch.objects.filter(business=ctx.business, id=parse_uuid(data["batch_id"])).first()
        if batch is None:
            raise NotFound("Batch not found.")
    return QualitySample.objects.create(business=ctx.business, batch=batch,
                                        location_description=data.get("location_description", ""),
                                        sampled_at=data.get("sampled_at") or timezone.now(), sampled_by=ctx.user)


def record_results(ctx, sample_id, data) -> list[TestResult]:
    ctx.require("quality.sample", "production.write")
    sample = QualitySample.objects.filter(business=ctx.business, id=parse_uuid(sample_id)).first()
    if sample is None:
        raise NotFound()
    results = []
    for raw in data.get("results", []):
        results.append(TestResult.objects.create(
            business=ctx.business, sample=sample, parameter=raw["parameter"], value=raw["value"], unit=raw["unit"],
            within_limits=raw.get("within_limits"), recorded_by=ctx.user,
            lab_attachment_id=parse_uuid(raw["lab_attachment_id"]) if raw.get("lab_attachment_id") else None,
        ))
    return results


def hold_batch(ctx, batch_id, data) -> ProductionBatch:
    """Stops reservation/dispatch/handover. Flags existing reservations so affected work is visible.
    Does not pretend delivered water has been recalled."""
    ctx.require("quality.sample", "quality.release", "production.write")
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise ValidationFailed(fields={"reason": ["A reason is required."]})
    with transaction.atomic():
        batch = ProductionBatch.objects.select_for_update().filter(business=ctx.business,
                                                                   id=parse_uuid(batch_id)).first()
        if batch is None:
            raise NotFound()
        if batch.status == ProductionBatch.Status.BLOCKED:
            raise BusinessConflict("Batch is blocked.", code="BATCH_BLOCKED")
        BatchHold.objects.create(business=ctx.business, batch=batch, reason=reason, created_by=ctx.user)
        batch.status = ProductionBatch.Status.HOLD
        batch.save(update_fields=["status", "updated_at"])
        flagged = Reservation.objects.filter(bucket__batch=batch, status=Reservation.Status.ACTIVE) \
            .update(flagged_reason="BATCH_HELD")
        audit(ctx, "batch.held", "production_batch", batch.id, reason=reason,
              changes={"flagged_reservations": flagged})
    return batch


def release_batch(ctx, batch_id, data) -> ProductionBatch:
    """Staff-controlled release with recorded evidence. No timer ever releases a batch."""
    ctx.require("quality.release")
    evidence = (data.get("evidence") or "").strip()
    if not evidence:
        raise ValidationFailed(fields={"evidence": ["Release evidence is required."]})
    with transaction.atomic():
        batch = ProductionBatch.objects.select_for_update().filter(business=ctx.business,
                                                                   id=parse_uuid(batch_id)).first()
        if batch is None:
            raise NotFound()
        if batch.status not in {ProductionBatch.Status.PENDING_RELEASE, ProductionBatch.Status.HOLD}:
            raise BusinessConflict(f"Batch is {batch.status}.", code="INVALID_BATCH_STATE")
        results = TestResult.objects.filter(sample__batch=batch)
        if not results.exists():
            raise BusinessConflict("Record quality test results before release.", code="NO_TEST_RESULTS")
        if results.filter(within_limits=False).exists() and not data.get("override_out_of_limit"):
            raise BusinessConflict("Some results are out of limits.", code="RESULTS_OUT_OF_LIMITS")
        if data.get("override_out_of_limit"):
            ctx.require("batch.override")
        results.filter(reviewed_by__isnull=True).update(reviewed_by=ctx.user)
        now = timezone.now()
        for hold in BatchHold.objects.select_for_update().filter(batch=batch, released_at__isnull=True):
            hold.released_by, hold.released_at, hold.release_evidence = ctx.user, now, evidence
            hold.save(update_fields=["released_by", "released_at", "release_evidence"])
        batch.status, batch.released_at, batch.released_by = ProductionBatch.Status.RELEASED, now, ctx.user
        batch.save(update_fields=["status", "released_at", "released_by", "updated_at"])
        Reservation.objects.filter(bucket__batch=batch, flagged_reason="BATCH_HELD").update(flagged_reason="")
        audit(ctx, "batch.released", "production_batch", batch.id, reason=evidence)
    return batch


# ------------------------------------------------------------ maintenance

def complete_maintenance(ctx, task_id, data) -> MaintenanceLog:
    ctx.require("maintenance.write")
    work = (data.get("work_done") or "").strip()
    if not work:
        raise ValidationFailed(fields={"work_done": ["Required."]})
    with transaction.atomic():
        task = MaintenanceTask.objects.select_for_update().filter(business=ctx.business,
                                                                  id=parse_uuid(task_id)).first()
        if task is None:
            raise NotFound()
        log = MaintenanceLog.objects.create(business=ctx.business, task=task, work_done=work,
                                            parts_consumed=data.get("parts_consumed", []), technician=ctx.user,
                                            performed_at=data.get("performed_at") or timezone.now())
        if task.interval_days:
            task.due_date = timezone.localdate() + timedelta(days=task.interval_days)
            task.status = MaintenanceTask.Status.SCHEDULED
        else:
            task.status = MaintenanceTask.Status.DONE
        task.version += 1
        task.save(update_fields=["due_date", "status", "version", "updated_at"])
        audit(ctx, "maintenance.completed", "maintenance_task", task.id)
    return log
