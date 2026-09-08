"""PancakeSwap Prediction V2 public aggregate rounds, never synthetic order books."""

import asyncio
import re
from dataclasses import dataclass

import httpx

from ._http import MAX_RESPONSE_BYTES, client_context, endpoint
from .types import JsonRecord, unix_time_ms

RPC = "https://bsc-dataseed.bnbchain.org"
CONTRACTS = {
    "BNB": "0x18b2a687610328590bc8f2e5fedde3b582a49cda",
    "BTC": "0x48781a7d35f6137a9135bbb984af65fd6ab25618",
    "ETH": "0x7451f994a8d510cbcb46cf57d50f31f188ff58f5",
}


@dataclass(frozen=True, slots=True)
class PoolRound(JsonRecord):
    contract: str
    epoch: str
    entry_start_ms: int
    scheduled_lock_ms: int
    window_start_ms: int
    window_end_ms: int
    lock_price_raw: str
    close_price_raw: str
    oracle_decimals: int
    total_amount_wei: str
    side_total_amount_wei: str
    totals_consistent: bool
    data_status: str
    bull_amount_wei: str
    bear_amount_wei: str
    oracle_called: bool
    paused: bool
    block_number: str
    block_hash: str
    block_timestamp_ms: int
    phase: str
    message_type: str = "prediction_pool"
    venue: str = "pancakeswap"
    order_book_available: bool = False
    pool_currency: str = "BNB"
    interval_seconds: int = 300


def words(encoded: object, count: int) -> list[int]:
    if not isinstance(encoded, str) or not re.fullmatch(
        "0x[0-9a-fA-F]{" + str(count * 64) + "}", encoded
    ):
        raise ValueError("invalid_rpc_abi")
    return [int(encoded[2 + i * 64 : 2 + (i + 1) * 64], 16) for i in range(count)]


def decode_round(
    encoded: str, *, contract: str, epoch: int, oracle_decimals: int, paused: bool, block: dict
) -> PoolRound:
    data = words(encoded, 14)
    if data[0] != epoch or data[13] not in (0, 1) or data[3] - data[2] < 300:
        raise ValueError("invalid_five_minute_round")
    if not 0 < data[1] < data[2] < data[3]:
        raise ValueError("invalid_round_totals_or_times")
    at = int(block["timestamp"], 16)
    locked = data[6] != 0
    window_start = data[3] - 300 if locked else data[2]
    phase = (
        "entry"
        if at < data[2]
        else "awaiting_lock"
        if not locked
        else "live"
        if at < data[3]
        else "closed"
    )
    if data[13]:
        phase = "resolved"

    def signed(n):
        return str(n - 2**256 if n >= 2**255 else n)

    return PoolRound(
        contract,
        str(epoch),
        data[1] * 1000,
        data[2] * 1000,
        window_start * 1000,
        data[3] * 1000,
        signed(data[4]),
        signed(data[5]),
        oracle_decimals,
        str(data[8]),
        str(data[9] + data[10]),
        data[8] == data[9] + data[10],
        "consistent" if data[8] == data[9] + data[10] else "inconsistent_totals",
        str(data[9]),
        str(data[10]),
        bool(data[13]),
        paused,
        block["number"],
        block["hash"],
        at * 1000,
        phase,
    )


async def rpc_read(client: httpx.AsyncClient, rpc_url: str, method: str, params: list):
    if method not in ("eth_chainId", "eth_getBlockByNumber", "eth_call"):
        raise ValueError("unsupported_rpc_read")
    url = endpoint(rpc_url)
    async with client.stream(
        "POST", url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
    import json

    result = json.loads(data)
    if (
        not isinstance(result, dict)
        or result.get("id") != 1
        or result.get("error")
        or "result" not in result
    ):
        raise ValueError("rpc_read_failed")
    return result["result"]


async def fetch_rounds(
    asset: str = "BNB", *, rpc_url: str = RPC, client: httpx.AsyncClient | None = None
) -> tuple[PoolRound, ...]:
    contract = CONTRACTS[asset.upper()]
    async with client_context(client) as session:
        if await rpc_read(session, rpc_url, "eth_chainId", []) != "0x38":
            raise ValueError("wrong_chain")
        block = await rpc_read(session, rpc_url, "eth_getBlockByNumber", ["latest", False])
        if not isinstance(block, dict) or not re.fullmatch(
            r"0x[0-9a-fA-F]{64}", block.get("hash", "")
        ):
            raise ValueError("invalid_block")
        age = unix_time_ms() - int(block["timestamp"], 16) * 1000
        if not -15000 <= age <= 60000:
            raise ValueError("stale_block")

        async def call(address, data):
            return await rpc_read(
                session, rpc_url, "eth_call", [{"to": address, "data": data}, block["number"]]
            )

        if words(await call(contract, "0x7d1cd04f"), 1)[0] != 300:
            raise ValueError("not_five_minute_contract")
        paused = words(await call(contract, "0x5c975abb"), 1)[0]
        if paused not in (0, 1):
            raise ValueError("invalid_pause_flag")
        epoch = words(await call(contract, "0x76671808"), 1)[0]
        oracle = words(await call(contract, "0x7dc0d1d0"), 1)[0]
        if not 0 < oracle < 2**160:
            raise ValueError("invalid_oracle")
        decimals = words(await call(f"0x{oracle:040x}", "0x313ce567"), 1)[0]
        if not 0 <= decimals <= 18:
            raise ValueError("unsupported_oracle_precision")
        result = []
        for selected in (epoch, epoch - 1):
            if selected <= 0:
                continue
            raw = await call(contract, "0x8c65c81f" + f"{selected:064x}")
            result.append(
                decode_round(
                    raw,
                    contract=contract,
                    epoch=selected,
                    oracle_decimals=decimals,
                    paused=bool(paused),
                    block=block,
                )
            )
        confirmed = await rpc_read(
            session, rpc_url, "eth_getBlockByNumber", [block["number"], False]
        )
        if not isinstance(confirmed, dict) or confirmed.get("hash") != block["hash"]:
            raise ValueError("block_changed")
        if unix_time_ms() - int(block["timestamp"], 16) * 1000 > 60000:
            raise ValueError("stale_block")
        return tuple(result)


async def watch(
    asset: str = "BNB", *, poll_interval: float = 5, rpc_url: str = RPC, once: bool = False
):
    if asset.upper() not in CONTRACTS or not 1 <= poll_interval <= 60:
        raise ValueError("invalid_pool_configuration")
    endpoint(rpc_url)
    delay = poll_interval
    async with client_context() as session:
        while True:
            try:
                for item in await fetch_rounds(asset, rpc_url=rpc_url, client=session):
                    yield item.to_dict()
                delay = poll_interval
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                yield {
                    "message_type": "status",
                    "venue": "pancakeswap",
                    "state": "disconnected",
                    "detail": "public_pool_unavailable_or_invalid",
                    "order_book_available": False,
                }
                delay = min(30, delay * 2)
            if once:
                return
            await asyncio.sleep(delay)
