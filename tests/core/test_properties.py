"""Property tests for money and sizing arithmetic.

Example based tests check the cases someone thought of. These check the laws that must
hold for every value, which is what catches the case nobody thought of: a quantity that
rounds up through a maximum, a pip value that loses a digit, a lot conversion that does
not round trip.

Decimals are drawn with bounded exponents. Unbounded ones would generate values like
1E+900, which no venue can express and whose arithmetic says nothing useful.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from tests.factories import EUR, USD, btcusdt, btcusdt_perp, eurusd, eurusd_lots, usdjpy
from tradingsys.core.errors import CurrencyMismatchError, InvalidQuantityError
from tradingsys.core.instrument import Instrument
from tradingsys.core.money import Money
from tradingsys.core.rounding import Rounding

# Amounts spanning a satoshi to a large notional, at precisions a venue can express.
amounts = st.decimals(
    min_value=Decimal("-1000000000"),
    max_value=Decimal("1000000000"),
    allow_nan=False,
    allow_infinity=False,
    places=8,
)
positive_amounts = st.decimals(
    min_value=Decimal("0.00000001"),
    max_value=Decimal("1000000000"),
    allow_nan=False,
    allow_infinity=False,
    places=8,
)
factors = st.decimals(
    min_value=Decimal("-1000"),
    max_value=Decimal("1000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)
prices = st.decimals(
    min_value=Decimal("0.00001"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=5,
)
instruments = st.sampled_from([eurusd(), usdjpy(), eurusd_lots(), btcusdt(), btcusdt_perp()])


class TestMoneyIsAnAbelianGroup:
    """Addition must behave like arithmetic, not like floating point."""

    @given(amounts, amounts)
    def test_addition_is_commutative(self, first: Decimal, second: Decimal) -> None:
        assert Money(first, USD) + Money(second, USD) == Money(second, USD) + Money(first, USD)

    @given(amounts, amounts, amounts)
    def test_addition_is_associative(self, first: Decimal, second: Decimal, third: Decimal) -> None:
        # This is the law binary floating point breaks: (0.1 + 0.2) + 0.3 differs from
        # 0.1 + (0.2 + 0.3).
        left = (Money(first, USD) + Money(second, USD)) + Money(third, USD)
        right = Money(first, USD) + (Money(second, USD) + Money(third, USD))
        assert left == right

    @given(amounts)
    def test_zero_is_the_identity(self, amount: Decimal) -> None:
        assert Money(amount, USD) + Money.zero(USD) == Money(amount, USD)

    @given(amounts)
    def test_negation_is_the_inverse(self, amount: Decimal) -> None:
        assert Money(amount, USD) + (-Money(amount, USD)) == Money.zero(USD)

    @given(amounts, amounts)
    def test_subtraction_undoes_addition(self, first: Decimal, second: Decimal) -> None:
        total = Money(first, USD) + Money(second, USD)
        assert total - Money(second, USD) == Money(first, USD)


class TestMoneyScaling:
    @given(amounts, factors)
    def test_multiplication_is_commutative_with_the_scalar(
        self, amount: Decimal, factor: Decimal
    ) -> None:
        assert Money(amount, USD) * factor == factor * Money(amount, USD)

    @given(amounts, factors)
    def test_division_undoes_multiplication(self, amount: Decimal, factor: Decimal) -> None:
        assume(factor != 0)
        scaled = Money(amount, USD) * factor
        # Exact to the working precision, so compare at a precision well inside it.
        assert (scaled / factor).quantize_to(12) == Money(amount, USD).quantize_to(12)

    @given(amounts, factors, factors)
    def test_multiplication_distributes_over_addition(
        self, amount: Decimal, first: Decimal, second: Decimal
    ) -> None:
        money = Money(amount, USD)
        assert money * (first + second) == (money * first) + (money * second)

    @given(amounts)
    def test_multiplying_by_one_changes_nothing(self, amount: Decimal) -> None:
        assert Money(amount, USD) * 1 == Money(amount, USD)


class TestMoneyOrdering:
    @given(amounts, amounts)
    def test_ordering_is_total(self, first: Decimal, second: Decimal) -> None:
        left, right = Money(first, USD), Money(second, USD)
        assert (left < right) + (left > right) + (left == right) == 1

    @given(amounts, amounts, amounts)
    def test_ordering_is_transitive(self, first: Decimal, second: Decimal, third: Decimal) -> None:
        values = sorted([Money(first, USD), Money(second, USD), Money(third, USD)])
        assert values[0] <= values[1] <= values[2]

    @given(amounts, amounts)
    def test_addition_preserves_order(self, first: Decimal, second: Decimal) -> None:
        delta = Money(Decimal(100), USD)
        if Money(first, USD) <= Money(second, USD):
            assert Money(first, USD) + delta <= Money(second, USD) + delta

    @given(amounts)
    def test_equal_values_hash_equally(self, amount: Decimal) -> None:
        assert hash(Money(amount, USD)) == hash(Money(Decimal(str(amount)), USD))


class TestMoneyInvariants:
    @given(amounts)
    def test_absolute_value_is_never_negative(self, amount: Decimal) -> None:
        assert not abs(Money(amount, USD)).is_negative

    @given(amounts)
    def test_quantizing_never_moves_by_a_whole_unit(self, amount: Decimal) -> None:
        money = Money(amount, USD)
        difference = abs(money - money.quantize())
        assert difference < Money(Decimal("0.01"), USD)

    @given(amounts)
    def test_quantizing_is_idempotent(self, amount: Decimal) -> None:
        once = Money(amount, USD).quantize()
        assert once.quantize() == once

    @given(amounts)
    def test_rounding_down_never_increases_magnitude(self, amount: Decimal) -> None:
        money = Money(amount, USD)
        assert abs(money.quantize(Rounding.DOWN)) <= abs(money)

    @given(amounts)
    def test_rounding_up_never_decreases_magnitude(self, amount: Decimal) -> None:
        money = Money(amount, USD)
        assert abs(money.quantize(Rounding.UP)) >= abs(money)

    @given(amounts)
    def test_a_currency_mismatch_always_raises(self, amount: Decimal) -> None:
        with pytest.raises(CurrencyMismatchError):
            Money(amount, USD) + Money(amount, EUR)


class TestSizingRoundTrips:
    @given(instruments, positive_amounts)
    def test_base_unit_conversion_round_trips(
        self, instrument: Instrument, quantity: Decimal
    ) -> None:
        converted = instrument.from_base_units(instrument.to_base_units(quantity))
        assert converted == quantity

    @given(instruments, positive_amounts)
    def test_to_base_units_scales_monotonically(
        self, instrument: Instrument, quantity: Decimal
    ) -> None:
        assert instrument.to_base_units(quantity * 2) == instrument.to_base_units(quantity) * 2


class TestQuantization:
    @given(instruments, positive_amounts)
    def test_a_quantized_quantity_sits_on_the_step_grid(
        self, instrument: Instrument, quantity: Decimal
    ) -> None:
        snapped = instrument.quantize_quantity(quantity)
        assume(snapped != 0)
        assert snapped % instrument.quantity_increment == 0

    @given(instruments, positive_amounts)
    def test_rounding_down_never_exceeds_the_request(
        self, instrument: Instrument, quantity: Decimal
    ) -> None:
        # Overshooting is what breaches a venue maximum or an available margin limit.
        assert instrument.quantize_quantity(quantity, Rounding.DOWN) <= quantity

    @given(instruments, positive_amounts)
    def test_quantizing_is_idempotent(self, instrument: Instrument, quantity: Decimal) -> None:
        once = instrument.quantize_quantity(quantity)
        assert instrument.quantize_quantity(once) == once

    @given(instruments, positive_amounts)
    def test_an_accepted_quantity_survives_quantization_unchanged(
        self, instrument: Instrument, quantity: Decimal
    ) -> None:
        snapped = instrument.quantize_quantity(quantity)
        assume(instrument.is_valid_quantity(snapped))
        assert instrument.quantize_quantity(snapped) == snapped

    @given(instruments, prices)
    def test_a_quantized_price_sits_on_the_tick_grid(
        self, instrument: Instrument, price: Decimal
    ) -> None:
        snapped = instrument.quantize_price(price)
        assume(snapped > 0)
        assert snapped % instrument.price_increment == 0

    @given(instruments, prices)
    def test_price_quantization_is_idempotent(self, instrument: Instrument, price: Decimal) -> None:
        once = instrument.quantize_price(price)
        assume(once > 0)
        assert instrument.quantize_price(once) == once


class TestValuation:
    @given(instruments, positive_amounts, prices)
    def test_notional_is_never_negative(
        self, instrument: Instrument, quantity: Decimal, price: Decimal
    ) -> None:
        assert not instrument.notional(quantity, price).is_negative

    @given(instruments, positive_amounts, prices)
    def test_notional_ignores_direction(
        self, instrument: Instrument, quantity: Decimal, price: Decimal
    ) -> None:
        assert instrument.notional(quantity, price) == instrument.notional(-quantity, price)

    @given(instruments, positive_amounts, prices)
    def test_notional_is_denominated_in_the_quote_currency(
        self, instrument: Instrument, quantity: Decimal, price: Decimal
    ) -> None:
        assert instrument.notional(quantity, price).currency == instrument.quote_currency

    @given(instruments, positive_amounts, prices)
    def test_notional_scales_linearly_with_size(
        self, instrument: Instrument, quantity: Decimal, price: Decimal
    ) -> None:
        single = instrument.notional(quantity, price)
        double = instrument.notional(quantity * 2, price)
        assert double == single * 2

    @given(instruments, positive_amounts)
    def test_tick_value_scales_linearly(self, instrument: Instrument, quantity: Decimal) -> None:
        assert instrument.value_per_tick(quantity * 3) == instrument.value_per_tick(quantity) * 3

    @given(positive_amounts, prices)
    @settings(max_examples=50)
    def test_the_same_exposure_prices_identically_across_sizing_conventions(
        self, lots: Decimal, price: Decimal
    ) -> None:
        # One lot at a lot sized venue is 100000 units at a unit sized one. The two
        # must value identically, or risk sizing depends on which venue is quoted.
        units, lot_venue = eurusd(), eurusd_lots()
        exposure = lot_venue.to_base_units(lots)
        assert units.notional(exposure, price) == lot_venue.notional(lots, price)
        assert units.value_per_pip(exposure) == lot_venue.value_per_pip(lots)


class TestMargin:
    @given(instruments, positive_amounts, prices, st.integers(min_value=1, max_value=20))
    def test_margin_never_exceeds_notional_at_leverage_of_at_least_one(
        self, instrument: Instrument, quantity: Decimal, price: Decimal, leverage: int
    ) -> None:
        if instrument.max_leverage is not None and leverage > instrument.max_leverage:
            with pytest.raises(InvalidQuantityError):
                instrument.required_margin(quantity, price, leverage=leverage)
            return
        margin = instrument.required_margin(quantity, price, leverage=leverage)
        assert margin <= instrument.notional(quantity, price)

    @given(instruments, positive_amounts, prices)
    def test_higher_leverage_never_requires_more_margin(
        self, instrument: Instrument, quantity: Decimal, price: Decimal
    ) -> None:
        low = instrument.required_margin(quantity, price, leverage=2)
        high = instrument.required_margin(quantity, price, leverage=10)
        assert high <= low
