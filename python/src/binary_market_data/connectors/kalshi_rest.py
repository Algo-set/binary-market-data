"""Public REST polling, deduplicated at the requested output depth."""

import asyncio
from dataclasses import dataclass
from functools import partial
from urllib.parse import quote

import httpx

from .._http import client_context, endpoint, get_text
from ..book import BookError, BookStore, _depth
from ..discovery import KALSHI_REST
from ..types import BookMessage, ConnectionState, ConnectorMessage, StatusMessage, Venue
from ..venue import ParseError, kalshi
from ._common import identifier, managed_stream, positive


@dataclass(frozen=True, slots=True)
class Config:
    market_ticker: str
    rest_base: str = KALSHI_REST
    depth: int = 20
    poll_interval: float = 1.0
    reconnect_max: float = 30.0

    def __post_init__(self) -> None:
        identifier(self.market_ticker)
        endpoint(self.rest_base)
        _depth(self.depth)
        positive(self.poll_interval, "poll_interval")
        positive(self.reconnect_max, "reconnect_max")
        object.__setattr__(self, "poll_interval", max(0.1, self.poll_interval))
        if self.reconnect_max < self.poll_interval:
            raise ValueError("invalid_reconnect_max")

    @classmethod
    def for_market(cls, market_ticker: str) -> "Config":
        return cls(market_ticker)


def stream(config: Config, *, queue_size: int = 256, client: httpx.AsyncClient | None = None):
    """An async context manager yielding messages; exit cancels polling and closes owned IO."""
    return managed_stream(partial(run, config, client=client), queue_size)


async def run(
    config: Config,
    output: asyncio.Queue[ConnectorMessage],
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Advanced API: caller owns task cancellation and must supply a bounded queue."""
    if output.maxsize <= 0:
        raise ValueError("output_queue_must_be_bounded")
    url = endpoint(
        config.rest_base, "markets/" + quote(config.market_ticker, safe="") + "/orderbook"
    )
    connected = False
    last_levels = None
    retry_delay = config.poll_interval
    books = BookStore()
    await output.put(StatusMessage(Venue.KALSHI, ConnectionState.CONNECTING))
    async with client_context(client) as session:
        while True:
            error = None
            try:
                body = await get_text(session, url, params={"depth": str(config.depth)})
                update = kalshi.parse_rest_snapshot(config.market_ticker, body)
                view = books.apply(update, config.depth)
            except ParseError:
                error = "invalid_venue_payload"
            except BookError:
                error = "invalid_book_update"
            except (httpx.HTTPError, ValueError):
                error = "venue_request_failed"
            if error is not None:
                connected = False
                await output.put(
                    StatusMessage(Venue.KALSHI, ConnectionState.DISCONNECTED, detail=error)
                )
                retry_delay = min(retry_delay * 2, config.reconnect_max)
            else:
                if not connected:
                    await output.put(StatusMessage(Venue.KALSHI, ConnectionState.CONNECTED))
                    connected = True
                levels = (view.bids, view.asks)
                if levels != last_levels:
                    last_levels = levels
                    await output.put(BookMessage(view))
                retry_delay = config.poll_interval
            await asyncio.sleep(retry_delay)
