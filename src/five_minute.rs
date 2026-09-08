//! Public rolling five-minute catalogs and exact-contract order-book snapshots.
use crate::types::unix_time_ms;
use crate::venue::{kalshi, polymarket};
use crate::{BookStore, BookView, Level, NormalizedUpdate, SnapshotUpdate, Venue};
use chrono::DateTime;
use reqwest::{Client, Url};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::BTreeMap, time::Duration};

pub type Result<T> = std::result::Result<T, &'static str>;
pub const POLYMARKET: &str = "https://gamma-api.polymarket.com";
pub const CLOB: &str = "https://clob.polymarket.com";
pub const KALSHI: &str = "https://external-api.kalshi.com/trade-api/v2";
pub const LIMITLESS: &str = "https://api.limitless.exchange";
pub const MAX_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Contract {
    pub venue: Venue,
    pub market_id: String,
    pub instrument_ids: Vec<String>,
    pub window_start_ms: i64,
    pub window_end_ms: i64,
}
impl Contract {
    pub fn current(&self, now: i64) -> bool {
        self.window_end_ms.checked_sub(self.window_start_ms) == Some(300_000)
            && self.window_start_ms <= now
            && now < self.window_end_ms
    }
}
pub fn timestamp_ms(value: &Value) -> Option<i64> {
    value.as_i64().or_else(|| {
        DateTime::parse_from_rfc3339(value.as_str()?)
            .ok()
            .map(|d| d.timestamp_millis())
    })
}
pub fn select_contract(venue: Venue, row: &Value, now: i64, asset: &str) -> Option<Contract> {
    let (market_id, ids, start, end) = match venue {
        Venue::Polymarket => {
            if row["active"] != true
                || row["closed"] != false
                || row["enableOrderBook"] != true
                || row["acceptingOrders"] != true
                || !row["slug"]
                    .as_str()?
                    .starts_with(&format!("{}-updown-5m-", asset.to_lowercase()))
            {
                return None;
            }
            let raw = &row["clobTokenIds"];
            let tokens = if let Some(text) = raw.as_str() {
                serde_json::from_str::<Value>(text).ok()?
            } else {
                raw.clone()
            };
            let ids: Vec<String> = tokens
                .as_array()?
                .iter()
                .map(|v| Some(v.as_str()?.to_string()))
                .collect::<Option<_>>()?;
            if ids.len() != 2 {
                return None;
            }
            (
                row["conditionId"].as_str()?.to_string(),
                ids,
                timestamp_ms(&row["eventStartTime"])?,
                timestamp_ms(&row["endDate"])?,
            )
        }
        Venue::Kalshi => {
            if !matches!(row["status"].as_str(), Some("open" | "active"))
                || row["market_type"] != "binary"
            {
                return None;
            }
            let ticker = row["ticker"].as_str()?;
            if !ticker.starts_with(&format!("KX{}", asset.to_uppercase())) {
                return None;
            }
            (
                ticker.to_string(),
                vec![ticker.to_string()],
                timestamp_ms(&row["open_time"])?,
                timestamp_ms(&row["close_time"])?,
            )
        }
        Venue::Limitless => {
            if row["tradeType"] != "clob" || row["expired"] != false || row["status"] != "FUNDED" {
                return None;
            }
            let slug = row["slug"].as_str()?;
            let lower = asset.to_lowercase();
            let alias = match lower.as_str() {
                "btc" => "bitcoin",
                "eth" => "ethereum",
                "sol" => "solana",
                _ => &lower,
            };
            if !slug.to_lowercase().starts_with(&format!("{lower}-"))
                && !slug.to_lowercase().starts_with(&format!("{alias}-"))
            {
                return None;
            }
            (
                slug.to_string(),
                vec![slug.to_string()],
                timestamp_ms(&row["startAt"])?,
                timestamp_ms(&row["expirationTimestamp"])?,
            )
        }
        _ => return None,
    };
    if market_id.trim().is_empty()
        || ids.iter().any(|v| v.trim().is_empty())
        || (ids.len() == 2 && ids[0] == ids[1])
    {
        return None;
    }
    let contract = Contract {
        venue,
        market_id,
        instrument_ids: ids,
        window_start_ms: start,
        window_end_ms: end,
    };
    contract.current(now).then_some(contract)
}
pub fn public_client() -> Result<Client> {
    Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(10))
        .user_agent("binary-market-data-public/0.1")
        .build()
        .map_err(|_| "invalid_http_configuration")
}
pub fn endpoint(base: &str, path: &str) -> Result<String> {
    let u = Url::parse(base).map_err(|_| "invalid_endpoint")?;
    if !matches!(u.scheme(), "http" | "https")
        || u.host_str().is_none()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.query().is_some()
        || u.fragment().is_some()
        || base.chars().any(char::is_whitespace)
    {
        return Err("invalid_endpoint");
    }
    Ok(if path.is_empty() {
        base.to_string()
    } else {
        format!("{}/{}", base.trim_end_matches('/'), path)
    })
}
pub async fn response_text(response: reqwest::Response) -> Result<String> {
    let mut response = response
        .error_for_status()
        .map_err(|_| "public_request_failed")?;
    let mut bytes = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| "public_request_failed")?
    {
        if bytes.len().saturating_add(chunk.len()) > MAX_RESPONSE_BYTES {
            return Err("response_too_large");
        }
        bytes.extend_from_slice(&chunk);
    }
    String::from_utf8(bytes).map_err(|_| "invalid_venue_payload")
}
pub async fn get(
    client: &Client,
    base: &str,
    path: &str,
    params: &[(&str, String)],
) -> Result<String> {
    let response = client
        .get(endpoint(base, path)?)
        .query(params)
        .send()
        .await
        .map_err(|_| "public_request_failed")?;
    response_text(response).await
}
pub fn base_for(venue: Venue) -> Result<&'static str> {
    match venue {
        Venue::Polymarket => Ok(POLYMARKET),
        Venue::Kalshi => Ok(KALSHI),
        Venue::Limitless => Ok(LIMITLESS),
        _ => Err("public_book_transport_unavailable"),
    }
}
pub async fn discover(
    client: &Client,
    venue: Venue,
    asset: &str,
    pages: usize,
    now: i64,
    base: Option<&str>,
) -> Result<Vec<Contract>> {
    if !(1..=20).contains(&pages)
        || asset.is_empty()
        || !asset.bytes().all(|c| c.is_ascii_alphanumeric())
    {
        return Err("invalid_discovery_configuration");
    }
    let base = base.unwrap_or(base_for(venue)?);
    let mut cursor = String::new();
    let mut contracts = BTreeMap::new();
    for page in 1..=pages {
        let (path, params) = match venue {
            Venue::Polymarket => (
                "markets",
                vec![(
                    "slug",
                    format!("{}-updown-5m-{}", asset.to_lowercase(), now / 300_000 * 300),
                )],
            ),
            Venue::Kalshi => {
                let mut p = vec![
                    ("min_close_ts", (now / 1000).to_string()),
                    ("max_close_ts", (now / 1000 + 300).to_string()),
                    ("limit", "100".into()),
                ];
                if !cursor.is_empty() {
                    p.push(("cursor", cursor.clone()));
                }
                ("markets", p)
            }
            Venue::Limitless => (
                "markets/active",
                vec![
                    ("tradeType", "clob".into()),
                    ("limit", "25".into()),
                    ("page", page.to_string()),
                    ("sortBy", "newest".into()),
                ],
            ),
            _ => return Err("public_book_transport_unavailable"),
        };
        let root: Value = serde_json::from_str(&get(client, base, path, &params).await?)
            .map_err(|_| "invalid_catalog")?;
        let rows = match venue {
            Venue::Polymarket => root.as_array(),
            Venue::Kalshi => root["markets"].as_array(),
            _ => root["data"].as_array(),
        }
        .ok_or("invalid_catalog")?;
        for row in rows {
            if !row.is_object() {
                return Err("invalid_catalog");
            }
            if let Some(c) = select_contract(venue, row, now, asset) {
                contracts.insert(c.market_id.clone(), c);
            }
        }
        match venue {
            Venue::Polymarket => break,
            Venue::Kalshi => {
                let next = root["cursor"].as_str().unwrap_or("");
                if next.is_empty() {
                    break;
                }
                if next == cursor {
                    return Err("invalid_cursor");
                }
                cursor = next.into();
            }
            _ => {
                if rows.len() < 25 {
                    break;
                }
            }
        }
    }
    Ok(contracts.into_values().collect())
}
pub fn parse_limitless_book(slug: &str, text: &str) -> Result<NormalizedUpdate> {
    let root: Value = serde_json::from_str(text).map_err(|_| "invalid_venue_payload")?;
    let levels = |key: &str| -> Result<Vec<Level>> {
        root[key]
            .as_array()
            .ok_or("missing_book_side")?
            .iter()
            .map(|item| {
                let decimal = |key: &str| -> Result<Decimal> {
                    let v = &item[key];
                    let s = if let Some(s) = v.as_str() {
                        s.to_string()
                    } else if v.is_number() {
                        v.to_string()
                    } else {
                        return Err("invalid_decimal");
                    };
                    Decimal::from_str_exact(&s).map_err(|_| "invalid_decimal")
                };
                Ok(Level::new(decimal("price")?, decimal("size")?))
            })
            .collect()
    };
    Ok(NormalizedUpdate::Snapshot(SnapshotUpdate {
        venue: Venue::Limitless,
        instrument_id: slug.into(),
        market_id: Some(slug.into()),
        sequence: None,
        source_ts_ms: None,
        received_ts_ms: unix_time_ms(),
        bids: levels("bids")?,
        asks: levels("asks")?,
    }))
}
pub async fn fetch_books(
    client: &Client,
    contract: &Contract,
    depth: usize,
    base: Option<&str>,
) -> Result<Vec<BookView>> {
    if !contract.current(unix_time_ms()) {
        return Err("contract_window_closed");
    }
    if !(1..=100).contains(&depth) {
        return Err("invalid_depth");
    }
    let mut store = BookStore::default();
    let mut result = Vec::new();
    for instrument in &contract.instrument_ids {
        let update = match contract.venue {
            Venue::Polymarket => {
                let body = get(
                    client,
                    base.unwrap_or(CLOB),
                    "book",
                    &[("token_id", instrument.clone())],
                )
                .await?;
                let mut root: Value =
                    serde_json::from_str(&body).map_err(|_| "invalid_venue_payload")?;
                if root["asset_id"] != *instrument || root["market"] != contract.market_id {
                    return Err("book_identity_mismatch");
                }
                root["event_type"] = json!("book");
                polymarket::parse_message(&root.to_string())
                    .map_err(|_| "invalid_venue_payload")?
                    .into_iter()
                    .next()
                    .ok_or("missing_snapshot")?
            }
            Venue::Kalshi => {
                let body = get(
                    client,
                    base.unwrap_or(KALSHI),
                    &format!("markets/{}/orderbook", urlencoding::encode(instrument)),
                    &[("depth", depth.to_string())],
                )
                .await?;
                kalshi::parse_rest_snapshot(instrument, &body)
                    .map_err(|_| "invalid_venue_payload")?
            }
            Venue::Limitless => {
                let body = get(
                    client,
                    base.unwrap_or(LIMITLESS),
                    &format!("markets/{}/orderbook", urlencoding::encode(instrument)),
                    &[],
                )
                .await?;
                parse_limitless_book(instrument, &body)?
            }
            _ => return Err("public_book_transport_unavailable"),
        };
        result.push(
            store
                .apply(update, depth)
                .map_err(|_| "invalid_book_update")?,
        );
    }
    if !contract.current(unix_time_ms()) {
        return Err("contract_window_closed");
    }
    Ok(result)
}
