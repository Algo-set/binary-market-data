import json

import httpx
import pytest

from binary_market_data.discovery import (
    DiscoveryError,
    KalshiDiscoveryConfig,
    PolymarketDiscoveryConfig,
    discover_kalshi,
    discover_polymarket,
    parse_kalshi_page,
    parse_polymarket_market_info,
    parse_polymarket_page,
    resolve_polymarket_condition,
)
from binary_market_data.venue import ParseError


def poly_market(**changes):
    return {
        "conditionId": "example-condition",
        "question": "Example question?",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "slug": "example-question",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["example-yes","example-no"]',
        **changes,
    }


def test_catalog_outcomes_accept_json_strings_and_arrays():
    page = parse_polymarket_page(json.dumps({"markets": [poly_market()], "next_cursor": "next"}))
    assert page.next_cursor == "next"
    assert page.markets[0].book_instrument_ids() == ["example-yes", "example-no"]
    assert page.markets[0].instruments[1].outcome == "No"
    assert page.markets[0].status == "active" and page.markets[0].accepting_orders
    alt = poly_market(outcomes=["Yes", "No"], clobTokenIds=["example-yes", "example-no"])
    assert parse_polymarket_page(json.dumps({"markets": [alt], "next_cursor": "next"})) == page


def test_misaligned_outcomes_fail():
    with pytest.raises(ParseError, match="outcomes"):
        parse_polymarket_page(json.dumps({"markets": [poly_market(outcomes=["Yes"])]}))


@pytest.mark.parametrize(
    "payload",
    [
        {"t": [{"t": "example-yes", "o": "Yes"}, {"t": "example-no", "o": "No"}]},
        {
            "tokens": [
                {"token_id": "example-yes", "outcome": "Yes"},
                {"token_id": "example-no", "outcome": "No"},
            ]
        },
    ],
)
def test_condition_resolution_field_aliases(payload):
    instruments = parse_polymarket_market_info(json.dumps(payload))
    assert [item.instrument_id for item in instruments] == ["example-yes", "example-no"]


def test_kalshi_catalog_maps_ticker_and_status():
    page = parse_kalshi_page(
        json.dumps(
            {
                "markets": [
                    {
                        "ticker": "EXAMPLE",
                        "market_type": "binary",
                        "status": "active",
                    }
                ],
                "cursor": "",
            }
        )
    )
    assert page.next_cursor is None
    assert page.markets[0].book_instrument_ids() == ["EXAMPLE"]
    assert page.markets[0].accepting_orders


async def test_polymarket_page_filter_preserves_cursor_and_encodes_query():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "markets": [
                    poly_market(),
                    poly_market(conditionId="disabled", enableOrderBook=False),
                    poly_market(conditionId="other", question="different", slug="other"),
                ],
                "next_cursor": "next-page",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await discover_polymarket(
            PolymarketDiscoveryConfig(
                limit=5,
                cursor="cursor +/=&",
                query="QUESTION",
            ),
            client=client,
        )
        assert not client.is_closed
    assert len(page.markets) == 1 and page.next_cursor == "next-page"
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/markets/keyset"
    assert dict(requests[0].url.params) == {
        "limit": "5",
        "closed": "false",
        "after_cursor": "cursor +/=&",
    }


async def test_kalshi_all_status_and_condition_identifier_are_safe_path_segments():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"markets": [], "tokens": [{"token_id": "example"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await discover_kalshi(KalshiDiscoveryConfig(status=None, cursor="next"), client=client)
        await resolve_polymarket_condition(
            "https://example.test/base", "condition/?=x", client=client
        )
    assert "status" not in requests[0].url.params
    assert requests[1].url.raw_path == b"/base/clob-markets/condition%2F%3F%3Dx"
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize(
    "base",
    [
        "file:///tmp/example",
        "https://user:password@example.test",
        "https://example.test?token=example",
        "https://example.test/#fragment",
        "not-a-url",
    ],
)
async def test_invalid_endpoint_fails_before_any_request(base):
    def unexpected(_request):
        pytest.fail("invalid endpoints must not reach the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(DiscoveryError, match="invalid_catalog_endpoint"):
            await discover_kalshi(KalshiDiscoveryConfig(rest_base=base), client=client)


@pytest.mark.parametrize(
    "config,discover",
    [
        (PolymarketDiscoveryConfig(limit=0), discover_polymarket),
        (PolymarketDiscoveryConfig(limit=101), discover_polymarket),
        (KalshiDiscoveryConfig(limit=1001), discover_kalshi),
        (KalshiDiscoveryConfig(limit=True), discover_kalshi),
    ],
)
async def test_limit_validation_without_network(config, discover):
    with pytest.raises(DiscoveryError, match="invalid_catalog_limit"):
        await discover(config)


@pytest.mark.parametrize(
    "response", [httpx.Response(429, text="private-fixture"), httpx.Response(200, text="invalid")]
)
async def test_errors_never_echo_responses_or_endpoints(response):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        with pytest.raises(DiscoveryError) as error:
            await discover_kalshi(
                KalshiDiscoveryConfig(rest_base="https://example.test/sensitive-fixture"),
                client=client,
            )
    assert str(error.value) in ("catalog_request_failed", "invalid_catalog_response")


async def test_response_size_cap(monkeypatch):
    monkeypatch.setattr("binary_market_data._http.MAX_RESPONSE_BYTES", 8)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 9))
    ) as client:
        with pytest.raises(DiscoveryError, match="catalog_request_failed"):
            await discover_kalshi(client=client)
