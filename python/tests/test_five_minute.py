import json
from decimal import Decimal

import httpx
import pytest

from binary_market_data import BookStore
from binary_market_data import five_minute as feed
from binary_market_data import prediction_pool as pool
from binary_market_data.types import Venue

START = 1893456000000
NOW = START + 10000


def row(venue):
    if venue == Venue.POLYMARKET:
        return {
            "active": True,
            "closed": False,
            "enableOrderBook": True,
            "acceptingOrders": True,
            "slug": "btc-updown-5m-1893456000",
            "conditionId": "synthetic-condition",
            "clobTokenIds": '["yes-token","no-token"]',
            "startDate": "2029-12-31T00:00:00Z",
            "eventStartTime": "2030-01-01T00:00:00Z",
            "endDate": "2030-01-01T00:05:00Z",
        }
    if venue == Venue.KALSHI:
        return {
            "status": "active",
            "market_type": "binary",
            "ticker": "KXBTC5M-SYNTHETIC",
            "open_time": "2030-01-01T00:00:00Z",
            "close_time": "2030-01-01T00:05:00Z",
        }
    return {
        "tradeType": "clob",
        "expired": False,
        "status": "FUNDED",
        "slug": "bitcoin-up-or-down-synthetic",
        "startAt": "2030-01-01T00:00:00Z",
        "expirationTimestamp": START + 300000,
    }


@pytest.mark.parametrize("venue", [Venue.POLYMARKET, Venue.KALSHI, Venue.LIMITLESS])
def test_exact_window_and_asset_binding(venue):
    data = row(venue)
    c = feed.select_contract(venue, data, NOW, "BTC")
    assert c and c.current(START) and not c.current(START + 300000)
    assert feed.select_contract(venue, data, NOW, "ETH") is None
    assert feed.select_contract(venue, data, START - 1, "BTC") is None
    assert feed.select_contract(venue, data, START + 300000, "BTC") is None
    field = {
        Venue.POLYMARKET: "endDate",
        Venue.KALSHI: "close_time",
        Venue.LIMITLESS: "expirationTimestamp",
    }[venue]
    data[field] = START + 900000 if venue == Venue.LIMITLESS else "2030-01-01T00:15:00Z"
    # Fifteen-minute contracts with five minutes left still cannot qualify.
    assert feed.select_contract(venue, data, START + 600000, "BTC") is None


def test_missing_timing_never_falls_back_to_listing_or_slug():
    data = row(Venue.POLYMARKET)
    del data["eventStartTime"]
    assert feed.select_contract(Venue.POLYMARKET, data, NOW, "BTC") is None
    data["eventStartTime"] = "2030-01-01T00:00:00"
    assert feed.select_contract(Venue.POLYMARKET, data, NOW, "BTC") is None
    data = row(Venue.LIMITLESS)
    data["tradeType"] = "amm"
    assert feed.select_contract(Venue.LIMITLESS, data, NOW, "BTC") is None


def test_limitless_precision_sides_empty_and_invalid():
    body = '{"bids":[{"price":0.1234567890123456789012345678,"size":1.25}],"asks":[]}'
    book = BookStore().apply(feed.parse_limitless_book("synthetic", body), 20)
    assert book.venue == Venue.LIMITLESS
    assert book.best_bid.price == Decimal("0.1234567890123456789012345678")
    assert not book.asks
    for bad in ["{}", '{"bids":null,"asks":[]}', '{"bids":[{"price":2,"size":1}],"asks":[]}']:
        with pytest.raises(ValueError):
            BookStore().apply(feed.parse_limitless_book("synthetic", bad), 20)


async def test_catalog_public_paths_and_cursor_pagination():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and "authorization" not in request.headers
        assert request.url.params["min_close_ts"] == str(NOW // 1000)
        assert request.url.params["max_close_ts"] == str(NOW // 1000 + 300)
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"markets": [], "cursor": "next"})
        return httpx.Response(200, json={"markets": [row(Venue.KALSHI)], "cursor": ""})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await feed.discover(Venue.KALSHI, now_ms=NOW, client=client)
    assert len(found) == 1 and len(requests) == 2


async def test_rollover_queries_new_slug(monkeypatch):
    slugs = []

    def handler(request):
        slugs.append(request.url.params["slug"])
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await feed.discover(Venue.POLYMARKET, now_ms=NOW, client=client)
        await feed.discover(Venue.POLYMARKET, now_ms=NOW + 300000, client=client)
    assert slugs == ["btc-updown-5m-1893456000", "btc-updown-5m-1893456300"]


async def test_book_identity_and_expiry_during_request(monkeypatch):
    monkeypatch.setattr(feed, "unix_time_ms", lambda: NOW)
    contract = feed.select_contract(Venue.POLYMARKET, row(Venue.POLYMARKET), NOW, "BTC")

    def mismatched(request):
        return httpx.Response(
            200, json={"market": "different", "asset_id": "yes-token", "bids": [], "asks": []}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(mismatched)) as client:
        with pytest.raises(ValueError, match="identity"):
            await feed.fetch_books(contract, client=client)

    def expires(request):
        monkeypatch.setattr(feed, "unix_time_ms", lambda: START + 300000)
        return httpx.Response(
            200,
            json={
                "market": contract.market_id,
                "asset_id": request.url.params["token_id"],
                "bids": [],
                "asks": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(expires)) as client:
        with pytest.raises(ValueError, match="closed"):
            await feed.fetch_books(contract, client=client)


def encoded(values):
    return "0x" + "".join(f"{v % 2**256:064x}" for v in values)


def pool_data(epoch=10):
    # Scheduled lock at 300, actual lock 3s late; contract extends close to 603.
    return [epoch, 100, 400, 703, -1, 0, 20, 0, 2**180 + 7, 2**180, 7, 0, 0, 0]


def test_pool_exact_uint256_signed_oracle_and_delayed_lock():
    result = pool.decode_round(
        encoded(pool_data()),
        contract=pool.CONTRACTS["BNB"],
        epoch=10,
        oracle_decimals=8,
        paused=False,
        block={"number": "0x1", "hash": "0x" + "a" * 64, "timestamp": hex(450)},
    )
    assert result.total_amount_wei == str(2**180 + 7)
    assert result.lock_price_raw == "-1"
    assert result.scheduled_lock_ms == 400000 and result.window_start_ms == 403000
    assert result.window_end_ms - result.window_start_ms == 300000
    assert result.order_book_available is False and result.phase == "live"
    assert "bids" not in result.to_dict()
    data = pool_data()
    data[8] = 1
    mismatch = pool.decode_round(
        encoded(data),
        contract="synthetic",
        epoch=10,
        oracle_decimals=8,
        paused=False,
        block={"number": "0x1", "hash": "0x" + "a" * 64, "timestamp": "0x1c2"},
    )
    assert mismatch.total_amount_wei == "1"
    assert mismatch.side_total_amount_wei == str(2**180 + 7)
    assert mismatch.data_status == "inconsistent_totals" and not mismatch.totals_consistent


@pytest.mark.parametrize("failure", [None, "chain", "duration", "reorg", "stale"])
async def test_rpc_reads_are_pinned_and_fail_closed(monkeypatch, failure):
    monkeypatch.setattr(pool, "unix_time_ms", lambda: 450000)
    methods = []
    block = {
        "number": "0x1",
        "hash": "0x" + "a" * 64,
        "timestamp": hex(450 if failure != "stale" else 100),
    }

    def handler(request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        methods.append(method)
        assert "authorization" not in request.headers
        if method == "eth_chainId":
            result = "0x1" if failure == "chain" else "0x38"
        elif method == "eth_getBlockByNumber":
            result = (
                {**block, "hash": "0x" + "b" * 64}
                if failure == "reorg" and params[0] == "0x1"
                else block
            )
        else:
            assert method == "eth_call" and params[1] == block["number"]
            assert set(params[0]) == {"to", "data"}
            selector = params[0]["data"][:10]
            values = {
                "0x7d1cd04f": 900 if failure == "duration" else 300,
                "0x5c975abb": 0,
                "0x76671808": 10,
                "0x7dc0d1d0": 1,
                "0x313ce567": 8,
            }
            result = (
                encoded([values[selector]])
                if selector in values
                else encoded(pool_data(int(params[0]["data"][10:], 16)))
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if failure:
            with pytest.raises(ValueError):
                await pool.fetch_rounds(client=client)
        else:
            result = await pool.fetch_rounds(client=client)
            assert [r.epoch for r in result] == ["10", "9"]
    assert set(methods) <= {"eth_chainId", "eth_getBlockByNumber", "eth_call"}


async def test_crypto_com_never_substitutes_exchange_spot():
    messages = [m async for m in feed.watch(Venue.CRYPTO_COM, once=True)]
    assert len(messages) == 1 and messages[0]["state"] == "unsupported"
