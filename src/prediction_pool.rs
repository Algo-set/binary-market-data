//! PancakeSwap V2 aggregate rounds via read-only JSON-RPC; no synthetic depth.
use crate::five_minute::{endpoint, response_text, Result};
use crate::types::unix_time_ms;
use num_bigint::{BigInt, BigUint, Sign};
use reqwest::Client;
use serde_json::{json, Value};

pub const RPC: &str = "https://bsc-dataseed.bnbchain.org";
pub fn contract_for(asset: &str) -> Result<&'static str> {
    match asset.to_uppercase().as_str() {
        "BNB" => Ok("0x18b2a687610328590bc8f2e5fedde3b582a49cda"),
        "BTC" => Ok("0x48781a7d35f6137a9135bbb984af65fd6ab25618"),
        "ETH" => Ok("0x7451f994a8d510cbcb46cf57d50f31f188ff58f5"),
        _ => Err("unsupported_pool_asset"),
    }
}
pub fn words(encoded: &str, count: usize) -> Result<Vec<BigUint>> {
    let text = encoded.strip_prefix("0x").ok_or("invalid_rpc_abi")?;
    if text.len() != count * 64 || !text.bytes().all(|v| v.is_ascii_hexdigit()) {
        return Err("invalid_rpc_abi");
    }
    text.as_bytes()
        .chunks(64)
        .map(|word| BigUint::parse_bytes(word, 16).ok_or("invalid_rpc_abi"))
        .collect()
}
fn small(value: &BigUint) -> Result<u64> {
    u64::try_from(value).map_err(|_| "integer_overflow")
}
fn hex(value: &Value) -> Result<u64> {
    u64::from_str_radix(
        value
            .as_str()
            .and_then(|s| s.strip_prefix("0x"))
            .ok_or("invalid_hex")?,
        16,
    )
    .map_err(|_| "invalid_hex")
}
fn signed(value: &BigUint) -> String {
    let n = BigInt::from_biguint(Sign::Plus, value.clone());
    if value.bits() == 256 {
        (n - (BigInt::from(1) << 256usize)).to_string()
    } else {
        n.to_string()
    }
}
pub fn decode_round(
    encoded: &str,
    contract: &str,
    epoch: u64,
    decimals: u64,
    paused: bool,
    block: &Value,
) -> Result<Value> {
    let d = words(encoded, 14)?;
    let start = small(&d[1])?;
    let scheduled_lock = small(&d[2])?;
    let close = small(&d[3])?;
    if small(&d[0])? != epoch {
        return Err("round_identity_mismatch");
    }
    if small(&d[13])? > 1 {
        return Err("invalid_round_flag");
    }
    if !(start > 0 && start < scheduled_lock && scheduled_lock < close)
        || close.checked_sub(scheduled_lock).is_none_or(|s| s < 300)
    {
        return Err("invalid_round_times");
    }
    if close > i64::MAX as u64 / 1000 {
        return Err("integer_overflow");
    }
    let locked = d[6] != BigUint::from(0u8);
    // _safeLockRound assigns closeTimestamp = actual block timestamp + interval.
    let window_start = if locked { close - 300 } else { scheduled_lock };
    let at = hex(&block["timestamp"])?;
    let phase = if small(&d[13])? == 1 {
        "resolved"
    } else if at < scheduled_lock {
        "entry"
    } else if !locked {
        "awaiting_lock"
    } else if at < close {
        "live"
    } else {
        "closed"
    };
    Ok(
        json!({"message_type":"prediction_pool","venue":"pancakeswap","order_book_available":false,
        "contract":contract,"epoch":epoch.to_string(),"entry_start_ms":start*1000,"scheduled_lock_ms":scheduled_lock*1000,
        "window_start_ms":window_start*1000,"window_end_ms":close*1000,"interval_seconds":300,
        "lock_price_raw":signed(&d[4]),"close_price_raw":signed(&d[5]),"oracle_decimals":decimals,
        "total_amount_wei":d[8].to_string(),"side_total_amount_wei":(&d[9]+&d[10]).to_string(),
        "totals_consistent":&d[9]+&d[10]==d[8],"data_status":if &d[9]+&d[10]==d[8] {"consistent"}else{"inconsistent_totals"},"bull_amount_wei":d[9].to_string(),"bear_amount_wei":d[10].to_string(),
        "oracle_called":small(&d[13])?==1,"paused":paused,"block_number":block["number"],"block_hash":block["hash"],
        "block_timestamp_ms":at*1000,"phase":phase,"pool_currency":"BNB"}),
    )
}
async fn rpc_read(client: &Client, base: &str, method: &str, params: Value) -> Result<Value> {
    if !matches!(method, "eth_chainId" | "eth_getBlockByNumber" | "eth_call") {
        return Err("unsupported_rpc_read");
    }
    let response = client
        .post(endpoint(base, "")?)
        .json(&json!({"jsonrpc":"2.0","id":1,"method":method,"params":params}))
        .send()
        .await
        .map_err(|_| "public_rpc_failed")?;
    let root: Value =
        serde_json::from_str(&response_text(response).await?).map_err(|_| "invalid_rpc_payload")?;
    if root["id"] != 1 || !root["error"].is_null() || root.get("result").is_none() {
        return Err("public_rpc_failed");
    }
    Ok(root["result"].clone())
}
async fn call(
    client: &Client,
    base: &str,
    address: &str,
    data: &str,
    block: &Value,
) -> Result<String> {
    let value = rpc_read(
        client,
        base,
        "eth_call",
        json!([{"to":address,"data":data},block["number"]]),
    )
    .await?;
    value.as_str().map(String::from).ok_or("invalid_rpc_abi")
}
pub async fn fetch_rounds(client: &Client, asset: &str, base: &str) -> Result<Vec<Value>> {
    let contract = contract_for(asset)?;
    if rpc_read(client, base, "eth_chainId", json!([])).await? != "0x38" {
        return Err("wrong_chain");
    }
    let block = rpc_read(
        client,
        base,
        "eth_getBlockByNumber",
        json!(["latest", false]),
    )
    .await?;
    let hash = block["hash"].as_str().ok_or("invalid_block")?;
    if hash.len() != 66
        || !hash.starts_with("0x")
        || !hash[2..].bytes().all(|c| c.is_ascii_hexdigit())
    {
        return Err("invalid_block");
    }
    let at = i64::try_from(hex(&block["timestamp"])?)
        .map_err(|_| "invalid_block")?
        .checked_mul(1000)
        .ok_or("invalid_block")?;
    let age = unix_time_ms().checked_sub(at).ok_or("invalid_block")?;
    if !(-15000..=60000).contains(&age) {
        return Err("stale_block");
    }
    if small(
        &words(
            &call(client, base, contract, "0x7d1cd04f", &block).await?,
            1,
        )?[0],
    )? != 300
    {
        return Err("not_five_minute_contract");
    }
    let paused = small(
        &words(
            &call(client, base, contract, "0x5c975abb", &block).await?,
            1,
        )?[0],
    )?;
    if paused > 1 {
        return Err("invalid_pause_flag");
    }
    let epoch = small(
        &words(
            &call(client, base, contract, "0x76671808", &block).await?,
            1,
        )?[0],
    )?;
    let oracle = words(
        &call(client, base, contract, "0x7dc0d1d0", &block).await?,
        1,
    )?
    .remove(0);
    if oracle.bits() > 160 || oracle == BigUint::from(0u8) {
        return Err("invalid_oracle");
    }
    let decimals = small(
        &words(
            &call(
                client,
                base,
                &format!("0x{oracle:040x}"),
                "0x313ce567",
                &block,
            )
            .await?,
            1,
        )?[0],
    )?;
    if decimals > 18 {
        return Err("unsupported_oracle_precision");
    }
    let mut result = Vec::new();
    for selected in [epoch, epoch.saturating_sub(1)] {
        if selected == 0 {
            continue;
        }
        let raw = call(
            client,
            base,
            contract,
            &format!("0x8c65c81f{selected:064x}"),
            &block,
        )
        .await?;
        result.push(decode_round(
            &raw,
            contract,
            selected,
            decimals,
            paused == 1,
            &block,
        )?);
    }
    let confirmed = rpc_read(
        client,
        base,
        "eth_getBlockByNumber",
        json!([block["number"], false]),
    )
    .await?;
    if confirmed["hash"] != block["hash"] {
        return Err("block_changed");
    }
    if unix_time_ms().saturating_sub(at) > 60000 {
        return Err("stale_block");
    }
    Ok(result)
}
