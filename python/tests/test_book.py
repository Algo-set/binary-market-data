from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from random import Random

import pytest

from binary_market_data import (
    BookError,
    BookMessage,
    BookSide,
    BookStore,
    Level,
    LevelUpdate,
    OrderBook,
    SnapshotUpdate,
    UpdateType,
    Venue,
)

D = Decimal


def snapshot(**kwargs):
    return (
        SnapshotUpdate(
            venue=Venue.KALSHI,
            instrument_id="example-market",
            sequence=10,
            source_ts_ms=1,
            received_ts_ms=2,
            bids=(Level(D("0.40"), D("2")), Level(D("0.45"), D("3"))),
            asks=(Level(D("0.60"), D("4")), Level(D("0.55"), D("5"))),
        )
        if not kwargs
        else replace(snapshot(), **kwargs)
    )


def delta(**kwargs):
    return replace(
        LevelUpdate(
            venue=Venue.KALSHI,
            instrument_id="example-market",
            sequence=11,
            side=BookSide.BID,
            price=D("0.45"),
            quantity=D("1"),
            source_ts_ms=3,
            received_ts_ms=4,
            update_type=UpdateType.ADD_LEVEL,
        ),
        **kwargs,
    )


def stable(book):
    result = book.view().to_dict()
    result.pop("published_ts_ms")
    return result


def test_sorting_depth_and_immutable_view():
    book = OrderBook(snapshot())
    view = book.view(1)
    assert len(view.bids) == len(view.asks) == 1
    assert view.best_bid == Level(D("0.45"), D("3"))
    assert view.best_ask == Level(D("0.55"), D("5"))
    assert view.midpoint == D("0.50") and view.spread == D("0.10") and not view.crossed
    with pytest.raises(FrozenInstanceError):
        view.best_bid.quantity = D(9)
    book.add_level(delta())
    assert view.best_bid.quantity == D(3)
    assert book.view().best_bid.quantity == D(4)


def test_snapshot_duplicates_sum_and_zero_levels_are_removed():
    view = OrderBook(
        snapshot(
            bids=(
                Level(D("0.4"), D(2)),
                Level(D("0.40"), D(3)),
                Level(D("0.9"), D(0)),
            )
        )
    ).view()
    assert view.bids == (Level(D("0.4"), D(5)),)


def test_absolute_and_relative_changes_are_distinct_and_zero_removes():
    book = OrderBook(snapshot())
    book.set_level(delta(quantity=D(12)))
    assert book.view().best_bid.quantity == D(12)
    book.add_level(delta(sequence=12, quantity=D(-12)))
    assert book.view().best_bid.price == D("0.40")
    book.set_level(delta(sequence=13, price=D("0.40"), quantity=D(0)))
    assert book.view().best_bid is None


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"sequence": 12}, "sequence_gap"),
        ({"sequence": 10}, "stale_sequence"),
        ({"sequence": 9}, "stale_sequence"),
        ({"quantity": D(-4)}, "negative_result"),
        ({"price": D("1.001")}, "invalid_price"),
        ({"price": D("-0.01")}, "invalid_price"),
        ({"instrument_id": "different-example"}, "identity_mismatch"),
        ({"venue": Venue.POLYMARKET}, "identity_mismatch"),
    ],
)
def test_rejected_delta_leaves_levels_and_metadata_unchanged(change, code):
    book = OrderBook(snapshot())
    before = stable(book)
    with pytest.raises(BookError, match=code):
        book.add_level(delta(**change))
    assert stable(book) == before
    book.add_level(delta())
    assert book.view().best_bid.quantity == D(4)


def test_metadata_none_preserves_previous_values_and_updates_receive_clock():
    book = OrderBook(snapshot(market_id="example-condition"))
    book.add_level(delta(sequence=None, source_ts_ms=None))
    view = book.view()
    assert (view.sequence, view.source_ts_ms, view.market_id, view.received_ts_ms) == (
        10,
        1,
        "example-condition",
        4,
    )


def test_store_requires_snapshot_and_isolates_venue_and_instrument():
    store = BookStore()
    with pytest.raises(BookError, match="missing_snapshot"):
        store.apply(delta())
    store.apply(snapshot())
    with pytest.raises(BookError, match="missing_snapshot"):
        store.apply(delta(venue=Venue.POLYMARKET))
    with pytest.raises(BookError, match="missing_snapshot"):
        store.apply(delta(instrument_id="second-example"))
    store.apply(snapshot(venue=Venue.POLYMARKET))
    store.apply(delta(venue=Venue.POLYMARKET, quantity=D(7)))
    assert store.apply(delta()).best_bid.quantity == D(4)


def test_invalid_replacement_does_not_destroy_existing_snapshot():
    store = BookStore()
    store.apply(snapshot())
    with pytest.raises(BookError, match="invalid_quantity"):
        store.apply(snapshot(bids=(Level(D("0.45"), D(-1)),)))
    assert store.apply(delta()).best_bid.quantity == D(4)


@pytest.mark.parametrize("depth", [0, -1, 101, True, 1.5])
def test_depth_is_validated_before_mutation(depth):
    store = BookStore()
    store.apply(snapshot())
    with pytest.raises(BookError, match="invalid_depth"):
        store.apply(delta(), depth)
    assert store.apply(delta()).sequence == 11


@pytest.mark.parametrize(
    "asks,crossed,spread",
    [
        ((Level(D("0.45"), D(1)),), True, D(0)),
        ((Level(D("0.40"), D(1)),), True, D("-0.05")),
        ((), False, None),
    ],
)
def test_crossed_and_one_sided_books(asks, crossed, spread):
    view = OrderBook(snapshot(asks=asks)).view()
    assert view.crossed == crossed and view.spread == spread
    assert (view.midpoint is None) == (not asks)


@pytest.mark.parametrize(
    "value", [0.1, True, "NaN", "Infinity", "-Infinity", "1e999999", "1e-29", " 1", ""]
)
def test_float_nonfinite_and_extreme_decimals_are_rejected(value):
    with pytest.raises(ValueError):
        Level(value, D(1))


def test_decimal_arithmetic_does_not_use_callers_precision():
    with localcontext() as ctx:
        ctx.prec = 2
        book = OrderBook(
            snapshot(
                bids=(Level(D("0.1234567890123456789012345678"), D("0.123456789")),),
                asks=(Level(D("0.1234567890123456789012345679"), D("1")),),
            )
        )
        view = book.view()
        assert view.midpoint == D("0.12345678901234567890123456785")
        assert view.spread == D("0.0000000000000000000000000001")
        book.add_level(delta(price=view.best_bid.price, quantity=D("0.987654321")))
        assert book.view().best_bid.quantity == D("1.111111110")


def test_json_contract_uses_decimal_strings_and_flat_book_tag():
    result = BookMessage(OrderBook(snapshot()).view()).to_dict()
    assert result["message_type"] == "book" and "book" not in result
    assert result["venue"] == "kalshi" and result["market_id"] is None
    assert result["best_bid"] == {"price": "0.45", "quantity": "3"}
    assert isinstance(result["midpoint"], str)
    assert delta().to_dict()["update_type"] == "add_level"
    assert snapshot().to_dict()["update_type"] == "snapshot"


def test_deterministic_delta_replay_matches_integer_reference():
    # A separate integer reference catches cumulative rounding and level-deletion bugs.
    rng = Random(801)
    store = BookStore()
    store.apply(snapshot(bids=(), asks=(), sequence=0))
    expected = {}
    for seq in range(1, 301):
        cents = rng.randrange(1, 100)
        change = rng.randrange(-expected.get(cents, 0), 21)
        expected[cents] = expected.get(cents, 0) + change
        if expected[cents] == 0:
            del expected[cents]
        view = store.apply(delta(sequence=seq, price=D(cents) / 100, quantity=D(change)), 100)
        assert [(int(level.price * 100), int(level.quantity)) for level in view.bids] == sorted(
            expected.items(),
            reverse=True,
        )
