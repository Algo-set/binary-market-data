import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from ..types import ConnectorMessage


def positive(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid_{name}")


def identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or value in (".", ".."):
        raise ValueError("invalid_instrument_id")


@asynccontextmanager
async def managed_stream(
    worker: Callable[[asyncio.Queue[ConnectorMessage]], Awaitable[None]],
    queue_size: int,
) -> AsyncIterator[AsyncIterator[ConnectorMessage]]:
    if type(queue_size) is not int or not 1 <= queue_size <= 65536:
        raise ValueError("invalid_queue_size")
    queue: asyncio.Queue[ConnectorMessage] = asyncio.Queue(maxsize=queue_size)
    task = asyncio.create_task(worker(queue), name="binary-market-data-connector")

    async def messages() -> AsyncIterator[ConnectorMessage]:
        while True:
            if task.done() and queue.empty():
                task.result()
                return
            getter = asyncio.create_task(queue.get())
            try:
                done, _ = await asyncio.wait((getter, task), return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    message = getter.result()
                else:
                    task.result()
                    return
            finally:
                getter.cancel()
                with suppress(asyncio.CancelledError):
                    await getter
            yield message

    iterator = messages()
    try:
        yield iterator
    finally:
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            await iterator.aclose()
