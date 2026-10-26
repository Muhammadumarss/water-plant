"""Shared settings. Environment-specific modules import from here."""

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def database_from_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must be a PostgreSQL URL")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": int(env("DB_CONN_MAX_AGE", "60")),
        "ATOMIC_REQUESTS": False,
        "OPTIONS": {"connect_timeout": int(env("DB_CONNECT_TIMEOUT", "5"))},
    }


SECRET_KEY = env("DJANGO_SECRET_KEY", "")
DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    # Domain modules (code modules, not microservices)
    "modules.organization",
    "modules.identity",
    "modules.customers",
    "modules.catalog",
    "modules.inventory",
    "modules.production",
    "modules.plant_care",
    "modules.orders",
    "modules.billing",
    "modules.finance",
    "modules.fulfillment",
    "modules.notifications",
    "modules.support",
    "modules.documents",
    "modules.audit",
    "modules.synchronization",
    "modules.jobs",
    "modules.idempotency",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "common.middleware.RequestIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {
    "default": database_from_url(
        env("DATABASE_URL", "postgresql://water:water@localhost:5432/water_plant")
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "identity.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Store UTC; group business days in the business timezone.
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("ur", "Urdu")]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
BUSINESS_TIMEZONE = "Asia/Karachi"
BUSINESS_CURRENCY = "PKR"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Private document storage. Never served directly by the web server.
DOCUMENT_STORAGE_BACKEND = env("DOCUMENT_STORAGE_BACKEND", "local")  # local | s3
DOCUMENT_LOCAL_ROOT = Path(env("DOCUMENT_LOCAL_ROOT", str(BASE_DIR / "var" / "documents")))
DOCUMENT_S3_BUCKET = env("DOCUMENT_S3_BUCKET", "")
DOCUMENT_S3_ENDPOINT_URL = env("DOCUMENT_S3_ENDPOINT_URL", "") or None
DOCUMENT_DOWNLOAD_TTL_SECONDS = 300
DOCUMENT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DOCUMENT_ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "text/csv"}

# Browser sessions
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", True)
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
SESSION_COOKIE_AGE = 60 * 60 * 12

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "modules.identity.authentication.NativeSessionJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "common.pagination.DefaultPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "common.errors.api_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
        "token": "20/min",
        "public": "60/min",
        "webhook": "600/min",
    },
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "COERCE_DECIMAL_TO_STRING": True,
}

# Desktop/mobile tokens. Rotation and replay protection are implemented with
# the NativeSession row lock (modules/identity/services.py), not by defaults.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(hours=24),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("JWT_SIGNING_KEY") or SECRET_KEY,
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Water Plant Management API",
    "DESCRIPTION": "Versioned API for website, staff web, Windows desktop, delivery and future mobile apps.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

# Celery: logical queues; workers may share processes initially.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_DEFAULT_QUEUE = "maintenance"
CELERY_TASK_ROUTES = {
    "jobs.process_event_delivery": {"queue": "notifications"},
    "notifications.*": {"queue": "notifications"},
    "documents.*": {"queue": "documents"},
    "reports.*": {"queue": "documents"},
}
CELERY_BEAT_SCHEDULE = {
    "dispatch-outbox": {"task": "jobs.dispatch_outbox", "schedule": 15.0},
    "recover-stale-deliveries": {"task": "jobs.recover_stale_deliveries", "schedule": 60.0},
    "generate-recurring-orders": {"task": "orders.generate_recurring_orders", "schedule": crontab(minute=5)},
    "expire-reservations": {"task": "orders.expire_reservations", "schedule": crontab(minute="*/15")},
    "maintenance-alerts": {"task": "plant_care.maintenance_alerts", "schedule": crontab(hour=3, minute=0)},
    "overdue-reminders": {"task": "billing.overdue_reminders", "schedule": crontab(hour=5, minute=0)},
    "reconcile-projections": {"task": "jobs.reconcile_projections", "schedule": crontab(hour=2, minute=30)},
}
# Wake the dispatcher after commit. The durable outbox scan is the real guarantee.
OUTBOX_WAKE_ON_COMMIT = env_bool("OUTBOX_WAKE_ON_COMMIT", True)
OUTBOX_LEASE_SECONDS = 120
OUTBOX_MAX_ATTEMPTS = 8

# Messaging: ONE selected channel per customer; fallback OFF in v1.
MESSAGING_PROVIDERS = {
    "whatsapp": env("MESSAGING_WHATSAPP_ADAPTER", "infrastructure.messaging.console.ConsoleAdapter"),
    "sms": env("MESSAGING_SMS_ADAPTER", "infrastructure.messaging.console.ConsoleAdapter"),
}
MESSAGING_WEBHOOK_SECRETS = {
    "console": env("MESSAGING_WEBHOOK_SECRET_CONSOLE", ""),
    "whatsapp": env("MESSAGING_WEBHOOK_SECRET_WHATSAPP", ""),
    "sms": env("MESSAGING_WEBHOOK_SECRET_SMS", ""),
}
MESSAGING_SUBMIT_TIMEOUT_SECONDS = 10
MESSAGING_MAX_ATTEMPTS = 5

# Command transaction bounds (PostgreSQL only).
COMMAND_LOCK_TIMEOUT_MS = 5000
COMMAND_STATEMENT_TIMEOUT_MS = 15000

# Offline synchronization
SYNC_MAX_BATCH = 100
SYNC_CHANGE_WINDOW_DAYS = 30
OFFLINE_LEASE_DEFAULT_HOURS = 10

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "common.middleware.RequestIdLogFilter"}},
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain", "filters": ["request_id"]},
    },
    "loggers": {
        "security": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
