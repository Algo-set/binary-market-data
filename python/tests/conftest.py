import json

import pytest


@pytest.fixture
def poly_book():
    return {
        "event_type": "book",
        "asset_id": "example-token",
        "market": "example-condition",
        "bids": [{"price": "0.48", "size": "30"}],
        "asks": [{"price": "0.52", "size": "25"}],
        "timestamp": "1757908892351",
    }


@pytest.fixture
def poly_change():
    return {
        "event_type": "price_change",
        "market": "example-condition",
        "timestamp": "1757908892352",
        "price_changes": [
            {"asset_id": "example-token", "price": "0.48", "size": "12", "side": "BUY"}
        ],
    }


@pytest.fixture
def kalshi_body():
    return json.dumps(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4200", "13.00"]],
                "no_dollars": [["0.5600", "17.00"]],
            }
        }
    )
