use binary_market_data::discovery::{
    parse_kalshi_page, parse_polymarket_market_info, parse_polymarket_page,
};

#[test]
fn parses_polymarket_catalog_and_maps_outcomes_to_book_tokens() {
    let payload = r#"{
      "markets": [{
        "question": "Example question?",
        "conditionId": "condition-example",
        "slug": "example-question",
        "active": true,
        "closed": false,
        "archived": false,
        "enableOrderBook": true,
        "acceptingOrders": true,
        "endDateIso": "2030-01-01T00:00:00Z",
        "outcomes": "[\"Yes\", \"No\"]",
        "clobTokenIds": "[\"yes-token\", \"no-token\"]"
      }],
      "next_cursor": "next-page"
    }"#;
    let page = parse_polymarket_page(payload).unwrap();
    assert_eq!(page.markets[0].market_id, "condition-example");
    assert_eq!(
        page.markets[0].instruments[0].outcome.as_deref(),
        Some("Yes")
    );
    assert_eq!(page.markets[0].instruments[0].instrument_id, "yes-token");
    assert_eq!(page.next_cursor.as_deref(), Some("next-page"));
}

#[test]
fn parses_polymarket_clob_market_resolution() {
    let payload = r#"{
      "t": [
        {"t": "yes-token", "o": "Yes"},
        {"t": "no-token", "o": "No"}
      ]
    }"#;
    let instruments = parse_polymarket_market_info(payload).unwrap();
    assert_eq!(instruments.len(), 2);
    assert_eq!(instruments[1].outcome.as_deref(), Some("No"));
    assert_eq!(instruments[1].instrument_id, "no-token");
}

#[test]
fn parses_kalshi_catalog_ticker_as_book_identifier() {
    let payload = r#"{
      "markets": [{
        "ticker": "EXAMPLE-MARKET",
        "event_ticker": "EXAMPLE-EVENT",
        "market_type": "binary",
        "status": "active",
        "title": "Example market",
        "close_time": "2030-01-01T00:00:00Z"
      }],
      "cursor": "next-page"
    }"#;
    let page = parse_kalshi_page(payload).unwrap();
    let market = &page.markets[0];
    assert_eq!(market.market_id, "EXAMPLE-MARKET");
    assert_eq!(market.instruments[0].instrument_id, "EXAMPLE-MARKET");
    assert!(market.accepting_orders);
}

#[test]
fn rejects_misaligned_polymarket_outcomes() {
    let payload = r#"{
      "markets": [{
        "conditionId": "condition-example",
        "outcomes": "[\"Yes\"]",
        "clobTokenIds": "[\"yes-token\", \"no-token\"]"
      }]
    }"#;
    assert!(parse_polymarket_page(payload).is_err());
}
