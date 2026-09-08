# Binary market data

A standalone Rust library and [Python package](python/) with JSONL probes for
normalized, read-only order books from binary prediction markets. Both implementations
have no strategy, wallet, credential,
order submission, cancellation, or execution surface.

This is unofficial community software and is not affiliated with, endorsed by,
or sponsored by Polymarket or Kalshi. Their APIs may change without notice;
consumers are responsible for monitoring connection state, schema failures, and
data freshness.

## Choose an implementation

- **Rust:** the crate and probe at the repository root; instructions below.
- **Python 3.11+:** install with `python -m pip install ./python` and follow the
  [Python README](python/README.md). No Rust compiler is required.

The implementations share venue conventions and JSON field names. Offline replay
fixtures compare their parsers and books. Python-specific validation, decimal
limits and reconnect behavior are documented in its README. Neither package is
currently published to a package registry.

## Venue support

| Venue path | Transport | Authentication | Behavior |
|---|---|---|---|
| Polymarket market catalog | REST | None | Open-market discovery with stable keyset pagination |
| Polymarket market data | WebSocket | None | Snapshots, absolute level changes, heartbeat, and reconnect |
| Kalshi market catalog | REST | None | Status-filtered discovery with cursor pagination |
| Kalshi order book | REST polling | None | Snapshots, configurable polling, deduplication, and backoff |
| Kalshi order-book messages | Parser only | Transport-specific | Snapshot and relative-delta parsing with sequence enforcement |

Kalshi WebSocket transport is intentionally not included because its handshake
requires credentials. A caller can add an authenticated transport outside this
crate and pass its payloads through the provided Kalshi parser and `BookStore`.

## Architecture

```text
Public catalog -> MarketDescriptor -> outcome token or market ticker
                                             |
                                             v
Public venue feed -> venue parser -> NormalizedUpdate -> BookStore -> BookView
                                                                    |
                                                                    v
                                                              bounded channel
```

The normalized book is YES-centric:

- Kalshi YES bids remain bids.
- A Kalshi NO bid at price `p` becomes a YES ask at `1 - p`.
- Polymarket levels remain on the subscribed outcome-token price scale.
- Rust uses `rust_decimal`; Python uses `decimal.Decimal`. Prices and quantities
  never pass through binary floating point.
- A snapshot must be received before a delta can be applied.
- Kalshi sequence gaps fail closed without mutating the current book.

## Build and test

Rust 1.88 or newer is required.

```bash
cargo build --locked
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
```

Tests cover snapshots, absolute and relative deltas, sequence gaps, fixed-point
conversion, venue message shapes, CLI arguments, and the absence of execution or
secret-handling capabilities.

## Connectivity probe

The probe prints one JSON object per line to standard output. It never places an
order.

### Discover markets

Discover open Polymarket markets:

```bash
cargo run --locked --bin market-data-probe -- \
  discover polymarket --limit 20
```

Discover Kalshi markets, optionally filtering by status:

```bash
cargo run --locked --bin market-data-probe -- \
  discover kalshi --limit 20 --status open
```

Both commands return a normalized `MarketPage`. Every descriptor includes the
venue market ID and the instrument identifiers consumed by the corresponding
book connector. Use `--cursor` to request the next page and `--query TEXT` to
filter the returned page by ID, title, or slug.

### Polymarket

Supply one or more public CLOB outcome-token IDs directly:

```bash
cargo run --locked --bin market-data-probe -- \
  polymarket --depth 20 --max-updates 3 TOKEN_ID
```

Or pass a discovered condition ID. The probe resolves all outcome-token IDs and
subscribes to their books:

```bash
cargo run --locked --bin market-data-probe -- \
  polymarket --condition-id CONDITION_ID --max-updates 3
```

Optional arguments:

| Argument | Default | Meaning |
|---|---:|---|
| `--endpoint URL` | Official market WebSocket | Override the WebSocket endpoint |
| `--clob-base URL` | Official CLOB REST API | Override condition-to-token resolution |
| `--condition-id ID` | None | Resolve and subscribe to all outcomes for one market |
| `--depth N` | `20` | Published levels per side, from 1 through 100 |
| `--max-updates N` | Unlimited | Exit after publishing this many books |

### Kalshi

Supply one public market ticker:

```bash
cargo run --locked --bin market-data-probe -- \
  kalshi --depth 20 --poll-ms 1000 --max-updates 3 MARKET_TICKER
```

Optional arguments:

| Argument | Default | Meaning |
|---|---:|---|
| `--rest-base URL` | Official trade API | Override the REST base URL |
| `--depth N` | `20` | Requested and published levels per side, from 1 through 100 |
| `--poll-ms N` | `1000` | Poll interval, clamped to at least 100 ms |
| `--max-updates N` | Unlimited | Exit after publishing this many changed books |

Unchanged Kalshi responses are suppressed. Polymarket changes in one WebSocket
message are coalesced into one publication per instrument. The connector output
uses a bounded Tokio channel, so a slow consumer cannot create unbounded memory
growth.

## JSONL contract

Discovery returns a page containing normalized market descriptors:

```json
{
  "venue": "polymarket",
  "markets": [{
    "venue": "polymarket",
    "market_id": "CONDITION_ID",
    "title": "Example question?",
    "slug": "example-question",
    "status": "active",
    "book_enabled": true,
    "accepting_orders": true,
    "close_time": "2030-01-01T00:00:00Z",
    "instruments": [
      {"outcome": "Yes", "instrument_id": "YES_TOKEN_ID"},
      {"outcome": "No", "instrument_id": "NO_TOKEN_ID"}
    ]
  }],
  "next_cursor": "OPAQUE_CURSOR"
}
```

Kalshi descriptors expose the market ticker as their single combined-book
instrument. Polymarket descriptors expose one token ID per outcome.

Connection transitions use a status record:

```json
{
  "message_type": "status",
  "venue": "polymarket",
  "state": "connected",
  "at_ms": 1800000000000,
  "detail": null
}
```

Books include normalized levels and calculated top-of-book values:

```json
{
  "message_type": "book",
  "venue": "kalshi",
  "instrument_id": "MARKET_TICKER",
  "market_id": null,
  "sequence": null,
  "source_ts_ms": null,
  "received_ts_ms": 1800000000000,
  "published_ts_ms": 1800000000001,
  "bids": [{"price": "0.48", "quantity": "25.00"}],
  "asks": [{"price": "0.52", "quantity": "18.00"}],
  "best_bid": {"price": "0.48", "quantity": "25.00"},
  "best_ask": {"price": "0.52", "quantity": "18.00"},
  "midpoint": "0.50",
  "spread": "0.04",
  "crossed": false
}
```

All decimal fields are encoded as strings to preserve exact values. Optional
venue fields remain `null` when the source does not provide them.

## Library use

Connector tasks publish `ConnectorMessage` values over a caller-owned bounded
channel:

```rust,no_run
use binary_market_data::connectors::polymarket_ws;
use tokio::sync::mpsc;

#[tokio::main]
async fn main() {
    let config = polymarket_ws::Config::for_assets(vec!["TOKEN_ID".to_string()]);
    let (sender, mut receiver) = mpsc::channel(256);
    tokio::spawn(polymarket_ws::run(config, sender));

    while let Some(message) = receiver.recv().await {
        println!("{message:?}");
    }
}
```

For a custom transport, parse venue messages into `NormalizedUpdate` values and
apply them through `BookStore`. This keeps transport, normalization, and book
state independently testable.

## Reliability behavior

- Polymarket reconnects with bounded exponential backoff and sends heartbeats.
- Kalshi request failures trigger bounded exponential backoff.
- Status is emitted on connection transitions, not on every successful poll.
- Endpoint-bearing upstream error strings are replaced with fixed safe codes.
- A closed downstream channel stops its connector task.
- No files are written by either connector.

## Security boundary

- Polymarket writes only public subscription and heartbeat frames.
- Kalshi performs unauthenticated HTTP GET requests only.
- There are no API-key, signer, wallet, strategy, or execution dependencies.
- Market identifiers and endpoint overrides are supplied by the caller.
- Discovery calls only documented public catalog GET endpoints.
- The source test rejects known order, cancellation, signing, and secret APIs.

## Current limitations

- Kalshi live WebSocket authentication and transport are intentionally outside
  the package; only its public payload normalization is included.
- `--query` filters one fetched page; callers should paginate when searching the
  full catalog.
- The package normalizes market data but does not infer contract outcomes or
  settlement state.
- Durability is the consumer's responsibility; the connector itself is
  in-memory and read-only.

## Protocol references

- [Polymarket market WebSocket](https://docs.polymarket.com/api-reference/wss/market)
- [Polymarket market discovery](https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination)
- [Polymarket CLOB market info](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [Kalshi markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Kalshi order-book responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Kalshi order-book updates](https://docs.kalshi.com/websockets/orderbook-updates)
- [Kalshi WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets)

## License

Licensed under the MIT License. See [`LICENSE`](LICENSE).

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and review guidance,
[DEPENDENCIES.md](DEPENDENCIES.md) for dependency policy, and
[SECURITY.md](SECURITY.md) for private vulnerability reporting.
