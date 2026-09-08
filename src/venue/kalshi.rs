use crate::types::{
    unix_time_ms, BookSide, Level, LevelUpdate, NormalizedUpdate, SnapshotUpdate, Venue,
};
use rust_decimal::Decimal;
use serde_json::Value;
use std::str::FromStr;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ParseError {
    #[error("invalid JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("missing or invalid field: {0}")]
    Field(&'static str),
    #[error("invalid decimal in {field}: {value}")]
    Decimal { field: &'static str, value: String },
}

pub fn websocket_subscription(request_id: u64, market_tickers: &[String]) -> Value {
    serde_json::json!({
        "id": request_id,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": market_tickers,
            "use_yes_price": true
        }
    })
}

pub fn parse_rest_snapshot(ticker: &str, text: &str) -> Result<NormalizedUpdate, ParseError> {
    let root: Value = serde_json::from_str(text)?;
    let book = root
        .get("orderbook_fp")
        .or_else(|| root.get("orderbook"))
        .ok_or(ParseError::Field("orderbook_fp"))?;
    snapshot_from_sides(
        ticker.to_string(),
        None,
        None,
        None,
        unix_time_ms(),
        false,
        side_levels(book, &["yes_dollars", "yes_dollars_fp"], "yes_dollars")?,
        side_levels(book, &["no_dollars", "no_dollars_fp"], "no_dollars")?,
    )
}

pub fn parse_websocket_message(text: &str) -> Result<Vec<NormalizedUpdate>, ParseError> {
    let root: Value = serde_json::from_str(text)?;
    let event_type = root.get("type").and_then(Value::as_str).unwrap_or_default();
    let message = root.get("msg").ok_or(ParseError::Field("msg"))?;
    let received_ts_ms = unix_time_ms();
    match event_type {
        "orderbook_snapshot" => snapshot_from_sides(
            string_field(message, "market_ticker")?,
            optional_string(message.get("market_id")),
            root.get("seq").and_then(Value::as_u64),
            optional_i64(message.get("ts_ms")),
            received_ts_ms,
            true,
            side_levels(
                message,
                &["yes_dollars_fp", "yes_dollars"],
                "yes_dollars_fp",
            )?,
            side_levels(message, &["no_dollars_fp", "no_dollars"], "no_dollars_fp")?,
        )
        .map(|update| vec![update]),
        "orderbook_delta" => {
            let outcome = message
                .get("outcome_side")
                .or_else(|| message.get("side"))
                .and_then(Value::as_str)
                .ok_or(ParseError::Field("msg.outcome_side"))?;
            let side = if outcome.eq_ignore_ascii_case("yes") {
                BookSide::Bid
            } else if outcome.eq_ignore_ascii_case("no") {
                BookSide::Ask
            } else {
                return Err(ParseError::Field("msg.outcome_side"));
            };
            Ok(vec![NormalizedUpdate::AddLevel(LevelUpdate {
                venue: Venue::Kalshi,
                instrument_id: string_field(message, "market_ticker")?,
                market_id: optional_string(message.get("market_id")),
                sequence: root.get("seq").and_then(Value::as_u64),
                source_ts_ms: optional_i64(message.get("ts_ms")),
                received_ts_ms,
                side,
                price: decimal_field(message, "price_dollars", "msg.price_dollars")?,
                quantity: decimal_field(message, "delta_fp", "msg.delta_fp")?,
            })])
        }
        _ => Ok(Vec::new()),
    }
}

#[allow(clippy::too_many_arguments)]
fn snapshot_from_sides(
    ticker: String,
    market_id: Option<String>,
    sequence: Option<u64>,
    source_ts_ms: Option<i64>,
    received_ts_ms: i64,
    no_prices_are_yes_scale: bool,
    yes_bids: Vec<Level>,
    no_bids: Vec<Level>,
) -> Result<NormalizedUpdate, ParseError> {
    let asks = no_bids
        .into_iter()
        .map(|level| {
            let price = if no_prices_are_yes_scale {
                level.price
            } else {
                Decimal::ONE - level.price
            };
            Level::new(price, level.quantity)
        })
        .collect();
    Ok(NormalizedUpdate::Snapshot(SnapshotUpdate {
        venue: Venue::Kalshi,
        instrument_id: ticker,
        market_id,
        sequence,
        source_ts_ms,
        received_ts_ms,
        bids: yes_bids,
        asks,
    }))
}

fn side_levels(
    value: &Value,
    keys: &[&str],
    error_field: &'static str,
) -> Result<Vec<Level>, ParseError> {
    let rows = keys
        .iter()
        .find_map(|key| value.get(*key).and_then(Value::as_array))
        .ok_or(ParseError::Field(error_field))?;
    rows.iter()
        .map(|row| {
            let values = row.as_array().ok_or(ParseError::Field(error_field))?;
            if values.len() < 2 {
                return Err(ParseError::Field(error_field));
            }
            Ok(Level::new(
                decimal_value(&values[0], error_field)?,
                decimal_value(&values[1], error_field)?,
            ))
        })
        .collect()
}

fn string_field(value: &Value, field: &'static str) -> Result<String, ParseError> {
    value
        .get(field)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .map(ToOwned::to_owned)
        .ok_or(ParseError::Field(field))
}

fn decimal_field(
    value: &Value,
    field: &'static str,
    error_field: &'static str,
) -> Result<Decimal, ParseError> {
    let raw = value.get(field).ok_or(ParseError::Field(error_field))?;
    decimal_value(raw, error_field)
}

fn decimal_value(value: &Value, field: &'static str) -> Result<Decimal, ParseError> {
    let raw = value
        .as_str()
        .map(ToOwned::to_owned)
        .or_else(|| value.as_number().map(ToString::to_string))
        .ok_or(ParseError::Field(field))?;
    Decimal::from_str(&raw).map_err(|_| ParseError::Decimal { field, value: raw })
}

fn optional_string(value: Option<&Value>) -> Option<String> {
    value.and_then(Value::as_str).map(ToOwned::to_owned)
}

fn optional_i64(value: Option<&Value>) -> Option<i64> {
    value.and_then(|item| {
        item.as_i64()
            .or_else(|| item.as_u64().and_then(|number| i64::try_from(number).ok()))
            .or_else(|| item.as_str().and_then(|text| text.parse().ok()))
    })
}
