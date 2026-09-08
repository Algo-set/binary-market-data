"""Public catalog pages and condition-to-token resolution, with explicit pagination."""

from dataclasses import dataclass, replace
from urllib.parse import quote

import httpx

from ._http import client_context, endpoint, get_text
from .types import JsonRecord, Venue
from .venue._parsing import ParseError, array, load, obj

POLYMARKET_CATALOG = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
KALSHI_REST = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_STATUSES = ("open", "unopened", "paused", "closed", "settled")


class DiscoveryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OutcomeInstrument(JsonRecord):
    outcome: str | None
    instrument_id: str


@dataclass(frozen=True, slots=True)
class MarketDescriptor(JsonRecord):
    venue: Venue
    market_id: str
    title: str | None
    slug: str | None
    status: str
    book_enabled: bool
    accepting_orders: bool
    close_time: str | None
    instruments: tuple[OutcomeInstrument, ...]

    def book_instrument_ids(self) -> list[str]:
        return [instrument.instrument_id for instrument in self.instruments]


@dataclass(frozen=True, slots=True)
class MarketPage(JsonRecord):
    venue: Venue
    markets: tuple[MarketDescriptor, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PolymarketDiscoveryConfig:
    limit: int = 20
    catalog_base: str = POLYMARKET_CATALOG
    cursor: str | None = None
    query: str | None = None


@dataclass(frozen=True, slots=True)
class KalshiDiscoveryConfig:
    limit: int = 20
    rest_base: str = KALSHI_REST
    cursor: str | None = None
    query: str | None = None
    status: str | None = "open"


def _text(value: dict, *keys: str) -> str | None:
    return next((value[k] for k in keys if isinstance(value.get(k), str) and value[k]), None)


def _required(value: dict, *keys: str) -> str:
    result = _text(value, *keys)
    if result is None:
        raise ParseError("identifier")
    return result


def _bool(value: dict, *keys: str, default: bool = False) -> bool:
    return next((value[k] for k in keys if type(value.get(k)) is bool), default)


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    values = array(load(value) if isinstance(value, str) else value, "string_array")
    if not all(isinstance(item, str) and item for item in values):
        raise ParseError("string_array.item")
    return values


def parse_polymarket_page(text: str | bytes) -> MarketPage:
    root = obj(load(text))
    markets = []
    for value in array(root.get("markets"), "markets"):
        value = obj(value)
        tokens, outcomes = _strings(value.get("clobTokenIds")), _strings(value.get("outcomes"))
        if tokens and outcomes and len(tokens) != len(outcomes):
            raise ParseError("outcomes")
        instruments = tuple(
            OutcomeInstrument(outcomes[index] if outcomes else None, token)
            for index, token in enumerate(tokens)
        )
        active, closed, archived = (_bool(value, key) for key in ("active", "closed", "archived"))
        status = (
            "archived" if archived else "closed" if closed else "active" if active else "inactive"
        )
        markets.append(
            MarketDescriptor(
                venue=Venue.POLYMARKET,
                market_id=_required(value, "conditionId", "condition_id"),
                title=_text(value, "question", "title"),
                slug=_text(value, "slug", "market_slug"),
                status=status,
                book_enabled=_bool(
                    value, "enableOrderBook", "enable_order_book", default=bool(tokens)
                ),
                accepting_orders=_bool(
                    value, "acceptingOrders", "accepting_orders", default=active and not closed
                ),
                close_time=_text(value, "endDateIso", "endDate", "end_date_iso"),
                instruments=instruments,
            )
        )
    return MarketPage(Venue.POLYMARKET, tuple(markets), _text(root, "next_cursor"))


def parse_kalshi_page(text: str | bytes) -> MarketPage:
    root = obj(load(text))
    markets = []
    for value in array(root.get("markets"), "markets"):
        value = obj(value)
        ticker, status = _required(value, "ticker"), _text(value, "status") or "unknown"
        markets.append(
            MarketDescriptor(
                venue=Venue.KALSHI,
                market_id=ticker,
                title=_text(value, "title", "subtitle", "yes_sub_title"),
                slug=_text(value, "event_ticker"),
                status=status,
                book_enabled=(_text(value, "market_type") or "binary").lower() == "binary",
                accepting_orders=status.lower() in ("open", "active"),
                close_time=_text(
                    value, "close_time", "expiration_time", "expected_expiration_time"
                ),
                instruments=(OutcomeInstrument(None, ticker),),
            )
        )
    return MarketPage(Venue.KALSHI, tuple(markets), _text(root, "cursor"))


def parse_polymarket_market_info(text: str | bytes) -> tuple[OutcomeInstrument, ...]:
    root = obj(load(text))
    tokens = array(root.get("t", root.get("tokens")), "tokens")
    if not tokens:
        raise ParseError("tokens")
    return tuple(
        OutcomeInstrument(_text(obj(token), "o", "outcome"), _required(token, "t", "token_id"))
        for token in tokens
    )


def _filter(page: MarketPage, query: str | None, limit: int) -> MarketPage:
    needle = (query or "").strip().lower()
    return replace(
        page,
        markets=tuple(
            market
            for market in page.markets
            if market.book_enabled
            and (
                not needle
                or any(
                    needle in field.lower()
                    for field in (
                        market.market_id,
                        market.title or "",
                        market.slug or "",
                    )
                )
            )
        )[:limit],
    )


def _limit(limit: int, maximum: int) -> None:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise DiscoveryError("invalid_catalog_limit")


async def _request(base: str, path: str, params: dict, client: httpx.AsyncClient | None) -> str:
    try:
        url = endpoint(base, path)
    except ValueError:
        raise DiscoveryError("invalid_catalog_endpoint") from None
    try:
        async with client_context(client) as session:
            return await get_text(session, url, params=params)
    except (httpx.HTTPError, ValueError):
        raise DiscoveryError("catalog_request_failed") from None


async def discover_polymarket(
    config: PolymarketDiscoveryConfig | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> MarketPage:
    config = config or PolymarketDiscoveryConfig()
    _limit(config.limit, 100)
    params = {"limit": str(config.limit), "closed": "false"}
    if config.cursor is not None:
        params["after_cursor"] = config.cursor
    body = await _request(config.catalog_base, "markets/keyset", params, client)
    try:
        return _filter(parse_polymarket_page(body), config.query, config.limit)
    except ParseError:
        raise DiscoveryError("invalid_catalog_response") from None


async def discover_kalshi(
    config: KalshiDiscoveryConfig | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> MarketPage:
    config = config or KalshiDiscoveryConfig()
    _limit(config.limit, 1000)
    if config.status is not None and config.status not in KALSHI_STATUSES:
        raise DiscoveryError("invalid_catalog_status")
    params = {"limit": str(config.limit)}
    if config.cursor is not None:
        params["cursor"] = config.cursor
    if config.status is not None:
        params["status"] = config.status
    body = await _request(config.rest_base, "markets", params, client)
    try:
        return _filter(parse_kalshi_page(body), config.query, config.limit)
    except ParseError:
        raise DiscoveryError("invalid_catalog_response") from None


async def resolve_polymarket_condition(
    clob_base: str,
    condition_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[OutcomeInstrument, ...]:
    if not condition_id.strip() or condition_id in (".", ".."):
        raise DiscoveryError("invalid_condition_id")
    body = await _request(clob_base, "clob-markets/" + quote(condition_id, safe=""), {}, client)
    try:
        return parse_polymarket_market_info(body)
    except ParseError:
        raise DiscoveryError("invalid_catalog_response") from None
