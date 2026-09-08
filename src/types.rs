use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Venue {
    Polymarket,
    Kalshi,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BookSide {
    Bid,
    Ask,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Level {
    #[serde(with = "rust_decimal::serde::str")]
    pub price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub quantity: Decimal,
}

impl Level {
    pub fn new(price: Decimal, quantity: Decimal) -> Self {
        Self { price, quantity }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotUpdate {
    pub venue: Venue,
    pub instrument_id: String,
    pub market_id: Option<String>,
    pub sequence: Option<u64>,
    pub source_ts_ms: Option<i64>,
    pub received_ts_ms: i64,
    pub bids: Vec<Level>,
    pub asks: Vec<Level>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LevelUpdate {
    pub venue: Venue,
    pub instrument_id: String,
    pub market_id: Option<String>,
    pub sequence: Option<u64>,
    pub source_ts_ms: Option<i64>,
    pub received_ts_ms: i64,
    pub side: BookSide,
    #[serde(with = "rust_decimal::serde::str")]
    pub price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub quantity: Decimal,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "update_type", rename_all = "snake_case")]
pub enum NormalizedUpdate {
    Snapshot(SnapshotUpdate),
    SetLevel(LevelUpdate),
    AddLevel(LevelUpdate),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BookView {
    pub venue: Venue,
    pub instrument_id: String,
    pub market_id: Option<String>,
    pub sequence: Option<u64>,
    pub source_ts_ms: Option<i64>,
    pub received_ts_ms: i64,
    pub published_ts_ms: i64,
    pub bids: Vec<Level>,
    pub asks: Vec<Level>,
    pub best_bid: Option<Level>,
    pub best_ask: Option<Level>,
    #[serde(with = "rust_decimal::serde::str_option")]
    pub midpoint: Option<Decimal>,
    #[serde(with = "rust_decimal::serde::str_option")]
    pub spread: Option<Decimal>,
    pub crossed: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionState {
    Connecting,
    Connected,
    Disconnected,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "message_type", rename_all = "snake_case")]
pub enum ConnectorMessage {
    Status {
        venue: Venue,
        state: ConnectionState,
        at_ms: i64,
        detail: Option<String>,
    },
    Book(Box<BookView>),
}

pub fn unix_time_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}
