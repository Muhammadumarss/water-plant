from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import NativeSession


class NativeSessionJWTAuthentication(JWTAuthentication):
    """JWT that is only valid while its native session and device are live.

    Logout, device revocation and user deactivation take effect immediately
    instead of waiting for the five-minute access token to expire."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        sid = validated_token.get("sid")
        if not sid:
            raise AuthenticationFailed("Token is not bound to a session.")
        session = NativeSession.objects.select_related("device").filter(id=sid).first()
        if session is None or session.user_id != user.id or not session.is_live:
            raise AuthenticationFailed("Session is no longer valid.")
        if session.device is not None and session.device.revoked_at is not None:
            raise AuthenticationFailed("Device has been revoked.")
        now = timezone.now()
        if session.last_seen_at is None or now - session.last_seen_at > timedelta(seconds=60):
            NativeSession.objects.filter(id=session.id).update(last_seen_at=now)
        user._native_session = session
        return user
