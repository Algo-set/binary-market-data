# Public five-minute feeds

Rust and Python include a `five-minute-probe` command that discovers current
contracts, populates normalized books and moves to the next window. It reads
public market data only. It never reads account settings or loads credentials.
The existing `market-data-probe` remains available for explicit token IDs,
tickers and the Polymarket WebSocket.

## Support and limits

| Venue | Five-minute path | What it emits |
|---|---|---|
| Polymarket | Public Gamma discovery and CLOB REST snapshots | A separate book for each outcome token, bound to its condition and five-minute window |
| Limitless | Public active CLOB catalog and REST order book | The exact market slug's YES-side book |
| Kalshi | Public time-filtered catalog and REST order book | A YES-centric book only if the selected asset has an active binary market with a verified 300-second open-to-close window |
| PancakeSwap Prediction | Public BNB Chain RPC reads of the published BTC, ETH or BNB Prediction contract | Aggregate pool rounds, **not an order book** |
| Crypto.com Strike Options | Public transport not verified | An explicit `unsupported` status; no substitute spot or perpetual book |

A five-minute period must be established by public venue timestamps. Five
minutes *remaining* in a fifteen-minute market does not qualify. Unknown
windows, disabled books and closed markets are excluded. Unsupported or empty
catalogs are reported rather than filled with synthetic liquidity.

Crypto.com's newer GEN4 DCM documentation identifies a public instrument
catalog and a market-data WebSocket origin, but a working credential-free
Strike Options book subscription has not been verified here. The documented
catalog returned HTTP 403 during the September 8, 2026 check. This package
therefore does not claim a working Crypto.com Strike Options connector.

## Run

Rust, from the repository root:

```sh
cargo run --locked --bin five-minute-probe -- polymarket --asset BTC --max-updates 2
cargo run --locked --bin five-minute-probe -- limitless --asset BTC --once
cargo run --locked --bin five-minute-probe -- kalshi --asset BTC --once
cargo run --locked --bin five-minute-probe -- pancakeswap --asset BTC --once
cargo run --locked --bin five-minute-probe -- crypto_com --once
```

Python 3.11+, independently of Rust:

```sh
python -m pip install ./python
five-minute-probe polymarket --asset BTC --max-updates 2
five-minute-probe limitless --asset ETH --once
five-minute-probe kalshi --asset BTC --once
five-minute-probe pancakeswap --asset BNB --once
five-minute-probe crypto_com --once
```

When both implementations are installed, the Cargo command selects Rust and
`python -m binary_market_data.five_minute_cli` selects Python explicitly.

| Option | Default | Meaning |
|---|---|---|
| `--asset` | `BTC` | Requested asset; PancakeSwap supports BTC, ETH and BNB |
| `--once` | Off | One discovery/snapshot sweep, then exit |
| `--timeout` | `30` | Overall lifetime in seconds, including connection failures |
| `--max-updates` | `0` | Stop after this many book/pool records; 0 runs until timeout |
| `--poll-ms` | `5000` | Poll spacing, 1000–60000 ms; failures back off |
| `--pages` | `3` | Maximum catalog pages per discovery, 1–20 |
| `--depth` | `20` | Output order-book levels per side, 1–100 |
| `--rpc-url` | Official public BNB Chain RPC | Alternative public RPC for PancakeSwap; no embedded credentials or query tokens |

For a longer bounded run, use `--timeout 3600`. Ctrl-C exits and closes the
client. Nothing is installed as a background collector. To watch multiple
assets or venues, run separate processes or invoke the library from your own
bounded task manager. Respect the venues' public rate limits.

Catalog scans are bounded, so an empty scan is **not proof that the entire
venue has no matching markets**. Limitless paginates active CLOB markets.
Kalshi uses close-time filters, excludes inactive rows locally and checks the
asset ticker family and exact open/close duration; it does not assume that a
particular five-minute series ticker exists. Polymarket resolves the current
five-minute asset slug and verifies `eventStartTime` and `endDate` rather than
its earlier listing date.

## Consume books

A `contract_book` record contains:

- `contract`: venue, market ID, instrument IDs, window start/end in Unix UTC
  milliseconds. Contracts with a 300,000-ms window are selected.
- `book`: the existing normalized `BookView` with decimal strings, timestamps,
  full snapshot levels and best bid/ask. A Polymarket token must match both the
  requested instrument and condition ID.

Each successful poll emits fresh snapshots, including unchanged books. This
probe does not emit exchange sequence deltas. Keep records separate by venue,
market ID and instrument ID. Clear an expired contract at `window_end_ms`, and
stop treating its view as current on a disconnected status. A slow consumer
must independently check freshness; timestamps are not a latency guarantee.
The probe checks expiry again after receiving books and before publishing.

Library entry points are `five_minute.discover`, `five_minute.fetch_books` and
`five_minute.watch` in Python; Rust provides the corresponding discovery and
fetch functions in `binary_market_data::five_minute`. The Rust CLI supplies
the polling loop. `parse_limitless_book` also accepts a caller-supplied public
REST snapshot for use with the existing `BookStore`.

## Consume PancakeSwap pools

`prediction_pool` records include current and preceding epochs, aggregate
bull/bear/total amounts in **BNB wei strings**, oracle price integers and oracle
decimals, pause state, scheduled lock and window times, and block number/hash.
`total_amount_wei` preserves the venue-reported total. `side_total_amount_wei`
is the explicitly calculated sum of the two sides. `totals_consistent` and
`data_status` identify discrepancies; observed rounds sometimes report a zero
total alongside nonzero side pools. The adapter does not silently replace that
reported total. There are no fabricated bids, asks, spreads or tradeable quantities.

The public contract's `intervalSeconds` must be 300. All calls in a sweep use
one block number; the block hash is checked again before emission. A stale
head, wrong chain, malformed ABI, incompatible interval or changed block hash
rejects the sweep. The latest block is not a finality guarantee: consumers
that persist records must handle later chain reorganizations.

The scheduled lock may precede the actual lock transaction. After locking,
`window_start_ms` is derived from the contract's assigned `closeTimestamp`
minus its verified 300-second interval, as specified by `_safeLockRound`.
`scheduled_lock_ms` retains the original schedule. Before locking, that start
is a schedule; `phase=awaiting_lock` does not claim an observed lock price.
Final resolution is distinguished by `oracle_called`. Paused rounds remain
clearly marked and are not represented as current order books.

## Validation and public data boundary

Tests use invented market identifiers and synthetic timestamps and levels.
They cover exact window filtering, asset binding, rollover, pagination, wrong
book identities, expiry during an HTTP request, decimal precision, 256-bit
pool amounts, delayed locking, pinned RPC blocks, stale heads and reorg
rejection. Existing order-book, WebSocket and Rust/Python replay tests remain.

The built-in HTTP clients disable environment proxy/credential discovery and
redirects. Errors use fixed codes rather than upstream bodies or URLs. Only
public catalog/book GETs and the RPC read methods `eth_chainId`,
`eth_getBlockByNumber` and `eth_call` are used. No account, wallet transaction,
strategy, signal, customer or payment integration is included.

On September 8, 2026, bounded public probes retrieved books from Polymarket and
Limitless and aggregate rounds from PancakeSwap. Kalshi's scanned catalog did
not yield a qualifying five-minute BTC market. These observations are dated
connectivity evidence, not a guarantee of continuing venue availability.

## Official references

- [Polymarket market discovery](https://docs.polymarket.com/api-reference/markets/list-markets)
- [Polymarket order-book summary](https://docs.polymarket.com/api-reference/market-data/get-order-book)
- [Limitless active catalog](https://docs.limitless.exchange/api-reference/markets/browse-active)
- [Limitless YES-side order book](https://docs.limitless.exchange/api-reference/trading/orderbook)
- [Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data)
- [Kalshi market filters](https://docs.kalshi.com/api-reference/market/get-markets)
- [PancakeSwap prediction mechanics and addresses](https://docs.pancakeswap.finance/play/prediction/prediction-faq)
- [PancakeSwap V2 contract source](https://github.com/pancakeswap/pancake-smart-contracts/blob/master/projects/predictions/v2/contracts/PancakePredictionV2.sol)
- [BNB Chain public RPC endpoints](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)
- [Crypto.com Strike Options](https://help.crypto.com/en/articles/8462828-about-strike-options)
- [Crypto.com GEN4 DCM public catalog](https://exchange-developer.crypto.com/dcm/v1/docs/api/rest/public-get-instruments)
- [Crypto.com GEN4 DCM endpoints](https://exchange-developer.crypto.com/dcm/v1/docs/api/rest-common-api-reference)
