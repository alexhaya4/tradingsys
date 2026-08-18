"""The instrument universe as configuration, and the one field that decides gap policy.

`historical_source` is not descriptive metadata. Its presence is what makes a gap
repairable: an instrument with a historical feed has its gaps queued for backfill, and
one without has them recorded as permanent, because Bybit publishes no historical quote
data at all and crypto spread history begins when the recorder starts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingsys.config import load_settings
from tradingsys.config.settings import InstrumentRef, UniverseSettings

REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def ref(**overrides: object) -> InstrumentRef:
    defaults: dict[str, object] = {
        "venue": "bybit",
        "venue_symbol": "ETHUSDT",
        "symbol": "ETH/USDT",
    }
    defaults.update(overrides)
    return InstrumentRef(**defaults)


class TestHistoricalFeed:
    def test_absent_history_is_legal_and_means_permanent_gaps(self) -> None:
        """Bybit publishes no historical quote data, so this is the crypto case."""
        instrument = ref()

        assert instrument.historical_source is None
        assert UniverseSettings(instruments=(instrument,)).with_history() == ()

    def test_a_named_feed_makes_the_instrument_repairable(self) -> None:
        instrument = ref(
            venue="ctrader",
            venue_symbol="EURUSD",
            symbol="EUR/USD",
            historical_source="dukascopy",
            historical_symbol="EURUSD",
        )

        assert UniverseSettings(instruments=(instrument,)).with_history() == (instrument,)

    @pytest.mark.parametrize(
        ("source", "symbol"),
        [("dukascopy", None), (None, "EURUSD")],
    )
    def test_half_a_feed_is_refused(self, source: str | None, symbol: str | None) -> None:
        """A source without a symbol cannot be fetched and a symbol without a source has
        nothing to fetch it from. Either leaves a gap looking repairable when nothing can
        repair it, which is the failure this section exists to prevent."""
        with pytest.raises(ValidationError, match="together or not at all"):
            ref(historical_source=source, historical_symbol=symbol)


class TestTheUniverseIsSingleValued:
    def test_a_repeated_canonical_symbol_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="lists a symbol twice"):
            UniverseSettings(instruments=(ref(), ref(venue_symbol="ETHUSD")))

    def test_a_repeated_venue_symbol_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="venue symbol twice"):
            UniverseSettings(instruments=(ref(), ref(symbol="ETH/USDT2")))

    def test_an_empty_universe_is_refused(self) -> None:
        """A process configured to record nothing would start, report healthy, and
        record nothing."""
        with pytest.raises(ValidationError, match="universe is empty"):
            UniverseSettings(instruments=())


class TestLookups:
    def test_symbols_come_back_in_the_venue_s_own_spelling(self) -> None:
        universe = UniverseSettings(
            instruments=(
                ref(),
                ref(venue="ctrader", venue_symbol="EURUSD", symbol="EUR/USD"),
            )
        )

        assert universe.venue_symbols("bybit") == ("ETHUSDT",)
        assert universe.venue_symbols("ctrader") == ("EURUSD",)

    def test_an_unknown_venue_returns_nothing_rather_than_raising(self) -> None:
        """A venue with no configured instruments is a venue this deployment does not
        use, which is a legitimate state and not an error."""
        assert UniverseSettings(instruments=(ref(),)).venue_symbols("kraken") == ()


class TestTheShippedConfiguration:
    """Against config/ in the repository, not a fixture, with a supplied environment so
    the test needs no secrets on the host."""

    @staticmethod
    def shipped() -> UniverseSettings:
        return load_settings(
            config_dir=REPO_CONFIG_DIR,
            environ={"TRADINGSYS_DATABASE__PASSWORD": "not-a-real-password"},
        ).universe

    def test_the_universe_matches_what_spec_section_4_names(self) -> None:
        """Pinned so that a symbol cannot be dropped from the recorder without the
        change being deliberate."""
        universe = self.shipped()

        assert {ref.symbol for ref in universe.instruments} == {
            "ETH/USDT",
            "BTC/USDT",
            "EUR/USD",
            "GBP/USD",
            "USD/JPY",
            "AUD/USD",
        }

    def test_crypto_has_no_historical_feed_and_forex_does(self) -> None:
        """The asymmetry is the whole reason the field exists."""
        universe = self.shipped()

        assert universe.venue_symbols("bybit") == ("ETHUSDT", "BTCUSDT")
        assert {r.venue for r in universe.with_history()} == {"ctrader"}
