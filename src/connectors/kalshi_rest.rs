use crate::book::BookStore;
use crate::types::{unix_time_ms, ConnectionState, ConnectorMessage, Venue};
use crate::venue::kalshi;
use std::time::Duration;
use tokio::sync::mpsc;

#[derive(Debug, Clone)]
pub struct Config {
    pub rest_base: String,
    pub market_ticker: String,
    pub depth: usize,
    pub poll_interval: Duration,
    pub reconnect_max: Duration,
}

impl Config {
    pub fn for_market(market_ticker: String) -> Self {
        Self {
            rest_base: "https://external-api.kalshi.com/trade-api/v2".to_string(),
            market_ticker,
            depth: 20,
            poll_interval: Duration::from_secs(1),
            reconnect_max: Duration::from_secs(30),
        }
    }
}

pub async fn run(config: Config, output: mpsc::Sender<ConnectorMessage>) {
    let client = match reqwest::Client::builder()
        .user_agent("binary-market-data/0.1")
        .timeout(Duration::from_secs(10))
        .build()
    {
        Ok(client) => client,
        Err(_error) => {
            send_status(
                &output,
                ConnectionState::Disconnected,
                Some("client_configuration_error".to_string()),
            )
            .await;
            return;
        }
    };
    let encoded_ticker = urlencoding::encode(&config.market_ticker);
    let url = format!(
        "{}/markets/{}/orderbook?depth={}",
        config.rest_base.trim_end_matches('/'),
        encoded_ticker,
        config.depth.min(100)
    );
    let mut books = BookStore::default();
    let mut retry_delay = config.poll_interval;
    let mut connected = false;
    let mut last_levels = None;
    send_status(&output, ConnectionState::Connecting, None).await;

    loop {
        if output.is_closed() {
            return;
        }
        let result = async {
            let response = client.get(&url).send().await?.error_for_status()?;
            response.text().await
        }
        .await;
        match result {
            Ok(body) => match kalshi::parse_rest_snapshot(&config.market_ticker, &body) {
                Ok(update) => match books.apply(update, config.depth) {
                    Ok(view) => {
                        if !connected {
                            send_status(&output, ConnectionState::Connected, None).await;
                            connected = true;
                        }
                        let levels = (view.bids.clone(), view.asks.clone());
                        if last_levels.as_ref() != Some(&levels) {
                            last_levels = Some(levels);
                            if output
                                .send(ConnectorMessage::Book(Box::new(view)))
                                .await
                                .is_err()
                            {
                                return;
                            }
                        }
                        retry_delay = config.poll_interval;
                    }
                    Err(_error) => {
                        connected = false;
                        send_status(
                            &output,
                            ConnectionState::Disconnected,
                            Some("invalid_book_update".to_string()),
                        )
                        .await;
                    }
                },
                Err(_error) => {
                    connected = false;
                    send_status(
                        &output,
                        ConnectionState::Disconnected,
                        Some("invalid_venue_payload".to_string()),
                    )
                    .await;
                }
            },
            Err(_error) => {
                connected = false;
                send_status(
                    &output,
                    ConnectionState::Disconnected,
                    Some("venue_request_failed".to_string()),
                )
                .await;
                retry_delay = retry_delay.saturating_mul(2).min(config.reconnect_max);
            }
        }
        tokio::select! {
            _ = tokio::time::sleep(retry_delay) => {}
            _ = output.closed() => return,
        }
    }
}

async fn send_status(
    output: &mpsc::Sender<ConnectorMessage>,
    state: ConnectionState,
    detail: Option<String>,
) {
    let _ = output
        .send(ConnectorMessage::Status {
            venue: Venue::Kalshi,
            state,
            at_ms: unix_time_ms(),
            detail,
        })
        .await;
}
