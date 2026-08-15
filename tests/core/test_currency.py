"""Tests for currency definitions and the registry."""

from __future__ import annotations

import pytest

from tradingsys.core.currency import Currency, CurrencyKind, CurrencyRegistry, default_registry
from tradingsys.core.errors import DomainError, UnknownCurrencyError


class TestCurrency:
    def test_rejects_empty_code(self) -> None:
        with pytest.raises(DomainError, match="must not be empty"):
            Currency("", 2, CurrencyKind.FIAT, "Nothing")

    def test_rejects_lowercase_code(self) -> None:
        with pytest.raises(DomainError, match="upper case"):
            Currency("usd", 2, CurrencyKind.FIAT, "Dollar")

    def test_rejects_punctuation_in_code(self) -> None:
        with pytest.raises(DomainError, match="alphanumeric"):
            Currency("US-D", 2, CurrencyKind.FIAT, "Dollar")

    def test_rejects_negative_precision(self) -> None:
        with pytest.raises(DomainError, match="must not be negative"):
            Currency("USD", -1, CurrencyKind.FIAT, "Dollar")

    def test_rejects_absurd_precision(self) -> None:
        with pytest.raises(DomainError, match="exceeds the supported maximum"):
            Currency("USD", 19, CurrencyKind.FIAT, "Dollar")

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(DomainError, match="name must not be empty"):
            Currency("USD", 2, CurrencyKind.FIAT, "")

    def test_is_frozen(self) -> None:
        usd = default_registry.get("USD")
        with pytest.raises(AttributeError):
            usd.precision = 4  # type: ignore[misc]

    def test_str_is_the_code(self) -> None:
        assert str(default_registry.get("USD")) == "USD"

    def test_value_equality(self) -> None:
        assert Currency(
            "USD", 2, CurrencyKind.FIAT, "United States dollar"
        ) == default_registry.get("USD")


class TestRegistry:
    def test_seed_precisions_reflect_reality(self) -> None:
        assert default_registry.get("USD").precision == 2
        assert default_registry.get("JPY").precision == 0
        assert default_registry.get("BTC").precision == 8
        assert default_registry.get("ETH").precision == 18

    def test_kinds(self) -> None:
        assert default_registry.get("EUR").kind is CurrencyKind.FIAT
        assert default_registry.get("BTC").kind is CurrencyKind.CRYPTO
        assert default_registry.get("XAU").kind is CurrencyKind.METAL

    def test_lookup_normalises_case_and_whitespace(self) -> None:
        assert default_registry.get(" usd ") == default_registry.get("USD")

    def test_unknown_code_raises_with_guidance(self) -> None:
        with pytest.raises(UnknownCurrencyError, match="Register it at startup"):
            default_registry.get("ZZZ")

    def test_contains(self) -> None:
        assert default_registry.contains("usd")
        assert not default_registry.contains("ZZZ")

    def test_register_new_currency(self) -> None:
        registry = CurrencyRegistry()
        token = Currency("XYZ", 4, CurrencyKind.CRYPTO, "Example token")
        registry.register(token)
        assert registry.get("XYZ") is token
        assert len(registry) == 1

    def test_registering_the_same_definition_twice_is_allowed(self) -> None:
        registry = CurrencyRegistry()
        token = Currency("XYZ", 4, CurrencyKind.CRYPTO, "Example token")
        registry.register(token)
        registry.register(Currency("XYZ", 4, CurrencyKind.CRYPTO, "Example token"))
        assert len(registry) == 1

    def test_conflicting_redefinition_raises(self) -> None:
        registry = CurrencyRegistry()
        registry.register(Currency("XYZ", 4, CurrencyKind.CRYPTO, "Example token"))
        with pytest.raises(DomainError, match="refusing to redefine"):
            registry.register(Currency("XYZ", 8, CurrencyKind.CRYPTO, "Example token"))

    def test_all_is_sorted_by_code(self) -> None:
        registry = CurrencyRegistry(
            [
                Currency("ZZZ", 2, CurrencyKind.FIAT, "Last"),
                Currency("AAA", 2, CurrencyKind.FIAT, "First"),
            ]
        )
        assert [currency.code for currency in registry.all()] == ["AAA", "ZZZ"]

    def test_copy_is_independent(self) -> None:
        copied = default_registry.copy()
        copied.register(Currency("XYZ", 4, CurrencyKind.CRYPTO, "Example token"))
        assert copied.contains("XYZ")
        assert not default_registry.contains("XYZ")

    def test_default_registry_covers_the_venues_we_target(self) -> None:
        for code in ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "BTC", "ETH", "USDT", "USDC"):
            assert default_registry.contains(code), code
