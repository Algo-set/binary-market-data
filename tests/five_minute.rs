use binary_market_data::{five_minute as feed, prediction_pool as pool, BookStore, Venue};
use serde_json::json;

#[test]
fn real_windows_only_and_no_listing_time_fallback() {
    let start = 1893456000000;
    let mut row = json!({"active":true,"closed":false,"enableOrderBook":true,"acceptingOrders":true,
        "slug":"btc-updown-5m-1893456000","conditionId":"synthetic","clobTokenIds":"[\"yes\",\"no\"]",
        "startDate":"2029-12-31T00:00:00Z","eventStartTime":"2030-01-01T00:00:00Z","endDate":"2030-01-01T00:05:00Z"});
    let c = feed::select_contract(Venue::Polymarket, &row, start, "BTC").unwrap();
    assert!(c.current(start));
    assert!(!c.current(start + 300000));
    assert!(feed::select_contract(Venue::Polymarket, &row, start, "ETH").is_none());
    row["eventStartTime"] = json!(null);
    assert!(feed::select_contract(Venue::Polymarket, &row, start, "BTC").is_none());
    let fifteen = json!({"status":"active","market_type":"binary","ticker":"KXBTC5M-SYNTHETIC","open_time":"2030-01-01T00:00:00Z","close_time":"2030-01-01T00:15:00Z"});
    assert!(feed::select_contract(Venue::Kalshi, &fifteen, start + 600000, "BTC").is_none());
    let mut limitless = json!({"tradeType":"clob","expired":false,"status":"FUNDED","slug":"bitcoin-synthetic","startAt":"2030-01-01T00:00:00Z","expirationTimestamp":start+300000});
    assert!(feed::select_contract(Venue::Limitless, &limitless, start, "BTC").is_some());
    limitless["tradeType"] = json!("amm");
    assert!(feed::select_contract(Venue::Limitless, &limitless, start, "BTC").is_none());
}
#[test]
fn limitless_decimal_precision_and_no_missing_side() {
    let body = r#"{"bids":[{"price":0.1234567890123456789012345678,"size":1.25}],"asks":[]}"#;
    let book = BookStore::default()
        .apply(feed::parse_limitless_book("synthetic", body).unwrap(), 20)
        .unwrap();
    assert_eq!(
        book.best_bid.unwrap().price.to_string(),
        "0.1234567890123456789012345678"
    );
    assert!(book.asks.is_empty());
    assert!(feed::parse_limitless_book("synthetic", "{}").is_err());
    assert!(BookStore::default()
        .apply(
            feed::parse_limitless_book("synthetic", r#"{"bids":[{"price":2,"size":1}],"asks":[]}"#)
                .unwrap(),
            20
        )
        .is_err());
}
#[test]
fn pool_keeps_uint256_precision_and_lock_delay_without_fabricating_depth() {
    let mut words = vec!["0".repeat(64); 14];
    for (i, v) in [(0, 10), (1, 100), (2, 400), (3, 703), (6, 20), (10, 7)] {
        words[i] = format!("{v:064x}");
    }
    words[4] = "f".repeat(64);
    words[9] = format!("{:064x}", num_bigint::BigUint::from(1u8) << 180usize);
    words[8] = format!(
        "{:064x}",
        (num_bigint::BigUint::from(1u8) << 180usize) + num_bigint::BigUint::from(7u8)
    );
    let value = pool::decode_round(
        &format!("0x{}", words.join("")),
        pool::contract_for("BNB").unwrap(),
        10,
        8,
        false,
        &json!({"number":"0x1","hash":format!("0x{}","a".repeat(64)),"timestamp":"0x1c2"}),
    )
    .unwrap();
    assert_eq!(value["lock_price_raw"], "-1");
    assert_eq!(value["scheduled_lock_ms"], 400000);
    assert_eq!(value["window_start_ms"], 403000);
    assert_eq!(value["window_end_ms"], 703000);
    assert_eq!(value["order_book_available"], false);
    assert!(value.get("bids").is_none());
    assert_eq!(
        value["total_amount_wei"],
        ((num_bigint::BigUint::from(1u8) << 180usize) + num_bigint::BigUint::from(7u8)).to_string()
    );
    assert!(pool::words("0x01", 14).is_err());
}
#[test]
fn public_endpoint_rejects_credentials_and_query_strings() {
    for base in [
        "https://user:example@feed.invalid",
        "https://feed.invalid?token=example",
        "file:///etc/passwd",
    ] {
        assert!(feed::endpoint(base, "book").is_err());
    }
    assert!(feed::endpoint("https://api.limitless.exchange", "markets").is_ok());
}
