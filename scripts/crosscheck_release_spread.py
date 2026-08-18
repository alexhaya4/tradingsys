"""Does any real feed widen at a scheduled release, where the demo feed does not?

The Pepperstone demo shows a median spread of 0.000 pips on EUR/USD straight through the
2026-08-07 non-farm payrolls release. The measurement method was validated, so either the
market genuinely does not widen at NFP, which would be remarkable, or the demo feed does
not model widening.

This settles it against a different feed. Dukascopy is **research only** in
`tradingsys.core.provenance`, so nothing here is a cost estimate for Pepperstone and no
number it produces may enter an execution cost model. The question it answers is narrower
and does not need that: whether widening at release is a thing real feeds show at all.

**Dukascopy is the better instrument for this particular question**, which is worth
noting. Each of its tick records carries bid and ask together, so there is no two series
alignment problem of the kind that had to be handled on the cTrader side. Whatever it
shows is the spread as that feed published it.

Usage:
    uv run python scripts/crosscheck_release_spread.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx

from tradingsys.config import load_settings
from tradingsys.marketdata.backfill import HttpHourFetcher
from tradingsys.marketdata.dukascopy import decode_hour, hour_url

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.marketdata.dukascopy import DukascopyTick


def default_symbol() -> str:
    """The first forex instrument with a historical feed, from configuration.

    This cross check reads Dukascopy, so the instrument has to be one configuration says
    has a historical source. Taking it from the universe rather than naming it here keeps
    one definition of what this system covers.
    """
    with_history = load_settings().universe.with_history()
    if not with_history:
        raise SystemExit("no instrument in the universe has a historical source configured")
    symbol = with_history[0].historical_symbol
    assert symbol is not None  # guaranteed by InstrumentRef validation
    return symbol


DIGITS: Final = 5
PIP: Final = Decimal("0.0001")

RELEASE: Final = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
"""Non-farm payrolls, first Friday of August 2026, 13:30 UTC."""

POLITE_DELAY_SECONDS: Final = 5.0
"""Spacing between requests. The feed rate limits and this is not a race."""

QUIET_HOUR: Final = datetime(2026, 8, 7, 11, tzinfo=UTC)
"""A baseline hour on the same day, well clear of the release and inside the London
session, so the comparison is not confounded by time of day or liquidity regime."""


async def fetch_with_retry(
    fetcher: HttpHourFetcher, url: str, *, attempts: int = 5
) -> bytes | None:
    """Fetch one hour, backing off on a refusal.

    The fetcher raises on a 429 rather than returning empty bytes, which is correct and
    is why this retries instead of silently recording a hole.
    """
    delay = POLITE_DELAY_SECONDS
    for attempt in range(1, attempts + 1):
        try:
            return await fetcher.fetch(url)
        except Exception as exc:
            if attempt == attempts:
                raise
            print(f"  {type(exc).__name__} on attempt {attempt}, waiting {delay:.0f}s")
            await asyncio.sleep(delay)
            delay *= 2
    return None


def summarise(label: str, ticks: Sequence[DukascopyTick]) -> Decimal | None:
    """Print spread statistics for a set of ticks and return the median in pips."""
    if not ticks:
        print(f"{label:<22}{'no ticks':>9}")
        return None
    pips = sorted((tick.ask - tick.bid) / PIP for tick in ticks)
    index = min(len(pips) - 1, int(0.95 * len(pips)))
    median = statistics.median(pips)
    print(
        f"{label:<22}{len(pips):>9}{median:>10.3f}"
        f"{statistics.fmean(pips):>10.3f}{pips[index]:>10.3f}{max(pips):>10.3f}"
    )
    return median


async def main() -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        fetcher = HttpHourFetcher(client)

        hours = {
            "quiet 11:00": QUIET_HOUR,
            "release 13:00": RELEASE.replace(minute=0),
        }
        decoded: dict[str, tuple[DukascopyTick, ...]] = {}
        for index, (label, hour) in enumerate(hours.items()):
            if index:
                # Dukascopy answers an impatient client with 429, which the fetcher
                # refuses rather than recording as an empty hour. Spacing the requests
                # is cheaper than retrying them.
                await asyncio.sleep(POLITE_DELAY_SECONDS)
            payload = await fetch_with_retry(fetcher, hour_url(default_symbol(), hour))
            if payload is None:
                print(f"{label}: the feed holds nothing for {hour.isoformat()}")
                return 1
            decoded[label] = decode_hour(payload, hour=hour, digits=DIGITS)
            print(f"{label}: {len(decoded[label])} ticks from {hour.isoformat()}")

        print(f"\n{default_symbol()} spread in pips, Dukascopy, research only")
        print(f"{'window':<22}{'ticks':>9}{'median':>10}{'mean':>10}{'p95':>10}{'max':>10}")

        quiet_median = summarise("quiet hour 11:00", decoded["quiet 11:00"])

        release_hour = decoded["release 13:00"]
        # Minute by minute across the release, because the widening is expected to be
        # seconds wide and an hourly summary would average it away entirely.
        for offset in (-5, -1, 0, 1, 2, 5, 15):
            start = RELEASE + timedelta(minutes=offset)
            end = start + timedelta(minutes=1)
            window = [tick for tick in release_hour if start <= tick.ts < end]
            median = summarise(f"13:30 {offset:+d} min", window)
            if median is not None and quiet_median is not None and quiet_median > 0:
                factor = median / quiet_median
                print(f"{'':<22}{'':>9}{'x' + f'{factor:.1f}':>10} against the quiet hour")

        if quiet_median is None:
            return 1

        release_minute = [
            tick for tick in release_hour if RELEASE <= tick.ts < RELEASE + timedelta(minutes=1)
        ]
        if release_minute:
            worst = max((tick.ask - tick.bid) / PIP for tick in release_minute)
            print(
                f"\nwidest spread in the release minute: {worst:.3f} pips, "
                f"against a quiet median of {quiet_median:.3f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
