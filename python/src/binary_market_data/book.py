"""Snapshot-first books with atomic validation of individual updates."""

from decimal import Decimal, DecimalException, localcontext

from ._decimal import CONTEXT
from .types import (
    BookSide,
    BookView,
    Level,
    LevelUpdate,
    NormalizedUpdate,
    SnapshotUpdate,
    UpdateType,
    Venue,
    unix_time_ms,
)


class BookError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _price(price: Decimal) -> None:
    if not 0 <= price <= 1:
        raise BookError("invalid_price")


def _levels(levels: tuple[Level, ...]) -> dict[Decimal, Decimal]:
    result: dict[Decimal, Decimal] = {}
    with localcontext(CONTEXT):
        for level in levels:
            _price(level.price)
            if level.quantity < 0:
                raise BookError("invalid_quantity")
            if level.quantity:
                result[level.price] = result.get(level.price, Decimal(0)) + level.quantity
    return result


def _depth(depth: int) -> None:
    if type(depth) is not int or not 1 <= depth <= 100:
        raise BookError("invalid_depth")


class OrderBook:
    def __init__(self, snapshot: SnapshotUpdate) -> None:
        self._bids = _levels(snapshot.bids)
        self._asks = _levels(snapshot.asks)
        self._venue = snapshot.venue
        self._instrument_id = snapshot.instrument_id
        self._market_id = snapshot.market_id
        self._sequence = snapshot.sequence
        self._source_ts_ms = snapshot.source_ts_ms
        self._received_ts_ms = snapshot.received_ts_ms

    @classmethod
    def from_snapshot(cls, snapshot: SnapshotUpdate) -> "OrderBook":
        return cls(snapshot)

    def set_level(self, update: LevelUpdate) -> None:
        self._apply_level(update, relative=False)

    def add_level(self, update: LevelUpdate) -> None:
        self._apply_level(update, relative=True)

    def _apply_level(self, update: LevelUpdate, *, relative: bool) -> None:
        if (update.venue, update.instrument_id) != (self._venue, self._instrument_id):
            raise BookError("identity_mismatch")
        _price(update.price)
        if not relative and update.quantity < 0:
            raise BookError("invalid_quantity")
        if self._sequence is not None and update.sequence is not None:
            if update.sequence <= self._sequence:
                raise BookError("stale_sequence")
            if update.sequence != self._sequence + 1:
                raise BookError("sequence_gap")
        side = self._bids if update.side == BookSide.BID else self._asks
        with localcontext(CONTEXT):
            quantity = (
                side.get(update.price, Decimal(0)) + update.quantity
                if relative
                else update.quantity
            )
        if quantity < 0:
            raise BookError("negative_result")
        # Validate the result before changing either levels or metadata.
        try:
            Level(update.price, quantity)
        except ValueError:
            raise BookError("decimal_out_of_range") from None
        if quantity == 0:
            side.pop(update.price, None)
        else:
            side[update.price] = quantity
        if update.sequence is not None:
            self._sequence = update.sequence
        if update.source_ts_ms is not None:
            self._source_ts_ms = update.source_ts_ms
        if update.market_id is not None:
            self._market_id = update.market_id
        self._received_ts_ms = update.received_ts_ms

    def view(self, depth: int = 20) -> BookView:
        _depth(depth)
        bids = tuple(Level(p, self._bids[p]) for p in sorted(self._bids, reverse=True)[:depth])
        asks = tuple(Level(p, self._asks[p]) for p in sorted(self._asks)[:depth])
        best_bid, best_ask = bids[0] if bids else None, asks[0] if asks else None
        midpoint = spread = None
        crossed = False
        if best_bid is not None and best_ask is not None:
            with localcontext(CONTEXT):
                midpoint = (best_bid.price + best_ask.price) / Decimal(2)
                spread = best_ask.price - best_bid.price
            crossed = best_bid.price >= best_ask.price
        return BookView(
            venue=self._venue,
            instrument_id=self._instrument_id,
            market_id=self._market_id,
            sequence=self._sequence,
            source_ts_ms=self._source_ts_ms,
            received_ts_ms=self._received_ts_ms,
            published_ts_ms=unix_time_ms(),
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            crossed=crossed,
        )


class BookStore:
    def __init__(self) -> None:
        self._books: dict[tuple[Venue, str], OrderBook] = {}

    def apply(self, update: NormalizedUpdate, depth: int = 20) -> BookView:
        _depth(depth)
        key = (update.venue, update.instrument_id)
        try:
            if isinstance(update, SnapshotUpdate):
                book = OrderBook.from_snapshot(update)
                view = book.view(depth)
                self._books[key] = book
                return view
            book = self._books.get(key)
            if book is None:
                raise BookError("missing_snapshot")
            if update.update_type == UpdateType.SET_LEVEL:
                book.set_level(update)
            else:
                book.add_level(update)
            return book.view(depth)
        except DecimalException:
            raise BookError("decimal_out_of_range") from None
