"""Tests that research data cannot reach the cost calibration path.

The mistake being prevented is quiet and expensive: calibrating a cost model on
Dukascopy spreads would produce a backtest whose numbers all look reasonable and whose
costs are somebody else's. These tests exist to prove the separation is enforced rather
than documented.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tradingsys.core.instrument import InstrumentId
from tradingsys.core.provenance import (
    CalibrationTicks,
    DataProvenance,
    ProvenanceError,
    TickSource,
)

EURUSD = InstrumentId("ctrader", "EUR/USD")
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0002_tick_provenance_and_aggregates.py"
)


class TestClassification:
    def test_the_execution_venues_are_the_ones_we_trade_on(self) -> None:
        assert set(TickSource.execution_venues()) == {TickSource.CTRADER, TickSource.BYBIT}

    def test_dukascopy_is_research_only(self) -> None:
        # A different liquidity pool from Pepperstone. Its spreads are not ours.
        assert TickSource.DUKASCOPY.provenance is DataProvenance.RESEARCH_ONLY
        assert not TickSource.DUKASCOPY.is_execution_venue

    def test_every_source_is_classified(self) -> None:
        # The module raises at import if one is missing, so reaching this line at all
        # is most of the assertion.
        for source in TickSource:
            assert source.provenance in DataProvenance

    def test_the_two_categories_partition_the_sources(self) -> None:
        execution = set(TickSource.execution_venues())
        research = set(TickSource.research_sources())
        assert execution | research == set(TickSource)
        assert not execution & research


class TestCalibrationTicksRefusesResearchData:
    def test_an_execution_venue_series_is_accepted(self) -> None:
        series = CalibrationTicks.of(EURUSD, TickSource.CTRADER, ["tick"])
        assert len(series) == 1
        assert series.source is TickSource.CTRADER

    def test_a_research_series_is_refused(self) -> None:
        with pytest.raises(ProvenanceError, match="research_only"):
            CalibrationTicks.of(EURUSD, TickSource.DUKASCOPY, ["tick"])

    def test_the_refusal_explains_why_rather_than_just_refusing(self) -> None:
        with pytest.raises(ProvenanceError) as caught:
            CalibrationTicks.of(EURUSD, TickSource.DUKASCOPY, [])
        message = str(caught.value)
        assert "liquidity pool" in message
        assert "spreads we will pay" in message
        assert "ctrader" in message  # names what may be used instead

    def test_the_direct_constructor_is_guarded_too(self) -> None:
        # Not only the factory: bypassing `of` must not bypass the check.
        with pytest.raises(ProvenanceError):
            CalibrationTicks(instrument_id=EURUSD, source=TickSource.DUKASCOPY, ticks=())

    def test_an_empty_research_series_is_still_refused(self) -> None:
        # Emptiness is not a loophole: the provenance is wrong regardless of the count.
        with pytest.raises(ProvenanceError):
            CalibrationTicks.of(EURUSD, TickSource.DUKASCOPY, [])

    @pytest.mark.parametrize("source", TickSource.research_sources())
    def test_no_research_source_can_be_calibrated_on(self, source: TickSource) -> None:
        with pytest.raises(ProvenanceError):
            CalibrationTicks.of(EURUSD, source, [])

    @pytest.mark.parametrize("source", TickSource.execution_venues())
    def test_every_execution_venue_can_be(self, source: TickSource) -> None:
        assert CalibrationTicks.of(EURUSD, source, []).source is source


class TestTheDatabaseAgreesWithThePythonModel:
    """The SQL view and the enum must not drift apart.

    ``execution_venue_ticks`` hard codes the permitted sources, because a view cannot
    call into Python. If the two lists disagree, either real data is silently excluded
    from calibration or research data is silently admitted. Both are quiet failures, so
    the agreement is asserted rather than trusted.
    """

    def test_the_migration_constant_matches_the_enum(self) -> None:
        text = MIGRATION.read_text()
        declared = re.search(r"EXECUTION_VENUE_SOURCES = \(([^)]*)\)", text)
        assert declared is not None
        listed = set(re.findall(r'"([a-z]+)"', declared.group(1)))
        assert listed == {source.value for source in TickSource.execution_venues()}

    def test_the_view_filters_on_those_same_sources(self) -> None:
        text = MIGRATION.read_text()
        assert "CREATE VIEW execution_venue_ticks" in text
        assert "WHERE t.source IN ({sources})" in text

    def test_no_research_source_appears_in_the_view_definition(self) -> None:
        text = MIGRATION.read_text()
        view = text[text.index("CREATE VIEW execution_venue_ticks") :][:600]
        for source in TickSource.research_sources():
            assert source.value not in view
