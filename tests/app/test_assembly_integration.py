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
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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


@pytest.fixture
async def stored(instruments: InstrumentRepository) -> int:
    return await instruments.upsert(eth_perpetual())


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
    stored: int,
) -> AsyncIterator[tuple[AssembledIngest, ScriptedSocket, int]]:
    del stored
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
        connect=connect,
    )
    try:
        yield ingest, socket, await instruments.row_id(INSTRUMENT_ID)
    finally:
        await ingest.process.stop()
        await ingest.aclose()


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
