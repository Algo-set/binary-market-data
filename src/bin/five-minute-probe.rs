use binary_market_data::{
    five_minute as feed, prediction_pool as pool, types::unix_time_ms, Venue,
};
use serde_json::{json, Value};
use std::{
    env,
    io::{self, Write},
    time::Duration,
};

struct Config {
    venue: Venue,
    asset: String,
    depth: usize,
    pages: usize,
    poll_ms: u64,
    timeout: u64,
    max_updates: usize,
    once: bool,
    rpc_url: String,
}
fn parse(args: &[String]) -> feed::Result<Config> {
    let venue = match args.first().map(String::as_str) {
        Some("polymarket") => Venue::Polymarket,
        Some("kalshi") => Venue::Kalshi,
        Some("limitless") => Venue::Limitless,
        Some("pancakeswap") => Venue::Pancakeswap,
        Some("crypto_com") => Venue::CryptoCom,
        _ => return Err("invalid_venue"),
    };
    let mut c = Config {
        venue,
        asset: "BTC".into(),
        depth: 20,
        pages: 3,
        poll_ms: 5000,
        timeout: 30,
        max_updates: 0,
        once: false,
        rpc_url: pool::RPC.into(),
    };
    let mut i = 1;
    while i < args.len() {
        if args[i] == "--once" {
            c.once = true;
            i += 1;
            continue;
        }
        let v = args.get(i + 1).ok_or("missing_argument")?;
        match args[i].as_str() {
            "--asset" => c.asset = v.clone(),
            "--rpc-url" => c.rpc_url = v.clone(),
            "--depth" => c.depth = v.parse().map_err(|_| "invalid_depth")?,
            "--pages" => c.pages = v.parse().map_err(|_| "invalid_pages")?,
            "--poll-ms" => c.poll_ms = v.parse().map_err(|_| "invalid_poll_interval")?,
            "--timeout" => c.timeout = v.parse().map_err(|_| "invalid_timeout")?,
            "--max-updates" => c.max_updates = v.parse().map_err(|_| "invalid_limit")?,
            _ => return Err("unknown_argument"),
        }
        i += 2;
    }
    if !(1..=100).contains(&c.depth)
        || !(1..=20).contains(&c.pages)
        || !(1000..=60000).contains(&c.poll_ms)
        || c.timeout == 0
        || c.timeout > 86400
        || c.asset.is_empty()
        || !c.asset.bytes().all(|v| v.is_ascii_alphanumeric())
    {
        return Err("invalid_configuration");
    }
    feed::endpoint(&c.rpc_url, "")?;
    if c.venue == Venue::Pancakeswap {
        pool::contract_for(&c.asset)?;
    }
    Ok(c)
}
fn emit(value: Value) -> feed::Result<()> {
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{value}")
        .and_then(|_| stdout.flush())
        .map_err(|_| "output_closed")
}
async fn run(c: Config) -> feed::Result<()> {
    if c.venue == Venue::CryptoCom {
        return emit(
            json!({"message_type":"status","venue":c.venue,"state":"unsupported","detail":"no_verified_credential_free_strike_options_book_transport"}),
        );
    }
    let client = feed::public_client()?;
    let mut updates = 0;
    let mut contracts: Vec<feed::Contract> = Vec::new();
    let mut refresh_at = 0;
    let mut delay = c.poll_ms;
    loop {
        let outcome:feed::Result<Vec<Value>>=async {
            if c.venue==Venue::Pancakeswap{return pool::fetch_rounds(&client,&c.asset,&c.rpc_url).await;}
            let now=unix_time_ms();contracts.retain(|v|v.current(now));
            if now>=refresh_at||contracts.is_empty(){
                contracts=feed::discover(&client,c.venue,&c.asset,c.pages,now,None).await?;
                if contracts.len()>20{return Err("too_many_matching_contracts");}refresh_at=now+15000;
            }
            if contracts.is_empty(){return Ok(vec![json!({"message_type":"status","venue":c.venue,"state":"unavailable","detail":"no_current_five_minute_contract_in_scanned_catalog"})]);}
            let mut messages=Vec::new();
            for contract in &contracts {
                for book in feed::fetch_books(&client,contract,c.depth,None).await? {
                    messages.push(json!({"message_type":"contract_book","contract":contract,"book":book}));
                }
            }Ok(messages)
        }.await;
        match outcome {
            Ok(messages) => {
                delay = c.poll_ms;
                for message in messages {
                    if message["message_type"] == "contract_book"
                        && message["contract"]["window_end_ms"]
                            .as_i64()
                            .is_some_and(|end| unix_time_ms() >= end)
                    {
                        continue;
                    }
                    if matches!(
                        message["message_type"].as_str(),
                        Some("contract_book" | "prediction_pool")
                    ) {
                        updates += 1;
                    }
                    emit(message)?;
                    if c.max_updates > 0 && updates >= c.max_updates {
                        return Ok(());
                    }
                }
            }
            Err(_) => {
                contracts.clear();
                emit(
                    json!({"message_type":"status","venue":c.venue,"state":"disconnected","detail":"public_feed_unavailable_or_invalid"}),
                )?;
                delay = (delay * 2).min(30000);
            }
        }
        if c.once {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(delay)).await;
    }
}
#[tokio::main]
async fn main() {
    let args: Vec<_> = env::args().skip(1).collect();
    if args.iter().any(|s| s == "--help" || s == "-h") {
        println!("five-minute-probe VENUE [--asset BTC] [--once] [--timeout 30] [--max-updates 3] [--poll-ms 5000] [--pages 3] [--depth 20] [--rpc-url PUBLIC_URL]\nVenues: polymarket, kalshi, limitless, pancakeswap, crypto_com. Public data only; PancakeSwap emits pools, Crypto.com reports unsupported book transport.");
        return;
    }
    let result = match parse(&args) {
        Ok(c) => {
            let deadline = Duration::from_secs(c.timeout);
            tokio::select! {r=tokio::time::timeout(deadline,run(c))=>r.unwrap_or(Err("probe_timeout")),_ = tokio::signal::ctrl_c()=>Ok(())}
        }
        Err(e) => Err(e),
    };
    if let Err(code) = result {
        if code == "output_closed" {
            return;
        }
        eprintln!("{}", json!({"error":code}));
        std::process::exit(1);
    }
}
