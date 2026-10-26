"""Role -> capability policy (architecture section 13).

Capabilities name what a command needs. Some are only granted through an
explicit membership.extra_capabilities entry (e.g. quality.release)."""

ROLE_CAPABILITIES = {
    "OWNER": frozenset({"*"}),
    "CASHIER": frozenset({
        "customer.view", "customer.manage", "order.view", "order.manage",
        "sale.counter", "payment.receive", "shift.operate", "stock.view",
        "adjustment.request", "complaint.manage", "report.own_shift", "notification.view",
        "document.upload",
    }),
    "OPERATOR": frozenset({
        "stock.view", "stock.transfer", "production.write", "maintenance.write", "quality.sample",
        "adjustment.request", "report.operational_stock", "document.upload",
    }),
    "RIDER": frozenset({"delivery.assigned", "payment.collect_assigned"}),
    "CUSTOMER": frozenset({"self.service"}),
}

# Capabilities that can be granted to non-owners only explicitly.
GRANTABLE = frozenset({
    "quality.release", "delivery.dispatch", "delivery.load", "payment.verify_digital",
    "customer.view", "order.manage", "stock.transfer",
})

# Owner-only approvals (never implied by a request capability).
OWNER_ONLY = frozenset({
    "credit.approve", "refund.approve", "stock.adjust.approve", "deposit.waive",
    "staff.manage", "device.manage", "report.financial", "shift.approve", "notification.retry",
    "settings.manage", "price.publish", "batch.override",
})


def capabilities_for(role: str, extra=()) -> frozenset:
    base = ROLE_CAPABILITIES.get(role, frozenset())
    granted = {c for c in (extra or ()) if c in GRANTABLE}
    return frozenset(base | granted)
