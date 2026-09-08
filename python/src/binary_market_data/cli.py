"""The same discovery and stream commands as the Rust market-data-probe binary."""

import argparse
import asyncio
import json
import sys

from .connectors import kalshi_rest, polymarket_ws
from .discovery import (
    KALSHI_REST,
    KALSHI_STATUSES,
    POLYMARKET_CATALOG,
    POLYMARKET_CLOB,
    DiscoveryError,
    KalshiDiscoveryConfig,
    PolymarketDiscoveryConfig,
    discover_kalshi,
    discover_polymarket,
    resolve_polymarket_condition,
)
from .types import BookMessage


def _positive(value: str) -> int:
    try:
        result = int(value)
        if result > 0:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Read public market data and print normalized JSON.")
    commands = root.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover")
    venues = discovery.add_subparsers(dest="venue", required=True)
    poly_discovery = venues.add_parser("polymarket")
    poly_discovery.add_argument("--catalog-base", default=POLYMARKET_CATALOG)
    kalshi_discovery = venues.add_parser("kalshi")
    kalshi_discovery.add_argument("--rest-base", default=KALSHI_REST)
    kalshi_discovery.add_argument("--status", choices=(*KALSHI_STATUSES, "all"), default="open")
    for venue in (poly_discovery, kalshi_discovery):
        venue.add_argument("--limit", type=_positive, default=20)
        venue.add_argument("--cursor")
        venue.add_argument("--query")
    poly = commands.add_parser("polymarket")
    poly.add_argument("asset_ids", nargs="*")
    poly.add_argument("--condition-id")
    poly.add_argument("--clob-base", default=POLYMARKET_CLOB)
    poly.add_argument("--endpoint", default=polymarket_ws.POLYMARKET_WS)
    kalshi = commands.add_parser("kalshi")
    kalshi.add_argument("ticker")
    kalshi.add_argument("--rest-base", default=KALSHI_REST)
    kalshi.add_argument("--poll-ms", type=_positive, default=1000)
    for venue in (poly, kalshi):
        venue.add_argument("--depth", type=_positive, default=20)
        venue.add_argument("--max-updates", type=_positive)
        venue.add_argument(
            "--timeout", type=_positive, help="stop the probe after this many seconds"
        )
    return root


async def run(args: argparse.Namespace) -> None:
    async with asyncio.timeout(getattr(args, "timeout", None)):
        await _run(args)


async def _run(args: argparse.Namespace) -> None:
    if args.command == "discover":
        if args.venue == "polymarket":
            page = await discover_polymarket(
                PolymarketDiscoveryConfig(
                    limit=args.limit,
                    catalog_base=args.catalog_base,
                    cursor=args.cursor,
                    query=args.query,
                )
            )
        else:
            page = await discover_kalshi(
                KalshiDiscoveryConfig(
                    limit=args.limit,
                    rest_base=args.rest_base,
                    cursor=args.cursor,
                    query=args.query,
                    status=None if args.status == "all" else args.status,
                )
            )
        print(page.to_json(indent=2))
        return
    if not 1 <= args.depth <= 100:
        raise ValueError("depth_must_be_between_1_and_100")
    if args.command == "polymarket":
        if bool(args.condition_id) == bool(args.asset_ids):
            raise ValueError("provide_either_condition_id_or_asset_ids")
        assets = args.asset_ids
        if args.condition_id:
            assets = [
                token.instrument_id
                for token in await resolve_polymarket_condition(
                    args.clob_base,
                    args.condition_id,
                )
            ]
        manager = polymarket_ws.stream(
            polymarket_ws.Config(
                asset_ids=tuple(assets),
                endpoint=args.endpoint,
                output_depth=args.depth,
            )
        )
    else:
        manager = kalshi_rest.stream(
            kalshi_rest.Config(
                market_ticker=args.ticker,
                rest_base=args.rest_base,
                depth=args.depth,
                poll_interval=max(100, args.poll_ms) / 1000,
            )
        )
    async with manager as messages:
        updates = 0
        async for message in messages:
            print(message.to_json(), flush=True)
            if isinstance(message, BookMessage):
                updates += 1
                if args.max_updates is not None and updates >= args.max_updates:
                    break


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except TimeoutError:
        print(json.dumps({"error": "probe_timeout"}), file=sys.stderr)
        return 1
    except DiscoveryError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 1
    except ValueError:
        # Do not print endpoint-containing exception text from dependencies.
        print(json.dumps({"error": "invalid_configuration"}), file=sys.stderr)
        return 2
    return 0
