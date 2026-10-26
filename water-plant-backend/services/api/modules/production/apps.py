from django.apps import AppConfig


class ProductionConfig(AppConfig):
    name = "modules.production"
    label = "production"
    default_auto_field = "django.db.models.BigAutoField"
