from django.apps import AppConfig


class IdempotencyConfig(AppConfig):
    name = "modules.idempotency"
    label = "idempotency"
    default_auto_field = "django.db.models.BigAutoField"
