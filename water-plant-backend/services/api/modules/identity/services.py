"""Native (desktop/mobile) token sessions with locked refresh rotation."""

import logging
from datetime import UTC, datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from common.errors import Unauthenticated

from .models import Device, NativeSession

security_log = logging.getLogger("security")


def _tokens_for(session: NativeSession) -> dict:
    refresh = RefreshToken.for_user(session.user)
    refresh["sid"] = str(session.id)
    if session.device_id:
        refresh["did"] = str(session.device_id)
    # The refresh token never outlives its session.
    refresh.payload["exp"] = int(session.expires_at.timestamp())
    access = refresh.access_token
    return {"refresh": str(refresh), "access": str(access), "jti": refresh["jti"]}


def issue_session(user, device: Device | None) -> dict:
    lifetime = settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]
    with transaction.atomic():
        session = NativeSession.objects.create(user=user, device=device, expires_at=timezone.now() + lifetime)
        tokens = _tokens_for(session)
        session.current_refresh_jti = tokens["jti"]
        session.save(update_fields=["current_refresh_jti"])
    return {"access": tokens["access"], "refresh": tokens["refresh"], "session_id": str(session.id),
            "access_expires_in": int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds())}


def rotate_refresh(raw_refresh: str) -> dict:
    try:
        token = RefreshToken(raw_refresh)
    except TokenError as exc:
        raise Unauthenticated("Invalid or expired refresh token.") from exc
    sid = token.get("sid")
    if not sid:
        raise Unauthenticated("Invalid refresh token.")

    reuse_detected = False
    with transaction.atomic():
        session = NativeSession.objects.select_for_update().select_related("user", "device").filter(id=sid).first()
        if session is None or not session.is_live or not session.user.is_active:
            raise Unauthenticated("Session is no longer valid.")
        if session.device is not None and session.device.revoked_at is not None:
            raise Unauthenticated("Device has been revoked.")
        if token["jti"] != session.current_refresh_jti:
            # A superseded refresh token was replayed: revoke the whole session.
            session.revoked_at = timezone.now()
            session.revoked_reason = "refresh_reuse"
            session.save(update_fields=["revoked_at", "revoked_reason"])
            reuse_detected = True
        else:
            tokens = _tokens_for(session)
            session.current_refresh_jti = tokens["jti"]
            session.last_seen_at = timezone.now()
            session.save(update_fields=["current_refresh_jti", "last_seen_at"])
    if reuse_detected:
        security_log.warning("refresh token reuse; session revoked sid=%s", sid)
        raise Unauthenticated("Session revoked.")
    return {"access": tokens["access"], "refresh": tokens["refresh"], "session_id": str(session.id),
            "access_expires_in": int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds())}


def revoke_session(session_id, reason: str = "logout") -> None:
    NativeSession.objects.filter(id=session_id, revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_reason=reason
    )


def revoke_device(device: Device, reason: str = "device_revoked") -> None:
    with transaction.atomic():
        Device.objects.filter(id=device.id, revoked_at__isnull=True).update(revoked_at=timezone.now())
        NativeSession.objects.filter(device=device, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason=reason
        )


def utc_from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)
