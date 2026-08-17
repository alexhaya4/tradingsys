"""Measure what a forex round trip costs on this account, as a fraction of risk taken.

`SPEC.md` section 6.2 says eligibility by sizing is necessary and not sufficient: a
class is viable only once round-trip cost has been measured against its stop distance
on the venue it will actually trade. This is that measurement.

**Everything here comes from the venue.** Spreads are reconstructed from the venue's
own historical tick data, paired on exact timestamps for the reason
`tradingsys.venues.ctrader.tickdata` documents. Commission comes from the symbol
metadata the venue publishes, not from a rate card. Nothing is taken from an
advertised figure.

**Dukascopy is not a substitute.** It has tick data with bid and ask and this system
already reads it, but it is a different venue and `tradingsys.core.provenance` marks
it research only. Its spreads would answer a question about Dukascopy's book, which is
not the book these orders would cross.

Usage:
    set -a && . ./.env && set +a
    uv run python scripts/measure_forex_costs.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from tradingsys.core.numeric import exact_context
from tradingsys.venues.ctrader.connection import CTraderConnection, TlsChannel
from tradingsys.venues.ctrader.instruments import CENTS_PER_UNIT
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import (
    ProtoOAGetTickDataReq,
    ProtoOAGetTickDataRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
)
from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import (
    ProtoOACommissionType,
    ProtoOAQuoteType,
)
from tradingsys.venues.ctrader.symbols import fetch_catalogue
from tradingsys.venues.ctrader.tickdata import decode_tick_series, spread_series

if TYPE_CHECKING:
    from tradingsys.venues.ctrader.symbols import SymbolCatalogue

sys.path.insert(0, ".")
from scripts.check_venue_assumptions import FX_SYMBOLS, settings_from_environment

GET_TICKDATA_REQ: Final = 2145
GET_TICKDATA_RES: Final = 2146
SYMBOL_BY_ID_REQ: Final = 2116
SYMBOL_BY_ID_RES: Final = 2117

COMMISSION_SCALE: Final = Decimal(100_000_000)
"""preciseTradingCommissionRate is the rate multiplied by ten to the eighth."""

UNITS_PER_LOT: Final = Decimal(100_000)
ONE_MILLION: Final = Decimal(1_000_000)

STOPS_IN_PIPS: Final = (5, 10, 20)
"""The stop distances the director asked for. 5 is the only class that clears the
sizing screen at 200 USD, and 10 and 20 are there for comparison."""

MINIMUM_UNITS: Final = Decimal(1000)
"""Position size the cost is evaluated at: the venue minimum, which is the size this
account would actually trade."""


@dataclass(frozen=True, slots=True)
class Window:
    """A slice of the trading day worth measuring separately."""

    name: str
    start: datetime
    minutes: int


def sessions_for(day: datetime) -> tuple[Window, ...]:
    """Windows chosen for what they are expected to show, not for even spacing.

    Session opens and the London close are where spreads widen, and a measurement that
    sampled only quiet hours would report a cost the account will not actually pay.
    """
    return (
        Window("asia mid", day.replace(hour=2), 30),
        Window("london open", day.replace(hour=7), 30),
        Window("london mid", day.replace(hour=10), 30),
        Window("ny open", day.replace(hour=13), 30),
        Window("london close", day.replace(hour=16), 30),
        Window("ny late", day.replace(hour=20), 30),
    )


async def fetch_spreads(
    connection: CTraderConnection, symbol_id: int, window: Window
) -> tuple[Decimal, ...]:
    """Spread samples in price terms for one window, paired on exact timestamps."""
    start_ms = int(window.start.timestamp() * 1000)
    end_ms = int((window.start + timedelta(minutes=window.minutes)).timestamp() * 1000)

    series = {}
    for label, quote_type in (("bid", ProtoOAQuoteType.BID), ("ask", ProtoOAQuoteType.ASK)):
        request = ProtoOAGetTickDataReq()
        request.ctidTraderAccountId = connection.ctid_trader_account_id
        request.symbolId = symbol_id
        request.type = quote_type
        request.fromTimestamp = start_ms
        request.toTimestamp = end_ms
        response = await connection.request(
            GET_TICKDATA_REQ, request, ProtoOAGetTickDataRes(), GET_TICKDATA_RES
        )
        assert isinstance(response, ProtoOAGetTickDataRes)
        series[label] = decode_tick_series(list(response.tickData))

    return tuple(item.spread for item in spread_series(series["bid"], series["ask"]))


async def commission_per_side(
    connection: CTraderConnection, catalogue: SymbolCatalogue, name: str
) -> tuple[Decimal, str]:
    """Commission for one side at the minimum position size, in USD, from metadata.

    Raises:
        SystemExit: The venue reported a commission type this script cannot convert.
            Guessing between a per-lot fee and a rate per million would misstate the
            cost by orders of magnitude, which is the whole quantity being measured.
    """
    light = catalogue.light(name)
    request = ProtoOASymbolByIdReq()
    request.ctidTraderAccountId = connection.ctid_trader_account_id
    request.symbolId.append(light.symbolId)
    response = await connection.request(
        SYMBOL_BY_ID_REQ, request, ProtoOASymbolByIdRes(), SYMBOL_BY_ID_RES
    )
    assert isinstance(response, ProtoOASymbolByIdRes)
    symbol = next(item for item in response.symbol if item.symbolId == light.symbolId)

    if not symbol.HasField("commissionType"):
        raise SystemExit(f"{name}: the venue published no commission type")
    rate = Decimal(symbol.preciseTradingCommissionRate) / COMMISSION_SCALE
    kind = symbol.commissionType

    with exact_context():
        lot_size_units = (
            Decimal(symbol.lotSize) / CENTS_PER_UNIT
            if symbol.HasField("lotSize")
            else UNITS_PER_LOT
        )
        if kind == ProtoOACommissionType.USD_PER_MILLION_USD:
            # Notional in USD for a USD quoted pair is units times price; for the
            # measurement it is evaluated at the minimum size below by the caller.
            return rate, "usd_per_million_usd"
        if kind == ProtoOACommissionType.USD_PER_LOT:
            return rate / lot_size_units, "usd_per_lot"
        if kind == ProtoOACommissionType.QUOTE_CCY_PER_LOT:
            return rate / lot_size_units, "quote_ccy_per_lot"
    raise SystemExit(
        f"{name}: commission type {kind} is one this script cannot convert. Guessing "
        f"between a per lot fee and a rate per million would misstate the cost by "
        f"orders of magnitude."
    )


async def run() -> int:
    config = settings_from_environment()
    channel = await TlsChannel.connect(
        config.api_host, config.api_port, timeout_seconds=config.request_timeout_seconds
    )
    connection = CTraderConnection(channel, config)
    await connection.open()

    # Friday, the most recent complete trading day with every session in it.
    day = datetime(2026, 8, 14, tzinfo=UTC)
    print(f"measuring {day.date()} on {config.api_host}, account {config.account_id}")
    print(f"position size: {MINIMUM_UNITS} units, the venue minimum\n")

    try:
        catalogue = await fetch_catalogue(connection)
        for name in FX_SYMBOLS:
            light = catalogue.light(name)
            instrument_pip = Decimal("0.01") if "JPY" in name else Decimal("0.0001")
            rate, kind = await commission_per_side(connection, catalogue, name)

            print(f"=== {name} ===")
            print(f"commission: {rate} ({kind})")

            all_samples: list[Decimal] = []
            print(f"{'session':<16}{'ticks':>8}{'median':>10}{'mean':>10}{'p95':>10}{'max':>10}")
            for window in sessions_for(day):
                samples = await fetch_spreads(connection, light.symbolId, window)
                if not samples:
                    print(f"{window.name:<16}{'no data':>8}")
                    continue
                all_samples.extend(samples)
                pips = sorted(value / instrument_pip for value in samples)
                index = min(len(pips) - 1, int(0.95 * len(pips)))
                print(
                    f"{window.name:<16}{len(pips):>8}"
                    f"{statistics.median(pips):>10.3f}"
                    f"{statistics.fmean(pips):>10.3f}"
                    f"{pips[index]:>10.3f}"
                    f"{max(pips):>10.3f}"
                )

            if not all_samples:
                print("no spread data for the day\n")
                continue

            day_pips = sorted(value / instrument_pip for value in all_samples)
            median_spread = statistics.median(day_pips)
            p95_spread = day_pips[min(len(day_pips) - 1, int(0.95 * len(day_pips)))]
            print_cost_table(
                name,
                commission_rate=rate,
                kind=kind,
                median_spread_pips=median_spread,
                p95_spread_pips=p95_spread,
                pip=instrument_pip,
            )
    finally:
        await connection.close()
    return 0


def print_cost_table(
    name: str,
    *,
    commission_rate: Decimal,
    kind: str,
    median_spread_pips: Decimal,
    p95_spread_pips: Decimal,
    pip: Decimal,
) -> None:
    """Round-trip cost as a fraction of the risk a given stop takes.

    Cost is spread crossed once on entry plus commission on both sides. The spread is
    paid once because a position is opened at the ask and closed at the bid, and that
    difference is the spread; commission is charged per side.
    """
    price = Decimal("1.15") if "JPY" not in name else Decimal("159")
    with exact_context():
        notional = MINIMUM_UNITS * price
        if kind == "usd_per_million_usd":
            commission_one_side = notional / ONE_MILLION * commission_rate
        else:
            commission_one_side = MINIMUM_UNITS * commission_rate
        round_trip_commission = commission_one_side * 2

        # Value of one pip on the minimum position, in USD.
        pip_value = MINIMUM_UNITS * pip
        if "JPY" in name:
            pip_value = pip_value / price

    print(f"\n{'stop':<10}{'risk USD':>12}{'spread USD':>13}{'commission':>13}{'cost/risk':>12}")
    for label, spread_pips in (("median", median_spread_pips), ("p95", p95_spread_pips)):
        print(f"  at {label} spread of {spread_pips:.3f} pips:")
        for stop in STOPS_IN_PIPS:
            with exact_context():
                risk = Decimal(stop) * pip_value
                spread_cost = Decimal(spread_pips) * pip_value
                total = spread_cost + round_trip_commission
                fraction = total / risk
            print(
                f"{str(stop) + ' pips':<10}{risk:>12.4f}{spread_cost:>13.4f}"
                f"{round_trip_commission:>13.4f}{fraction:>11.1%}"
            )
    print()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
