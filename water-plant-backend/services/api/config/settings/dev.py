from .base import *
from .base import env

DEBUG = True
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-secret-key-change-me-0123456789")
SIMPLE_JWT["SIGNING_KEY"] = env("JWT_SIGNING_KEY", SECRET_KEY)
ALLOWED_HOSTS = ["*"]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
]
