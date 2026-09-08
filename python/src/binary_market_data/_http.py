"""Shared public GET transport. Clients owned by the caller are never closed here."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def endpoint(base: str, relative: str = "", *, websocket: bool = False) -> str:
    try:
        parts = urlsplit(base)
        schemes = ("ws", "wss") if websocket else ("http", "https")
        if (
            parts.scheme not in schemes
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.port == 0
            or any(c.isspace() for c in base)
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("invalid_endpoint") from None
    return base.rstrip("/") + ("/" + relative if relative else "")


@asynccontextmanager
async def client_context(client: httpx.AsyncClient | None = None):
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": "binary-market-data-python/0.1"},
            follow_redirects=False,
            trust_env=False,
        ) as owned:
            yield owned


async def get_text(client: httpx.AsyncClient, url: str, *, params: dict | None = None) -> str:
    async with client.stream("GET", url, params=params) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
        return body.decode("utf-8")
