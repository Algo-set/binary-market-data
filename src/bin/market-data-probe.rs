use binary_market_data::connectors::{kalshi_rest, polymarket_ws};
use binary_market_data::discovery::{
    discover_kalshi, discover_polymarket, resolve_polymarket_condition, KalshiDiscoveryConfig,
    PolymarketDiscoveryConfig,
};
use binary_market_data::ConnectorMessage;
use std::env;
use std::process::ExitCode;
use std::time::Duration;
use tokio::sync::mpsc;

const DEFAULT_CLOB_BASE: &str = "https://clob.polymarket.com";
const USAGE: &str = "\
Usage:
  market-data-probe discover polymarket [--catalog-base URL] [--limit N] [--cursor CURSOR] [--query TEXT]
  market-data-probe discover kalshi [--rest-base URL] [--limit N] [--cursor CURSOR] [--query TEXT] [--status STATUS]
  market-data-probe polymarket [--condition-id ID | ASSET_ID...] [--clob-base URL] [--endpoint URL] [--depth N] [--max-updates N]
  market-data-probe kalshi [--rest-base URL] [--depth N] [--poll-ms N] [--max-updates N] MARKET_TICKER

STATUS may be open, unopened, paused, closed, settled, or all.
This program only reads public market data and writes normalized JSON to stdout.
";

#[derive(Debug)]
enum Command {
    DiscoverPolymarket(PolymarketDiscoveryConfig),
    DiscoverKalshi(KalshiDiscoveryConfig),
    Polymarket {
        endpoint: Option<String>,
        clob_base: String,
        condition_id: Option<String>,
        depth: usize,
        max_updates: Option<usize>,
        asset_ids: Vec<String>,
    },
    Kalshi {
        rest_base: Option<String>,
        depth: usize,
        poll_ms: u64,
        max_updates: Option<usize>,
        ticker: String,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    let command = match parse_args(env::args().skip(1).collect()) {
        Ok(command) => command,
        Err(error) => {
            eprintln!("{error}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    match run(command).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(1)
        }
    }
}

async fn run(command: Command) -> Result<(), String> {
    match command {
        Command::DiscoverPolymarket(config) => {
            let page = discover_polymarket(&config)
                .await
                .map_err(|error| error.to_string())?;
            println!(
                "{}",
                serde_json::to_string_pretty(&page).map_err(|error| error.to_string())?
            );
            Ok(())
        }
        Command::DiscoverKalshi(config) => {
            let page = discover_kalshi(&config)
                .await
                .map_err(|error| error.to_string())?;
            println!(
                "{}",
                serde_json::to_string_pretty(&page).map_err(|error| error.to_string())?
            );
            Ok(())
        }
        Command::Polymarket {
            endpoint,
            clob_base,
            condition_id,
            depth,
            max_updates,
            mut asset_ids,
        } => {
            if let Some(condition_id) = condition_id {
                asset_ids = resolve_polymarket_condition(&clob_base, &condition_id)
                    .await
                    .map_err(|error| error.to_string())?
                    .into_iter()
                    .map(|instrument| instrument.instrument_id)
                    .collect();
            }
            let mut config = polymarket_ws::Config::for_assets(asset_ids);
            config.output_depth = depth;
            if let Some(endpoint) = endpoint {
                config.endpoint = endpoint;
            }
            let (sender, receiver) = mpsc::channel(256);
            let task = tokio::spawn(polymarket_ws::run(config, sender));
            stream(receiver, task, max_updates).await
        }
        Command::Kalshi {
            rest_base,
            depth,
            poll_ms,
            max_updates,
            ticker,
        } => {
            let mut config = kalshi_rest::Config::for_market(ticker);
            config.depth = depth;
            config.poll_interval = Duration::from_millis(poll_ms);
            if let Some(rest_base) = rest_base {
                config.rest_base = rest_base;
            }
            let (sender, receiver) = mpsc::channel(256);
            let task = tokio::spawn(kalshi_rest::run(config, sender));
            stream(receiver, task, max_updates).await
        }
    }
}

async fn stream(
    mut receiver: mpsc::Receiver<ConnectorMessage>,
    task: tokio::task::JoinHandle<()>,
    limit: Option<usize>,
) -> Result<(), String> {
    let mut updates = 0usize;
    while let Some(message) = receiver.recv().await {
        println!(
            "{}",
            serde_json::to_string(&message).map_err(|error| error.to_string())?
        );
        if matches!(message, ConnectorMessage::Book(_)) {
            updates += 1;
            if limit.is_some_and(|limit| updates >= limit) {
                break;
            }
        }
    }
    task.abort();
    Ok(())
}

fn parse_args(args: Vec<String>) -> Result<Command, String> {
    match args.first().map(String::as_str) {
        Some("discover") => parse_discovery(&args[1..]),
        Some("polymarket" | "kalshi") => parse_stream(&args),
        Some(venue) => Err(format!("unsupported command or venue: {venue}")),
        None => Err("missing command or venue".to_string()),
    }
}

fn parse_discovery(args: &[String]) -> Result<Command, String> {
    let venue = args
        .first()
        .map(String::as_str)
        .ok_or_else(|| "missing discovery venue".to_string())?;
    let mut limit = 20usize;
    let mut cursor = None;
    let mut query = None;
    let mut catalog_base = None;
    let mut rest_base = None;
    let mut status = Some("open".to_string());
    let mut index = 1usize;
    while index < args.len() {
        let option = &args[index];
        let value = option_value(args, index)?;
        match option.as_str() {
            "--limit" => limit = parse_number(option, value)?,
            "--cursor" => cursor = Some(value.clone()),
            "--query" => query = Some(value.clone()),
            "--catalog-base" => catalog_base = Some(value.clone()),
            "--rest-base" => rest_base = Some(value.clone()),
            "--status" if value == "all" => status = None,
            "--status" if valid_kalshi_status(value) => status = Some(value.clone()),
            "--status" => return Err(format!("invalid Kalshi status: {value}")),
            _ => return Err(format!("unknown discovery option: {option}")),
        }
        index += 2;
    }
    match venue {
        "polymarket" if rest_base.is_none() && status == Some("open".to_string()) => {
            let mut config = PolymarketDiscoveryConfig::open_markets(limit);
            config.cursor = cursor;
            config.query = query;
            if let Some(base) = catalog_base {
                config.catalog_base = base;
            }
            Ok(Command::DiscoverPolymarket(config))
        }
        "polymarket" => {
            Err("Polymarket discovery accepts --catalog-base, not Kalshi options".into())
        }
        "kalshi" if catalog_base.is_none() => {
            let mut config = KalshiDiscoveryConfig::open_markets(limit);
            config.cursor = cursor;
            config.query = query;
            config.status = status;
            if let Some(base) = rest_base {
                config.rest_base = base;
            }
            Ok(Command::DiscoverKalshi(config))
        }
        "kalshi" => Err("Kalshi discovery accepts --rest-base, not --catalog-base".into()),
        _ => Err(format!("unsupported discovery venue: {venue}")),
    }
}

fn parse_stream(args: &[String]) -> Result<Command, String> {
    let venue = args[0].as_str();
    let mut depth = 20usize;
    let mut max_updates = None;
    let mut poll_ms = 1_000u64;
    let mut endpoint = None;
    let mut rest_base = None;
    let mut clob_base = DEFAULT_CLOB_BASE.to_string();
    let mut condition_id = None;
    let mut positionals = Vec::new();
    let mut index = 1usize;
    while index < args.len() {
        let item = &args[index];
        if !item.starts_with("--") {
            positionals.push(item.clone());
            index += 1;
            continue;
        }
        let value = option_value(args, index)?;
        match item.as_str() {
            "--depth" => depth = parse_number(item, value)?,
            "--max-updates" => max_updates = Some(parse_number(item, value)?),
            "--poll-ms" => poll_ms = parse_number(item, value)?,
            "--endpoint" => endpoint = Some(value.clone()),
            "--rest-base" => rest_base = Some(value.clone()),
            "--clob-base" => clob_base = value.clone(),
            "--condition-id" => condition_id = Some(value.clone()),
            _ => return Err(format!("unknown option: {item}")),
        }
        index += 2;
    }
    if depth == 0 || depth > 100 {
        return Err("depth must be between 1 and 100".to_string());
    }
    if max_updates == Some(0) {
        return Err("max-updates must be positive".to_string());
    }
    match venue {
        "polymarket"
            if rest_base.is_none() && (condition_id.is_some() ^ !positionals.is_empty()) =>
        {
            Ok(Command::Polymarket {
                endpoint,
                clob_base,
                condition_id,
                depth,
                max_updates,
                asset_ids: positionals,
            })
        }
        "polymarket" if condition_id.is_some() && !positionals.is_empty() => {
            Err("use either --condition-id or asset IDs, not both".to_string())
        }
        "polymarket" if rest_base.is_some() => {
            Err("Polymarket streaming does not accept --rest-base".to_string())
        }
        "polymarket" => Err("a condition ID or at least one asset ID is required".to_string()),
        "kalshi"
            if positionals.len() == 1
                && endpoint.is_none()
                && condition_id.is_none()
                && clob_base == DEFAULT_CLOB_BASE =>
        {
            Ok(Command::Kalshi {
                rest_base,
                depth,
                poll_ms: poll_ms.max(100),
                max_updates,
                ticker: positionals.remove(0),
            })
        }
        "kalshi" => Err("Kalshi streaming requires one ticker and only Kalshi options".to_string()),
        _ => Err(format!("unsupported venue: {venue}")),
    }
}

fn option_value(args: &[String], index: usize) -> Result<&String, String> {
    args.get(index + 1)
        .ok_or_else(|| format!("missing value for {}", args[index]))
}

fn valid_kalshi_status(value: &str) -> bool {
    matches!(value, "open" | "unopened" | "paused" | "closed" | "settled")
}

fn parse_number<T: std::str::FromStr>(name: &str, value: &str) -> Result<T, String> {
    value
        .parse()
        .map_err(|_| format!("invalid numeric value for {name}: {value}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_polymarket_condition_command() {
        let command = parse_args(vec![
            "polymarket".into(),
            "--condition-id".into(),
            "condition-example".into(),
            "--max-updates".into(),
            "2".into(),
        ])
        .unwrap();
        assert!(matches!(
            command,
            Command::Polymarket {
                condition_id: Some(_),
                max_updates: Some(2),
                ..
            }
        ));
    }

    #[test]
    fn parses_market_discovery_command() {
        let command = parse_args(vec![
            "discover".into(),
            "kalshi".into(),
            "--query".into(),
            "weather".into(),
            "--limit".into(),
            "5".into(),
        ])
        .unwrap();
        assert!(matches!(
            command,
            Command::DiscoverKalshi(KalshiDiscoveryConfig { limit: 5, .. })
        ));
    }

    #[test]
    fn rejects_condition_and_explicit_tokens_together() {
        let error = parse_args(vec![
            "polymarket".into(),
            "--condition-id".into(),
            "condition-example".into(),
            "token-example".into(),
        ])
        .unwrap_err();
        assert!(error.contains("either"));
    }

    #[test]
    fn rejects_zero_depth() {
        let error = parse_args(vec![
            "kalshi".into(),
            "--depth".into(),
            "0".into(),
            "EXAMPLE".into(),
        ])
        .unwrap_err();
        assert!(error.contains("depth"));
    }
}
