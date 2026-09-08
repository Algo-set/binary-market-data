use crate::types::{
    BookSide, BookView, Level, LevelUpdate, NormalizedUpdate, SnapshotUpdate, Venue,
};
use rust_decimal::Decimal;
use std::collections::{BTreeMap, HashMap};
use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum BookError {
    #[error("price must be between zero and one: {0}")]
    InvalidPrice(Decimal),
    #[error("quantity must not be negative: {0}")]
    InvalidQuantity(Decimal),
    #[error("delta would make quantity negative at {price}: {quantity}")]
    NegativeResult { price: Decimal, quantity: Decimal },
    #[error("sequence is stale: current={current}, received={received}")]
    StaleSequence { current: u64, received: u64 },
    #[error("sequence gap: expected={expected}, received={received}")]
    SequenceGap { expected: u64, received: u64 },
    #[error("a snapshot is required before incremental updates for {0}")]
    MissingSnapshot(String),
}

#[derive(Debug, Clone)]
pub struct OrderBook {
    venue: Venue,
    instrument_id: String,
    market_id: Option<String>,
    sequence: Option<u64>,
    source_ts_ms: Option<i64>,
    received_ts_ms: i64,
    bids: BTreeMap<Decimal, Decimal>,
    asks: BTreeMap<Decimal, Decimal>,
}

impl OrderBook {
    pub fn from_snapshot(update: SnapshotUpdate) -> Result<Self, BookError> {
        let mut book = Self {
            venue: update.venue,
            instrument_id: update.instrument_id,
            market_id: update.market_id,
            sequence: update.sequence,
            source_ts_ms: update.source_ts_ms,
            received_ts_ms: update.received_ts_ms,
            bids: BTreeMap::new(),
            asks: BTreeMap::new(),
        };
        book.bids = collect_levels(update.bids)?;
        book.asks = collect_levels(update.asks)?;
        Ok(book)
    }

    pub fn set_level(&mut self, update: LevelUpdate) -> Result<(), BookError> {
        self.validate_identity(&update);
        validate_price(update.price)?;
        validate_quantity(update.quantity)?;
        self.check_sequence(update.sequence)?;
        let side = self.side_mut(update.side);
        if update.quantity.is_zero() {
            side.remove(&update.price);
        } else {
            side.insert(update.price, update.quantity);
        }
        self.finish_update(update);
        Ok(())
    }

    pub fn add_level(&mut self, update: LevelUpdate) -> Result<(), BookError> {
        self.validate_identity(&update);
        validate_price(update.price)?;
        self.check_sequence(update.sequence)?;
        let current = self
            .side(update.side)
            .get(&update.price)
            .copied()
            .unwrap_or(Decimal::ZERO);
        let next = current + update.quantity;
        if next.is_sign_negative() {
            return Err(BookError::NegativeResult {
                price: update.price,
                quantity: next,
            });
        }
        let side = self.side_mut(update.side);
        if next.is_zero() {
            side.remove(&update.price);
        } else {
            side.insert(update.price, next);
        }
        self.finish_update(update);
        Ok(())
    }

    pub fn view(&self, depth: usize) -> BookView {
        let bids: Vec<_> = self
            .bids
            .iter()
            .rev()
            .take(depth)
            .map(|(price, quantity)| Level::new(*price, *quantity))
            .collect();
        let asks: Vec<_> = self
            .asks
            .iter()
            .take(depth)
            .map(|(price, quantity)| Level::new(*price, *quantity))
            .collect();
        let best_bid = bids.first().cloned();
        let best_ask = asks.first().cloned();
        let (midpoint, spread, crossed) = match (&best_bid, &best_ask) {
            (Some(bid), Some(ask)) => (
                Some((bid.price + ask.price) / Decimal::TWO),
                Some(ask.price - bid.price),
                bid.price >= ask.price,
            ),
            _ => (None, None, false),
        };
        BookView {
            venue: self.venue,
            instrument_id: self.instrument_id.clone(),
            market_id: self.market_id.clone(),
            sequence: self.sequence,
            source_ts_ms: self.source_ts_ms,
            received_ts_ms: self.received_ts_ms,
            published_ts_ms: crate::types::unix_time_ms(),
            bids,
            asks,
            best_bid,
            best_ask,
            midpoint,
            spread,
            crossed,
        }
    }

    fn validate_identity(&self, update: &LevelUpdate) {
        debug_assert_eq!(self.venue, update.venue);
        debug_assert_eq!(self.instrument_id, update.instrument_id);
    }

    fn check_sequence(&self, received: Option<u64>) -> Result<(), BookError> {
        let (Some(current), Some(received)) = (self.sequence, received) else {
            return Ok(());
        };
        if received <= current {
            return Err(BookError::StaleSequence { current, received });
        }
        if received != current + 1 {
            return Err(BookError::SequenceGap {
                expected: current + 1,
                received,
            });
        }
        Ok(())
    }

    fn finish_update(&mut self, update: LevelUpdate) {
        self.sequence = update.sequence.or(self.sequence);
        self.source_ts_ms = update.source_ts_ms.or(self.source_ts_ms);
        self.received_ts_ms = update.received_ts_ms;
        if update.market_id.is_some() {
            self.market_id = update.market_id;
        }
    }

    fn side(&self, side: BookSide) -> &BTreeMap<Decimal, Decimal> {
        match side {
            BookSide::Bid => &self.bids,
            BookSide::Ask => &self.asks,
        }
    }

    fn side_mut(&mut self, side: BookSide) -> &mut BTreeMap<Decimal, Decimal> {
        match side {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        }
    }
}

#[derive(Debug, Default)]
pub struct BookStore {
    books: HashMap<(Venue, String), OrderBook>,
}

impl BookStore {
    pub fn apply(&mut self, update: NormalizedUpdate, depth: usize) -> Result<BookView, BookError> {
        match update {
            NormalizedUpdate::Snapshot(snapshot) => {
                let key = (snapshot.venue, snapshot.instrument_id.clone());
                let book = OrderBook::from_snapshot(snapshot)?;
                let view = book.view(depth);
                self.books.insert(key, book);
                Ok(view)
            }
            NormalizedUpdate::SetLevel(level) => {
                let key = (level.venue, level.instrument_id.clone());
                let book = self
                    .books
                    .get_mut(&key)
                    .ok_or_else(|| BookError::MissingSnapshot(level.instrument_id.clone()))?;
                book.set_level(level)?;
                Ok(book.view(depth))
            }
            NormalizedUpdate::AddLevel(level) => {
                let key = (level.venue, level.instrument_id.clone());
                let book = self
                    .books
                    .get_mut(&key)
                    .ok_or_else(|| BookError::MissingSnapshot(level.instrument_id.clone()))?;
                book.add_level(level)?;
                Ok(book.view(depth))
            }
        }
    }
}

fn collect_levels(levels: Vec<Level>) -> Result<BTreeMap<Decimal, Decimal>, BookError> {
    let mut result = BTreeMap::new();
    for level in levels {
        validate_price(level.price)?;
        validate_quantity(level.quantity)?;
        if !level.quantity.is_zero() {
            result
                .entry(level.price)
                .and_modify(|quantity| *quantity += level.quantity)
                .or_insert(level.quantity);
        }
    }
    Ok(result)
}

fn validate_price(price: Decimal) -> Result<(), BookError> {
    if price < Decimal::ZERO || price > Decimal::ONE {
        Err(BookError::InvalidPrice(price))
    } else {
        Ok(())
    }
}

fn validate_quantity(quantity: Decimal) -> Result<(), BookError> {
    if quantity.is_sign_negative() {
        Err(BookError::InvalidQuantity(quantity))
    } else {
        Ok(())
    }
}
