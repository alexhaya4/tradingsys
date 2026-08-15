"""Exact monetary amounts.

:class:`Money` is a :class:`~decimal.Decimal` amount bound to a
:class:`~tradingsys.core.currency.Currency`. Three rules make it safe to use in an
execution path:

1. Binary floats are rejected at construction. ``Money.of(0.1, USD)`` raises rather
   than silently storing 0.1000000000000000055511151231257827.
2. Adding, subtracting, or comparing two amounts of different currencies raises
   :class:`~tradingsys.core.errors.CurrencyMismatchError`. There is no implicit
   conversion, because a conversion needs a rate and a rate needs a timestamp.
3. Amounts are held at full precision and rounded only when asked. Rounding at every
   intermediate step accumulates error across a day of position sizing;
   :meth:`Money.quantize` exists for the moment a number leaves the system.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import final

from tradingsys.core.currency import Currency
from tradingsys.core.errors import CurrencyMismatchError, DomainError
from tradingsys.core.numeric import Numeric, exact_context, to_decimal
from tradingsys.core.rounding import Rounding

__all__ = ["Money"]


@final
@dataclass(frozen=True, slots=True, order=False, eq=False)
class Money:
    """An exact amount of a single currency."""

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.currency, Currency):
            raise TypeError(f"currency must be a Currency, got {type(self.currency).__name__}")
        object.__setattr__(self, "amount", to_decimal(self.amount, what="amount"))

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    @classmethod
    def of(cls, amount: Numeric, currency: Currency) -> Money:
        """Build an amount from a Decimal, int, or decimal string."""
        return cls(to_decimal(amount, what="amount"), currency)

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        return cls(Decimal(0), currency)

    def with_amount(self, amount: Numeric) -> Money:
        """Return a new amount of the same currency."""
        return Money(to_decimal(amount, what="amount"), self.currency)

    # ------------------------------------------------------------------
    # currency safety
    # ------------------------------------------------------------------

    def _require_same_currency(self, other: Money, operation: str) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"Cannot {operation} {self.currency.code} and {other.currency.code}. "
                f"Convert one side explicitly with a dated exchange rate first."
            )

    # ------------------------------------------------------------------
    # arithmetic
    # ------------------------------------------------------------------

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "add")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "subtract")
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: Numeric) -> Money:
        multiplier = to_decimal(factor, what="factor")
        with exact_context():
            return Money(self.amount * multiplier, self.currency)

    def __rmul__(self, factor: Numeric) -> Money:
        return self.__mul__(factor)

    def __truediv__(self, divisor: Numeric) -> Money:
        quotient = to_decimal(divisor, what="divisor")
        if quotient == 0:
            raise DomainError(f"Cannot divide {self!r} by zero")
        with exact_context():
            return Money(self.amount / quotient, self.currency)

    def ratio_to(self, other: Money) -> Decimal:
        """Return ``self / other`` as a dimensionless Decimal.

        Money divided by money is a ratio, not money, so this is a named method
        rather than an overload of ``/``.
        """
        self._require_same_currency(other, "divide")
        if other.amount == 0:
            raise DomainError(f"Cannot divide {self!r} by a zero amount")
        with exact_context():
            return self.amount / other.amount

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __pos__(self) -> Money:
        return self

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    # ------------------------------------------------------------------
    # comparison
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            return False
        return self.amount == other.amount

    def __hash__(self) -> int:
        return hash((self.currency.code, self.amount.normalize()))

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self.amount >= other.amount

    # ------------------------------------------------------------------
    # rounding and inspection
    # ------------------------------------------------------------------

    def quantize(self, rounding: Rounding = Rounding.HALF_EVEN) -> Money:
        """Round to the currency's smallest unit.

        Call this when an amount leaves the system (an order payload, a report, a
        fixed scale database column), not between calculation steps.
        """
        return self.quantize_to(self.currency.precision, rounding)

    def quantize_to(self, decimal_places: int, rounding: Rounding = Rounding.HALF_EVEN) -> Money:
        """Round to an explicit number of decimal places."""
        if decimal_places < 0:
            raise DomainError(f"decimal_places must not be negative, got {decimal_places}")
        exponent = Decimal(1).scaleb(-decimal_places)
        with exact_context():
            return Money(self.amount.quantize(exponent, rounding=rounding.value), self.currency)

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    def __str__(self) -> str:
        return f"{self.quantize().amount} {self.currency.code}"

    def __repr__(self) -> str:
        return f"Money({str(self.amount)!r}, {self.currency.code})"
