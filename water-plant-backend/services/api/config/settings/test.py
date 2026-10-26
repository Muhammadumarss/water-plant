"""Test settings. Tests run against real PostgreSQL: SQLite cannot prove locking behaviour."""

import tempfile
from pathlib import Path

from .base import *
from .base import database_from_url, env

SECRET_KEY = "test-secret-key-not-for-production-0123456789abcdef"
SIMPLE_JWT["SIGNING_KEY"] = SECRET_KEY
DATABASES = {
    "default": database_from_url(
        env("TEST_DATABASE_URL", env("DATABASE_URL", "postgresql://water:water@localhost:5432/water_plant"))
    )
}
DATABASES["default"]["CONN_MAX_AGE"] = 0
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
OUTBOX_WAKE_ON_COMMIT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
DOCUMENT_LOCAL_ROOT = Path(tempfile.gettempdir()) / "water-plant-test-documents"
MESSAGING_PROVIDERS = {
    "whatsapp": "infrastructure.messaging.fake.FakeAdapter",
    "sms": "infrastructure.messaging.fake.FakeAdapter",
}
MESSAGING_WEBHOOK_SECRETS = {"fake": "test-webhook-secret", "whatsapp": "test-webhook-secret"}
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
    "login": "1000/min",
    "token": "1000/min",
    "public": "1000/min",
    "webhook": "1000/min",
}
