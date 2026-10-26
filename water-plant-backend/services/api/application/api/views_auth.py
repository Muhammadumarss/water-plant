"""Authentication: browser sessions (CSRF) and desktop/mobile token sessions."""

import logging

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from common.errors import Unauthenticated, ValidationFailed
from modules.customers.models import CustomerAccountLink
from modules.identity.models import Device
from modules.identity.services import issue_session, revoke_session, rotate_refresh

from .base import BaseView

security_log = logging.getLogger("security")
GENERIC_LOGIN_ERROR = "Invalid username or password."


def me_payload(ctx):
    return {"user_id": str(ctx.user.id), "username": ctx.user.username, "full_name": ctx.user.full_name,
            "role": ctx.role, "capabilities": sorted(ctx.capabilities), "business": {
                "id": str(ctx.business.id), "name": ctx.business.name, "currency": ctx.business.currency,
                "timezone": ctx.business.timezone},
            "device_id": str(ctx.device.id) if ctx.device else None}


class CsrfView(BaseView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrf_token": get_token(request)})


class WebLoginView(BaseView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "login"

    def post(self, request):
        SessionAuthentication().enforce_csrf(request)  # login is CSRF-protected too
        user = authenticate(request, username=request.data.get("username", ""),
                            password=request.data.get("password", ""))
        if user is None or not user.is_active:
            security_log.info("web login failed")
            raise Unauthenticated(GENERIC_LOGIN_ERROR)
        login(request, user)
        return Response(me_payload(self.ctx(request)))


class WebLogoutView(BaseView):
    authentication_classes = [SessionAuthentication]

    def post(self, request):
        logout(request)
        return Response(status=204)


class TokenView(BaseView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "token"

    def post(self, request):
        user = authenticate(request, username=request.data.get("username", ""),
                            password=request.data.get("password", ""))
        if user is None or not user.is_active:
            security_log.info("token login failed")
            raise Unauthenticated(GENERIC_LOGIN_ERROR)
        device = None
        client_id = request.data.get("device_client_id")
        if client_id:
            device = Device.objects.filter(
                client_identifier=client_id, revoked_at__isnull=True,
                business_id__in=user.memberships.filter(is_active=True).values("business_id"),
            ).first()
            if device is None:
                raise Unauthenticated("Device is not enrolled.")
        elif user.memberships.filter(is_active=True).exclude(role="CUSTOMER").exists() \
                and request.data.get("client_type") == "desktop":
            raise ValidationFailed(fields={"device_client_id": ["Desktop sessions require an enrolled device."]})
        return Response(issue_session(user, device), status=201)


class TokenRefreshView(BaseView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "token"

    def post(self, request):
        refresh = request.data.get("refresh")
        if not refresh:
            raise ValidationFailed(fields={"refresh": ["Required."]})
        return Response(rotate_refresh(refresh))


class TokenRevokeView(BaseView):
    def post(self, request):
        ctx = self.ctx(request)
        if ctx.session_id:
            revoke_session(ctx.session_id)
        return Response(status=204)


class MeView(BaseView):
    def get(self, request):
        return Response(me_payload(self.ctx(request)))


class MyCustomersView(BaseView):
    def get(self, request):
        ctx = self.ctx(request)
        links = CustomerAccountLink.objects.filter(user=ctx.user, revoked_at__isnull=True,
                                                   business=ctx.business).select_related("customer")
        return Response([{"customer_id": str(link.customer_id), "display_name": link.customer.display_name,
                          "relationship": link.relationship} for link in links])
