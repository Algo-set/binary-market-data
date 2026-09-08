from ..types import (
    BookSide,
    Level,
    LevelUpdate,
    NormalizedUpdate,
    SnapshotUpdate,
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
    text_field,
    timestamp,
)


def subscription(asset_ids: list[str] | tuple[str, ...]) -> dict:
    return {"assets_ids": list(asset_ids), "type": "market", "initial_dump": True}


def parse_message(
    text: str | bytes, *, received_ts_ms: int | None = None
) -> list[NormalizedUpdate]:
    value = load(text)
    received = unix_time_ms() if received_ts_ms is None else received_ts_ms
    items = value if isinstance(value, list) else [value]
    result: list[NormalizedUpdate] = []
    for item in items:
        item = obj(item)
        kind = item.get("event_type")
        if kind not in ("book", "price_change"):
            continue
        metadata = dict(
            venue=Venue.POLYMARKET,
            market_id=optional_text(item.get("market")),
            source_ts_ms=timestamp(item.get("timestamp")),
            received_ts_ms=received,
        )
        if kind == "book":
            result.append(
                SnapshotUpdate(
                    **metadata,
                    instrument_id=text_field(item, "asset_id"),
                    bids=_levels(item.get("bids"), "bids"),
                    asks=_levels(item.get("asks"), "asks"),
                )
            )
        else:
            for change in array(item.get("price_changes"), "price_changes"):
                change = obj(change, "price_changes.item")
                raw_side = change.get("side")
                if raw_side in ("BUY", "buy"):
                    side = BookSide.BID
                elif raw_side in ("SELL", "sell"):
                    side = BookSide.ASK
                else:
                    raise ParseError("price_changes.side")
                result.append(
                    LevelUpdate(
                        **metadata,
                        instrument_id=text_field(change, "asset_id"),
                        side=side,
                        price=number(change.get("price"), "price_changes.price"),
                        quantity=number(change.get("size"), "price_changes.size"),
                    )
                )
    return result


def _levels(value: object, field: str) -> tuple[Level, ...]:
    result = []
    for level in array(value, field):
        level = obj(level, field)
        result.append(Level(number(level.get("price"), field), number(level.get("size"), field)))
    return tuple(result)
