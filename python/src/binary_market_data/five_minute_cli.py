"""Bounded, public-only rolling contract probe."""

import argparse
import asyncio
import json
import sys

from . import five_minute, prediction_pool
from .types import Venue


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("venue", choices=[v.value for v in Venue])
    p.add_argument("--asset", default="BTC")
    p.add_argument("--depth", type=int, default=20)
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--poll-ms", type=int, default=5000)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--max-updates", type=int, default=0, help="0 means until timeout")
    p.add_argument("--once", action="store_true", help="one catalog and snapshot sweep")
    p.add_argument("--rpc-url", default=prediction_pool.RPC)
    return p


async def run(args):
    if args.timeout <= 0 or args.max_updates < 0:
        raise ValueError("invalid_limits")
    if args.venue == "pancakeswap":
        stream = prediction_pool.watch(
            args.asset, rpc_url=args.rpc_url, poll_interval=args.poll_ms / 1000, once=args.once
        )
    else:
        stream = five_minute.watch(
            Venue(args.venue),
            args.asset,
            depth=args.depth,
            pages=args.pages,
            poll_interval=args.poll_ms / 1000,
            once=args.once,
        )
    updates = 0
    try:
        async with asyncio.timeout(args.timeout):
            async for item in stream:
                print(json.dumps(item, allow_nan=False), flush=True)
                if item["message_type"] in ("contract_book", "prediction_pool"):
                    updates += 1
                    if args.max_updates and updates >= args.max_updates:
                        break
    finally:
        await stream.aclose()


def main(argv=None):
    try:
        asyncio.run(run(parser().parse_args(argv)))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except TimeoutError:
        print('{"error":"probe_timeout"}', file=sys.stderr)
        return 1
    except (ValueError, KeyError):
        print('{"error":"invalid_configuration"}', file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
