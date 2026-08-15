"""Rounding modes, named so that call sites state their intent.

Direction matters in trading. Rounding a quantity up through an exchange maximum,
or a price up through a limit, turns a valid order into a rejected one, so every
rounding call in this package names the mode it wants rather than inheriting the
ambient decimal context.
"""

from __future__ import annotations

import decimal
from enum import StrEnum
from typing import final

__all__ = ["Rounding"]


@final
class Rounding(StrEnum):
    """Rounding modes, mapped onto the stdlib decimal constants.

    Members are the stdlib strings themselves, so ``Rounding.HALF_EVEN.value`` is a
    valid ``rounding=`` argument to :meth:`decimal.Decimal.quantize` and the enum
    serialises to a stable, readable value in config and audit records.
    """

    HALF_EVEN = decimal.ROUND_HALF_EVEN
    """Banker's rounding. The default for monetary display: unbiased over many values."""

    HALF_UP = decimal.ROUND_HALF_UP
    """Round halves away from zero. Used where a venue documents this behaviour."""

    DOWN = decimal.ROUND_DOWN
    """Truncate toward zero. The safe default for order quantities: never exceeds
    the requested size, never breaches a maximum."""

    UP = decimal.ROUND_UP
    """Round away from zero. Used for margin and fee estimates, where understating
    the requirement is the dangerous direction."""

    FLOOR = decimal.ROUND_FLOOR
    """Round toward negative infinity."""

    CEILING = decimal.ROUND_CEILING
    """Round toward positive infinity."""
