"""Currency definitions and the process-wide currency registry.

A :class:`Currency` carries its own display precision because that precision differs
by three orders of magnitude across the venues we target: JPY settles in whole yen,
USD in cents, and BTC in satoshi. Any code that rounds a monetary amount must ask the
currency rather than assume two decimal places.

The seed table below is reference data (ISO 4217 minor units for fiat, native
smallest-unit precision for crypto), not configuration. Deployments that trade an
asset absent from the table register it at startup with :meth:`CurrencyRegistry.register`
rather than editing this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, final

from tradingsys.core.errors import DomainError, UnknownCurrencyError

__all__ = [
    "Currency",
    "CurrencyKind",
    "CurrencyRegistry",
    "default_registry",
]

_MAX_PRECISION: Final = 18


class CurrencyKind(StrEnum):
    """Broad class of a currency, which determines settlement conventions."""

    FIAT = "fiat"
    CRYPTO = "crypto"
    METAL = "metal"


@final
@dataclass(frozen=True, slots=True)
class Currency:
    """An immutable currency definition.

    Attributes:
        code: Upper case symbol, ISO 4217 for fiat (``USD``) or the venue-neutral
            ticker for crypto (``BTC``). Codes are compared case sensitively; use
            the registry to normalise input.
        precision: Number of decimal places in the smallest representable unit.
        kind: Whether the currency is fiat, crypto, or a precious metal.
        name: Human readable name, used in logs and operator facing output.
    """

    code: str
    precision: int
    kind: CurrencyKind
    name: str

    def __post_init__(self) -> None:
        if not self.code:
            raise DomainError("Currency code must not be empty")
        if self.code != self.code.upper():
            raise DomainError(f"Currency code must be upper case, got {self.code!r}")
        if not self.code.isalnum():
            raise DomainError(f"Currency code must be alphanumeric, got {self.code!r}")
        if self.precision < 0:
            raise DomainError(f"{self.code}: precision must not be negative")
        if self.precision > _MAX_PRECISION:
            raise DomainError(
                f"{self.code}: precision {self.precision} exceeds the supported "
                f"maximum of {_MAX_PRECISION}"
            )
        if not self.name:
            raise DomainError(f"{self.code}: name must not be empty")

    def __str__(self) -> str:
        return self.code


# Reference data. Fiat precisions follow ISO 4217 minor units; crypto precisions follow
# the native smallest unit of each chain.
_SEED_CURRENCIES: Final[tuple[Currency, ...]] = (
    Currency("USD", 2, CurrencyKind.FIAT, "United States dollar"),
    Currency("EUR", 2, CurrencyKind.FIAT, "Euro"),
    Currency("GBP", 2, CurrencyKind.FIAT, "Pound sterling"),
    Currency("CHF", 2, CurrencyKind.FIAT, "Swiss franc"),
    Currency("CAD", 2, CurrencyKind.FIAT, "Canadian dollar"),
    Currency("AUD", 2, CurrencyKind.FIAT, "Australian dollar"),
    Currency("NZD", 2, CurrencyKind.FIAT, "New Zealand dollar"),
    Currency("SEK", 2, CurrencyKind.FIAT, "Swedish krona"),
    Currency("NOK", 2, CurrencyKind.FIAT, "Norwegian krone"),
    Currency("SGD", 2, CurrencyKind.FIAT, "Singapore dollar"),
    Currency("HKD", 2, CurrencyKind.FIAT, "Hong Kong dollar"),
    Currency("MXN", 2, CurrencyKind.FIAT, "Mexican peso"),
    Currency("ZAR", 2, CurrencyKind.FIAT, "South African rand"),
    Currency("TRY", 2, CurrencyKind.FIAT, "Turkish lira"),
    Currency("PLN", 2, CurrencyKind.FIAT, "Polish zloty"),
    Currency("CZK", 2, CurrencyKind.FIAT, "Czech koruna"),
    Currency("HUF", 2, CurrencyKind.FIAT, "Hungarian forint"),
    Currency("CNH", 2, CurrencyKind.FIAT, "Chinese yuan (offshore)"),
    Currency("JPY", 0, CurrencyKind.FIAT, "Japanese yen"),
    Currency("KRW", 0, CurrencyKind.FIAT, "South Korean won"),
    Currency("XAU", 6, CurrencyKind.METAL, "Gold troy ounce"),
    Currency("XAG", 6, CurrencyKind.METAL, "Silver troy ounce"),
    Currency("BTC", 8, CurrencyKind.CRYPTO, "Bitcoin"),
    Currency("ETH", 18, CurrencyKind.CRYPTO, "Ether"),
    Currency("SOL", 9, CurrencyKind.CRYPTO, "Solana"),
    Currency("XRP", 6, CurrencyKind.CRYPTO, "XRP"),
    Currency("ADA", 6, CurrencyKind.CRYPTO, "Cardano"),
    Currency("DOGE", 8, CurrencyKind.CRYPTO, "Dogecoin"),
    Currency("LTC", 8, CurrencyKind.CRYPTO, "Litecoin"),
    Currency("USDT", 6, CurrencyKind.CRYPTO, "Tether USD"),
    Currency("USDC", 6, CurrencyKind.CRYPTO, "USD Coin"),
)


@final
class CurrencyRegistry:
    """Lookup table mapping currency codes to definitions.

    The registry is mutable by design: venues list assets we cannot enumerate ahead
    of time. It refuses to silently redefine a code, because two conflicting
    precisions for the same asset would round money differently depending on which
    definition a call site happened to hold.
    """

    __slots__ = ("_by_code",)

    def __init__(self, currencies: Iterable[Currency] = ()) -> None:
        self._by_code: dict[str, Currency] = {}
        for currency in currencies:
            self.register(currency)

    def copy(self) -> CurrencyRegistry:
        """Return an independent registry holding the same definitions.

        Tests and per-venue overrides use this to avoid mutating the process-wide
        registry.
        """
        return CurrencyRegistry(self.all())

    def register(self, currency: Currency) -> Currency:
        """Add a currency definition.

        Re-registering an identical definition is a no-op so that repeated startup
        of the same components stays safe. Registering a conflicting definition for
        an existing code raises :class:`DomainError`.
        """
        existing = self._by_code.get(currency.code)
        if existing is not None and existing != currency:
            raise DomainError(
                f"Currency {currency.code} is already registered as {existing!r}; "
                f"refusing to redefine it as {currency!r}"
            )
        self._by_code[currency.code] = currency
        return currency

    def get(self, code: str) -> Currency:
        """Return the definition for ``code``, normalising case and surrounding space."""
        normalised = code.strip().upper()
        try:
            return self._by_code[normalised]
        except KeyError:
            raise UnknownCurrencyError(
                f"Currency {code!r} is not registered. Register it at startup with "
                f"CurrencyRegistry.register() before using it."
            ) from None

    def contains(self, code: str) -> bool:
        return code.strip().upper() in self._by_code

    def all(self) -> tuple[Currency, ...]:
        return tuple(self._by_code[code] for code in sorted(self._by_code))

    def __len__(self) -> int:
        return len(self._by_code)


default_registry: Final = CurrencyRegistry(_SEED_CURRENCIES)
"""Process-wide registry seeded with the reference data above."""
