use crate::book::BookStore;
use crate::types::{unix_time_ms, ConnectionState, ConnectorMessage, Venue};
use crate::venue::polymarket;
use futures_util::{SinkExt, StreamExt};
use std::collections::BTreeMap;
use std::time::Duration;
use thiserror::Error;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

#[derive(Debug, Clone)]
pub struct Config {
    pub endpoint: String,
    pub asset_ids: Vec<String>,
    pub output_depth: usize,
    pub reconnect_min: Duration,
    pub reconnect_max: Duration,
    pub heartbeat: Duration,
}

impl Config {
    pub fn for_assets(asset_ids: Vec<String>) -> Self {
        Self {
            endpoint: "wss://ws-subscriptions-clob.polymarket.com/ws/market".to_string(),
            asset_ids,
            output_depth: 20,
            reconnect_min: Duration::from_millis(250),
            reconnect_max: Duration::from_secs(30),
            heartbeat: Duration::from_secs(10),
        }
    }
}

#[derive(Debug, Error)]
enum SessionError {
    #[error("websocket: {0}")]
    Websocket(#[from] tokio_tungstenite::tungstenite::Error),
    #[error("subscription serialization: {0}")]
    Json(#[from] serde_json::Error),
    #[error("connection closed")]
    Closed,
}

pub async fn run(config: Config, output: mpsc::Sender<ConnectorMessage>) {
    let mut delay = config.reconnect_min;
    loop {
        if output.is_closed() {
            return;
        }
        let _ = status(&output, ConnectionState::Connecting, None).await;
        match run_session(&config, &output).await {
            Ok(()) => return,
            Err(_error) => {
                if output.is_closed() {
                    return;
                }
                let _ = status(
                    &output,
                    ConnectionState::Disconnected,
                    Some("websocket_session_failed".to_string()),
                )
                .await;
            }
        }
        tokio::select! {
            _ = tokio::time::sleep(delay) => {}
            _ = output.closed() => return,
        }
        delay = delay.saturating_mul(2).min(config.reconnect_max);
    }
}

async fn run_session(
    config: &Config,
    output: &mpsc::Sender<ConnectorMessage>,
) -> Result<(), SessionError> {
    let (mut socket, _) = tokio_tungstenite::connect_async(&config.endpoint).await?;
    socket
        .send(Message::Text(
            serde_json::to_string(&polymarket::subscription(&config.asset_ids))?.into(),
        ))
        .await?;
    status(output, ConnectionState::Connected, None)
        .await
        .map_err(|_| SessionError::Closed)?;
    let mut books = BookStore::default();
    let mut heartbeat = tokio::time::interval(config.heartbeat);
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    loop {
        tokio::select! {
            _ = heartbeat.tick() => socket.send(Message::Text("PING".into())).await?,
            message = socket.next() => {
                let message = message.ok_or(SessionError::Closed)??;
                let text = match message {
                    Message::Text(text) if text != "PONG" => text,
                    Message::Binary(bytes) => String::from_utf8_lossy(&bytes).into_owned().into(),
                    Message::Ping(bytes) => {
                        socket.send(Message::Pong(bytes)).await?;
                        continue;
                    }
                    Message::Close(_) => return Err(SessionError::Closed),
                    _ => continue,
                };
                let mut latest = BTreeMap::new();
                for update in polymarket::parse_message(&text).unwrap_or_default() {
                    if let Ok(view) = books.apply(update, config.output_depth) {
                        latest.insert(view.instrument_id.clone(), view);
                    }
                }
                for view in latest.into_values() {
                    if output
                        .send(ConnectorMessage::Book(Box::new(view)))
                        .await
                        .is_err()
                    {
                            return Ok(());
                    }
                }
            }
            _ = output.closed() => return Ok(()),
        }
    }
}

async fn status(
    output: &mpsc::Sender<ConnectorMessage>,
    state: ConnectionState,
    detail: Option<String>,
) -> Result<(), mpsc::error::SendError<ConnectorMessage>> {
    output
        .send(ConnectorMessage::Status {
            venue: Venue::Polymarket,
            state,
            at_ms: unix_time_ms(),
            detail,
        })
        .await
}
