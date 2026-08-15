"""Exact numeric helpers shared by the money and instrument types.

Everything in the trading domain that can be counted, priced, or charged is a
:class:`~decimal.Decimal`. This module holds the conversion gate that keeps floats
out and the working precision used for multiplication and division.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Final

from tradingsys.core.errors import DomainError

__all__ = [
    "ARITHMETIC_PRECISION",
    "Numeric",
    "exact_context",
    "to_decimal",
]

ARITHMETIC_PRECISION: Final = 34
"""Significant digits for intermediate multiplication and division.

Wide enough that an 18 decimal crypto quantity multiplied by a price keeps every
meaningful digit, and wider than any notional we will trade.
"""

type Numeric = Decimal | int | str
"""Types accepted where an exact number is required. ``float`` is deliberately absent."""


@contextmanager
def exact_context() -> Iterator[Context]:
    """Run a block at :data:`ARITHMETIC_PRECISION` without touching global state."""
    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION
        yield ctx


def to_decimal(value: object, *, what: str = "value") -> Decimal:
    """Convert ``value`` to an exact, finite Decimal.

    Accepts Decimal, int, and decimal strings. Floats are rejected outright: the
    static type of every numeric field in this package excludes ``float``, and this
    runtime check catches values arriving from untyped sources such as JSON payloads
    and third party clients.

    Args:
        value: The value to convert.
        what: Name of the value, used in error messages.

    Raises:
        TypeError: ``value`` is a float or an otherwise unsupported type.
        DomainError: ``value`` is unparseable, NaN, or infinite.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{what} must not be a float: binary floats cannot represent decimal prices "
            f"exactly. Pass a Decimal, int, or str instead of {value!r}."
        )
    if isinstance(value, bool):
        raise TypeError(f"{what} must be a number, got a bool: {value!r}")
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int | str):
        try:
            decimal_value = Decimal(value)
        except InvalidOperation:
            raise DomainError(f"{what} is not a valid decimal number: {value!r}") from None
    else:
        raise TypeError(
            f"{what} must be a Decimal, int, or str, got {type(value).__name__}: {value!r}"
        )
    if not decimal_value.is_finite():
        raise DomainError(f"{what} must be finite, got {value!r}")
    return decimal_value
