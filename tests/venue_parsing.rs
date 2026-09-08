use binary_market_data::venue::{kalshi, polymarket};
use binary_market_data::{BookSide, BookStore, NormalizedUpdate};
use rust_decimal::Decimal;
use std::str::FromStr;

fn d(value: &str) -> Decimal {
    Decimal::from_str(value).unwrap()
}

#[test]
fn parses_polymarket_snapshot_and_absolute_change() {
    let snapshot = r#"{
      "event_type":"book", "asset_id":"asset-example", "market":"market-example",
      "bids":[{"price":"0.48","size":"30"}],
      "asks":[{"price":"0.52","size":"25"}], "timestamp":"1757908892351"
    }"#;
    let change = r#"{
      "event_type":"price_change", "market":"market-example", "timestamp":"1757908892352",
      "price_changes":[{"asset_id":"asset-example","price":"0.48","size":"12","side":"BUY"}]
    }"#;
    let mut store = BookStore::default();
    let view = store
        .apply(polymarket::parse_message(snapshot).unwrap().remove(0), 10)
        .unwrap();
    assert_eq!(view.best_bid.unwrap().quantity, d("30"));
    let view = store
        .apply(polymarket::parse_message(change).unwrap().remove(0), 10)
        .unwrap();
    assert_eq!(view.best_bid.unwrap().quantity, d("12"));
}

#[test]
fn converts_kalshi_rest_no_bids_to_yes_asks() {
    let payload = r#"{
      "orderbook_fp": {
        "yes_dollars":[["0.4200","13.00"]],
        "no_dollars":[["0.5600","17.00"]]
      }
    }"#;
    let update = kalshi::parse_rest_snapshot("example-market", payload).unwrap();
    let view = BookStore::default().apply(update, 10).unwrap();
    assert_eq!(view.best_bid.unwrap().price, d("0.42"));
    assert_eq!(view.best_ask.unwrap().price, d("0.44"));
}

#[test]
fn parses_kalshi_yes_price_websocket_sequence() {
    let snapshot = r#"{
      "type":"orderbook_snapshot", "sid":2, "seq":2,
      "msg":{"market_ticker":"example-market","market_id":"example-id",
      "yes_dollars_fp":[["0.42","13"]],"no_dollars_fp":[["0.44","17"]]}
    }"#;
    let delta = r#"{
      "type":"orderbook_delta", "sid":2, "seq":3,
      "msg":{"market_ticker":"example-market","market_id":"example-id",
      "price_dollars":"0.44","delta_fp":"-2","outcome_side":"no","ts_ms":1669149841000}
    }"#;
    let mut store = BookStore::default();
    let view = store
        .apply(
            kalshi::parse_websocket_message(snapshot).unwrap().remove(0),
            10,
        )
        .unwrap();
    assert_eq!(view.best_ask.unwrap().price, d("0.44"));
    let updates = kalshi::parse_websocket_message(delta).unwrap();
    assert!(matches!(
        &updates[0],
        NormalizedUpdate::AddLevel(level) if level.side == BookSide::Ask
    ));
    let view = store
        .apply(updates.into_iter().next().unwrap(), 10)
        .unwrap();
    assert_eq!(view.best_ask.unwrap().quantity, d("15"));
    assert_eq!(view.sequence, Some(3));
}

#[test]
fn kalshi_subscription_pins_unified_yes_price_scale() {
    let value = kalshi::websocket_subscription(7, &["example-market".to_string()]);
    assert_eq!(value["params"]["use_yes_price"], true);
}
