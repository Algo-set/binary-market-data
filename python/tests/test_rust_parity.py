"""Replay the same synthetic fixture through the independent Rust implementation."""

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from binary_market_data import BookError, BookStore
from binary_market_data.venue import ParseError, kalshi, polymarket

FIXTURE = Path(__file__).parent / "fixtures" / "replay.json"


def _canonical(value):
    if isinstance(value, dict):
        return {
            key: Decimal(item)
            if key in ("price", "quantity", "midpoint", "spread") and item is not None
            else _canonical(item)
            for key, item in value.items()
            if key not in ("received_ts_ms", "published_ts_ms")
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _python_replay():
    books = BookStore()
    results = []
    for step in json.loads(FIXTURE.read_text()):
        payload = json.dumps(step["payload"])
        try:
            if step["parser"] == "polymarket":
                updates = polymarket.parse_message(payload)
            elif step["parser"] == "kalshi_ws":
                updates = kalshi.parse_websocket_message(payload)
            else:
                updates = [kalshi.parse_rest_snapshot(step["instrument_id"], payload)]
        except ParseError:
            results.append({"error": "parse_error"})
            continue
        outcomes = []
        for update in updates:
            try:
                view = books.apply(update, 100)
                outcome = {"update": update.to_dict(), "view": view.to_dict()}
            except BookError as error:
                outcome = {"update": update.to_dict(), "error": error.code}
            outcomes.append(outcome)
        results.append({"outcomes": outcomes})
    return results


def test_python_fixture_is_self_contained_and_covers_failures():
    results = _python_replay()
    errors = {
        item["error"]
        for result in results
        for item in result.get("outcomes", [])
        if "error" in item
    }
    assert errors == {
        "missing_snapshot",
        "invalid_price",
        "stale_sequence",
        "sequence_gap",
        "negative_result",
    }
    assert {"error": "parse_error"} in results


def test_rust_and_python_replays_agree():
    executable = os.environ.get("BINARY_MARKET_DATA_RUST_REPLAY")
    if not executable:
        pytest.skip("build the Rust replay example and set BINARY_MARKET_DATA_RUST_REPLAY")
    result = subprocess.run(
        [str(Path(executable).resolve())],
        input=FIXTURE.read_text(),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert _canonical(json.loads(result.stdout)) == _canonical(_python_replay())
