"""Read-only binary-market data; a standalone Python counterpart to the Rust crate."""

from .book import BookError, BookStore, OrderBook
from .types import (
    BookMessage,
    BookSide,
    BookView,
    ConnectionState,
    ConnectorMessage,
    Level,
    LevelUpdate,
    NormalizedUpdate,
    SnapshotUpdate,
    StatusMessage,
    UpdateType,
    Venue,
)

__version__ = "0.2.0"
__all__ = [
    "BookError",
    "BookStore",
    "OrderBook",
    "BookMessage",
    "BookSide",
    "BookView",
    "ConnectionState",
    "ConnectorMessage",
    "Level",
    "LevelUpdate",
    "NormalizedUpdate",
    "SnapshotUpdate",
    "StatusMessage",
    "UpdateType",
    "Venue",
]
