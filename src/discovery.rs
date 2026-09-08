use crate::types::Venue;
use reqwest::{Client, Url};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::Duration;
use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutcomeInstrument {
    pub outcome: Option<String>,
    pub instrument_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MarketDescriptor {
    pub venue: Venue,
    pub market_id: String,
    pub title: Option<String>,
    pub slug: Option<String>,
    pub status: String,
    pub book_enabled: bool,
    pub accepting_orders: bool,
    pub close_time: Option<String>,
    pub instruments: Vec<OutcomeInstrument>,
}

impl MarketDescriptor {
    pub fn book_instrument_ids(&self) -> Vec<String> {
        self.instruments
            .iter()
            .map(|instrument| instrument.instrument_id.clone())
            .collect()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MarketPage {
    pub venue: Venue,
    pub markets: Vec<MarketDescriptor>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone)]
pub struct PolymarketDiscoveryConfig {
    pub catalog_base: String,
    pub limit: usize,
    pub cursor: Option<String>,
    pub query: Option<String>,
}

impl PolymarketDiscoveryConfig {
    pub fn open_markets(limit: usize) -> Self {
        Self {
            catalog_base: "https://gamma-api.polymarket.com".to_string(),
            limit,
            cursor: None,
            query: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct KalshiDiscoveryConfig {
    pub rest_base: String,
    pub limit: usize,
    pub cursor: Option<String>,
    pub query: Option<String>,
    pub status: Option<String>,
}

impl KalshiDiscoveryConfig {
    pub fn open_markets(limit: usize) -> Self {
        Self {
            rest_base: "https://external-api.kalshi.com/trade-api/v2".to_string(),
            limit,
            cursor: None,
            query: None,
            status: Some("open".to_string()),
        }
    }
}

#[derive(Debug, Error)]
pub enum DiscoveryError {
    #[error("catalog endpoint is invalid")]
    InvalidEndpoint,
    #[error("catalog limit is outside the supported range")]
    InvalidLimit,
    #[error("catalog request failed")]
    Request(#[source] reqwest::Error),
    #[error("catalog response is invalid")]
    Json(#[source] serde_json::Error),
    #[error("catalog response is missing required field: {0}")]
    Field(&'static str),
}

pub async fn discover_polymarket(
    config: &PolymarketDiscoveryConfig,
) -> Result<MarketPage, DiscoveryError> {
    if !(1..=100).contains(&config.limit) {
        return Err(DiscoveryError::InvalidLimit);
    }
    let mut url = endpoint(&config.catalog_base, "markets/keyset")?;
    {
        let mut query = url.query_pairs_mut();
        query.append_pair("limit", &config.limit.to_string());
        query.append_pair("closed", "false");
        if let Some(cursor) = &config.cursor {
            query.append_pair("after_cursor", cursor);
        }
    }
    let body = request_text(url).await?;
    let mut page = parse_polymarket_page(&body)?;
    filter_page(&mut page, config.query.as_deref(), config.limit);
    Ok(page)
}

pub async fn discover_kalshi(config: &KalshiDiscoveryConfig) -> Result<MarketPage, DiscoveryError> {
    if !(1..=1000).contains(&config.limit) {
        return Err(DiscoveryError::InvalidLimit);
    }
    let mut url = endpoint(&config.rest_base, "markets")?;
    {
        let mut query = url.query_pairs_mut();
        query.append_pair("limit", &config.limit.to_string());
        if let Some(cursor) = &config.cursor {
            query.append_pair("cursor", cursor);
        }
        if let Some(status) = &config.status {
            query.append_pair("status", status);
        }
    }
    let body = request_text(url).await?;
    let mut page = parse_kalshi_page(&body)?;
    filter_page(&mut page, config.query.as_deref(), config.limit);
    Ok(page)
}

pub async fn resolve_polymarket_condition(
    clob_base: &str,
    condition_id: &str,
) -> Result<Vec<OutcomeInstrument>, DiscoveryError> {
    if condition_id.trim().is_empty() {
        return Err(DiscoveryError::Field("condition_id"));
    }
    let mut url = endpoint(clob_base, "clob-markets")?;
    url.path_segments_mut()
        .map_err(|_| DiscoveryError::InvalidEndpoint)?
        .push(condition_id);
    let body = request_text(url).await?;
    parse_polymarket_market_info(&body)
}

pub fn parse_polymarket_page(text: &str) -> Result<MarketPage, DiscoveryError> {
    let root: Value = serde_json::from_str(text).map_err(DiscoveryError::Json)?;
    let values = root
        .get("markets")
        .and_then(Value::as_array)
        .ok_or(DiscoveryError::Field("markets"))?;
    let mut markets = Vec::with_capacity(values.len());
    for value in values {
        let market_id = required_string(value, &["conditionId", "condition_id"])?;
        let token_ids = string_list(value.get("clobTokenIds"))?;
        let outcomes = string_list(value.get("outcomes"))?;
        let instruments = pair_instruments(token_ids, outcomes)?;
        let active = optional_bool(value, &["active"]).unwrap_or(false);
        let closed = optional_bool(value, &["closed"]).unwrap_or(false);
        let archived = optional_bool(value, &["archived"]).unwrap_or(false);
        let book_enabled = optional_bool(value, &["enableOrderBook", "enable_order_book"])
            .unwrap_or(!instruments.is_empty());
        markets.push(MarketDescriptor {
            venue: Venue::Polymarket,
            market_id,
            title: optional_string(value, &["question", "title"]),
            slug: optional_string(value, &["slug", "market_slug"]),
            status: market_status(active, closed, archived),
            book_enabled,
            accepting_orders: optional_bool(value, &["acceptingOrders", "accepting_orders"])
                .unwrap_or(active && !closed),
            close_time: optional_string(value, &["endDateIso", "endDate", "end_date_iso"]),
            instruments,
        });
    }
    Ok(MarketPage {
        venue: Venue::Polymarket,
        markets,
        next_cursor: optional_string(&root, &["next_cursor"]),
    })
}

pub fn parse_kalshi_page(text: &str) -> Result<MarketPage, DiscoveryError> {
    let root: Value = serde_json::from_str(text).map_err(DiscoveryError::Json)?;
    let values = root
        .get("markets")
        .and_then(Value::as_array)
        .ok_or(DiscoveryError::Field("markets"))?;
    let mut markets = Vec::with_capacity(values.len());
    for value in values {
        let ticker = required_string(value, &["ticker"])?;
        let status = optional_string(value, &["status"]).unwrap_or_else(|| "unknown".to_string());
        markets.push(MarketDescriptor {
            venue: Venue::Kalshi,
            market_id: ticker.clone(),
            title: optional_string(value, &["title", "subtitle", "yes_sub_title"]),
            slug: optional_string(value, &["event_ticker"]),
            status: status.clone(),
            book_enabled: optional_string(value, &["market_type"])
                .is_none_or(|kind| kind.eq_ignore_ascii_case("binary")),
            accepting_orders: status.eq_ignore_ascii_case("open")
                || status.eq_ignore_ascii_case("active"),
            close_time: optional_string(
                value,
                &["close_time", "expiration_time", "expected_expiration_time"],
            ),
            instruments: vec![OutcomeInstrument {
                outcome: None,
                instrument_id: ticker,
            }],
        });
    }
    Ok(MarketPage {
        venue: Venue::Kalshi,
        markets,
        next_cursor: optional_string(&root, &["cursor"]),
    })
}

pub fn parse_polymarket_market_info(text: &str) -> Result<Vec<OutcomeInstrument>, DiscoveryError> {
    let root: Value = serde_json::from_str(text).map_err(DiscoveryError::Json)?;
    let tokens = root
        .get("t")
        .or_else(|| root.get("tokens"))
        .and_then(Value::as_array)
        .ok_or(DiscoveryError::Field("tokens"))?;
    let instruments: Result<Vec<_>, _> = tokens
        .iter()
        .map(|token| {
            Ok(OutcomeInstrument {
                outcome: optional_string(token, &["o", "outcome"]),
                instrument_id: required_string(token, &["t", "token_id"])?,
            })
        })
        .collect();
    let instruments = instruments?;
    if instruments.is_empty() {
        return Err(DiscoveryError::Field("tokens"));
    }
    Ok(instruments)
}

async fn request_text(url: Url) -> Result<String, DiscoveryError> {
    let client = Client::builder()
        .timeout(Duration::from_secs(10))
        .user_agent("binary-market-data/0.1")
        .build()
        .map_err(DiscoveryError::Request)?;
    client
        .get(url)
        .send()
        .await
        .and_then(reqwest::Response::error_for_status)
        .map_err(DiscoveryError::Request)?
        .text()
        .await
        .map_err(DiscoveryError::Request)
}

fn endpoint(base: &str, relative: &str) -> Result<Url, DiscoveryError> {
    let base = format!("{}/", base.trim_end_matches('/'));
    let parsed = Url::parse(&base).map_err(|_| DiscoveryError::InvalidEndpoint)?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(DiscoveryError::InvalidEndpoint);
    }
    parsed
        .join(relative)
        .map_err(|_| DiscoveryError::InvalidEndpoint)
}

fn pair_instruments(
    token_ids: Vec<String>,
    outcomes: Vec<String>,
) -> Result<Vec<OutcomeInstrument>, DiscoveryError> {
    if token_ids.is_empty() {
        return Ok(Vec::new());
    }
    if !outcomes.is_empty() && outcomes.len() != token_ids.len() {
        return Err(DiscoveryError::Field("outcomes"));
    }
    Ok(token_ids
        .into_iter()
        .enumerate()
        .map(|(index, instrument_id)| OutcomeInstrument {
            outcome: outcomes.get(index).cloned(),
            instrument_id,
        })
        .collect())
}

fn string_list(value: Option<&Value>) -> Result<Vec<String>, DiscoveryError> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let parsed;
    let values = match value {
        Value::Array(values) => values,
        Value::String(text) => {
            parsed = serde_json::from_str::<Value>(text).map_err(DiscoveryError::Json)?;
            parsed
                .as_array()
                .ok_or(DiscoveryError::Field("string_array"))?
        }
        _ => return Err(DiscoveryError::Field("string_array")),
    };
    values
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|text| !text.is_empty())
                .map(ToOwned::to_owned)
                .ok_or(DiscoveryError::Field("string_array.item"))
        })
        .collect()
}

fn required_string(value: &Value, keys: &[&str]) -> Result<String, DiscoveryError> {
    optional_string(value, keys).ok_or(DiscoveryError::Field("identifier"))
}

fn optional_string(value: &Value, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| {
        value
            .get(*key)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .map(ToOwned::to_owned)
    })
}

fn optional_bool(value: &Value, keys: &[&str]) -> Option<bool> {
    keys.iter()
        .find_map(|key| value.get(*key).and_then(Value::as_bool))
}

fn market_status(active: bool, closed: bool, archived: bool) -> String {
    if archived {
        "archived"
    } else if closed {
        "closed"
    } else if active {
        "active"
    } else {
        "inactive"
    }
    .to_string()
}

fn filter_page(page: &mut MarketPage, query: Option<&str>, limit: usize) {
    page.markets.retain(|market| market.book_enabled);
    if let Some(query) = query.map(str::trim).filter(|query| !query.is_empty()) {
        let query = query.to_ascii_lowercase();
        page.markets.retain(|market| {
            market.market_id.to_ascii_lowercase().contains(&query)
                || market
                    .title
                    .as_deref()
                    .is_some_and(|value| value.to_ascii_lowercase().contains(&query))
                || market
                    .slug
                    .as_deref()
                    .is_some_and(|value| value.to_ascii_lowercase().contains(&query))
        });
    }
    page.markets.truncate(limit);
}
