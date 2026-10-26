from common.context import RequestContext
from common.errors import Forbidden, Unauthenticated
from modules.organization.models import Branch

from .models import Membership
from .policies import capabilities_for


def context_from_request(request) -> RequestContext:
    cached = getattr(request, "_wp_context", None)
    if cached is not None:
        return cached
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise Unauthenticated()
    memberships = list(
        Membership.objects.select_related("business").filter(user=user, is_active=True, business__is_active=True)[:2]
    )
    if len(memberships) != 1:
        # V1: one business. Ambiguity is refused rather than letting the client pick a tenant.
        raise Forbidden("No unique active membership for this account.")
    membership = memberships[0]
    session = getattr(user, "_native_session", None)
    device = session.device if session is not None else None
    branch = Branch.objects.filter(business=membership.business, is_active=True).order_by("created_at").first()
    ctx = RequestContext(
        business=membership.business,
        branch=branch,
        user=user,
        role=membership.role,
        capabilities=capabilities_for(membership.role, membership.extra_capabilities),
        device=device,
        session_id=str(session.id) if session else None,
        request_id=getattr(request, "request_id", "-"),
    )
    request._wp_context = ctx
    return ctx


def system_context(business, request_id: str = "system", user=None) -> RequestContext:
    """Context for workers, scheduled jobs and management commands."""
    branch = Branch.objects.filter(business=business, is_active=True).order_by("created_at").first()
    return RequestContext(business=business, branch=branch, user=user, role="SYSTEM",
                          capabilities=frozenset({"*"}), request_id=request_id)
