"""The system accepts any account size. `SPEC.md` section 6.1.

These are property tests over a wide range of balances rather than a fixture of chosen
values, because a table of expected numbers is only as good as the person who wrote it
and passes for exactly the sizes they thought of. What is asserted here is the
arithmetic itself: at every balance the screen is handed, the answer must satisfy the
identities that define it.

The range spans eight orders of magnitude, from a balance smaller than any venue
minimum to one where quantisation is irrelevant. There is no threshold in the code and
so there is deliberately none in the range.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.factories import btcusdt_perp, eurusd, usdjpy
from tradingsys.core.currency import default_registry
from tradingsys.core.errors import DomainError
from tradingsys.core.money import Money
from tradingsys.core.numeric import exact_context
from tradingsys.risk.eligibility import ExclusionReason, evaluate_eligibility
from tradingsys.risk.screen import InstrumentScreen, TransitionKind

USD = default_registry.get("USD")

# Eight orders of magnitude. The lower bound is far below any venue minimum and the
# upper bound is far above the point where quantisation stops mattering, so the range
# covers instruments moving in and out of eligibility rather than sitting on one side.
BALANCES = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("100000000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
RISK_FRACTIONS = st.decimals(
    min_value=Decimal("0.001"), max_value=Decimal("0.02"), allow_nan=False, places=4
)
STOPS = st.decimals(
    min_value=Decimal("0.0005"), max_value=Decimal("0.5"), allow_nan=False, places=5
)

SLOW = settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])

DEVIATION_LIMIT = Decimal("0.05")
"""The derived quantisation tolerance. See docs/DECISIONS.md for why it is 5 percent
and not a number someone liked the look of."""

RELATIVE_TOLERANCE = Decimal("1e-30")
"""Slack for the last places of 34 digit decimal arithmetic, and for nothing else.

Wide enough that reassociating a division and a multiplication passes, and far too
narrow for a threshold, an offset, or a rounding to a venue step to hide inside."""


class TestTheArithmeticHoldsAtEveryBalance:
    @given(balance=BALANCES, risk=RISK_FRACTIONS, stop=STOPS)
    @SLOW
    def test_the_budget_is_always_the_stated_fraction_of_equity(
        self, balance: Decimal, risk: Decimal, stop: Decimal
    ) -> None:
        verdict = evaluate_eligibility(
            eurusd(),
            price=Decimal("1.10"),
            equity=Money(balance, USD),
            risk_fraction=risk,
            stop_distance=stop,
            max_risk_deviation=DEVIATION_LIMIT,
        )
        assert verdict.risk_budget == balance * risk

    @given(balance=BALANCES, risk=RISK_FRACTIONS, stop=STOPS)
    @SLOW
    def test_the_intended_size_risks_exactly_the_budget(
        self, balance: Decimal, risk: Decimal, stop: Decimal
    ) -> None:
        # The definition of the intended size: if the stop is hit, it loses the budget.
        # This is the identity everything else is built on.
        price = Decimal("1.10")
        verdict = evaluate_eligibility(
            eurusd(),
            price=price,
            equity=Money(balance, USD),
            risk_fraction=risk,
            stop_distance=stop,
            max_risk_deviation=DEVIATION_LIMIT,
        )
        with exact_context():
            loss_at_stop = verdict.intended_quantity * stop * price
        assert abs(loss_at_stop - verdict.risk_budget) <= verdict.risk_budget * Decimal("1e-25")

    @given(balance=BALANCES, risk=RISK_FRACTIONS, stop=STOPS)
    @SLOW
    def test_the_tradeable_size_is_on_the_venue_grid_and_never_above_intended(
        self, balance: Decimal, risk: Decimal, stop: Decimal
    ) -> None:
        instrument = eurusd()
        verdict = evaluate_eligibility(
            instrument,
            price=Decimal("1.10"),
            equity=Money(balance, USD),
            risk_fraction=risk,
            stop_distance=stop,
            max_risk_deviation=DEVIATION_LIMIT,
        )
        assert verdict.tradeable_quantity <= verdict.intended_quantity
        remainder = verdict.tradeable_quantity % instrument.quantity_increment
        assert remainder == 0, f"{verdict.tradeable_quantity} is not on the venue's step"

    @given(balance=BALANCES, risk=RISK_FRACTIONS, stop=STOPS)
    @SLOW
    def test_the_verdict_follows_from_the_numbers_rather_than_from_a_table(
        self, balance: Decimal, risk: Decimal, stop: Decimal
    ) -> None:
        instrument = eurusd()
        limit = DEVIATION_LIMIT
        verdict = evaluate_eligibility(
            instrument,
            price=Decimal("1.10"),
            equity=Money(balance, USD),
            risk_fraction=risk,
            stop_distance=stop,
            max_risk_deviation=limit,
        )
        below_minimum = verdict.tradeable_quantity < instrument.min_quantity
        too_coarse = verdict.worst_case_deviation > limit

        if below_minimum:
            assert verdict.reason is ExclusionReason.BELOW_MINIMUM_SIZE
        elif too_coarse:
            assert verdict.reason is ExclusionReason.QUANTISATION_TOO_COARSE
        else:
            assert verdict.eligible

    @given(balance=BALANCES, risk=RISK_FRACTIONS, stop=STOPS)
    @SLOW
    def test_the_stop_ceiling_is_exactly_where_the_minimum_size_stops_fitting(
        self, balance: Decimal, risk: Decimal, stop: Decimal
    ) -> None:
        # At the ceiling, the intended size equals the venue minimum. This is what makes
        # the ceiling a strategy constraint rather than a presentational number.
        instrument = eurusd()
        price = Decimal("1.10")
        verdict = evaluate_eligibility(
            instrument,
            price=price,
            equity=Money(balance, USD),
            risk_fraction=risk,
            stop_distance=stop,
            max_risk_deviation=DEVIATION_LIMIT,
        )
        at_ceiling = evaluate_eligibility(
            instrument,
            price=price,
            equity=Money(balance, USD),
            risk_fraction=risk,
            stop_distance=verdict.widest_affordable_stop,
            max_risk_deviation=DEVIATION_LIMIT,
        )
        difference = abs(at_ceiling.intended_quantity - instrument.min_quantity)
        assert difference <= instrument.min_quantity * Decimal("1e-20")


class TestScalingIsExact:
    @given(balance=BALANCES, factor=st.integers(min_value=2, max_value=1000))
    @SLOW
    def test_multiplying_the_balance_multiplies_the_size_by_the_same_factor(
        self, balance: Decimal, factor: int
    ) -> None:
        # Capital independence, stated as arithmetic: nothing about the sizing is
        # anchored to a particular account size, so the intended quantity is linear in
        # equity with no offset and no threshold.
        instrument = eurusd()
        arguments = {
            "price": Decimal("1.10"),
            "risk_fraction": Decimal("0.01"),
            "stop_distance": Decimal("0.01"),
            "max_risk_deviation": DEVIATION_LIMIT,
        }
        small = evaluate_eligibility(instrument, equity=Money(balance, USD), **arguments)
        large = evaluate_eligibility(instrument, equity=Money(balance * factor, USD), **arguments)

        # Linearity is exact in real arithmetic. Decimal carries 34 significant digits,
        # so dividing then multiplying can differ from the direct computation in the
        # last place. What is asserted is the absence of a threshold or an offset, not
        # bit equality under rounding.
        with exact_context():
            for scaled, base in (
                (large.intended_quantity, small.intended_quantity),
                (large.widest_affordable_stop, small.widest_affordable_stop),
            ):
                expected = base * factor
                assert abs(scaled - expected) <= expected * RELATIVE_TOLERANCE

    @given(balance=BALANCES)
    @SLOW
    def test_the_stop_ceiling_does_not_depend_on_the_stop_it_was_reported_at(
        self, balance: Decimal
    ) -> None:
        instrument = eurusd()
        ceilings = {
            evaluate_eligibility(
                instrument,
                price=Decimal("1.10"),
                equity=Money(balance, USD),
                risk_fraction=Decimal("0.01"),
                stop_distance=stop,
                max_risk_deviation=DEVIATION_LIMIT,
            ).widest_affordable_stop
            for stop in (Decimal("0.001"), Decimal("0.01"), Decimal("0.1"))
        }
        assert len(ceilings) == 1


class TestTheTradeableSetIsDynamic:
    """Instruments enter and leave as the balance moves, with no restart and no code change."""

    @staticmethod
    def screen() -> InstrumentScreen:
        return InstrumentScreen(
            risk_fraction=Decimal("0.01"),
            max_risk_deviation=DEVIATION_LIMIT,
            reference_stop=Decimal("0.01"),
        )

    def test_an_instrument_excluded_when_poor_becomes_eligible_when_rich(self) -> None:
        instrument = eurusd()
        prices = {instrument.id: Decimal("1.10")}
        screen = self.screen()

        # At 5 USD the venue step is 22 percent of the intended size, which the screen
        # refuses as too coarse to hold the risk limit. At 200 USD it is half a percent.
        poor = screen.evaluate([instrument], equity=Money(Decimal("5"), USD), prices=prices)
        rich = screen.evaluate([instrument], equity=Money(Decimal("200"), USD), prices=prices)

        assert instrument.id in poor.excluded
        assert instrument.id in rich.tradeable

    def test_the_same_screen_object_answers_differently_at_a_different_balance(self) -> None:
        # No restart, no reconstruction, no cached verdict: the object holds policy and
        # nothing else, so the balance is the only thing that changed.
        instrument = eurusd()
        prices = {instrument.id: Decimal("1.10")}
        screen = self.screen()

        first = screen.evaluate([instrument], equity=Money(Decimal("5"), USD), prices=prices)
        second = screen.evaluate([instrument], equity=Money(Decimal("200"), USD), prices=prices)
        third = screen.evaluate([instrument], equity=Money(Decimal("5"), USD), prices=prices)

        assert first.tradeable == third.tradeable
        assert second.tradeable != first.tradeable

    def test_entering_the_set_is_reported_as_a_transition(self) -> None:
        instrument = eurusd()
        prices = {instrument.id: Decimal("1.10")}
        screen = self.screen()

        poor = screen.evaluate([instrument], equity=Money(Decimal("5"), USD), prices=prices)
        rich = screen.evaluate([instrument], equity=Money(Decimal("200"), USD), prices=prices)

        moved = rich.transitions(poor)
        assert len(moved) == 1
        assert moved[0].kind is TransitionKind.ENTERED
        assert moved[0].instrument_id == instrument.id

    def test_leaving_the_set_is_reported_as_a_transition(self) -> None:
        # The direction that matters operationally: an account that shrank stops being
        # able to trade something, and nobody should learn that from orders drying up.
        instrument = eurusd()
        prices = {instrument.id: Decimal("1.10")}
        screen = self.screen()

        rich = screen.evaluate([instrument], equity=Money(Decimal("200"), USD), prices=prices)
        poor = screen.evaluate([instrument], equity=Money(Decimal("5"), USD), prices=prices)

        moved = poor.transitions(rich)
        assert len(moved) == 1
        assert moved[0].kind is TransitionKind.LEFT
        assert "excluded" in moved[0].detail

    def test_a_first_evaluation_reports_no_transitions(self) -> None:
        instrument = eurusd()
        screen = self.screen()
        result = screen.evaluate(
            [instrument], equity=Money(Decimal("200"), USD), prices={instrument.id: Decimal("1.10")}
        )
        assert result.transitions(None) == ()

    def test_an_unchanged_balance_reports_no_transitions(self) -> None:
        instrument = eurusd()
        prices = {instrument.id: Decimal("1.10")}
        screen = self.screen()
        first = screen.evaluate([instrument], equity=Money(Decimal("200"), USD), prices=prices)
        second = screen.evaluate([instrument], equity=Money(Decimal("200"), USD), prices=prices)
        assert second.transitions(first) == ()

    @given(balance=BALANCES)
    @SLOW
    def test_every_instrument_is_assessed_at_every_balance(self, balance: Decimal) -> None:
        instruments = [eurusd(), btcusdt_perp()]
        prices = {
            instruments[0].id: Decimal("1.10"),
            instruments[1].id: Decimal("63035"),
        }
        # USDT is not USD, so the rate is stated rather than assumed, at every balance.
        rates = {instruments[1].id: Decimal("1.0")}
        result = self.screen().evaluate(
            instruments, equity=Money(balance, USD), prices=prices, quote_rates=rates
        )
        assert set(result.assessments) == {item.id for item in instruments}
        assert len(result.tradeable) + len(result.excluded) == len(instruments)


class TestRefusalsThatAreNotAboutSize:
    def test_a_zero_balance_is_refused_rather_than_treated_as_a_small_account(self) -> None:
        # There is no minimum account size, but zero is not a small account: it is an
        # account state to halt on, and sizing against it would divide by nothing.
        instrument = eurusd()
        with pytest.raises(DomainError, match="not a small account"):
            InstrumentScreen(
                risk_fraction=Decimal("0.01"),
                max_risk_deviation=DEVIATION_LIMIT,
                reference_stop=Decimal("0.01"),
            ).evaluate(
                [instrument],
                equity=Money(Decimal("0"), USD),
                prices={instrument.id: Decimal("1.10")},
            )

    def test_a_missing_price_is_refused_rather_than_silently_excluding(self) -> None:
        # An instrument screened out for lack of a price would be indistinguishable
        # from one screened out on risk grounds.
        instrument = eurusd()
        with pytest.raises(DomainError, match="has no price"):
            InstrumentScreen(
                risk_fraction=Decimal("0.01"),
                max_risk_deviation=DEVIATION_LIMIT,
                reference_stop=Decimal("0.01"),
            ).evaluate([instrument], equity=Money(Decimal("200"), USD), prices={})

    def test_a_quote_currency_mismatch_still_demands_an_explicit_rate(self) -> None:
        # Capital independence does not relax the conversion rule: a JPY quoted pair on
        # a USD account needs a stated rate at every balance.
        instrument = usdjpy()
        with pytest.raises(DomainError, match="conversion rate is required"):
            InstrumentScreen(
                risk_fraction=Decimal("0.01"),
                max_risk_deviation=DEVIATION_LIMIT,
                reference_stop=Decimal("0.01"),
            ).evaluate(
                [instrument],
                equity=Money(Decimal("200"), USD),
                prices={instrument.id: Decimal("159.326")},
            )

    def test_policy_values_must_be_fractions(self) -> None:
        # A policy carrying an absolute amount would be correct at one account size.
        with pytest.raises(DomainError, match="not a fraction"):
            InstrumentScreen(
                risk_fraction=Decimal("200"),
                max_risk_deviation=DEVIATION_LIMIT,
                reference_stop=Decimal("0.01"),
            )
