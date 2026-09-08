"""Run after installing the package. No endpoint, account or Rust toolchain needed."""

from decimal import Decimal

from binary_market_data import BookMessage, BookStore, Level, SnapshotUpdate, Venue

snapshot = SnapshotUpdate(
    venue=Venue.POLYMARKET,
    instrument_id="synthetic-example-token",
    bids=(Level(Decimal("0.48"), Decimal("30")),),
    asks=(Level(Decimal("0.52"), Decimal("25")),),
)
print(BookMessage(BookStore().apply(snapshot)).to_json())
