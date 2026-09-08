"""Exact bounded decimal input; arithmetic is independent of the caller's context."""

from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow

# Enough precision for every accepted input and its sum, difference or midpoint.
# Trap rather than silently rounding if a book eventually exceeds this capacity.
CONTEXT = Context(prec=128, traps=[Inexact, InvalidOperation, Overflow])


def decimal_value(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("invalid_decimal")
    if isinstance(value, str) and (not value or len(value) > 128 or value != value.strip()):
        raise ValueError("invalid_decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_decimal") from None
    if not result.is_finite():
        raise ValueError("invalid_decimal")
    parts = result.as_tuple()
    if len(parts.digits) > 60 or not -28 <= parts.exponent <= 28:
        raise ValueError("decimal_out_of_range")
    return result
