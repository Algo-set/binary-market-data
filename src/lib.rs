//! Read-only market-data components for binary prediction markets.
//!
//! This crate deliberately exposes no account, order, or execution API.

pub mod book;
pub mod connectors;
pub mod discovery;
pub mod five_minute;
pub mod prediction_pool;
pub mod types;
pub mod venue;

pub use book::{BookError, BookStore, OrderBook};
pub use discovery::{MarketDescriptor, MarketPage, OutcomeInstrument};
pub use types::{
    BookSide, BookView, ConnectionState, ConnectorMessage, Level, LevelUpdate, NormalizedUpdate,
    SnapshotUpdate, Venue,
};
