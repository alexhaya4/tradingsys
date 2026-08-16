#!/usr/bin/env python
"""Measure the crypto quote and trade rate, for retention sizing.

Crypto storage cannot be sized from a Sunday. The rate this counts is the number of
top of book updates per second per instrument, which is what the tick table will
receive, hour by hour so that the daily shape is visible rather than only a mean. A
single busy hour extrapolated to a day overstates storage; a single quiet one
understates it, and both look equally like a measurement.

Trade counts are captured alongside as a cross check. If one instrument's quote rate
is far above another's, the trade rates should be in a similar relation, and if they
are not then the difference is in how the book is quoted rather than in how much the
instrument trades, which is a different fact with different implications.

Nothing is written to the database. This is a measurement, and it produces a JSON
summary a human reads.

Usage::

    scripts/measure_crypto_rate.py --hours 24 --out /var/tmp/crypto-rate.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import websockets

DEFAULT_URL = "wss://stream.bybit.com/v5/public/linear"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")
RECEIVE_TIMEOUT_SECONDS = 30.0
PING_INTERVAL_SECONDS = 20.0


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0, help="how long to count for")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--out", type=Path, required=True, help="where to write the summary")
    return parser.parse_args(argv)


async def measure(
    url: str, symbols: list[str], seconds: float
) -> dict[str, Any]:  # pragma: no cover - a measurement tool, exercised by running it
    """Count messages per topic per UTC hour until ``seconds`` have passed."""
    per_hour: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    reconnects = 0
    started = datetime.now(tz=UTC)
    deadline = asyncio.get_running_loop().time() + seconds
    topics = [f"orderbook.1.{symbol}" for symbol in symbols]
    topics += [f"publicTrade.{symbol}" for symbol in symbols]

    while asyncio.get_running_loop().time() < deadline:
        try:
            async with websockets.connect(url, ping_interval=PING_INTERVAL_SECONDS) as socket:
                await socket.send(json.dumps({"op": "subscribe", "args": topics}))
                while asyncio.get_running_loop().time() < deadline:
                    raw = await asyncio.wait_for(socket.recv(), timeout=RECEIVE_TIMEOUT_SECONDS)
                    message = json.loads(raw)
                    topic = message.get("topic")
                    if not isinstance(topic, str):
                        continue
                    hour = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H")
                    count = len(message["data"]) if topic.startswith("publicTrade") else 1
                    per_hour[hour][topic] += count
        except Exception as error:
            # Deliberately broad: a dropped socket must not end a day long count.
            reconnects += 1
            print(f"reconnecting after {type(error).__name__}: {error}", file=sys.stderr)
            await asyncio.sleep(2)

    finished = datetime.now(tz=UTC)
    hours = {hour: dict(counts) for hour, counts in sorted(per_hour.items())}
    totals: dict[str, int] = defaultdict(int)
    for counts in hours.values():
        for topic, count in counts.items():
            totals[topic] += count
    elapsed = (finished - started).total_seconds()
    return {
        "url": url,
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "elapsed_seconds": elapsed,
        "reconnects": reconnects,
        "per_hour": hours,
        "totals": dict(totals),
        "rates_per_second": {topic: count / elapsed for topic, count in totals.items()},
    }


async def main() -> int:  # pragma: no cover - entry point
    arguments = parse_arguments()
    summary = await measure(arguments.url, arguments.symbols, arguments.hours * 3600)
    arguments.out.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["rates_per_second"], indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(asyncio.run(main()))
