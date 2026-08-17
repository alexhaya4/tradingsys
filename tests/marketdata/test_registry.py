"""The registry sync: what it stores, and what it notices.

Storing definitions is not the interesting part and the repository already covers it.
These are about the comparison, because that is the only automated place venue drift
can be caught: CI holds no venue credentials and cannot see a broker change a minimum
lot, so a sync that runs on real metadata and reports what moved is the half of that
control which is not manual.

The doubles are hand written, because the cases that matter are a venue that changed
something and a venue that stopped listing something, and neither can be asked of a
real one on demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from tests.factories import btcusdt, eurusd, usdjpy
from tradingsys.core.instrument import InstrumentStatus
from tradingsys.marketdata.registry import RegistrySync

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.core.instrument import Instrument, InstrumentId

pytestmark = pytest.mark.asyncio


@dataclass(slots=True)
class InMemoryInstruments:
    """The repository's read and write surface, without a database."""

    stored: dict[InstrumentId, Instrument] = field(default_factory=dict)
    upsert_calls: int = 0

    async def list_for_venue(self, venue: str) -> Sequence[Instrument]:
        return [item for item in self.stored.values() if item.id.venue == venue]

    async def upsert_many(self, instruments: Sequence[Instrument]) -> int:
        self.upsert_calls += 1
        for instrument in instruments:
            self.stored[instrument.id] = instrument
        return len(instruments)


@dataclass(slots=True)
class ScriptedSource:
    venue: str
    published: Sequence[Instrument] = ()
    raises: Exception | None = None

    async def instruments(self) -> Sequence[Instrument]:
        if self.raises is not None:
            raise self.raises
        return self.published


def sync_for(repository: InMemoryInstruments) -> RegistrySync:
    return RegistrySync(repository)  # type: ignore[arg-type]


class TestFirstSync:
    async def test_every_instrument_is_new_and_stored(self) -> None:
        repository = InMemoryInstruments()
        instruments = [eurusd(), usdjpy()]
        source = ScriptedSource(venue="fxbroker", published=instruments)

        report = await sync_for(repository).sync(source)

        assert set(report.added) == {item.id for item in instruments}
        assert report.changed == []
        assert report.unchanged == []
        assert set(repository.stored) == {item.id for item in instruments}
        assert report.moved

    async def test_a_second_identical_sync_reports_nothing_moved(self) -> None:
        # The expected steady state. A sync that reports movement every time it runs
        # trains an operator to ignore it.
        repository = InMemoryInstruments()
        source = ScriptedSource(venue="fxbroker", published=[eurusd()])
        sync = sync_for(repository)

        await sync.sync(source)
        second = await sync.sync(source)

        assert not second.moved
        assert second.unchanged == [eurusd().id]


class TestDriftIsNoticed:
    async def test_a_changed_minimum_lot_is_reported_by_field_name(self) -> None:
        # The change that matters most at this capital: a broker altering a minimum
        # changes which instruments are tradeable, and the sizing arithmetic is derived
        # from it rather than from anything stored here.
        repository = InMemoryInstruments()
        original = eurusd()
        source = ScriptedSource(venue="fxbroker", published=[original])
        sync = sync_for(repository)
        await sync.sync(source)

        source.published = [replace(original, min_quantity=Decimal(1000))]
        report = await sync.sync(source)

        assert len(report.changed) == 1
        change = report.changed[0]
        assert change.field_name == "min_quantity"
        assert change.before == "1"
        assert change.after == "1000"
        assert str(change).endswith("min_quantity: 1 -> 1000")

    async def test_several_fields_moving_are_reported_separately(self) -> None:
        # One entry per field rather than per instrument, so an alert names the thing
        # that changed rather than saying an instrument is different.
        repository = InMemoryInstruments()
        original = eurusd()
        source = ScriptedSource(venue="fxbroker", published=[original])
        sync = sync_for(repository)
        await sync.sync(source)

        source.published = [
            replace(
                original,
                min_quantity=Decimal(500),
                status=InstrumentStatus.REDUCE_ONLY,
            )
        ]
        report = await sync.sync(source)

        moved = {change.field_name for change in report.changed}
        assert moved == {"min_quantity", "status"}

    async def test_a_changed_definition_is_stored_rather_than_only_reported(self) -> None:
        repository = InMemoryInstruments()
        original = eurusd()
        source = ScriptedSource(venue="fxbroker", published=[original])
        sync = sync_for(repository)
        await sync.sync(source)

        source.published = [replace(original, min_quantity=Decimal(1000))]
        await sync.sync(source)

        assert repository.stored[original.id].min_quantity == Decimal(1000)

    async def test_trailing_zeros_are_not_a_change(self) -> None:
        # A venue that re-renders its own metadata must not raise an alert. An alert
        # that fires without a cause stops being read, which is worse than no alert.
        repository = InMemoryInstruments()
        original = replace(eurusd(), min_quantity=Decimal("1.0"))
        source = ScriptedSource(venue="fxbroker", published=[original])
        sync = sync_for(repository)
        await sync.sync(source)

        source.published = [replace(original, min_quantity=Decimal("1.000"))]
        report = await sync.sync(source)

        assert report.changed == []
        assert not report.moved


class TestAbsence:
    async def test_an_instrument_the_venue_stopped_listing_is_reported(self) -> None:
        repository = InMemoryInstruments()
        source = ScriptedSource(venue="fxbroker", published=[eurusd(), usdjpy()])
        sync = sync_for(repository)
        await sync.sync(source)

        source.published = [eurusd()]
        report = await sync.sync(source)

        assert report.absent == [usdjpy().id]
        assert report.moved

    async def test_an_absent_instrument_is_never_deleted(self) -> None:
        # Bars and ticks reference it. Removing the definition from under recorded data
        # leaves that data unreadable, so retiring one is a decision rather than a side
        # effect of a refresh.
        repository = InMemoryInstruments()
        source = ScriptedSource(venue="fxbroker", published=[eurusd(), usdjpy()])
        sync = sync_for(repository)
        await sync.sync(source)

        source.published = [eurusd()]
        await sync.sync(source)

        assert usdjpy().id in repository.stored

    async def test_another_venues_instruments_are_not_reported_absent(self) -> None:
        # A cTrader refresh must not conclude that every Bybit instrument has vanished.
        repository = InMemoryInstruments()
        repository.stored[btcusdt().id] = btcusdt()
        source = ScriptedSource(venue="fxbroker", published=[eurusd()])

        report = await sync_for(repository).sync(source)

        assert report.absent == []


class TestFailuresDoNotHalfApply:
    async def test_a_source_that_raises_stores_nothing(self) -> None:
        # A source must raise rather than return what it managed to fetch, and when it
        # does, the refresh must not land: a registry refreshed halfway is a state
        # nothing downstream can reason about.
        repository = InMemoryInstruments()
        source = ScriptedSource(venue="fxbroker", raises=ConnectionResetError("venue gone"))

        with pytest.raises(ConnectionResetError):
            await sync_for(repository).sync(source)

        assert repository.stored == {}
        assert repository.upsert_calls == 0

    async def test_the_comparison_happens_before_the_write(self) -> None:
        # If the write came first there would be nothing left to compare against, and
        # every sync would report no change. This is the reason the component exists.
        repository = InMemoryInstruments()
        original = eurusd()
        source = ScriptedSource(venue="fxbroker", published=[original])
        sync = sync_for(repository)
        await sync.sync(source)

        source.published = [replace(original, min_quantity=Decimal(1000))]
        report = await sync.sync(source)

        assert report.changed, "the change was invisible, so the write preceded the read"
