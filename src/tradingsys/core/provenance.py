"""Where a price came from, and what it may therefore be used for.

The system reads price history from two kinds of place, and conflating them would
quietly corrupt the thing the whole project turns on.

An **execution venue** is somewhere we will actually send orders. Its quotes are the
prices we will really pay, so its spread is the only spread a cost model may be
calibrated on.

A **research source** is a different liquidity pool. Dukascopy publishes years of
per-side tick history for free, which is exactly what signal research and walk-forward
need, but Dukascopy is not Pepperstone: its spreads are its own. Calibrating a cost
model on them would produce a backtest that is wrong in a direction nobody would
notice, because the numbers would look entirely reasonable.

The separation is enforced by type rather than by convention. Calibration inputs are
:class:`CalibrationTicks`, which cannot be constructed around research data, so the
mistake is not available to make. A comment saying "do not mix" is not enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Self, final

from tradingsys.core.errors import DomainError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.core.instrument import InstrumentId

__all__ = [
    "CalibrationTicks",
    "DataProvenance",
    "ProvenanceError",
    "TickSource",
]


class ProvenanceError(DomainError):
    """Data was used for something its provenance does not permit.

    Raised when research data reaches a path reserved for execution venue data. This
    is a programming error rather than a runtime condition, and it is loud on purpose.
    """


class DataProvenance(StrEnum):
    """What a source's prices may be used for."""

    EXECUTION_VENUE = "execution_venue"
    """Quotes from a venue we trade on. The spread here is the spread we will pay, so
    this is the only provenance a cost model may be calibrated on."""

    RESEARCH_ONLY = "research_only"
    """Quotes from a different liquidity pool. Sound for signal research and
    walk-forward, where depth of history is what matters, and unusable for cost
    calibration, where whose spread it is matters."""


@final
class TickSource(StrEnum):
    """Where a stored tick came from.

    Recorded on every tick row. The value is what makes the provenance rule checkable
    after the fact as well as at write time: given any row in the database, it is
    possible to say what it may be used for.
    """

    CTRADER = "ctrader"
    """The forex execution venue, cTrader Open API via Pepperstone."""

    BYBIT = "bybit"
    """The crypto execution venue."""

    DUKASCOPY = "dukascopy"
    """Free historical forex tick data, used for deep history. A different liquidity
    pool from Pepperstone, so research only."""

    @property
    def provenance(self) -> DataProvenance:
        return _PROVENANCE[self]

    @property
    def is_execution_venue(self) -> bool:
        return self.provenance is DataProvenance.EXECUTION_VENUE

    @classmethod
    def execution_venues(cls) -> tuple[TickSource, ...]:
        """Every source whose data may be calibrated on, for use in SQL filters."""
        return tuple(source for source in cls if source.is_execution_venue)

    @classmethod
    def research_sources(cls) -> tuple[TickSource, ...]:
        return tuple(source for source in cls if not source.is_execution_venue)


_PROVENANCE: dict[TickSource, DataProvenance] = {
    TickSource.CTRADER: DataProvenance.EXECUTION_VENUE,
    TickSource.BYBIT: DataProvenance.EXECUTION_VENUE,
    TickSource.DUKASCOPY: DataProvenance.RESEARCH_ONLY,
}
"""Every source is classified explicitly.

A mapping rather than a default, so that adding a source without deciding what it may
be used for fails at import rather than silently inheriting a permissive answer.
"""

if set(_PROVENANCE) != set(TickSource):  # pragma: no cover - checked at import
    missing = sorted(source.value for source in TickSource if source not in _PROVENANCE)
    raise ProvenanceError(
        f"these tick sources have no declared provenance: {missing}. Every source must "
        f"say whether it may be used for cost calibration."
    )


@final
@dataclass(frozen=True, slots=True)
class CalibrationTicks[TickT]:
    """Ticks that a cost model is permitted to calibrate on.

    The type is the enforcement. A cost model takes this rather than a bare sequence of
    quotes, and this cannot be constructed around research data, so calibrating on
    Dukascopy prices is not an error to be caught in review: it does not typecheck, and
    if the type is bypassed it raises here.

    Generic in the tick type so that this module stays inside the core layer and does
    not import the venue value objects, which import core themselves. Call sites write
    ``CalibrationTicks[Quote]`` and get the concrete type back.

    Attributes:
        instrument_id: The instrument these ticks price.
        source: Which execution venue they came from.
        ticks: The quotes, in ascending time order.
    """

    instrument_id: InstrumentId
    source: TickSource
    ticks: tuple[TickT, ...]

    def __post_init__(self) -> None:
        if not self.source.is_execution_venue:
            raise ProvenanceError(
                f"cannot calibrate a cost model on {self.source.value} data: it is "
                f"{self.source.provenance.value}. {self.source.value} is a different "
                f"liquidity pool from the venue we trade on, so its spreads are not the "
                f"spreads we will pay. Use data from one of "
                f"{[item.value for item in TickSource.execution_venues()]}."
            )

    @classmethod
    def of(cls, instrument_id: InstrumentId, source: TickSource, ticks: Sequence[TickT]) -> Self:
        """Build a calibration series, refusing research sources."""
        return cls(instrument_id=instrument_id, source=source, ticks=tuple(ticks))

    def __len__(self) -> int:
        return len(self.ticks)
