from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "modules.audit"
    label = "audit"
    default_auto_field = "django.db.models.BigAutoField"
