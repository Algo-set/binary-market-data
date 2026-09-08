import json
from decimal import Decimal

import pytest

from binary_market_data import BookSide, BookStore
from binary_market_data.venue import ParseError, kalshi, polymarket


def test_polymarket_snapshot_and_absolute_change(poly_book, poly_change):
    store = BookStore()
    view = store.apply(polymarket.parse_message(json.dumps(poly_book), received_ts_ms=100)[0])
    assert view.best_bid.quantity == 30 and view.received_ts_ms == 100
    view = store.apply(polymarket.parse_message(json.dumps(poly_change), received_ts_ms=101)[0])
    assert view.best_bid.quantity == 12 and view.source_ts_ms == 1757908892352


def test_array_message_shares_receive_timestamp(poly_book, poly_change):
    updates = polymarket.parse_message(json.dumps([poly_book, poly_change]), received_ts_ms=123)
    assert len(updates) == 2 and {update.received_ts_ms for update in updates} == {123}


def test_json_numeric_prices_are_parsed_without_binary_float_rounding():
    payload = (
        '{"event_type":"book","asset_id":"example",'
        '"bids":[{"price":0.1234567890123456789012345678,"size":12.5}],"asks":[]}'
    )
    update = polymarket.parse_message(payload)[0]
    assert update.bids[0].price == Decimal("0.1234567890123456789012345678")
    assert update.bids[0].quantity == Decimal("12.5")


def test_rest_no_bids_convert_exactly_to_yes_asks(kalshi_body):
    view = BookStore().apply(kalshi.parse_rest_snapshot("EXAMPLE", kalshi_body))
    assert view.best_bid.price == Decimal("0.42") and view.best_ask.price == Decimal("0.44")


def test_kalshi_yes_scale_websocket_does_not_invert_no_side_twice():
    payload = {
        "type": "orderbook_snapshot",
        "seq": 2,
        "msg": {
            "market_ticker": "EXAMPLE",
            "yes_dollars_fp": [["0.42", "13"]],
            "no_dollars_fp": [["0.44", "17"]],
        },
    }
    store = BookStore()
    view = store.apply(kalshi.parse_websocket_message(json.dumps(payload))[0])
    assert view.best_ask.price == Decimal("0.44")
    change = {
        "type": "orderbook_delta",
        "seq": 3,
        "msg": {
            "market_ticker": "EXAMPLE",
            "price_dollars": "0.44",
            "delta_fp": "-2",
            "outcome_side": "no",
        },
    }
    update = kalshi.parse_websocket_message(json.dumps(change))[0]
    assert update.side == BookSide.ASK
    view = store.apply(update)
    assert view.best_ask.quantity == 15 and view.sequence == 3
    assert kalshi.websocket_subscription(7, ["EXAMPLE"])["params"]["use_yes_price"] is True


@pytest.mark.parametrize(
    "body",
    [
        "null",
        "[]",
        "{",
        '{"orderbook_fp":{}}',
        '{"orderbook_fp":{"yes_dollars":[[]],"no_dollars":[]}}',
    ],
)
def test_invalid_kalshi_payloads_have_safe_errors(body):
    with pytest.raises(ParseError):
        kalshi.parse_rest_snapshot("EXAMPLE", body)


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", True, {}, None, "sensitive-fixture-value", "1e10000"]
)
def test_invalid_polymarket_decimal_does_not_echo_payload(poly_book, value):
    poly_book["bids"][0]["price"] = value
    with pytest.raises(ParseError) as error:
        polymarket.parse_message(json.dumps(poly_book))
    assert str(error.value) == "invalid_venue_payload:bids"


def test_nonstandard_json_constants_and_unknown_events():
    with pytest.raises(ParseError):
        polymarket.parse_message('{"unused":NaN}')
    assert polymarket.parse_message('{"event_type":"tick_size_change"}') == []
    assert polymarket.subscription(["example-token"])["initial_dump"] is True


def test_bad_change_rejects_whole_message(poly_change):
    poly_change["price_changes"].append({"asset_id": "second-example", "side": "wrong"})
    with pytest.raises(ParseError):
        polymarket.parse_message(json.dumps(poly_change))
