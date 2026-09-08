use binary_market_data::{
    BookError, BookSide, BookStore, Level, LevelUpdate, NormalizedUpdate, SnapshotUpdate, Venue,
};
use rust_decimal::Decimal;
use std::str::FromStr;

fn d(value: &str) -> Decimal {
    Decimal::from_str(value).unwrap()
}

fn snapshot(sequence: Option<u64>) -> NormalizedUpdate {
    NormalizedUpdate::Snapshot(SnapshotUpdate {
        venue: Venue::Kalshi,
        instrument_id: "example-market".to_string(),
        market_id: None,
        sequence,
        source_ts_ms: Some(1),
        received_ts_ms: 2,
        bids: vec![Level::new(d("0.40"), d("2")), Level::new(d("0.45"), d("3"))],
        asks: vec![Level::new(d("0.60"), d("4")), Level::new(d("0.55"), d("5"))],
    })
}

fn delta(sequence: u64, side: BookSide, price: &str, quantity: &str) -> NormalizedUpdate {
    NormalizedUpdate::AddLevel(LevelUpdate {
        venue: Venue::Kalshi,
        instrument_id: "example-market".to_string(),
        market_id: None,
        sequence: Some(sequence),
        source_ts_ms: Some(3),
        received_ts_ms: 4,
        side,
        price: d(price),
        quantity: d(quantity),
    })
}

#[test]
fn sorts_best_prices_and_computes_top_of_book() {
    let mut store = BookStore::default();
    let view = store.apply(snapshot(Some(10)), 10).unwrap();
    assert_eq!(view.best_bid.unwrap().price, d("0.45"));
    assert_eq!(view.best_ask.unwrap().price, d("0.55"));
    assert_eq!(view.midpoint, Some(d("0.50")));
    assert_eq!(view.spread, Some(d("0.10")));
    assert!(!view.crossed);
}

#[test]
fn applies_relative_delta_and_removes_zero_level() {
    let mut store = BookStore::default();
    store.apply(snapshot(Some(10)), 10).unwrap();
    let view = store
        .apply(delta(11, BookSide::Bid, "0.45", "-3"), 10)
        .unwrap();
    assert_eq!(view.best_bid.unwrap().price, d("0.40"));
}

#[test]
fn rejects_sequence_gap_without_mutating_book() {
    let mut store = BookStore::default();
    store.apply(snapshot(Some(10)), 10).unwrap();
    let error = store
        .apply(delta(12, BookSide::Bid, "0.45", "1"), 10)
        .unwrap_err();
    assert_eq!(
        error,
        BookError::SequenceGap {
            expected: 11,
            received: 12
        }
    );
    let view = store
        .apply(delta(11, BookSide::Bid, "0.45", "1"), 10)
        .unwrap();
    assert_eq!(view.best_bid.unwrap().quantity, d("4"));
}

#[test]
fn requires_snapshot_before_delta() {
    let error = BookStore::default()
        .apply(delta(1, BookSide::Bid, "0.45", "1"), 10)
        .unwrap_err();
    assert!(matches!(error, BookError::MissingSnapshot(_)));
}
