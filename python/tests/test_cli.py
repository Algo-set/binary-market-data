import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from binary_market_data import BookMessage, BookStore, ConnectionState, StatusMessage, Venue, cli
from binary_market_data.venue import polymarket


@pytest.mark.parametrize(
    "args",
    [
        ["kalshi", "--depth", "0", "EXAMPLE"],
        ["polymarket", "--max-updates", "0", "example"],
        ["discover", "kalshi", "--status", "invalid"],
        ["kalshi", "--endpoint", "https://example.test", "EXAMPLE"],
    ],
)
def test_cli_rejects_bad_options(args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["polymarket"],
        ["polymarket", "--condition-id", "example", "example-token"],
        ["kalshi", "--depth", "101", "EXAMPLE"],
    ],
)
def test_cli_rejects_ambiguous_or_invalid_configuration(args, capsys):
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_configuration"}


def test_cli_max_updates_counts_books_and_closes_stream(poly_book, monkeypatch, capsys):
    closed = []
    view = BookStore().apply(polymarket.parse_message(json.dumps(poly_book))[0])

    async def messages():
        yield StatusMessage(Venue.POLYMARKET, ConnectionState.CONNECTED)
        yield BookMessage(view)
        pytest.fail("CLI must stop after its requested book count")

    @asynccontextmanager
    async def stream(_config):
        try:
            yield messages()
        finally:
            closed.append(True)

    monkeypatch.setattr(cli.polymarket_ws, "stream", stream)
    assert cli.main(["polymarket", "--max-updates", "1", "example-token"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["message_type"] for line in lines] == ["status", "book"]
    assert closed == [True]


async def test_cli_timeout_cancels_stream(monkeypatch):
    closed = []

    async def messages():
        await asyncio.Event().wait()
        yield  # pragma: no cover

    @asynccontextmanager
    async def stream(_config):
        try:
            yield messages()
        finally:
            closed.append(True)

    monkeypatch.setattr(cli.kalshi_rest, "stream", stream)
    args = cli.parser().parse_args(["kalshi", "EXAMPLE"])
    args.timeout = 0.01
    with pytest.raises(TimeoutError):
        await cli.run(args)
    assert closed == [True]
