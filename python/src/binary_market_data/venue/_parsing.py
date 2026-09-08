import json
from decimal import Decimal

from .._decimal import decimal_value


class ParseError(ValueError):
    def __init__(self, field: str = "json") -> None:
        self.field = field
        super().__init__(f"invalid_venue_payload:{field}")


def _constant(_value: str) -> None:
    raise ParseError()


def load(text: str | bytes) -> object:
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ParseError() from None


def obj(value: object, field: str = "object") -> dict:
    if not isinstance(value, dict):
        raise ParseError(field)
    return value


def array(value: object, field: str) -> list:
    if not isinstance(value, list):
        raise ParseError(field)
    return value


def text_field(value: dict, field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ParseError(field)
    return result


def number(value: object, field: str) -> Decimal:
    try:
        return decimal_value(value)
    except ValueError:
        raise ParseError(field) from None


def optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def timestamp(value: object) -> int | None:
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
    return value if type(value) is int and -(2**63) <= value <= 2**63 - 1 else None


def sequence(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**64 - 1 else None
