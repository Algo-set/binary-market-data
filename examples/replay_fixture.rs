//! Offline JSON replay for comparing the Rust and Python implementations.
use binary_market_data::venue::{kalshi, polymarket};
use binary_market_data::{BookError, BookStore, NormalizedUpdate};
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, Read};

#[derive(Deserialize)]
struct Step {
    parser: String,
    payload: Value,
    instrument_id: Option<String>,
}

fn code(error: &BookError) -> &'static str {
    match error {
        BookError::InvalidPrice(_) => "invalid_price",
        BookError::InvalidQuantity(_) => "invalid_quantity",
        BookError::NegativeResult { .. } => "negative_result",
        BookError::StaleSequence { .. } => "stale_sequence",
        BookError::SequenceGap { .. } => "sequence_gap",
        BookError::MissingSnapshot(_) => "missing_snapshot",
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let steps: Vec<Step> = serde_json::from_str(&input)?;
    let mut books = BookStore::default();
    let mut results = Vec::new();
    for step in steps {
        let payload = serde_json::to_string(&step.payload)?;
        let updates: Result<Vec<NormalizedUpdate>, String> = match step.parser.as_str() {
            "polymarket" => polymarket::parse_message(&payload).map_err(|_| "parse_error".into()),
            "kalshi_ws" => {
                kalshi::parse_websocket_message(&payload).map_err(|_| "parse_error".into())
            }
            "kalshi_rest" => kalshi::parse_rest_snapshot(
                step.instrument_id
                    .as_deref()
                    .ok_or("missing instrument_id")?,
                &payload,
            )
            .map(|update| vec![update])
            .map_err(|_| "parse_error".into()),
            _ => return Err("unsupported fixture parser".into()),
        };
        match updates {
            Err(error) => results.push(json!({"error": error})),
            Ok(updates) => {
                let mut outcomes = Vec::new();
                for update in updates {
                    let serialized = serde_json::to_value(&update)?;
                    let outcome = match books.apply(update, 100) {
                        Ok(view) => json!({"update": serialized, "view": view}),
                        Err(error) => json!({"update": serialized, "error": code(&error)}),
                    };
                    outcomes.push(outcome);
                }
                results.push(json!({"outcomes": outcomes}));
            }
        }
    }
    println!("{}", serde_json::to_string(&results)?);
    Ok(())
}
