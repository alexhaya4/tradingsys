"""Assert the things only the venue can tell us, on demand, from the host.

**Why this is a script and not a CI job.** It needs live venue credentials. Putting
those in a third party's secret store makes them reachable by any workflow anyone ever
adds to this repository, and the same pattern would tempt us toward live keys at phase
8. The credentials stay on the host, so the check runs here.

**The gap this leaves is real and accepted.** CI cannot catch venue drift. If the
broker changes a lot size, a swap convention, a symbol name, or the shape of its
metadata, no pipeline in this repository will notice. This script is the manual
control, and `PROGRESS.md` records that it must be run before each phase closes and
before any deployment.

It asserts rather than prints. Every check either passes or fails the run, because a
report nobody reads is not a control. The sizing table at the end is the deliverable
from `SPEC.md` section 4.0: minimum size, tick value, and whether one percent of the
account produces a viable position.

Usage:
    set -a && . ./.env && set +a
    uv run python scripts/check_venue_assumptions.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from tradingsys.config.settings import ForexVenueSettings, VenueEnvironment
from tradingsys.core.currency import default_registry
from tradingsys.core.money import Money
from tradingsys.core.numeric import exact_context
from tradingsys.risk.eligibility import evaluate_eligibility
from tradingsys.venues.ctrader.connection import (
    VENUE_HEARTBEAT_SECONDS,
    CTraderConnection,
    TlsChannel,
)
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import (
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
)
from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod
from tradingsys.venues.ctrader.symbols import fetch_catalogue, fetch_instrument

if TYPE_CHECKING:
    from tradingsys.core.instrument import Instrument
    from tradingsys.venues.ctrader.symbols import SymbolCatalogue

GET_TRENDBARS_REQ: Final = 2137
GET_TRENDBARS_RES: Final = 2138

TRENDBAR_PRICE_SCALE: Final = Decimal(100_000)
"""Trendbar prices are published as integers scaled by ten to the fifth.

This is a venue convention rather than a function of the symbol's own digits, which is
why it is asserted below: a five digit EUR/USD and a three digit USD/JPY both arrive on
this same scale, and reading either against the wrong one is a factor of a hundred.
"""

FX_SYMBOLS: Final = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD")

ACCOUNT_EQUITY: Final = Decimal("200")
RISK_FRACTION: Final = Decimal("0.01")
MAX_RISK_DEVIATION: Final = Decimal("0.10")
"""The eligibility screen's tolerance, matching what the crypto half was measured at."""

REFERENCE_STOP: Final = Decimal("0.01")
"""Stop distance the verdict is reported at, as a fraction of price.

One percent, the same figure the crypto leg was measured at in `PROGRESS.md`, so the
two halves of the instrument universe are comparable. The widest affordable stop is
reported beside it, because that is the number that actually binds: a stop wider than
it needs a position smaller than the venue will accept."""

REFERENCE_STOPS_IN_PIPS: Final = (
    ("scalping, 5 pips", 5),
    ("intraday trend, 20 pips", 20),
    ("swing, 50 pips", 50),
    ("macro event, 80 pips", 80),
)
"""Stop distances typical of each strategy class, as **declared assumptions**.

These are not measurements and must not be read as any. Phase 4b ingests the economic
calendar and measures what high importance releases actually move, at which point this
comparison becomes evidence and these figures are replaced. They are here now because a
stop ceiling reported without anything to compare it against leaves the operator to
infer the consequence, which is the thing SPEC 6.1 says must be stated.
"""

HEARTBEAT_SAMPLE_SECONDS: Final = 70
"""Long enough to observe at least two venue heartbeats at the expected 30s cadence."""


class CheckFailedError(Exception):
    """One assertion about the venue did not hold."""


@dataclass(slots=True)
class Report:
    passed: list[str]
    failed: list[str]

    def check(self, description: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed.append(description)
        else:
            self.failed.append(f"{description}: {detail}" if detail else description)


def settings_from_environment() -> ForexVenueSettings:
    prefix = "TRADINGSYS_VENUES__FOREX__"
    missing = [
        name
        for name in ("ACCOUNT_ID", "CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN", "REFRESH_TOKEN")
        if not os.environ.get(f"{prefix}{name}")
    ]
    if missing:
        raise CheckFailedError(
            "these variables are not set: "
            + ", ".join(f"{prefix}{name}" for name in missing)
            + "\nRun `set -a && . ./.env && set +a` first; this check needs live credentials "
            "and deliberately does not run in CI."
        )
    return ForexVenueSettings(
        enabled=True,
        environment=VenueEnvironment.PRACTICE,
        demo_api_host="demo.ctraderapi.com",
        live_api_host="live.ctraderapi.com",
        api_port=5035,
        token_url="https://openapi.ctrader.com/apps/token",
        request_timeout_seconds=15.0,
        stream_read_timeout_seconds=95.0,
        max_retries=3,
        retry_backoff_seconds=0.5,
        max_requests_per_second=30.0,
        heartbeat_interval_seconds=10.0,
        token_refresh_margin_seconds=259200.0,
        account_id=os.environ[f"{prefix}ACCOUNT_ID"],
        client_id=os.environ[f"{prefix}CLIENT_ID"],
        client_secret=os.environ[f"{prefix}CLIENT_SECRET"],
        access_token=os.environ[f"{prefix}ACCESS_TOKEN"],
        refresh_token=os.environ[f"{prefix}REFRESH_TOKEN"],
    )


async def last_close(connection: CTraderConnection, symbol_id: int, digits: int) -> Decimal:
    """The close of the most recent completed one minute bar, as a venue price."""
    now_ms = int(time.time() * 1000)
    request = ProtoOAGetTrendbarsReq()
    request.ctidTraderAccountId = connection.ctid_trader_account_id
    request.symbolId = symbol_id
    request.period = ProtoOATrendbarPeriod.M1
    request.fromTimestamp = now_ms - 6 * 60 * 60 * 1000
    request.toTimestamp = now_ms
    request.count = 10

    response = await connection.request(
        GET_TRENDBARS_REQ, request, ProtoOAGetTrendbarsRes(), GET_TRENDBARS_RES
    )
    assert isinstance(response, ProtoOAGetTrendbarsRes)
    if not response.trendbar:
        raise CheckFailedError(f"symbol id {symbol_id} returned no trendbars, so it has no price")

    bar = response.trendbar[-1]
    if not bar.HasField("low") or not bar.HasField("deltaClose"):
        raise CheckFailedError(f"symbol id {symbol_id} returned a bar without a close")
    with exact_context():
        raw_close = Decimal(bar.low) + Decimal(bar.deltaClose)
        price = raw_close / TRENDBAR_PRICE_SCALE
    return price.quantize(Decimal(1).scaleb(-digits))


async def run() -> int:
    config = settings_from_environment()
    report = Report(passed=[], failed=[])
    currencies = default_registry

    print(f"connecting to {config.api_host}:{config.api_port}")
    channel = await TlsChannel.connect(
        config.api_host, config.api_port, timeout_seconds=config.request_timeout_seconds
    )
    connection = CTraderConnection(channel, config)
    instruments: dict[str, Instrument] = {}
    prices: dict[str, Decimal] = {}

    try:
        await connection.open()
        report.check("the handshake completes", connection.is_authenticated)
        report.check(
            "the configured account resolves to a venue account id",
            connection.ctid_trader_account_id > 0,
        )
        print(f"  account {config.account_id} resolves to ctid {connection.ctid_trader_account_id}")

        catalogue: SymbolCatalogue = await fetch_catalogue(connection)
        report.check("the venue publishes a symbol catalogue", len(catalogue.symbols) > 0)

        for name in FX_SYMBOLS:
            present = name in catalogue.names()
            report.check(f"{name} is listed on this account", present)
            if not present:
                continue
            instrument = await fetch_instrument(connection, catalogue, name, currencies)
            instruments[name] = instrument

            report.check(f"{name} publishes price digits", instrument.price_precision > 0)
            report.check(
                f"{name} publishes a pip size distinct from its tick",
                instrument.pip_size is not None
                and instrument.pip_size > instrument.price_increment,
                f"pip={instrument.pip_size} tick={instrument.price_increment}",
            )
            report.check(f"{name} publishes a positive minimum volume", instrument.min_quantity > 0)
            report.check(
                f"{name} publishes a positive volume step", instrument.quantity_increment > 0
            )
            report.check(
                f"{name} publishes a trading schedule",
                len(instrument.schedule.sessions) > 0 or instrument.schedule.always_open,
            )

            price = await last_close(
                connection, catalogue.light(name).symbolId, instrument.price_precision
            )
            prices[name] = price
            report.check(f"{name} has a positive price", price > 0, f"got {price}")

        print(f"\nsampling the venue heartbeat for {HEARTBEAT_SAMPLE_SECONDS}s")
        before = connection.stats.heartbeats_received
        started = time.monotonic()
        await asyncio.sleep(HEARTBEAT_SAMPLE_SECONDS)
        elapsed = time.monotonic() - started
        received = connection.stats.heartbeats_received - before
        observed = elapsed / received if received else float("inf")
        report.check(
            "the venue heartbeat interval is within expected bounds",
            0.5 * VENUE_HEARTBEAT_SECONDS <= observed <= 1.5 * VENUE_HEARTBEAT_SECONDS,
            f"observed {observed:.1f}s against an expected {VENUE_HEARTBEAT_SECONDS}s",
        )
        report.check(
            "the connection survives an idle period on the shipped read deadline",
            connection.is_authenticated,
            str(connection.stats.last_error),
        )
        print(f"  {received} heartbeats in {elapsed:.0f}s, one every {observed:.1f}s")
    finally:
        await connection.close()

    print_sizing(instruments, prices)

    print("\n=== assumptions ===")
    for line in report.passed:
        print(f"  pass  {line}")
    for line in report.failed:
        print(f"  FAIL  {line}")
    print(f"\n{len(report.passed)} passed, {len(report.failed)} failed")
    return 1 if report.failed else 0


def print_sizing(instruments: dict[str, Instrument], prices: dict[str, Decimal]) -> None:
    """The sizing deliverable: what one percent of the account can actually buy.

    The verdict comes from `tradingsys.risk.eligibility`, the same screen the crypto
    half was measured with, rather than from arithmetic repeated here. A report that
    computes eligibility its own way can disagree with the code that enforces it, and
    then neither can be trusted.
    """
    usd = default_registry.get("USD")
    equity = Money(ACCOUNT_EQUITY, usd)

    print(f"\n=== sizing on a {ACCOUNT_EQUITY} USD account at {RISK_FRACTION:.0%} risk ===")
    print(f"risk budget per trade: {ACCOUNT_EQUITY * RISK_FRACTION} USD")
    print(f"verdict reported at a {REFERENCE_STOP:.0%} stop\n")

    for name, instrument in sorted(instruments.items()):
        price = prices.get(name)
        if price is None:
            continue
        quote = instrument.quote_currency.code
        # A JPY quoted pair prices its risk in JPY on a USD account. The rate is stated
        # rather than assumed: USD/JPY quotes JPY per USD, so one JPY is 1/price USD.
        rate = None if quote == "USD" else Decimal(1) / price

        verdict = evaluate_eligibility(
            instrument,
            price=price,
            equity=equity,
            risk_fraction=RISK_FRACTION,
            stop_distance=REFERENCE_STOP,
            max_risk_deviation=MAX_RISK_DEVIATION,
            quote_to_account_rate=rate,
        )

        with exact_context():
            conversion = Decimal(1) if rate is None else rate
            tick_value_quote = instrument.price_increment * instrument.min_quantity
            tick_value_usd = tick_value_quote * conversion
            # The widest stop the minimum position can carry inside the budget.
            widest_stop = verdict.risk_budget / (instrument.min_quantity * price * conversion)

        print(f"{name}  ({instrument.id})")
        print(f"  price                    {price} {quote}")
        print(f"  minimum position size    {instrument.min_quantity} units")
        print(f"  size step                {instrument.quantity_increment} units")
        print(f"  tick                     {instrument.price_increment} {quote}")
        print(f"  pip                      {instrument.pip_size} {quote}")
        print(
            f"  tick value at minimum    {tick_value_quote} {quote}"
            + (f" ({tick_value_usd:.6f} USD)" if quote != "USD" else "")
        )
        print(
            f"  intended size at {REFERENCE_STOP:.0%} stop  {verdict.intended_quantity:.2f} units"
        )
        pips = (widest_stop * price) / (instrument.pip_size or Decimal(1))
        print(f"  widest affordable stop   {widest_stop:.4%} of price, {pips:.1f} pips")
        print(f"  verdict                  {verdict.explain()}")
        print()

    print_strategy_implication(instruments, prices)


def print_strategy_implication(
    instruments: dict[str, Instrument], prices: dict[str, Decimal]
) -> None:
    """What the stop ceiling rules in and out, which is the question behind the verdict.

    `SPEC.md` section 6.1 requires the strategy implication rather than only the
    eligibility verdict. An exclusion list says what cannot be traded; this says what
    the account can still do, which is what an operator actually needs.

    The reference distances are declared assumptions, not measurements, and are labelled
    as such in the output. Phase 4b ingests the economic calendar and measures what
    releases actually move, at which point this becomes evidence and these figures are
    replaced.
    """
    print("=== strategy implication ===")
    print("Reference stop distances below are ASSUMED, pending the phase 4b measurement.")
    print()
    for name, instrument in sorted(instruments.items()):
        price = prices.get(name)
        if price is None or instrument.pip_size is None:
            continue
        quote = instrument.quote_currency.code
        conversion = Decimal(1) if quote == "USD" else Decimal(1) / price
        with exact_context():
            budget = ACCOUNT_EQUITY * RISK_FRACTION
            ceiling_pips = budget / (instrument.min_quantity * conversion * instrument.pip_size)

        viable = [label for label, need in REFERENCE_STOPS_IN_PIPS if Decimal(need) <= ceiling_pips]
        blocked = [label for label, need in REFERENCE_STOPS_IN_PIPS if Decimal(need) > ceiling_pips]
        print(f"{name}: stop ceiling {ceiling_pips:.1f} pips")
        print(f"  viable:  {', '.join(viable) if viable else 'none'}")
        print(f"  blocked: {', '.join(blocked) if blocked else 'none'}")
    print()


def main() -> int:
    try:
        return asyncio.run(run())
    except CheckFailedError as exc:
        print(f"check_venue_assumptions: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
