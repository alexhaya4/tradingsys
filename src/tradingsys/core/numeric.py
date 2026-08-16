"""Exact numeric helpers shared by the money and instrument types.

Everything in the trading domain that can be counted, priced, or charged is a
:class:`~decimal.Decimal`. This module holds the conversion gate that keeps floats
out and the working precision used for multiplication and division.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Final

from tradingsys.core.errors import DomainError

__all__ = [
    "ARITHMETIC_PRECISION",
    "Numeric",
    "decimal_from_double",
    "exact_context",
    "from_binary32",
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


def from_binary32(value: float, *, what: str = "value") -> Decimal:
    """The exact decimal value of a number that arrived on the wire as IEEE-754 binary32.

    This is the one sanctioned float to Decimal door in the system, and it exists for a
    narrow case: a venue that publishes a field as 32 bit binary, as Dukascopy does for
    tick volumes. There the float is not an approximation of some truer decimal number,
    it *is* the value the source published, and its exact expansion is therefore the
    faithful record rather than a lossy one. ``Decimal(0.9)`` looks alarming at
    0.89999997615814208984375 but converts back to the identical 32 bit pattern, which
    is what "bit exact against what the venue reported" means here.

    Rounding to something prettier is the lossy option, not the safe one, because the
    rounded value no longer round trips and the discrepancy can never be recovered.

    Args:
        value: A float that came from unpacking four bytes of binary32.
        what: Name of the value, used in error messages.

    Raises:
        TypeError: ``value`` is not a float, so it did not come from a binary32 field.
        DomainError: ``value`` is NaN, infinite, or is a double that no binary32 can
            represent, which means it did not come from where the caller believes.
    """
    if isinstance(value, bool) or not isinstance(value, float):
        raise TypeError(
            f"{what} must be a float unpacked from a binary32 field, got "
            f"{type(value).__name__}: {value!r}. Values that are already exact belong "
            f"in to_decimal."
        )
    if not math.isfinite(value):
        raise DomainError(f"{what} must be finite, got {value!r}")
    try:
        round_tripped: float = struct.unpack(">f", struct.pack(">f", value))[0]
    except OverflowError:
        raise DomainError(f"{what} is outside the binary32 range: {value!r}") from None
    if round_tripped != value:
        raise DomainError(
            f"{what} is not exactly representable in binary32: {value!r}. A double that "
            f"survives no 32 bit round trip did not come from a binary32 field, so "
            f"converting it here would record a precision the source never had."
        )
    return Decimal(value)


def decimal_from_double(value: float, *, what: str = "value") -> Decimal:
    """The decimal a venue meant when it published a value as an IEEE-754 double.

    The second and last sanctioned float to Decimal door, and it is deliberately not
    the same conversion as :func:`from_binary32`. The distinction is what the source
    intended, not how it was transported.

    :func:`from_binary32` is for a field whose value genuinely *is* binary, such as a
    Dukascopy tick volume, where the exact expansion is the faithful record and
    rounding would lose a value that can never be recovered.

    This is for a field the venue means as a decimal number and happens to transport
    as a double, such as a cTrader swap rate of -1.2 pips. Its exact expansion is
    -1.1999999999999999555910790149937383830547332763671875, which is not a swap rate
    anyone quoted, and recording it would invent nineteen digits of precision the
    broker never published. The shortest decimal that maps back to the identical
    double is the value that was meant, and Python's ``repr`` produces exactly that.

    Neither conversion is safe in the other's place. Using this one on a binary field
    discards real precision; using the other on a quoted decimal fabricates it.

    Args:
        value: A float taken from a protobuf ``double`` or an equivalent field.
        what: Name of the value, used in error messages.

    Raises:
        TypeError: ``value`` is not a float, so it did not come from a double field.
        DomainError: ``value`` is NaN or infinite.
    """
    if isinstance(value, bool) or not isinstance(value, float):
        raise TypeError(
            f"{what} must be a float taken from a double field, got "
            f"{type(value).__name__}: {value!r}. Values that are already exact belong "
            f"in to_decimal."
        )
    if not math.isfinite(value):
        raise DomainError(f"{what} must be finite, got {value!r}")
    return Decimal(repr(value))
