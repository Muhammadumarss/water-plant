import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from common.models import BusinessOwned
from common.phone import normalize_phone


class Role(models.TextChoices):
    OWNER = "OWNER", "Owner"
    CASHIER = "CASHIER", "Cashier"
    OPERATOR = "OPERATOR", "Operator"
    RIDER = "RIDER", "Rider"
    CUSTOMER = "CUSTOMER", "Customer"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, username, password=None, **extra):
        if not username:
            raise ValueError("username is required")
        if extra.get("phone"):
            extra["phone_normalized"] = normalize_phone(extra.pop("phone"))
        user = self.model(username=username, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(username, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """Custom user defined before the first migration."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=150, unique=True)
    full_name = models.CharField(max_length=200, blank=True)
    phone_normalized = models.CharField(max_length=20, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    contact_verified_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False, help_text="Django Admin access (operational support only).")
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.full_name or self.username


class Membership(BusinessOwned):
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=16, choices=Role.choices)
    # Explicit extra permissions, e.g. "quality.release", "delivery.dispatch".
    extra_capabilities = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "business"], name="membership_unique_user_business")]


class Device(BusinessOwned):
    client_identifier = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    assigned_till = models.ForeignKey(
        "inventory.StockLocation", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    offline_enabled = models.BooleanField(default=False)
    enrolled_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "client_identifier"], name="device_unique_client"),
        ]

    @property
    def is_active(self):
        return self.revoked_at is None


class NativeSession(models.Model):
    """Desktop/mobile token session. Refresh rotation locks this row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="native_sessions")
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.PROTECT, related_name="sessions")
    current_refresh_jti = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=60, blank=True)

    @property
    def is_live(self):
        return self.revoked_at is None and self.expires_at > timezone.now()
