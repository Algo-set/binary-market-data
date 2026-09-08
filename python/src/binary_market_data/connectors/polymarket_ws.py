"""Public WebSocket books with application heartbeat and snapshot reset on reconnect."""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from functools import partial

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .._http import endpoint
from ..book import BookError, BookStore, _depth
from ..types import BookMessage, ConnectionState, ConnectorMessage, StatusMessage, Venue
from ..venue import ParseError, polymarket
from ._common import identifier, managed_stream, positive

POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(frozen=True, slots=True)
class Config:
    asset_ids: tuple[str, ...]
    endpoint: str = POLYMARKET_WS
    output_depth: int = 20
    reconnect_min: float = 0.25
    reconnect_max: float = 30.0
    heartbeat: float = 10.0
    idle_timeout: float = 30.0

    def __post_init__(self) -> None:
        if isinstance(self.asset_ids, str):
            raise ValueError("invalid_asset_ids")
        values = tuple(dict.fromkeys(self.asset_ids))
        if not 1 <= len(values) <= 1000:
            raise ValueError("invalid_asset_ids")
        for value in values:
            identifier(value)
        object.__setattr__(self, "asset_ids", values)
        endpoint(self.endpoint, websocket=True)
        _depth(self.output_depth)
        for name in ("reconnect_min", "reconnect_max", "heartbeat", "idle_timeout"):
            positive(getattr(self, name), name)
        if self.reconnect_max < self.reconnect_min or self.idle_timeout <= self.heartbeat:
            raise ValueError("invalid_timing_configuration")

    @classmethod
    def for_assets(cls, asset_ids: list[str] | tuple[str, ...]) -> "Config":
        return cls(tuple(asset_ids))


class _SessionError(Exception):
    pass


def stream(config: Config, *, queue_size: int = 256):
    """An async context manager yielding messages; exit also stops heartbeats and reconnects."""
    return managed_stream(partial(run, config), queue_size)


async def run(config: Config, output: asyncio.Queue[ConnectorMessage]) -> None:
    """Advanced API: caller owns task cancellation and must supply a bounded queue."""
    if output.maxsize <= 0:
        raise ValueError("output_queue_must_be_bounded")
    delay = config.reconnect_min
    while True:
        await output.put(StatusMessage(Venue.POLYMARKET, ConnectionState.CONNECTING))
        try:
            await _session(config, output)
        except (OSError, WebSocketException, TimeoutError, _SessionError):
            await output.put(
                StatusMessage(
                    Venue.POLYMARKET,
                    ConnectionState.DISCONNECTED,
                    detail="websocket_session_failed",
                )
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, config.reconnect_max)


async def _session(config: Config, output: asyncio.Queue[ConnectorMessage]) -> None:
    async with connect(
        config.endpoint,
        proxy=None,
        open_timeout=10,
        close_timeout=2,
        ping_interval=20,
        ping_timeout=20,
        max_size=8 * 1024 * 1024,
        max_queue=16,
        user_agent_header="binary-market-data-python/0.1",
    ) as socket:
        await socket.send(json.dumps(polymarket.subscription(config.asset_ids)))
        await output.put(StatusMessage(Venue.POLYMARKET, ConnectionState.CONNECTED))

        async def heartbeat() -> None:
            while True:
                await socket.send("PING")
                await asyncio.sleep(config.heartbeat)

        async def reader() -> None:
            books = BookStore()
            allowed = set(config.asset_ids)
            while True:
                async with asyncio.timeout(config.idle_timeout):
                    text = await socket.recv()
                if text in ("PONG", b"PONG"):
                    continue
                latest = {}
                try:
                    for update in polymarket.parse_message(text):
                        if update.instrument_id not in allowed:
                            continue
                        view = books.apply(update, config.output_depth)
                        latest[view.instrument_id] = view
                except (ParseError, BookError):
                    # A dropped price change can invalidate a book. Resubscribe for a
                    # fresh snapshot instead of publishing potentially incomplete state.
                    raise _SessionError from None
                for instrument in sorted(latest):
                    await output.put(BookMessage(latest[instrument]))

        tasks = [asyncio.create_task(reader()), asyncio.create_task(heartbeat())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise _SessionError
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(
                    asyncio.CancelledError, WebSocketException, _SessionError, TimeoutError
                ):
                    await task
