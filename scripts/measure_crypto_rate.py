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


MAX_CREDITED_GAP_SECONDS = 30.0
"""Most wall clock time one message may credit to coverage.

A gap longer than this is an outage rather than a quiet moment, and crediting it as
coverage would report an hour as fully observed when it was not.
"""

WRITE_INTERVAL_SECONDS = 300.0
"""How often the summary is persisted while the run continues."""


async def measure(
    url: str, symbols: list[str], seconds: float, out: Path | None = None
) -> dict[str, Any]:  # pragma: no cover - a measurement tool, exercised by running it
    """Count messages per topic per UTC hour until ``seconds`` of wall clock have passed.

    Three things are the way they are because the first version got them wrong and lost a
    day of measurement to it.

    **The deadline is wall clock, not the event loop clock.** The loop clock is monotonic
    and does not advance while the host is suspended, so a 24 hour capture on a machine
    that sleeps becomes an indefinite one. The first run accumulated 9 hours of loop time
    across 24 hours of wall clock and would have needed days more to finish.

    **Coverage is recorded per hour.** An hour the socket was connected for ten minutes
    is otherwise indistinguishable from a quiet hour, so an outage silently understates
    the rate rather than showing as a gap. Each hour carries the seconds actually spent
    receiving, and rates are computed against that.

    **The summary is written as the run proceeds.** The first version wrote only on
    completion, so stopping the drifted run cost every hour it had collected.
    """
    per_hour: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    coverage: dict[str, float] = defaultdict(float)
    reconnects = 0
    started = datetime.now(tz=UTC)
    deadline_wall = started.timestamp() + seconds
    topics = [f"orderbook.1.{symbol}" for symbol in symbols]
    topics += [f"publicTrade.{symbol}" for symbol in symbols]
    last_write = started.timestamp()

    while datetime.now(tz=UTC).timestamp() < deadline_wall:
        try:
            async with websockets.connect(url, ping_interval=PING_INTERVAL_SECONDS) as socket:
                await socket.send(json.dumps({"op": "subscribe", "args": topics}))
                since = datetime.now(tz=UTC)
                while datetime.now(tz=UTC).timestamp() < deadline_wall:
                    raw = await asyncio.wait_for(socket.recv(), timeout=RECEIVE_TIMEOUT_SECONDS)
                    now = datetime.now(tz=UTC)
                    # Credit elapsed wall time to the hour it was spent in before
                    # counting the message, so coverage and counts cannot disagree.
                    coverage[since.strftime("%Y-%m-%dT%H")] += min(
                        (now - since).total_seconds(), MAX_CREDITED_GAP_SECONDS
                    )
                    since = now

                    message = json.loads(raw)
                    topic = message.get("topic")
                    if isinstance(topic, str):
                        count = len(message["data"]) if topic.startswith("publicTrade") else 1
                        per_hour[now.strftime("%Y-%m-%dT%H")][topic] += count

                    if out is not None and now.timestamp() - last_write > WRITE_INTERVAL_SECONDS:
                        last_write = now.timestamp()
                        write_summary(out, summarise(url, started, per_hour, coverage, reconnects))
        except Exception as error:
            # Deliberately broad: a dropped socket must not end a day long count.
            reconnects += 1
            print(
                f"{datetime.now(tz=UTC).isoformat()} reconnecting after "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
            await asyncio.sleep(2)

    return summarise(url, started, per_hour, coverage, reconnects)


def summarise(
    url: str,
    started: datetime,
    per_hour: dict[str, dict[str, int]],
    coverage: dict[str, float],
    reconnects: int,
) -> dict[str, Any]:
    """Build the summary, with every rate computed against measured coverage.

    Rates are per connected second rather than per elapsed second. An hour with ten
    minutes of coverage reports the rate it actually saw, with its coverage beside it, so
    a reader can tell a quiet hour from an outage instead of averaging the two together.
    """
    hours = {hour: dict(counts) for hour, counts in sorted(per_hour.items())}
    totals: dict[str, int] = defaultdict(int)
    for counts in hours.values():
        for topic, count in counts.items():
            totals[topic] += count
    covered = sum(coverage.values())
    finished = datetime.now(tz=UTC)
    elapsed = (finished - started).total_seconds()
    return {
        "url": url,
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "wall_clock_seconds": elapsed,
        "covered_seconds": round(covered, 1),
        "coverage_fraction": round(covered / elapsed, 4) if elapsed > 0 else 0.0,
        "reconnects": reconnects,
        "per_hour": hours,
        "coverage_seconds_per_hour": {k: round(v, 1) for k, v in sorted(coverage.items())},
        "totals": dict(totals),
        "rates_per_covered_second": (
            {topic: count / covered for topic, count in totals.items()} if covered else {}
        ),
    }


def write_summary(out: Path, summary: dict[str, Any]) -> None:
    """Persist the summary so an interruption costs one interval, not the whole run."""
    out.write_text(json.dumps(summary, indent=2) + "\n")


async def main() -> int:  # pragma: no cover - entry point
    arguments = parse_arguments()
    summary = await measure(arguments.url, arguments.symbols, arguments.hours * 3600, arguments.out)
    write_summary(arguments.out, summary)
    print(json.dumps(summary["rates_per_covered_second"], indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(asyncio.run(main()))
