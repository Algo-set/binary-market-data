"""Credential-free, rolling five-minute discovery and REST book snapshots.

A contract is selected only when public timestamps prove a 300-second window.
No shorter remaining lifetime, listing date, spot feed or different duration is
substituted. Crypto.com has an explicit unsupported transport result.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx

from ._http import client_context, endpoint, get_text
from .book import BookStore
from .types import JsonRecord, Level, SnapshotUpdate, Venue, unix_time_ms
from .venue import kalshi, polymarket
from .venue._parsing import ParseError, array, load, number, obj, text_field

BASES = {
    Venue.POLYMARKET: "https://gamma-api.polymarket.com",
    Venue.KALSHI: "https://external-api.kalshi.com/trade-api/v2",
    Venue.LIMITLESS: "https://api.limitless.exchange",
}
CLOB = "https://clob.polymarket.com"


@dataclass(frozen=True, slots=True)
class Contract(JsonRecord):
    venue: Venue
    market_id: str
    instrument_ids: tuple[str, ...]
    window_start_ms: int
    window_end_ms: int

    def current(self, now_ms: int) -> bool:
        return self.window_end_ms - self.window_start_ms == 300_000 and (
            self.window_start_ms <= now_ms < self.window_end_ms
        )


def timestamp_ms(value: object) -> int:
    if type(value) is int:
        return value
    if not isinstance(value, str):
        raise ValueError("invalid_window_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("window_timezone_required")
    return int(parsed.timestamp() * 1000)


def select_contract(venue: Venue, row: dict, now_ms: int, asset: str) -> Contract | None:
    """Unknown timing is excluded. A malformed book is never a valid empty book."""
    try:
        if venue == Venue.POLYMARKET:
            if not (
                row.get("active") is True
                and row.get("closed") is False
                and row.get("enableOrderBook") is True
                and row.get("acceptingOrders") is True
            ):
                return None
            if not text_field(row, "slug").startswith(asset.lower() + "-updown-5m-"):
                return None
            start = timestamp_ms(row["eventStartTime"])
            end = timestamp_ms(row["endDate"])
            raw = row["clobTokenIds"]
            ids = array(load(raw) if isinstance(raw, str) else raw, "clobTokenIds")
            market_id = text_field(row, "conditionId")
            if len(ids) != 2:
                return None
        elif venue == Venue.KALSHI:
            if row.get("status") not in ("active", "open") or row.get("market_type") != "binary":
                return None
            # open_time is the tradable window, not expected settlement/expiration.
            start, end = timestamp_ms(row["open_time"]), timestamp_ms(row["close_time"])
            market_id = text_field(row, "ticker")
            if not market_id.startswith("KX" + asset.upper()):
                return None
            ids = [market_id]
        elif venue == Venue.LIMITLESS:
            if row.get("tradeType") != "clob" or row.get("expired") is not False:
                return None
            if row.get("status") != "FUNDED":
                return None
            market_id = text_field(row, "slug")
            aliases = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
            prefixes = (asset.lower() + "-", aliases.get(asset.upper(), asset.lower()) + "-")
            if not market_id.lower().startswith(prefixes):
                return None
            start, end = timestamp_ms(row["startAt"]), timestamp_ms(row["expirationTimestamp"])
            # The venue's REST book is YES-centric and bound to this exact slug.
            ids = [market_id]
        else:
            return None
        if not all(isinstance(item, str) and item.strip() for item in ids):
            return None
        if len(set(ids)) != len(ids):
            return None
        contract = Contract(venue, market_id, tuple(ids), start, end)
        return contract if contract.current(now_ms) else None
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


async def discover(
    venue: Venue,
    asset: str = "BTC",
    *,
    pages: int = 3,
    now_ms: int | None = None,
    base: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[Contract, ...]:
    venue = Venue(venue)
    if venue not in BASES or not asset.isascii() or not asset.isalnum() or not 1 <= pages <= 20:
        raise ValueError("invalid_discovery_configuration")
    now = unix_time_ms() if now_ms is None else now_ms
    base = base or BASES[venue]
    cursor = None
    contracts = {}
    async with client_context(client) as session:
        for page in range(1, pages + 1):
            if venue == Venue.POLYMARKET:
                params = {"slug": f"{asset.lower()}-updown-5m-{now // 300000 * 300}"}
                path = "markets"
            elif venue == Venue.KALSHI:
                params = {
                    "min_close_ts": now // 1000,
                    "max_close_ts": now // 1000 + 300,
                    "limit": 100,
                }
                if cursor:
                    params["cursor"] = cursor
                path = "markets"
            else:
                params = {"tradeType": "clob", "limit": 25, "page": page, "sortBy": "newest"}
                path = "markets/active"
            body = load(await get_text(session, endpoint(base, path), params=params))
            if venue == Venue.POLYMARKET:
                rows = array(body, "markets")
            else:
                rows = array(
                    obj(body).get("markets" if venue == Venue.KALSHI else "data"), "markets"
                )
            for row in rows:
                contract = select_contract(venue, obj(row), now, asset)
                if contract:
                    contracts[contract.market_id] = contract
            if venue == Venue.POLYMARKET:
                break
            if venue == Venue.KALSHI:
                next_cursor = body.get("cursor")
                if not next_cursor:
                    break
                if not isinstance(next_cursor, str) or next_cursor == cursor:
                    raise ParseError("cursor")
                cursor = next_cursor
            elif len(rows) < 25:
                break
    return tuple(contracts.values())


def parse_limitless_book(slug: str, text: str | bytes) -> SnapshotUpdate:
    root = obj(load(text))
    sides = []
    for side in ("bids", "asks"):
        levels = []
        for value in array(root.get(side), side):
            value = obj(value)
            levels.append(
                Level(number(value.get("price"), "price"), number(value.get("size"), "size"))
            )
        sides.append(tuple(levels))
    return SnapshotUpdate(
        venue=Venue.LIMITLESS, instrument_id=slug, market_id=slug, bids=sides[0], asks=sides[1]
    )


async def fetch_books(
    contract: Contract,
    *,
    depth: int = 20,
    base: str | None = None,
    client: httpx.AsyncClient | None = None,
):
    """Fresh snapshots; do not merge generations or infer a missing side."""
    if not contract.current(unix_time_ms()):
        raise ValueError("contract_window_closed")
    store, views = BookStore(), []
    async with client_context(client) as session:
        for instrument in contract.instrument_ids:
            if contract.venue == Venue.POLYMARKET:
                body = await get_text(
                    session, endpoint(base or CLOB, "book"), params={"token_id": instrument}
                )
                root = obj(load(body))
                if root.get("asset_id") != instrument or root.get("market") != contract.market_id:
                    raise ParseError("book_identity")
                # REST and WebSocket snapshots share the documented level shape.
                import json

                root["event_type"] = "book"
                update = polymarket.parse_message(json.dumps(root, default=str))[0]
            elif contract.venue == Venue.KALSHI:
                body = await get_text(
                    session,
                    endpoint(
                        base or BASES[contract.venue],
                        "markets/" + quote(instrument, safe="") + "/orderbook",
                    ),
                    params={"depth": depth},
                )
                update = kalshi.parse_rest_snapshot(instrument, body)
            elif contract.venue == Venue.LIMITLESS:
                body = await get_text(
                    session,
                    endpoint(
                        base or BASES[contract.venue],
                        "markets/" + quote(instrument, safe="") + "/orderbook",
                    ),
                )
                update = parse_limitless_book(instrument, body)
            else:
                raise ValueError("public_book_transport_unavailable")
            views.append(store.apply(update, depth))
    if not contract.current(unix_time_ms()):
        raise ValueError("contract_window_closed")
    return tuple(views)


async def watch(
    venue: Venue,
    asset: str = "BTC",
    *,
    poll_interval: float = 1,
    pages: int = 3,
    depth: int = 20,
    once: bool = False,
):
    """A pull-based stream with bounded market count and periodic rediscovery."""
    venue = Venue(venue)
    if venue == Venue.CRYPTO_COM:
        yield {
            "message_type": "status",
            "venue": venue,
            "state": "unsupported",
            "detail": "no_verified_credential_free_strike_options_book_transport",
        }
        return
    if not 1 <= poll_interval <= 60 or not 1 <= depth <= 100 or not 1 <= pages <= 20:
        raise ValueError("invalid_poll_configuration")
    contracts, refresh_at, delay = (), 0, poll_interval
    async with client_context() as session:
        while True:
            try:
                now = unix_time_ms()
                contracts = tuple(c for c in contracts if c.current(now))
                if now >= refresh_at or not contracts:
                    contracts = await discover(venue, asset, pages=pages, client=session)
                    if len(contracts) > 20:
                        raise ValueError("too_many_matching_contracts")
                    refresh_at = now + 15000
                if not contracts:
                    yield {
                        "message_type": "status",
                        "venue": venue,
                        "state": "unavailable",
                        "detail": "no_current_five_minute_contract_in_scanned_catalog",
                    }
                for contract in contracts:
                    for book in await fetch_books(contract, depth=depth, client=session):
                        if contract.current(unix_time_ms()):
                            yield {
                                "message_type": "contract_book",
                                "contract": contract.to_dict(),
                                "book": book.to_dict(),
                            }
                delay = poll_interval
            except (httpx.HTTPError, ValueError):
                # Never echo upstream bodies, URLs, request headers or account data.
                contracts = ()
                yield {
                    "message_type": "status",
                    "venue": venue,
                    "state": "disconnected",
                    "detail": "public_feed_unavailable_or_invalid",
                }
                delay = min(30, delay * 2)
            if once:
                return
            await asyncio.sleep(delay)
