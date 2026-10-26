"""Inventory, purchases, production, quality, maintenance and delivery."""

from rest_framework.response import Response

from application.commands import delivery, plant
from common.errors import NotFound
from common.ids import parse_uuid
from modules.fulfillment.models import DeliveryStop
from modules.inventory.models import StockBucket, StockMovement
from modules.plant_care.models import Equipment, MaintenanceTask
from modules.production.models import ProductionBatch

from .base import BaseView, ListMixin, command_response, paginate
from .serializers import BatchOut, BucketOut, MaintenanceTaskOut, MovementOut, RunOut, StopOut


class InventoryBalancesView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        ctx.require("stock.view")
        qs = StockBucket.objects.filter(business=ctx.business, is_virtual=False) \
            .select_related("item", "location", "batch").order_by("item__code", "location__name")
        params = request.query_params
        if params.get("location_id"):
            qs = qs.filter(location_id=parse_uuid(params["location_id"], "location_id"))
        if params.get("item_id"):
            qs = qs.filter(item_id=parse_uuid(params["item_id"], "item_id"))
        if params.get("condition"):
            qs = qs.filter(condition=params["condition"].upper())
        if params.get("bottles_only") == "true":
            qs = qs.filter(item__kind="REUSABLE_CONTAINER")  # the "bottle ledger" is a filtered view
        if params.get("include_zero") != "true":
            qs = qs.exclude(on_hand=0)
        return paginate(self, request, qs, BucketOut)


class InventoryMovementsView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        ctx.require("stock.view")
        qs = StockMovement.objects.filter(business=ctx.business).select_related("group").order_by("-created_at")
        if request.query_params.get("bucket_id"):
            bid = parse_uuid(request.query_params["bucket_id"], "bucket_id")
            from django.db.models import Q

            qs = qs.filter(Q(source_bucket_id=bid) | Q(target_bucket_id=bid))
        if request.query_params.get("item_id"):
            qs = qs.filter(item_id=parse_uuid(request.query_params["item_id"], "item_id"))
        return paginate(self, request, qs, MovementOut)


class TransferView(BaseView):
    def post(self, request):
        return command_response(*plant.transfer_stock(self.ctx(request), self.data(request),
                                                      idempotency_key=self.idempotency_key(request)))


class CountsView(BaseView):
    def post(self, request):
        count = plant.submit_count(self.ctx(request), self.data(request))
        return Response({"count_id": str(count.id), "status": count.status,
                         "lines": [{"bucket_id": str(line.bucket_id), "expected": str(line.expected_snapshot),
                                    "counted": str(line.counted_quantity), "variance": str(line.variance)}
                                   for line in count.lines.all()]}, status=201)


class CountApproveView(BaseView):
    def post(self, request, count_id):
        return command_response(*plant.approve_count(self.ctx(request), count_id, self.data(request),
                                                     idempotency_key=self.idempotency_key(request)))


class PurchasesView(BaseView):
    def post(self, request):
        purchase = plant.create_purchase(self.ctx(request), self.data(request))
        return Response({"purchase_id": str(purchase.id), "status": purchase.status,
                         "lines": [{"id": str(pl.id), "item_id": str(pl.item_id),
                                    "ordered_quantity": str(pl.ordered_quantity)} for pl in purchase.lines.all()]},
                        status=201)


class PurchaseReceiveView(BaseView):
    def post(self, request, purchase_id):
        return command_response(*plant.receive_purchase(self.ctx(request), purchase_id, self.data(request),
                                                        idempotency_key=self.idempotency_key(request)))


class BatchesView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        ctx.require("production.write", "stock.view")
        return paginate(self, request, ProductionBatch.objects.filter(business=ctx.business)
                        .order_by("-production_date"), BatchOut)

    def post(self, request):
        return command_response(*plant.record_batch(self.ctx(request), self.data(request),
                                                    idempotency_key=self.idempotency_key(request)))


class FillingRunsView(BaseView):
    def post(self, request):
        return command_response(*plant.record_filling_run(self.ctx(request), self.data(request),
                                                          idempotency_key=self.idempotency_key(request)))


class BatchHoldView(BaseView):
    def post(self, request, batch_id):
        return Response(BatchOut(plant.hold_batch(self.ctx(request), batch_id, self.data(request))).data)


class BatchReleaseView(BaseView):
    def post(self, request, batch_id):
        return Response(BatchOut(plant.release_batch(self.ctx(request), batch_id, self.data(request))).data)


class QualitySamplesView(BaseView):
    def post(self, request):
        sample = plant.record_sample(self.ctx(request), self.data(request))
        return Response({"sample_id": str(sample.id)}, status=201)


class QualityResultsView(BaseView):
    def post(self, request, sample_id):
        results = plant.record_results(self.ctx(request), sample_id, self.data(request))
        return Response({"result_ids": [str(r.id) for r in results]}, status=201)


class MaintenanceTasksView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        ctx.require("maintenance.write")
        return paginate(self, request, MaintenanceTask.objects.filter(business=ctx.business).order_by("due_date"),
                        MaintenanceTaskOut)

    def post(self, request):
        ctx = self.ctx(request)
        ctx.require("maintenance.write")
        data = self.data(request)
        equipment = Equipment.objects.filter(business=ctx.business, id=parse_uuid(data["equipment_id"])).first()
        if equipment is None:
            raise NotFound("Equipment not found.")
        task = MaintenanceTask.objects.create(business=ctx.business, equipment=equipment, title=data["title"],
                                              kind=data.get("kind", "MAINTENANCE"),
                                              interval_days=data.get("interval_days"), due_date=data["due_date"],
                                              created_by=ctx.user)
        return Response(MaintenanceTaskOut(task).data, status=201)


class MaintenanceCompleteView(BaseView):
    def post(self, request, task_id):
        log = plant.complete_maintenance(self.ctx(request), task_id, self.data(request))
        return Response({"log_id": str(log.id), "task": MaintenanceTaskOut(log.task).data}, status=201)


# ---------------------------------------------------------------- delivery

class DeliveryRunsView(BaseView):
    def post(self, request):
        return Response(RunOut(delivery.create_run(self.ctx(request), self.data(request))).data, status=201)


class MyStopsView(ListMixin, BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        ctx.require("delivery.assigned")
        qs = DeliveryStop.objects.filter(business=ctx.business, run__rider=ctx.user,
                                         run__status="DISPATCHED").select_related("order").order_by("sequence")
        return paginate(self, request, qs, StopOut)


class RunDispatchView(BaseView):
    def post(self, request, run_id):
        return command_response(*delivery.dispatch_run(self.ctx(request), run_id, self.data(request),
                                                       idempotency_key=self.idempotency_key(request)))


class RunSettleView(BaseView):
    def post(self, request, run_id):
        return command_response(*delivery.settle_run(self.ctx(request), run_id, self.data(request),
                                                     idempotency_key=self.idempotency_key(request)))


class StopCompleteView(BaseView):
    def post(self, request, stop_id):
        return command_response(*delivery.complete_stop(self.ctx(request), stop_id, self.data(request),
                                                        idempotency_key=self.idempotency_key(request)))


class StopFailView(BaseView):
    def post(self, request, stop_id):
        return command_response(*delivery.fail_stop(self.ctx(request), stop_id, self.data(request),
                                                    idempotency_key=self.idempotency_key(request)))
