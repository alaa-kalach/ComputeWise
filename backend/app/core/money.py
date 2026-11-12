"""Money in API models: USD as a Decimal in Python, a 4-decimal string in JSON (D7).

Use the `Money` type in request and response models instead of Decimal or float:

    class GrantOut(BaseModel):
        ceiling_usd: Money | None        # -> "150.0000" or null

    class TopUpIn(BaseModel):
        amount_usd: PositiveMoney        # accepts "20", "20.5", "20.1234"; refuses 20.5 (a float)

Input must be a string (or an int); JSON numbers with a fraction are refused, because a
float has already lost cents before it reaches us.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, PlainSerializer, WithJsonSchema

CENT4 = Decimal("0.0001")
_MAX = Decimal("99999999.9999")  # NUMERIC(12,4)


def quantize(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(CENT4, rounding=ROUND_HALF_UP)


def money_str(value: Decimal) -> str:
    return str(quantize(value))


def _parse(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError('send money as a string, e.g. "12.5000"')
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, str)):
        try:
            d = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError("not a decimal amount") from None
    else:
        raise ValueError("not a decimal amount")
    if not d.is_finite():
        raise ValueError("not a decimal amount")
    if d != d.quantize(CENT4):
        raise ValueError("at most 4 decimal places")
    if abs(d) > _MAX:
        raise ValueError("amount too large")
    return quantize(d)


def _non_negative(d: Decimal) -> Decimal:
    if d < 0:
        raise ValueError("must not be negative")
    return d


def _positive(d: Decimal) -> Decimal:
    if d <= 0:
        raise ValueError("must be greater than 0")
    return d


_SCHEMA = WithJsonSchema({"type": "string", "pattern": r"^-?\d+(\.\d{1,4})?$", "examples": ["12.5000"]})
_SERIALIZE = PlainSerializer(money_str, return_type=str)

Money = Annotated[Decimal, BeforeValidator(_parse), _SERIALIZE, _SCHEMA]
NonNegativeMoney = Annotated[Decimal, BeforeValidator(_parse), AfterValidator(_non_negative), _SERIALIZE, _SCHEMA]
PositiveMoney = Annotated[Decimal, BeforeValidator(_parse), AfterValidator(_positive), _SERIALIZE, _SCHEMA]

__all__ = ["CENT4", "Money", "NonNegativeMoney", "PositiveMoney", "money_str", "quantize"]
