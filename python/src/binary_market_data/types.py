"""Immutable public messages. Decimal values serialize as strings, as in Rust."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TypeAlias

from ._decimal import decimal_value


class Venue(StrEnum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    LIMITLESS = "limitless"
    CRYPTO_COM = "crypto_com"
    PANCAKESWAP = "pancakeswap"


class BookSide(StrEnum):
    BID = "bid"
    ASK = "ask"


class ConnectionState(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class UpdateType(StrEnum):
    SNAPSHOT = "snapshot"
    SET_LEVEL = "set_level"
    ADD_LEVEL = "add_level"


def unix_time_ms() -> int:
    return max(0, min(time.time_ns() // 1_000_000, 2**63 - 1))


class JsonRecord:
    def to_dict(self) -> dict:
        return json.loads(self.to_json())

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(asdict(self), default=_json_default, indent=indent, allow_nan=False)


def _json_default(value: object) -> str:
    if isinstance(value, Decimal) and value.is_finite():
        return format(value, "f")
    raise TypeError("unsupported_json_value")


@dataclass(frozen=True, slots=True)
class Level(JsonRecord):
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", decimal_value(self.price))
        object.__setattr__(self, "quantity", decimal_value(self.quantity))


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateMetadata(JsonRecord):
    venue: Venue
    instrument_id: str
    received_ts_ms: int = field(default_factory=unix_time_ms)
    market_id: str | None = None
    sequence: int | None = None
    source_ts_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", Venue(self.venue))
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise ValueError("invalid_instrument_id")
        if self.market_id is not None and not isinstance(self.market_id, str):
            raise ValueError("invalid_market_id")
        for name, value, low, high in (
            ("sequence", self.sequence, 0, 2**64 - 1),
            ("source_ts_ms", self.source_ts_ms, -(2**63), 2**63 - 1),
            ("received_ts_ms", self.received_ts_ms, -(2**63), 2**63 - 1),
        ):
            if value is None and name != "received_ts_ms":
                continue
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"invalid_{name}")


@dataclass(frozen=True, slots=True, kw_only=True)
class SnapshotUpdate(UpdateMetadata):
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()
    update_type: UpdateType = field(default=UpdateType.SNAPSHOT, init=False)

    def __post_init__(self) -> None:
        UpdateMetadata.__post_init__(self)
        for name in ("bids", "asks"):
            levels = tuple(getattr(self, name))
            if not all(isinstance(level, Level) for level in levels):
                raise ValueError("invalid_levels")
            object.__setattr__(self, name, levels)


@dataclass(frozen=True, slots=True, kw_only=True)
class LevelUpdate(UpdateMetadata):
    side: BookSide
    price: Decimal
    quantity: Decimal
    update_type: UpdateType = UpdateType.SET_LEVEL

    def __post_init__(self) -> None:
        UpdateMetadata.__post_init__(self)
        object.__setattr__(self, "side", BookSide(self.side))
        object.__setattr__(self, "price", decimal_value(self.price))
        object.__setattr__(self, "quantity", decimal_value(self.quantity))
        object.__setattr__(self, "update_type", UpdateType(self.update_type))
        if self.update_type == UpdateType.SNAPSHOT:
            raise ValueError("invalid_level_update_type")


NormalizedUpdate: TypeAlias = SnapshotUpdate | LevelUpdate


@dataclass(frozen=True, slots=True, kw_only=True)
class BookView(UpdateMetadata):
    published_ts_ms: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    best_bid: Level | None
    best_ask: Level | None
    midpoint: Decimal | None
    spread: Decimal | None
    crossed: bool


@dataclass(frozen=True, slots=True)
class StatusMessage(JsonRecord):
    venue: Venue
    state: ConnectionState
    at_ms: int = field(default_factory=unix_time_ms)
    detail: str | None = None
    message_type: str = field(default="status", init=False)


@dataclass(frozen=True, slots=True)
class BookMessage(JsonRecord):
    book: BookView

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            {"message_type": "book", **asdict(self.book)},
            default=_json_default,
            indent=indent,
            allow_nan=False,
        )


ConnectorMessage: TypeAlias = StatusMessage | BookMessage
