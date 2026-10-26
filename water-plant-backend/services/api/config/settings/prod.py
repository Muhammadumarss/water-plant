from django.core.exceptions import ImproperlyConfigured

from .base import *
from .base import SECRET_KEY, SIMPLE_JWT, env

if not SECRET_KEY or len(SECRET_KEY) < 40:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set to a long random value in production")
if not env("JWT_SIGNING_KEY"):
    raise ImproperlyConfigured("JWT_SIGNING_KEY must be set separately in production")
SIMPLE_JWT["SIGNING_KEY"] = env("JWT_SIGNING_KEY")

DEBUG = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
