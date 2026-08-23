"""The assembly, against a real database, doing what the deploy would otherwise discover.

Written before deploying rather than after, because "written but untested" is the exact
failure the last several days have been correcting, and treating the first run on the
host as the test would be making an exception to that rule on the turn it became
inconvenient.

A test that only asserted construction would leave the interesting half of the risk on
the host, so this drives the whole path: the process constructs from real configuration
against a real database, starts, takes a quote off the socket, writes it to the tick
table, reports ready, and stops cleanly.

The socket is scripted rather than live. What is under test is our wiring, not Bybit's
uptime, and an integration test needing the public internet would make CI depend on a
venue being reachable.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from tests.factories import USDT
from tradingsys.app.assembly import AssembledIngest, assemble_ingest
from tradingsys.config.settings import InstrumentRef, UniverseSettings
from tradingsys.core.clock import SystemClock
from tradingsys.core.currency import default_registry
from tradingsys.core.financing import FinancingSpec
from tradingsys.core.instrument import AssetClass, Instrument, InstrumentId, QuantityUnit
from tradingsys.core.money import Money
from tradingsys.core.provenance import TickSource
from tradingsys.core.schedule import TradingSchedule
from tradingsys.observability.health import HealthRegistry
from tradingsys.observability.metrics import Metrics
from tradingsys.persistence.audit import AuditCategory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from tradingsys.config.settings import Settings
    from tradingsys.persistence.audit import AuditLog
    from tradingsys.persistence.database import Database
    from tradingsys.persistence.repositories import InstrumentRepository, MarketDataRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SYMBOL = "ETH/USDT"
VENUE_SYMBOL = "ETHUSDT"
INSTRUMENT_ID = InstrumentId(venue="bybit", symbol=SYMBOL)
BID = Decimal("1880.26")
ASK = Decimal("1880.27")


def eth_perpetual() -> Instrument:
    """The instrument the registry sync would have stored for this venue."""
    return Instrument(
        id=INSTRUMENT_ID,
        venue_symbol=VENUE_SYMBOL,
        asset_class=AssetClass.CRYPTO_SPOT,
        base_currency=default_registry.get("ETH"),
        quote_currency=USDT,
        settlement_currency=USDT,
        price_increment=Decimal("0.01"),
        price_precision=2,
        pip_size=None,
        quantity_unit=QuantityUnit.UNITS,
        contract_size=Decimal(1),
        quantity_increment=Decimal("0.01"),
        min_quantity=Decimal("0.01"),
        min_notional=Money.of(5, USDT),
        financing=FinancingSpec.none(),
        schedule=TradingSchedule.continuous(),
    )


def snapshot(bid: Decimal = BID, ask: Decimal = ASK, *, update_id: int = 1) -> str:
    """One orderbook.1 snapshot, shaped like the recorded fixture."""
    return json.dumps(
        {
            "topic": f"orderbook.1.{VENUE_SYMBOL}",
            "ts": int(datetime.now(tz=UTC).timestamp() * 1000),
            "type": "snapshot",
            "data": {
                "s": VENUE_SYMBOL,
                "b": [[str(bid), "101.09"]],
                "a": [[str(ask), "24.95"]],
                "u": update_id,
                "seq": update_id,
            },
        }
    )


class ScriptedSocket:
    """Replays frames, then goes quiet, which is a live connection between quotes."""

    def __init__(self, frames: list[str]) -> None:
        self.sent: list[str] = []
        self._frames = list(frames)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if self._frames:
            return self._frames.pop(0)
        # Not an error. A stream with nothing to say is the normal state between quotes,
        # and raising here would exercise reconnection rather than recording.
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None


def venue_responses() -> Callable[[httpx.Request], httpx.Response]:
    """Serve instruments-info and tickers from the recorded fixtures.

    The assembly performs its own registry sync, so this is what populates the
    instruments table. The previous version of this file upserted the definition
    directly, which constructed the precondition production cannot construct for itself
    and is why the suite passed while the process could not start.
    """
    data = Path(__file__).resolve().parents[1] / "venues" / "bybit" / "data"
    definition = json.loads((data / "instruments_linear_ETHUSDT.json").read_text())
    ticker = json.loads((data / "tickers_linear_BTCUSDT.json").read_text())
    for row in ticker["result"]["list"]:
        row["symbol"] = VENUE_SYMBOL

    def handler(request: httpx.Request) -> httpx.Response:
        if "instruments-info" in request.url.path:
            return httpx.Response(200, json=definition)
        return httpx.Response(200, json=ticker)

    return handler


@pytest.fixture
def universe() -> UniverseSettings:
    """One instrument, so assertions name a row rather than count rows."""
    return UniverseSettings(
        instruments=(InstrumentRef(venue="bybit", venue_symbol=VENUE_SYMBOL, symbol=SYMBOL),)
    )


@pytest.fixture
async def assembled(
    *,
    live_settings: Settings,
    universe: UniverseSettings,
    database: Database,
    instruments: InstrumentRepository,
    market_data: MarketDataRepository,
    audit_log: AuditLog,
    ingest_metrics: Metrics,
) -> AsyncIterator[tuple[AssembledIngest, ScriptedSocket, int]]:
    socket = ScriptedSocket([snapshot()])

    async def connect(_url: str) -> ScriptedSocket:
        return socket

    # The shipped configuration disables the venue, because a checkout should not
    # start recording by existing. The deployment enables it; so does this test.
    crypto = live_settings.venues.crypto["bybit"].model_copy(update={"enabled": True})
    venues = live_settings.venues.model_copy(update={"crypto": {"bybit": crypto}})
    settings = live_settings.model_copy(update={"universe": universe, "venues": venues})
    ingest = await assemble_ingest(
        settings,
        SystemClock(),
        database=database,
        instruments=instruments,
        market_data=market_data,
        audit_log=audit_log,
        currencies=default_registry,
        metrics=ingest_metrics,
        connect=connect,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(venue_responses())),
    )
    try:
        yield ingest, socket, await instruments.row_id(INSTRUMENT_ID)
    finally:
        await ingest.process.stop()
        await ingest.aclose()


@pytest.fixture
def ingest_metrics() -> Metrics:
    """The registry the assembled process publishes to.

    A fixture rather than an inline construction so a test can read what the running
    process exported. Counters that only the process can see are the defect this wiring
    exists to end.
    """
    return Metrics.create(service="tradingsys", environment="test", version="0")


class TestItStartsFromAnEmptyDatabase:
    """The deploy failure of 2026-08-19, as a test.

    Assembly resolves instrument row ids, the registry sync is what stores the
    definitions those ids come from, and the sync used to run only as a supervised
    activity started after assembly had already succeeded. On a fresh database assembly
    therefore raised InstrumentNotFoundError, the process exited, and the sync that
    would have fixed it never ran. Restarting met the same empty table, so it
    crash-looped for four hours.

    Every test in this file starts from a truncated instruments table, which is why this
    now holds for all of them. It is stated separately because the ordering is the point
    rather than an incidental precondition.
    """

    async def test_the_instruments_table_is_empty_before_assembly(
        self, instruments: InstrumentRepository
    ) -> None:
        """Pins the precondition. Without this the rest of the file could pass on rows
        left by an earlier run, which is how the original defect survived a green suite."""
        assert await instruments.get(INSTRUMENT_ID) is None

    async def test_assembly_populates_the_definitions_it_then_resolves(
        self,
        assembled: tuple[AssembledIngest, ScriptedSocket, int],
        instruments: InstrumentRepository,
    ) -> None:
        del assembled
        stored = await instruments.get(INSTRUMENT_ID)

        assert stored is not None
        assert stored.venue_symbol == VENUE_SYMBOL
        assert stored.id.symbol == SYMBOL

    async def test_the_venue_symbol_and_canonical_symbol_both_survive(
        self,
        assembled: tuple[AssembledIngest, ScriptedSocket, int],
        instruments: InstrumentRepository,
    ) -> None:
        """The mapping the first diagnosis suspected. It was never the fault, and the
        assertion is kept because the two spellings are easy to conflate: configuration
        and lookups use ETH/USDT, the wire uses ETHUSDT."""
        del assembled
        stored = await instruments.get(InstrumentId(venue="bybit", symbol="ETH/USDT"))

        assert stored is not None
        assert stored.venue_symbol == "ETHUSDT"


class TestItAssembles:
    async def test_the_activities_are_the_ones_this_deployment_runs(
        self, assembled: tuple[AssembledIngest, ScriptedSocket, int]
    ) -> None:
        ingest, _, _ = assembled
        ingest.process.register_activities()

        registered = set(ingest.process.supervisor.states)
        assert "crypto_quotes" in registered
        assert "instrument_registry" in registered
        assert "gap_detection" in registered

    async def test_no_forex_live_stream_is_claimed(
        self, assembled: tuple[AssembledIngest, ScriptedSocket, int]
    ) -> None:
        """Forex waits on reconnection with resynchronisation, and the process must not
        imply it is recording something it is not."""
        ingest, _, _ = assembled
        ingest.process.register_activities()

        assert "forex_quotes" not in ingest.process.supervisor.states


class TestAQuoteReachesTheDatabase:
    async def test_a_real_quote_is_written_to_the_tick_table(
        self,
        assembled: tuple[AssembledIngest, ScriptedSocket, int],
        market_data: MarketDataRepository,
    ) -> None:
        """The half of the risk construction alone does not cover: socket to stored row,
        through the real stream, the real recorder and the real repository."""
        ingest, _, row_id = assembled
        ingest.process.register_activities()
        ingest.process.start()

        # The recorder batches, so a shutdown flush is what lands the single quote. That
        # ordering is the behaviour under test, not an artefact of it.
        await asyncio.sleep(0.2)
        await ingest.process.stop()

        ticks = await market_data.fetch_ticks(row_id, TickSource.BYBIT)
        assert len(ticks) == 1
        assert ticks[0].bid == BID
        assert ticks[0].ask == ASK
        assert ticks[0].instrument_id == INSTRUMENT_ID

    async def test_it_subscribed_to_the_configured_symbol(
        self, assembled: tuple[AssembledIngest, ScriptedSocket, int]
    ) -> None:
        """A stream that connects and subscribes to nothing looks healthy and records
        nothing, which is the failure the universe configuration exists to prevent."""
        ingest, socket, _ = assembled
        ingest.process.register_activities()
        ingest.process.start()
        await asyncio.sleep(0.2)

        assert socket.sent
        assert any(VENUE_SYMBOL in message for message in socket.sent)


class TestTheCountersLeaveTheProcess:
    """`StreamStats` and `RecorderStats` were maintained from the day they were written
    and read by nothing, which made a claim in `docs/DECISIONS.md` false: the region
    decision was recorded against a dataset the 72 hour run was said to produce and in
    fact discarded. This asserts the export from the outside, through the assembly that
    the deployment actually runs, because that is where it was missing."""

    async def test_the_export_activity_is_registered(
        self, assembled: tuple[AssembledIngest, ScriptedSocket, int]
    ) -> None:
        ingest, _, _ = assembled
        ingest.process.register_activities()

        assert "stats_export" in ingest.process.supervisor.states

    async def test_a_quote_that_arrives_is_visible_in_the_metrics(
        self,
        assembled: tuple[AssembledIngest, ScriptedSocket, int],
        ingest_metrics: Metrics,
    ) -> None:
        ingest, _, _ = assembled
        ingest.process.register_activities()
        ingest.process.start()
        try:
            await asyncio.sleep(0.2)
            received = ingest_metrics.registry.get_sample_value(
                "tradingsys_recorder_quotes_total", {"source": "bybit", "outcome": "received"}
            )
            events = ingest_metrics.registry.get_sample_value(
                "tradingsys_venue_stream_events_total",
                {"venue": "bybit", "stream": "orderbook.1"},
            )
            assert received is not None, "the recorder counters never reached the metrics"
            assert received >= 1
            assert events is not None, "the stream counters never reached the metrics"
            assert events >= 1
        finally:
            await ingest.process.stop()


def shipped_venue_responses() -> Callable[[httpx.Request], httpx.Response]:
    """Serve both configured crypto symbols, from both recorded definitions.

    The catalogue is the concatenation of two real recorded responses rather than one
    response with a rewritten symbol, because the assembly under test resolves two
    symbols out of a catalogue and a one entry catalogue would not exercise that.

    The ticker payload is the recorded BTCUSDT one with an ETHUSDT copy appended, its
    symbol rewritten. That much is a construction and is stated as one: the ticker
    supplies only the funding cycle anchor, no assertion below depends on its value, and
    the alternative is a live venue call inside CI.
    """
    data = Path(__file__).resolve().parents[1] / "venues" / "bybit" / "data"
    eth = json.loads((data / "instruments_linear_ETHUSDT.json").read_text())
    btc = json.loads((data / "instruments_linear_BTCUSDT.json").read_text())
    definitions = eth
    definitions["result"]["list"] = [*eth["result"]["list"], *btc["result"]["list"]]

    ticker = json.loads((data / "tickers_linear_BTCUSDT.json").read_text())
    btc_row = ticker["result"]["list"][0]
    eth_row = dict(btc_row)
    eth_row["symbol"] = VENUE_SYMBOL
    ticker["result"]["list"] = [btc_row, eth_row]

    def handler(request: httpx.Request) -> httpx.Response:
        if "instruments-info" in request.url.path:
            return httpx.Response(200, json=definitions)
        return httpx.Response(200, json=ticker)

    return handler


@pytest.fixture
async def shipped(
    *,
    live_settings: Settings,
    database: Database,
    instruments: InstrumentRepository,
    market_data: MarketDataRepository,
    audit_log: AuditLog,
    ingest_metrics: Metrics,
) -> AsyncIterator[AssembledIngest]:
    """The assembly against the universe that ships, with nothing narrowed.

    The only thing changed from `config/base.toml` is enabling the crypto venue, which
    is what `config/production.toml` does on the host. The universe itself is untouched.
    """
    socket = ScriptedSocket([snapshot()])

    async def connect(_url: str) -> ScriptedSocket:
        return socket

    crypto = live_settings.venues.crypto["bybit"].model_copy(update={"enabled": True})
    venues = live_settings.venues.model_copy(update={"crypto": {"bybit": crypto}})
    settings = live_settings.model_copy(update={"venues": venues})
    ingest = await assemble_ingest(
        settings,
        SystemClock(),
        database=database,
        instruments=instruments,
        market_data=market_data,
        audit_log=audit_log,
        currencies=default_registry,
        metrics=ingest_metrics,
        connect=connect,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(shipped_venue_responses())),
    )
    try:
        yield ingest
    finally:
        await ingest.process.stop()
        await ingest.aclose()


class TestTheShippedUniverse:
    """The universe that ships, assembled. Nothing here uses a narrowed fixture.

    **Both deploy failures lived in this gap.** Every other test in this file runs
    against a one instrument universe written so that assertions can name a row instead
    of counting rows, which is a good fixture and is by construction not the thing that
    ships. The startup deadlock and the deferred forex backfill were both invisible to
    it: the first because the fixture upserted a definition production cannot create,
    the second because the fixture carries no instrument with a historical source, so
    the entire forex path never executed.

    The rule this test exists to enforce: where a fixture exists for readability, at
    least one test runs the real configured artifact.
    """

    async def test_the_universe_under_test_is_the_shipped_one(
        self, live_settings: Settings
    ) -> None:
        """Pins what this file is claiming to cover.

        Without it, narrowing `config/base.toml` would quietly narrow the test that
        exists to stop the configuration being narrowed.
        """
        refs = live_settings.universe.instruments
        assert {ref.venue for ref in refs} == {"bybit", "ctrader"}
        assert len(refs) == 6
        assert len(live_settings.universe.with_history()) == 4

    async def test_it_assembles(self, shipped: AssembledIngest) -> None:
        """The regression. This raised InstrumentNotFoundError for EUR/USD on the host,
        because the backfill jobs were built from configuration alone and the cTrader
        registry has never been populated by anything."""
        shipped.process.register_activities()

        assert "crypto_quotes" in shipped.process.supervisor.states

    @pytest.mark.usefixtures("shipped")
    async def test_the_crypto_leg_still_resolves_both_instruments(
        self, instruments: InstrumentRepository
    ) -> None:
        # The fixture is requested for its effect: assembly has run against the shipped
        # universe by the time this body executes, and what is asserted is what it left
        # in the database rather than anything about the object it returns.
        for symbol in ("ETH/USDT", "BTC/USDT"):
            stored = await instruments.get(InstrumentId(venue="bybit", symbol=symbol))
            assert stored is not None, f"{symbol} was not seeded from the venue catalogue"

    async def test_no_backfill_runs_for_a_venue_nothing_defines(
        self, shipped: AssembledIngest
    ) -> None:
        """Four instruments carry a historical source and none of them can be built.

        Dukascopy history is stored against the execution venue's instrument row, so a
        job needs that venue's definition for the row id and the price precision. With
        no cTrader source there is no definition, and building the job anyway is what
        stopped the deploy.
        """
        shipped.process.register_activities()

        assert "dukascopy_backfill" not in shipped.process.supervisor.states

    @pytest.mark.usefixtures("shipped")
    async def test_the_deferral_is_audited_rather_than_silent(self, audit_log: AuditLog) -> None:
        """A configured instrument that is quietly absent is indistinguishable from one
        being recorded, which is why this is an audited event and not a log line only."""
        entries = await audit_log.read(category=AuditCategory.SYSTEM)
        deferrals = [entry for entry in entries if entry.action == "instruments_deferred"]
        assert len(deferrals) == 1

        payload = deferrals[0].payload
        assert payload["venues"] == ["ctrader"]
        assert payload["populated_venues"] == ["bybit"]
        assert sorted(payload["instruments"]) == [
            "ctrader:AUD/USD",
            "ctrader:EUR/USD",
            "ctrader:GBP/USD",
            "ctrader:USD/JPY",
        ]

    async def test_a_quote_still_reaches_the_database(
        self,
        shipped: AssembledIngest,
        instruments: InstrumentRepository,
        market_data: MarketDataRepository,
    ) -> None:
        """The deferral must not cost the leg that works."""
        shipped.process.register_activities()
        shipped.process.start()
        await asyncio.sleep(0.2)
        await shipped.process.stop()

        row_id = await instruments.row_id(INSTRUMENT_ID)
        assert len(await market_data.fetch_ticks(row_id, TickSource.BYBIT)) == 1


class TestReadiness:
    async def test_it_reports_ready_while_quotes_are_arriving(
        self, assembled: tuple[AssembledIngest, ScriptedSocket, int]
    ) -> None:
        ingest, _, _ = assembled
        ingest.process.register_activities()
        ingest.process.start()
        await asyncio.sleep(0.2)

        result = await ingest.process.progress_check().check()

        assert result.passed, result.detail

    async def test_the_check_registers_on_a_health_registry(
        self, assembled: tuple[AssembledIngest, ScriptedSocket, int], live_settings: Settings
    ) -> None:
        """The defect this test exists for: ProgressCheck did not implement HealthCheck
        and nothing noticed, because nothing had ever registered it."""
        ingest, _, _ = assembled
        registry = HealthRegistry(
            timeout_seconds=live_settings.observability.readiness_timeout_seconds
        )
        registry.register(ingest.process.progress_check())

        report = await registry.evaluate()

        assert report.healthy
        assert [check.name for check in report.checks] == ["ingest_progress"]


class TestShutdown:
    async def test_stopping_flushes_and_leaves_nothing_running(
        self,
        assembled: tuple[AssembledIngest, ScriptedSocket, int],
        market_data: MarketDataRepository,
    ) -> None:
        ingest, _, row_id = assembled
        ingest.process.register_activities()
        ingest.process.start()
        await asyncio.sleep(0.2)

        await ingest.process.stop()

        assert all(not state.running for state in ingest.process.supervisor.states.values())
        assert len(await market_data.fetch_ticks(row_id, TickSource.BYBIT)) == 1

    async def test_stopping_twice_is_safe(
        self, assembled: tuple[AssembledIngest, ScriptedSocket, int]
    ) -> None:
        """Shutdown runs from a signal handler and from the context manager, so it has
        to tolerate being asked twice."""
        ingest, _, _ = assembled
        ingest.process.register_activities()
        ingest.process.start()
        await asyncio.sleep(0.1)

        await ingest.process.stop()
        await ingest.process.stop()
