"""Money and quantity primitives.

Money is numeric(14,2) and travels as a decimal string plus currency.
Quantities are numeric(14,3); container quantities must be whole numbers.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import models

from .errors import ValidationFailed

SUPPORTED_CURRENCIES = ("PKR",)
DEFAULT_CURRENCY = "PKR"
CENT = Decimal("0.01")
MILLI = Decimal("0.001")
ZERO = Decimal("0.00")


def money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"Invalid amount: {value!r}") from exc


def quantity(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(MILLI, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"Invalid quantity: {value!r}") from exc


def money_str(value) -> str:
    return f"{money(value):.2f}"


def qty_str(value) -> str:
    q = quantity(value)
    return str(q.normalize()) if q == q.to_integral_value() else f"{q:.3f}"


def money_payload(value, currency=DEFAULT_CURRENCY) -> dict:
    return {"amount": money_str(value), "currency": currency}


def require_whole(value: Decimal, field: str = "quantity") -> None:
    if quantity(value) != quantity(value).to_integral_value():
        raise ValidationFailed("Container quantities must be whole numbers.", fields={field: ["Must be whole."]})


def require_currency(currency: str, field: str = "currency") -> None:
    if currency not in SUPPORTED_CURRENCIES:
        raise ValidationFailed("Unsupported currency.", fields={field: [f"Supported: {', '.join(SUPPORTED_CURRENCIES)}"]})


class MoneyField(models.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 2)
        super().__init__(*args, **kwargs)


class QuantityField(models.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 3)
        super().__init__(*args, **kwargs)


def currency_field():
    return models.CharField(max_length=3, default=DEFAULT_CURRENCY, choices=[(c, c) for c in SUPPORTED_CURRENCIES])


def currency_check(name: str, field: str = "currency") -> models.CheckConstraint:
    return models.CheckConstraint(condition=models.Q(**{f"{field}__in": SUPPORTED_CURRENCIES}), name=name)
