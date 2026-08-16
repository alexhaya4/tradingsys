"""Bybit adapter: instrument metadata, market data, and execution.

Bybit's v5 API is one surface with a ``category`` on every call, and the categories are
not interchangeable. ``spot`` and ``linear`` differ in how a position is sized, what it
costs to hold, and whether it can be leveraged, so this package treats them as separate
instruments rather than as a flag on one.
"""

from tradingsys.venues.bybit.instruments import (
    VENUE,
    instrument_from_linear,
    instrument_from_spot,
    next_funding_time,
)

__all__ = [
    "VENUE",
    "instrument_from_linear",
    "instrument_from_spot",
    "next_funding_time",
]
