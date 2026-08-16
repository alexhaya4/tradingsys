"""Tests for the instrument eligibility screen.

The two crypto cases run against the instrument metadata recorded from Bybit on
2026-08-16, at the prices of that afternoon, because the decision the director took
was taken on those numbers and this is where it is checked rather than asserted.

The rest are constructed instruments, since the point of a configurable threshold is
that it produces different answers for different steps, minimums, and account sizes,
and four real instruments cannot cover that space.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tradingsys.core.currency import default_registry
from tradingsys.core.errors import DomainError
from tradingsys.core.financing import FinancingSpec
from tradingsys.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    QuantityUnit,
)
from tradingsys.core.money import Money
from tradingsys.core.schedule import TradingSchedule
from tradingsys.risk.eligibility import Eligibility, ExclusionReason, evaluate_eligibility
from tradingsys.venues.bybit.instruments import instrument_from_linear

BYBIT_DATA = Path(__file__).parents[1] / "venues" / "bybit" / "data"
USD = default_registry.get("USD")
JPY = default_registry.get("JPY")
EUR = default_registry.get("EUR")
EQUITY = Money(Decimal(200), USD)
ONE_PERCENT = Decimal("0.01")
# The director's threshold: realised risk within a tenth of intended. Ten distinct
# sizes inside the budget, which ETH clears and BTC does not.
MAX_DEVIATION = Decimal("0.10")
# USDT is not USD. The screen refuses to assume otherwise, so every Bybit case states
# the rate it is using, and that rate is a market observation like any other price.
# One to one is what it traded at on the day; the point is that it is written down.
USDT_PER_USD = Decimal("1")


def bybit_perpetual(symbol: str) -> Instrument:
    payload: dict[str, Any] = json.loads(
        (BYBIT_DATA / f"instruments_linear_{symbol}.json").read_text()
    )
    return instrument_from_linear(
        payload["result"]["list"][0],
        default_registry,
        funding_anchor=datetime(2026, 8, 16, 16, tzinfo=UTC),
    )


def instrument(
    *,
    step: str,
    minimum: str,
    contract_size: str = "1",
    quote: object = USD,
    min_notional: str | None = None,
    unit: QuantityUnit = QuantityUnit.UNITS,
) -> Instrument:
    return Instrument(
        id=InstrumentId(venue="test", symbol="TEST/USD"),
        venue_symbol="TESTUSD",
        asset_class=AssetClass.CRYPTO_PERPETUAL,
        base_currency=EUR,
        quote_currency=quote,  # type: ignore[arg-type]
        settlement_currency=quote,  # type: ignore[arg-type]
        price_increment=Decimal("0.01"),
        price_precision=2,
        quantity_unit=unit,
        contract_size=Decimal(contract_size),
        quantity_increment=Decimal(step),
        min_quantity=Decimal(minimum),
        min_notional=Money(Decimal(min_notional), quote) if min_notional else None,  # type: ignore[arg-type]
        financing=FinancingSpec.none(),
        schedule=TradingSchedule.continuous(),
    )


def screen(subject: Instrument, price: str, **kwargs: Any) -> Eligibility:
    if subject.quote_currency.code == "USDT":
        kwargs.setdefault("quote_to_account_rate", USDT_PER_USD)
    kwargs.setdefault("risk_fraction", ONE_PERCENT)
    kwargs.setdefault("stop_distance", ONE_PERCENT)
    kwargs.setdefault("max_risk_deviation", MAX_DEVIATION)
    kwargs.setdefault("equity", EQUITY)
    return evaluate_eligibility(subject, price=Decimal(price), **kwargs)


class TestTheDirectorsDecision:
    """The two instruments the crypto product decision was actually taken on."""

    def test_eth_perpetual_is_tradeable_at_200_usd(self) -> None:
        result = screen(bybit_perpetual("ETHUSDT"), "1880.13")
        assert result.eligible
        assert result.risk_budget == Decimal(2)
        # 2.00 USD of risk with a 1 percent stop is 200 USD of notional, which at
        # 1880.13 is 0.106 ETH, rounded down to the venue's 0.01 step.
        assert result.tradeable_quantity == Decimal("0.10")
        assert result.realised_risk == Decimal("1.880130")

    def test_eth_quantisation_is_within_the_threshold(self) -> None:
        result = screen(bybit_perpetual("ETHUSDT"), "1880.13")
        assert result.worst_case_deviation < MAX_DEVIATION
        assert round(result.worst_case_deviation, 4) == Decimal("0.0940")

    def test_btc_perpetual_is_excluded_for_coarseness_not_for_size(self) -> None:
        # The distinction matters: 0.003 BTC clears the venue's 0.001 minimum, so a
        # rule that only checked the minimum would let this through and then miss the
        # risk limit by up to a third on every trade.
        result = screen(bybit_perpetual("BTCUSDT"), "63034.90")
        assert not result.eligible
        assert result.reason is ExclusionReason.QUANTISATION_TOO_COARSE
        assert result.tradeable_quantity > result.instrument.min_quantity

    def test_btc_is_off_by_almost_a_third_of_the_intended_risk(self) -> None:
        result = screen(bybit_perpetual("BTCUSDT"), "63034.90")
        assert round(result.worst_case_deviation, 4) == Decimal("0.3152")

    def test_btc_becomes_tradeable_when_the_account_grows(self) -> None:
        # Why the threshold is configuration and the answer is recomputed: nothing
        # about BTC changed, the account did. A hardcoded exclusion would outlive its
        # reason.
        result = screen(bybit_perpetual("BTCUSDT"), "63034.90", equity=Money(Decimal(2000), USD))
        assert result.eligible
        assert result.worst_case_deviation < MAX_DEVIATION

    def test_a_slacker_threshold_would_admit_btc(self) -> None:
        # The exclusion is a policy choice, not a fact about the venue, and the test
        # says which knob expresses it.
        result = screen(bybit_perpetual("BTCUSDT"), "63034.90", max_risk_deviation=Decimal("0.40"))
        assert result.eligible


class TestBelowTheVenueMinimum:
    def test_an_intended_size_under_the_minimum_is_excluded(self) -> None:
        result = screen(instrument(step="1", minimum="10"), "100")
        assert result.reason is ExclusionReason.BELOW_MINIMUM_SIZE
        assert result.tradeable_quantity == Decimal(2)

    def test_the_minimum_is_the_venues_own_number(self) -> None:
        # Same account and price, a hundredth of the step and minimum: eligible, and
        # nothing but the venue's own metadata changed.
        result = screen(instrument(step="0.01", minimum="0.01"), "100")
        assert result.eligible
        assert screen(instrument(step="0.01", minimum="10"), "100").reason is (
            ExclusionReason.BELOW_MINIMUM_SIZE
        )

    def test_a_size_below_one_whole_step_is_excluded(self) -> None:
        result = screen(instrument(step="10", minimum="10"), "100")
        assert result.tradeable_quantity == 0
        assert result.reason is ExclusionReason.BELOW_MINIMUM_SIZE

    def test_a_cash_minimum_can_bind_when_the_quantity_one_does_not(self) -> None:
        # Spot venues floor orders by notional rather than by quantity, so an order
        # can clear every quantity rule and still be refused.
        result = screen(instrument(step="0.000001", minimum="0.000001", min_notional="5000"), "100")
        assert result.reason is ExclusionReason.BELOW_MINIMUM_NOTIONAL

    def test_a_cash_minimum_that_is_met_does_not_exclude(self) -> None:
        result = screen(instrument(step="0.000001", minimum="0.000001", min_notional="5"), "100")
        assert result.eligible


class TestTheStopDistanceChangesEverything:
    def test_a_tighter_stop_needs_a_bigger_position(self) -> None:
        subject = instrument(step="0.001", minimum="0.001")
        wide = screen(subject, "100", stop_distance=Decimal("0.02"))
        tight = screen(subject, "100", stop_distance=Decimal("0.005"))
        assert tight.intended_quantity == wide.intended_quantity * 4

    def test_a_wider_stop_makes_quantisation_worse(self) -> None:
        # A wider stop means a smaller position, so one step is a larger share of it.
        subject = instrument(step="0.1", minimum="0.1")
        assert (
            screen(subject, "100", stop_distance=Decimal("0.05")).worst_case_deviation
            > screen(subject, "100", stop_distance=Decimal("0.01")).worst_case_deviation
        )

    def test_the_stop_distance_is_required(self) -> None:
        with pytest.raises(TypeError):
            evaluate_eligibility(  # type: ignore[call-arg]
                instrument(step="1", minimum="1"),
                price=Decimal(100),
                equity=EQUITY,
                risk_fraction=ONE_PERCENT,
                max_risk_deviation=MAX_DEVIATION,
            )


class TestLotSizedInstruments:
    def test_contract_size_multiplies_the_risk_per_unit(self) -> None:
        # A forex lot is 100,000 units of base currency, so one lot of quantity risks
        # 100,000 times what a unit sized instrument does.
        units = instrument(step="0.01", minimum="0.01")
        lots = instrument(
            step="0.01", minimum="0.01", contract_size="100000", unit=QuantityUnit.LOTS
        )
        assert screen(units, "1").intended_quantity == (
            screen(lots, "1").intended_quantity * 100000
        )


class TestCurrencyConversion:
    def test_a_quote_currency_that_is_not_the_account_currency_needs_a_rate(self) -> None:
        # Risk on a JPY quoted pair is in yen, and treating yen as dollars overstates
        # the position by about a hundred and fifty times.
        with pytest.raises(DomainError, match="conversion rate is required"):
            screen(instrument(step="0.01", minimum="0.01", quote=JPY), "150")

    def test_the_rate_converts_the_risk(self) -> None:
        subject = instrument(step="0.001", minimum="0.001", quote=JPY)
        result = screen(subject, "150", quote_to_account_rate=Decimal("0.0067"))
        # 2 USD budget, 1 percent stop of 150 JPY is 1.5 JPY per unit, which is
        # 0.01005 USD per unit, so just under 199 units.
        assert result.intended_quantity > Decimal(198)
        assert result.intended_quantity < Decimal(200)

    def test_a_matching_currency_needs_no_rate(self) -> None:
        assert screen(instrument(step="0.01", minimum="0.01"), "100").eligible

    def test_a_rate_may_be_supplied_even_when_currencies_match(self) -> None:
        # A caller holding a rate table should not have to special case the identity.
        result = screen(
            instrument(step="0.01", minimum="0.01"), "100", quote_to_account_rate=Decimal(1)
        )
        assert result.eligible


class TestValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("price", Decimal(0)),
            ("risk_fraction", Decimal(0)),
            ("stop_distance", Decimal("-0.01")),
            ("max_risk_deviation", Decimal(0)),
            ("quote_to_account_rate", Decimal(0)),
        ],
    )
    def test_non_positive_inputs_are_refused(self, field: str, value: Decimal) -> None:
        arguments: dict[str, Any] = {
            "price": Decimal(100),
            "equity": EQUITY,
            "risk_fraction": ONE_PERCENT,
            "stop_distance": ONE_PERCENT,
            "max_risk_deviation": MAX_DEVIATION,
        }
        arguments[field] = value
        with pytest.raises(DomainError, match=f"{field} must be positive"):
            evaluate_eligibility(instrument(step="1", minimum="1"), **arguments)

    def test_a_float_is_refused(self) -> None:
        # The same gate as everywhere else: a float price here would put a binary
        # approximation inside a risk calculation.
        with pytest.raises(TypeError, match="must not be a float"):
            screen(instrument(step="1", minimum="1"), "100", risk_fraction=0.01)


class TestExplanations:
    def test_an_eligible_instrument_reports_its_precision(self) -> None:
        line = screen(bybit_perpetual("ETHUSDT"), "1880.13").explain()
        assert "tradeable at 0.10" in line
        assert "9.4% worst case deviation" in line

    def test_a_coarse_instrument_explains_which_number_excluded_it(self) -> None:
        line = screen(bybit_perpetual("BTCUSDT"), "63034.90").explain()
        assert "one step of 0.001" in line
        assert "31.5%" in line

    def test_a_small_instrument_names_the_minimum(self) -> None:
        line = screen(instrument(step="1", minimum="10"), "100").explain()
        assert "below the venue minimum of 10" in line

    def test_a_notional_exclusion_names_the_notional(self) -> None:
        line = screen(
            instrument(step="0.000001", minimum="0.000001", min_notional="5000"), "100"
        ).explain()
        assert "minimum notional" in line
