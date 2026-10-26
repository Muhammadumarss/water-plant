from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def business_tz() -> ZoneInfo:
    return ZoneInfo(settings.BUSINESS_TIMEZONE)


def business_today() -> date:
    return timezone.now().astimezone(business_tz()).date()


def to_business_date(moment: datetime) -> date:
    return moment.astimezone(business_tz()).date()
