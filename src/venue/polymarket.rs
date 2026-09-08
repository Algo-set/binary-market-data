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

pub fn subscription(asset_ids: &[String]) -> Value {
    serde_json::json!({
        "assets_ids": asset_ids,
        "type": "market",
        "initial_dump": true
    })
}

pub fn parse_message(text: &str) -> Result<Vec<NormalizedUpdate>, ParseError> {
    let value: Value = serde_json::from_str(text)?;
    let received_ts_ms = unix_time_ms();
    match value {
        Value::Array(items) => items
            .iter()
            .map(|item| parse_object(item, received_ts_ms))
            .collect::<Result<Vec<_>, _>>()
            .map(|groups| groups.into_iter().flatten().collect()),
        object => parse_object(&object, received_ts_ms),
    }
}

fn parse_object(value: &Value, received_ts_ms: i64) -> Result<Vec<NormalizedUpdate>, ParseError> {
    let event_type = value
        .get("event_type")
        .and_then(Value::as_str)
        .unwrap_or_default();
    match event_type {
        "book" => parse_snapshot(value, received_ts_ms).map(|update| vec![update]),
        "price_change" => parse_changes(value, received_ts_ms),
        _ => Ok(Vec::new()),
    }
}

fn parse_snapshot(value: &Value, received_ts_ms: i64) -> Result<NormalizedUpdate, ParseError> {
    let instrument_id = string_field(value, "asset_id")?;
    let bids = parse_levels(value.get("bids"), "bids")?;
    let asks = parse_levels(value.get("asks"), "asks")?;
    Ok(NormalizedUpdate::Snapshot(SnapshotUpdate {
        venue: Venue::Polymarket,
        instrument_id,
        market_id: optional_string(value.get("market")),
        sequence: None,
        source_ts_ms: optional_i64(value.get("timestamp")),
        received_ts_ms,
        bids,
        asks,
    }))
}

fn parse_changes(value: &Value, received_ts_ms: i64) -> Result<Vec<NormalizedUpdate>, ParseError> {
    let market_id = optional_string(value.get("market"));
    let source_ts_ms = optional_i64(value.get("timestamp"));
    let changes = value
        .get("price_changes")
        .and_then(Value::as_array)
        .ok_or(ParseError::Field("price_changes"))?;
    changes
        .iter()
        .map(|change| {
            let side = match change.get("side").and_then(Value::as_str) {
                Some("BUY") | Some("buy") => BookSide::Bid,
                Some("SELL") | Some("sell") => BookSide::Ask,
                _ => return Err(ParseError::Field("price_changes.side")),
            };
            Ok(NormalizedUpdate::SetLevel(LevelUpdate {
                venue: Venue::Polymarket,
                instrument_id: string_field(change, "asset_id")?,
                market_id: market_id.clone(),
                sequence: None,
                source_ts_ms,
                received_ts_ms,
                side,
                price: decimal_field(change, "price", "price_changes.price")?,
                quantity: decimal_field(change, "size", "price_changes.size")?,
            }))
        })
        .collect()
}

fn parse_levels(value: Option<&Value>, field: &'static str) -> Result<Vec<Level>, ParseError> {
    value
        .and_then(Value::as_array)
        .ok_or(ParseError::Field(field))?
        .iter()
        .map(|level| {
            Ok(Level::new(
                decimal_field(level, "price", field)?,
                decimal_field(level, "size", field)?,
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
    let raw = value
        .get(field)
        .and_then(value_text)
        .ok_or(ParseError::Field(error_field))?;
    Decimal::from_str(&raw).map_err(|_| ParseError::Decimal {
        field: error_field,
        value: raw,
    })
}

fn value_text(value: &Value) -> Option<String> {
    value
        .as_str()
        .map(ToOwned::to_owned)
        .or_else(|| value.as_number().map(ToString::to_string))
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
