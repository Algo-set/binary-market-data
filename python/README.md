# binary-market-data · Python

A standalone Python implementation of this repository's Rust market-data component.
It discovers public binary markets and maintains normalized order books, with
rolling five-minute feeds and separate aggregate prediction pools.
It does not depend on Rust, the Algo Set platform, trading models, or account credentials.

This is unofficial community software, unaffiliated with any supported venue.
Venue APIs can change. Consumers must monitor connection state and data freshness.

## Install

Requires Python 3.11 or newer. From a checkout of this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ./python
market-data-probe --help
```

The package has not been published to PyPI. Install this directory or a locally
built wheel. The Python and Rust commands are both named `market-data-probe`; use
`python -m binary_market_data` to select Python unambiguously.

## Discover, then subscribe

```bash
python -m binary_market_data discover polymarket --limit 20
python -m binary_market_data discover kalshi --limit 20 --status open
python -m binary_market_data polymarket --condition-id CONDITION_ID --max-updates 3 --timeout 30
python -m binary_market_data polymarket TOKEN_ID --depth 20 --max-updates 3 --timeout 30
python -m binary_market_data kalshi MARKET_TICKER --poll-ms 1000 --max-updates 3 --timeout 30
```

Replace the capitalized identifiers with values returned by discovery.
Polymarket uses **outcome token IDs** for books; a condition ID resolves to all its
outcome tokens. Kalshi uses a market ticker. Each discovery request fetches one
page. Pass `--cursor` the returned `next_cursor` to advance; `--query` filters only
the fetched page. `--status all` removes Kalshi's status filter.

Discovery prints one JSON object. Streams print JSON Lines containing `status`
and `book` messages. `--max-updates` counts book messages, including the initial
snapshot, and does not count status messages. Unchanged Kalshi books are
suppressed, so use `--timeout` to bound a quiet probe. Timeout exits with code 1;
Ctrl-C exits with 130. Output is stdout only; the connectors write no files.

## Python API

```python
import asyncio
from binary_market_data import BookMessage
from binary_market_data.connectors import polymarket_ws


async def main():
    config = polymarket_ws.Config.for_assets(["TOKEN_ID"])
    async with polymarket_ws.stream(config, queue_size=256) as messages:
        async for message in messages:
            print(message.to_json())
            if isinstance(message, BookMessage):
                print(message.book.best_bid)
                break


asyncio.run(main())
```

Always use the stream's `async with` block. Exiting cancels pending IO, heartbeat
and retry tasks and closes owned clients. The queue is bounded; a slow consumer
applies backpressure. WebSocket buffering is also bounded. Advanced callers can
use `run(config, bounded_queue)` and own its task cancellation themselves.

For Kalshi, use `kalshi_rest.Config.for_market("MARKET_TICKER")` and
`kalshi_rest.stream(config)`. Polls are clamped to at least 100 ms; follow venue
rate limits when choosing a higher rate. REST failures use capped exponential
backoff. Successful polls restore the configured interval. Both connectors emit
fixed error codes instead of raw endpoint or response text.

Discovery functions accept an optional caller-owned `httpx.AsyncClient` for
transport configuration or tests. Default clients verify TLS, disable redirects
and ambient proxy credentials, use a 10-second request timeout, and cap response
bodies at 8 MiB. Custom endpoints support local fixtures; provide only trusted
public HTTP(S)/WS(S) endpoints, without URL credentials, queries or fragments.

## Books and wire format

- `Venue`, `BookSide`, `Level`, `SnapshotUpdate`, `LevelUpdate`, `BookView`,
  `BookStore` and `OrderBook` mirror the Rust concepts. Records are immutable.
- `LevelUpdate(update_type=UpdateType.SET_LEVEL, ...)` replaces a quantity.
  `ADD_LEVEL` applies a relative change. Zero removes a level. Snapshots sum
  duplicate prices and replace the previous book only after validation succeeds.
- Delta updates require a snapshot. Stale sequences, sequence gaps, negative
  quantities and invalid prices fail without changing the existing book.
  Sequence checks are per venue/instrument, matching the Rust store. Callers
  ingesting multiplexed Kalshi channels must validate channel sequences too.
- Prices are in `[0, 1]`. Polymarket preserves the subscribed outcome token's
  price scale. Kalshi REST NO bids become YES asks at `1 - NO price`.
- The Kalshi WebSocket **parser only** requires a subscription with
  `use_yes_price=true`; its NO-side prices are already YES-scale asks. There is
  no authenticated Kalshi WebSocket transport in either implementation.
- Depth is 1–100. Best bid/ask, midpoint, spread and crossed status come from the
  requested view. Decimal fields are JSON strings, with the Rust field names and
  `message_type` / `update_type` tags. `BookMessage.book` is flattened in JSON.

Use `Decimal` or decimal strings for prices and quantities; direct Python floats
and nonfinite values are rejected. JSON numbers are parsed directly as decimals.
Arithmetic uses a private 128-digit context, independent of a caller's decimal
precision. Inputs allow up to 60 coefficient digits and exponents from -28 to
28. This preserves normal venue values exactly; it deliberately does not mimic
Rust's 96-bit overflow or rounding at extreme values. Midpoints can retain an
additional fractional digit. Compare decimal values, not trailing-zero spelling.

Python also validates book identity in release operation, rejects zero depth,
reconnects after malformed Polymarket data or a missing snapshot, and reconnects
after an idle timeout. The Rust connector currently drops invalid messages.
Python discards the session's books on reconnect and waits for fresh snapshots.
Coalescing is per instrument per WebSocket message, and Kalshi deduplication is
at the requested depth. Neither output is a guaranteed replay of every venue tick.

## Test and build offline

```bash
cd python
python -m pip install -r requirements-dev.txt
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build --no-isolation
```

Tests use synthetic books, mock HTTP responses and loopback WebSocket servers.
No venue requests or credentials are needed. To compare both implementations,
build the repository's offline replay example, then point the test at it:

```bash
cargo build --locked --manifest-path ../Cargo.toml --example replay_fixture
BINARY_MARKET_DATA_RUST_REPLAY=../target/debug/examples/replay_fixture python -m pytest tests/test_rust_parity.py
```

CI runs Python 3.11–3.14, minimum supported transport versions, a wheel smoke test,
and Rust/Python replay parity. `requirements-dev.txt` pins the development environment;
package metadata keeps compatible dependency ranges for library consumers.

See the repository's [security policy](../SECURITY.md),
[dependency policy](../DEPENDENCIES.md) and [MIT license](LICENSE).

## Rolling five-minute markets

Version 0.2.0 adds `five-minute-probe` for public five-minute discovery and
order-book snapshots. For example:

```sh
five-minute-probe polymarket --asset BTC --max-updates 2
five-minute-probe limitless --asset BTC --once
five-minute-probe kalshi --asset BTC --once
five-minute-probe pancakeswap --asset BNB --once
five-minute-probe crypto_com --once
```

PancakeSwap outputs aggregate pool rounds, not bids and asks. Crypto.com
reports an unsupported transport; no spot feed is substituted for Strike
Options. No active five-minute Kalshi market is assumed. The default probe
exits after 30 seconds; `--once` performs one sweep. See the repository's
[five-minute feed guide](../FIVE_MINUTE_FEEDS.md) for lifecycle, freshness,
public-only scope, library usage and documented limitations.
