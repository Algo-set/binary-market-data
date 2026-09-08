from decimal import Decimal, localcontext

from .._decimal import CONTEXT
from ..types import (
    BookSide,
    Level,
    LevelUpdate,
    NormalizedUpdate,
    SnapshotUpdate,
    UpdateType,
    Venue,
    unix_time_ms,
)
from ._parsing import (
    ParseError,
    array,
    load,
    number,
    obj,
    optional_text,
    sequence,
    text_field,
    timestamp,
)


def websocket_subscription(request_id: int, market_tickers: list[str] | tuple[str, ...]) -> dict:
    return {
        "id": request_id,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": list(market_tickers),
            "use_yes_price": True,
        },
    }


def parse_rest_snapshot(
    ticker: str,
    text: str | bytes,
    *,
    received_ts_ms: int | None = None,
) -> SnapshotUpdate:
    root = obj(load(text))
    book = obj(root.get("orderbook_fp", root.get("orderbook")), "orderbook_fp")
    yes = _levels(book, ("yes_dollars", "yes_dollars_fp"))
    no = _levels(book, ("no_dollars", "no_dollars_fp"))
    with localcontext(CONTEXT):
        asks = tuple(Level(Decimal(1) - level.price, level.quantity) for level in no)
    return SnapshotUpdate(
        venue=Venue.KALSHI,
        instrument_id=ticker,
        bids=yes,
        asks=asks,
        received_ts_ms=unix_time_ms() if received_ts_ms is None else received_ts_ms,
    )


def parse_websocket_message(
    text: str | bytes,
    *,
    received_ts_ms: int | None = None,
) -> list[NormalizedUpdate]:
    """Parse only a channel subscribed with use_yes_price=true. No WS transport is provided."""
    root = obj(load(text))
    message = obj(root.get("msg"), "msg")
    kind = root.get("type")
    if kind not in ("orderbook_snapshot", "orderbook_delta"):
        return []
    metadata = dict(
        venue=Venue.KALSHI,
        instrument_id=text_field(message, "market_ticker"),
        market_id=optional_text(message.get("market_id")),
        sequence=sequence(root.get("seq")),
        source_ts_ms=timestamp(message.get("ts_ms")),
        received_ts_ms=unix_time_ms() if received_ts_ms is None else received_ts_ms,
    )
    if kind == "orderbook_snapshot":
        return [
            SnapshotUpdate(
                **metadata,
                bids=_levels(message, ("yes_dollars_fp", "yes_dollars")),
                asks=_levels(message, ("no_dollars_fp", "no_dollars")),
            )
        ]
    outcome = message.get("outcome_side", message.get("side"))
    if not isinstance(outcome, str) or outcome.lower() not in ("yes", "no"):
        raise ParseError("msg.outcome_side")
    return [
        LevelUpdate(
            **metadata,
            side=BookSide.BID if outcome.lower() == "yes" else BookSide.ASK,
            price=number(message.get("price_dollars"), "msg.price_dollars"),
            quantity=number(message.get("delta_fp"), "msg.delta_fp"),
            update_type=UpdateType.ADD_LEVEL,
        )
    ]


def _levels(value: dict, keys: tuple[str, str]) -> tuple[Level, ...]:
    rows = next((value[key] for key in keys if isinstance(value.get(key), list)), None)
    result = []
    for row in array(rows, keys[0]):
        row = array(row, keys[0])
        if len(row) < 2:
            raise ParseError(keys[0])
        result.append(Level(number(row[0], keys[0]), number(row[1], keys[0])))
    return tuple(result)
