import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from websockets.asyncio.server import serve

from binary_market_data import BookMessage, ConnectionState, StatusMessage, Venue
from binary_market_data.connectors import kalshi_rest, polymarket_ws
from binary_market_data.connectors._common import managed_stream


async def take_books(messages, count):
    result = []
    async with asyncio.timeout(5):
        async for message in messages:
            result.append(message)
            if sum(isinstance(item, BookMessage) for item in result) >= count:
                return result
    raise AssertionError("stream ended too early")


@asynccontextmanager
async def ws_server(handler):
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def test_kalshi_suppresses_duplicate_books_and_only_uses_public_get(kalshi_body):
    requests = []
    changed = kalshi_body.replace('"13.00"', '"14.00"')

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=kalshi_body if len(requests) < 3 else changed)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with kalshi_rest.stream(
            kalshi_rest.Config("EXAMPLE/?", poll_interval=0.1), client=client
        ) as messages:
            result = await take_books(messages, 2)
        assert not client.is_closed
    assert [item.book.best_bid.quantity for item in result if isinstance(item, BookMessage)] == [
        13,
        14,
    ]
    assert [item.state for item in result if isinstance(item, StatusMessage)] == [
        ConnectionState.CONNECTING,
        ConnectionState.CONNECTED,
    ]
    assert len(requests) == 3 and all(request.method == "GET" for request in requests)
    assert requests[0].url.raw_path.endswith(b"/markets/EXAMPLE%2F%3F/orderbook?depth=20")


async def test_kalshi_retries_with_backoff_and_recovers(kalshi_body, monkeypatch):
    calls = 0
    delays = []
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    def handler(_request):
        nonlocal calls
        calls += 1
        if calls < 4:
            return httpx.Response(503, text="sensitive-fixture-payload")
        return httpx.Response(200, text=kalshi_body)

    monkeypatch.setattr(kalshi_rest.asyncio, "sleep", fast_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with kalshi_rest.stream(
            kalshi_rest.Config("EXAMPLE", poll_interval=0.1, reconnect_max=0.4), client=client
        ) as messages:
            result = await take_books(messages, 1)
    assert delays[:3] == [0.2, 0.4, 0.4]
    assert all(
        item.detail == "venue_request_failed"
        for item in result
        if isinstance(item, StatusMessage) and item.state == ConnectionState.DISCONNECTED
    )
    assert "sensitive-fixture" not in "".join(item.to_json() for item in result)


async def test_poll_cancellation_interrupts_pending_request():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with kalshi_rest.stream(kalshi_rest.Config("EXAMPLE"), client=client):
            async with asyncio.timeout(2):
                await started.wait()
        assert cancelled.is_set()


async def test_bounded_output_applies_backpressure_and_exit_cancels_worker():
    produced = 0
    stopped = asyncio.Event()

    async def worker(queue):
        nonlocal produced
        try:
            while True:
                await queue.put(StatusMessage(Venue.KALSHI, ConnectionState.CONNECTED))
                produced += 1
        finally:
            stopped.set()

    async with managed_stream(worker, queue_size=1) as messages:
        await asyncio.sleep(0)
        assert produced == 1
        await anext(messages)
        await asyncio.sleep(0)
        assert produced == 2
    assert stopped.is_set()


async def test_worker_failure_is_observed_without_hanging_consumer():
    async def worker(_queue):
        raise RuntimeError("fixture_failure")

    async with asyncio.timeout(2):
        with pytest.raises(RuntimeError, match="fixture_failure"):
            async with managed_stream(worker, queue_size=1) as messages:
                await anext(messages)


async def test_ws_subscription_heartbeat_coalescing_and_close(poly_book, poly_change):
    frames = []
    closed = asyncio.Event()

    async def handler(socket):
        frames.append(json.loads(await socket.recv()))
        frames.append(await socket.recv())
        await socket.send("PONG")
        await socket.send(json.dumps([poly_book, poly_change]))
        await socket.wait_closed()
        closed.set()

    async with ws_server(handler) as endpoint:
        config = polymarket_ws.Config(("example-token",), endpoint=endpoint)
        async with polymarket_ws.stream(config) as messages:
            result = await take_books(messages, 1)
        async with asyncio.timeout(3):
            await closed.wait()
    assert frames == [
        {"assets_ids": ["example-token"], "type": "market", "initial_dump": True},
        "PING",
    ]
    assert [item.book.best_bid.quantity for item in result if isinstance(item, BookMessage)] == [12]


async def test_ws_reconnect_discards_old_books_and_waits_for_snapshot(poly_book, poly_change):
    sessions = 0

    async def handler(socket):
        nonlocal sessions
        sessions += 1
        current = sessions
        await socket.recv()
        if current == 1:
            await socket.send(json.dumps(poly_book))
            await socket.close()
        elif current == 2:
            await socket.send(json.dumps(poly_change))  # No snapshot in this new session.
            await socket.wait_closed()
        else:
            book = {**poly_book, "bids": [{"price": "0.48", "size": "99"}]}
            await socket.send(json.dumps(book))
            await socket.wait_closed()

    async with ws_server(handler) as endpoint:
        config = polymarket_ws.Config(
            ("example-token",), endpoint=endpoint, reconnect_min=0.01, reconnect_max=0.02
        )
        async with polymarket_ws.stream(config) as messages:
            result = await take_books(messages, 2)
    assert sessions == 3
    assert [item.book.best_bid.quantity for item in result if isinstance(item, BookMessage)] == [
        30,
        99,
    ]
    assert (
        sum(
            isinstance(item, StatusMessage) and item.state == ConnectionState.DISCONNECTED
            for item in result
        )
        == 2
    )


async def test_ws_ignores_unsubscribed_instruments(poly_book):
    async def handler(socket):
        await socket.recv()
        await socket.send(json.dumps([{**poly_book, "asset_id": "unsubscribed"}, poly_book]))
        await socket.wait_closed()

    async with ws_server(handler) as endpoint:
        async with polymarket_ws.stream(
            polymarket_ws.Config(("example-token",), endpoint=endpoint)
        ) as messages:
            result = await take_books(messages, 1)
    assert [item.book.instrument_id for item in result if isinstance(item, BookMessage)] == [
        "example-token"
    ]


async def test_ws_idle_connection_reconnects_and_exits_cleanly():
    sessions = 0

    async def handler(socket):
        nonlocal sessions
        sessions += 1
        await socket.wait_closed()

    async with ws_server(handler) as endpoint:
        config = polymarket_ws.Config(
            ("example",), endpoint=endpoint, heartbeat=0.01, idle_timeout=0.03, reconnect_min=0.01
        )
        async with polymarket_ws.stream(config) as messages:
            async with asyncio.timeout(3):
                async for message in messages:
                    if (
                        isinstance(message, StatusMessage)
                        and message.state == ConnectionState.CONNECTED
                        and sessions >= 2
                    ):
                        break
    assert sessions >= 2


@pytest.mark.parametrize(
    "changes",
    [
        {"heartbeat": 0},
        {"idle_timeout": 10},
        {"reconnect_min": -1},
        {"reconnect_max": 0.1},
        {"output_depth": 0},
        {"heartbeat": float("nan")},
        {"asset_ids": ()},
        {"asset_ids": "example"},
        {"endpoint": "https://example.test"},
    ],
)
def test_ws_configuration_validation(changes):
    with pytest.raises(ValueError):
        replace(polymarket_ws.Config(("example",)), **changes)


async def test_unbounded_queues_are_rejected():
    with pytest.raises(ValueError, match="bounded"):
        await kalshi_rest.run(kalshi_rest.Config("EXAMPLE"), asyncio.Queue())
    with pytest.raises(ValueError, match="bounded"):
        await polymarket_ws.run(polymarket_ws.Config(("example",)), asyncio.Queue())
