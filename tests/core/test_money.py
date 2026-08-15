"""Tests for exact monetary arithmetic."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradingsys.core.currency import Currency, CurrencyKind, default_registry
from tradingsys.core.errors import CurrencyMismatchError, DomainError
from tradingsys.core.money import Money
from tradingsys.core.rounding import Rounding

USD = default_registry.get("USD")
EUR = default_registry.get("EUR")
JPY = default_registry.get("JPY")
BTC = default_registry.get("BTC")


class TestConstruction:
    def test_accepts_decimal_int_and_string(self) -> None:
        assert Money.of(Decimal("1.25"), USD).amount == Decimal("1.25")
        assert Money.of(5, USD).amount == Decimal(5)
        assert Money.of("0.07", USD).amount == Decimal("0.07")

    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="must not be a float"):
            Money.of(0.1, USD)  # type: ignore[arg-type]

    def test_rejects_float_via_direct_construction(self) -> None:
        with pytest.raises(TypeError, match="must not be a float"):
            Money(0.1, USD)  # type: ignore[arg-type]

    def test_rejects_non_finite(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(DomainError, match="must be finite"):
                Money.of(value, USD)

    def test_rejects_unparseable_string(self) -> None:
        with pytest.raises(DomainError, match="not a valid decimal"):
            Money.of("twelve", USD)

    def test_rejects_non_currency(self) -> None:
        with pytest.raises(TypeError, match="currency must be a Currency"):
            Money.of(1, "USD")  # type: ignore[arg-type]

    def test_preserves_full_precision(self) -> None:
        amount = Money.of("1.00000000000000000001", USD)
        assert amount.amount == Decimal("1.00000000000000000001")

    def test_zero(self) -> None:
        assert Money.zero(USD).amount == Decimal(0)
        assert Money.zero(USD).is_zero

    def test_with_amount_keeps_currency(self) -> None:
        assert Money.of(1, EUR).with_amount("9.5") == Money.of("9.5", EUR)


class TestArithmetic:
    def test_addition_is_exact(self) -> None:
        total = Money.of("0.1", USD) + Money.of("0.2", USD)
        assert total.amount == Decimal("0.3")

    def test_subtraction(self) -> None:
        assert Money.of("5", USD) - Money.of("1.25", USD) == Money.of("3.75", USD)

    def test_multiplication_by_scalar(self) -> None:
        assert Money.of("1.10", USD) * 3 == Money.of("3.30", USD)
        assert 3 * Money.of("1.10", USD) == Money.of("3.30", USD)
        assert Money.of("100", USD) * Decimal("0.015") == Money.of("1.500", USD)

    def test_multiplication_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="must not be a float"):
            Money.of(1, USD) * 1.5  # type: ignore[operator]

    def test_division_by_scalar(self) -> None:
        assert Money.of("10", USD) / 4 == Money.of("2.5", USD)

    def test_division_by_zero(self) -> None:
        with pytest.raises(DomainError, match="divide"):
            Money.of("10", USD) / 0

    def test_ratio_between_amounts_is_dimensionless(self) -> None:
        ratio = Money.of("50", USD).ratio_to(Money.of("200", USD))
        assert ratio == Decimal("0.25")

    def test_ratio_rejects_zero_denominator(self) -> None:
        with pytest.raises(DomainError, match="zero amount"):
            Money.of("1", USD).ratio_to(Money.zero(USD))

    def test_negation_and_absolute(self) -> None:
        assert -Money.of("3", USD) == Money.of("-3", USD)
        assert abs(Money.of("-3", USD)) == Money.of("3", USD)
        assert +Money.of("3", USD) == Money.of("3", USD)

    def test_division_keeps_working_precision(self) -> None:
        third = Money.of("1", USD) / 3
        assert str(third.amount).startswith("0.3333333333333333333333333333333333")


class TestCurrencySafety:
    def test_addition_across_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError, match="Cannot add USD and EUR"):
            Money.of(1, USD) + Money.of(1, EUR)

    def test_subtraction_across_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError, match="Cannot subtract"):
            Money.of(1, USD) - Money.of(1, EUR)

    def test_ordering_across_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError, match="Cannot compare"):
            _ = Money.of(1, USD) < Money.of(1, EUR)

    def test_ratio_across_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError, match="Cannot divide"):
            Money.of(1, USD).ratio_to(Money.of(1, EUR))

    def test_equality_across_currencies_is_false_not_an_error(self) -> None:
        # Equality is total: a dict or set holding mixed currencies must not explode.
        assert Money.of(1, USD) != Money.of(1, EUR)

    def test_equality_with_other_types_is_false(self) -> None:
        assert Money.of(1, USD) != 1
        assert Money.of(1, USD) != "1 USD"

    def test_adding_a_bare_number_is_a_type_error(self) -> None:
        # The operators return NotImplemented for a non-Money operand, which makes
        # Python raise rather than silently treating a bare 5 as five dollars.
        with pytest.raises(TypeError):
            _ = Money.of(1, USD) + 5  # type: ignore[operator]

    def test_subtracting_a_bare_number_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            _ = Money.of(1, USD) - 5  # type: ignore[operator]

    def test_adding_a_decimal_is_a_type_error(self) -> None:
        # Decimal is the one that would look most plausible, and is exactly the case
        # where a currency would be silently invented.
        with pytest.raises(TypeError):
            _ = Money.of(1, USD) + Decimal(5)  # type: ignore[operator]

    @pytest.mark.parametrize("other", [5, "5", Decimal(5), None])
    def test_ordering_against_a_non_money_value_is_a_type_error(self, other: object) -> None:
        amount = Money.of(1, USD)
        with pytest.raises(TypeError):
            _ = amount < other  # type: ignore[operator]
        with pytest.raises(TypeError):
            _ = amount <= other  # type: ignore[operator]
        with pytest.raises(TypeError):
            _ = amount > other  # type: ignore[operator]
        with pytest.raises(TypeError):
            _ = amount >= other  # type: ignore[operator]


class TestComparison:
    def test_ordering(self) -> None:
        assert Money.of(1, USD) < Money.of(2, USD)
        assert Money.of(2, USD) > Money.of(1, USD)
        assert Money.of(1, USD) <= Money.of(1, USD)
        assert Money.of(1, USD) >= Money.of(1, USD)

    def test_trailing_zeros_compare_and_hash_equal(self) -> None:
        assert Money.of("1.50", USD) == Money.of("1.5", USD)
        assert hash(Money.of("1.50", USD)) == hash(Money.of("1.5", USD))

    def test_usable_as_dict_key(self) -> None:
        counts = {Money.of("1.0", USD): "a"}
        counts[Money.of("1.00", USD)] = "b"
        assert counts == {Money.of(1, USD): "b"}

    def test_sorting(self) -> None:
        amounts = [Money.of(3, USD), Money.of(1, USD), Money.of(2, USD)]
        assert sorted(amounts) == [Money.of(1, USD), Money.of(2, USD), Money.of(3, USD)]


class TestRounding:
    def test_quantize_uses_currency_precision(self) -> None:
        assert Money.of("1.2345", USD).quantize().amount == Decimal("1.23")
        assert Money.of("1.2345", JPY).quantize().amount == Decimal("1")
        assert Money.of("1.234567891", BTC).quantize().amount == Decimal("1.23456789")

    def test_quantize_defaults_to_bankers_rounding(self) -> None:
        assert Money.of("1.005", USD).quantize().amount == Decimal("1.00")
        assert Money.of("1.015", USD).quantize().amount == Decimal("1.02")

    def test_quantize_honours_explicit_mode(self) -> None:
        amount = Money.of("1.005", USD)
        assert amount.quantize(Rounding.HALF_UP).amount == Decimal("1.01")
        assert amount.quantize(Rounding.DOWN).amount == Decimal("1.00")
        assert amount.quantize(Rounding.UP).amount == Decimal("1.01")

    def test_quantize_to_explicit_places(self) -> None:
        assert Money.of("1.23456", USD).quantize_to(4).amount == Decimal("1.2346")

    def test_quantize_to_rejects_negative_places(self) -> None:
        with pytest.raises(DomainError, match="must not be negative"):
            Money.of(1, USD).quantize_to(-1)

    def test_quantizing_does_not_mutate_the_original(self) -> None:
        original = Money.of("1.2345", USD)
        original.quantize()
        assert original.amount == Decimal("1.2345")


class TestPredicatesAndRendering:
    def test_sign_predicates(self) -> None:
        assert Money.of(1, USD).is_positive
        assert Money.of(-1, USD).is_negative
        assert Money.zero(USD).is_zero
        assert not Money.zero(USD).is_positive

    def test_str_renders_at_currency_precision(self) -> None:
        assert str(Money.of("1.239", USD)) == "1.24 USD"
        assert str(Money.of("1500", JPY)) == "1500 JPY"

    def test_repr_shows_unrounded_amount(self) -> None:
        assert repr(Money.of("1.239", USD)) == "Money('1.239', USD)"

    def test_high_precision_currency_round_trips(self) -> None:
        eth = default_registry.get("ETH")
        wei = Money.of("0.000000000000000001", eth)
        assert wei.quantize().amount == Decimal("0.000000000000000001")


class TestImmutability:
    def test_frozen(self) -> None:
        amount = Money.of(1, USD)
        with pytest.raises(AttributeError):
            amount.amount = Decimal(2)  # type: ignore[misc]

    def test_currency_identity_is_by_value(self) -> None:
        duplicate = Currency("USD", 2, CurrencyKind.FIAT, "United States dollar")
        assert Money.of(1, duplicate) == Money.of(1, USD)
