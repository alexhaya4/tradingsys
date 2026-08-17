"""Populating the instrument registry from venue metadata, and noticing when it moves.

Storing definitions is the easy half and the repository already does it. The half that
matters is the comparison: what the venue says now against what was stored last time.

**A changed definition is an event, not a detail.** A broker that alters a minimum lot
changes which instruments are tradeable at a given balance, and a venue that changes a
tick size changes every price this system has stored for it. Overwriting silently is
how a system ends up sized against a rule that stopped applying, so every field that
moved is reported and audited by field name, old value, and new value.

This is also the only automated place venue drift can be caught. `PROGRESS.md` records
that CI cannot see it, because CI holds no venue credentials, and that the manual
control is `scripts/check_venue_assumptions.py`. A sync that runs in the system, on
real metadata, and reports what changed is the automated half of that control.

**An instrument that vanishes from a venue is never deleted.** Bars and ticks reference
it, and a definition removed from under recorded data leaves that data unreadable. It is
reported as absent and left in place; retiring one is a decision, not a side effect of a
refresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, final

from tradingsys.core.instrument import Instrument
from tradingsys.observability.correlation import correlation_id as correlation_scope
from tradingsys.observability.logging import get_logger
from tradingsys.persistence.audit import AuditCategory

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tradingsys.core.instrument import InstrumentId
    from tradingsys.persistence.audit import AuditLog
    from tradingsys.persistence.repositories import InstrumentRepository

__all__ = [
    "FieldChange",
    "InstrumentSource",
    "RegistrySync",
    "SyncReport",
]

logger = get_logger("marketdata.registry")

COMPARED_FIELDS: tuple[str, ...] = (
    "venue_symbol",
    "asset_class",
    "price_increment",
    "price_precision",
    "pip_size",
    "quantity_unit",
    "contract_size",
    "quantity_increment",
    "min_quantity",
    "max_quantity",
    "min_notional",
    "max_leverage",
    "status",
)
"""Fields whose movement is worth an alert.

Deliberately not every field. Currencies and identifiers are identity rather than
configuration: an instrument whose base currency changed is a different instrument, and
would arrive as an addition rather than a change. Financing and schedule are excluded
because they carry nested values that change shape rather than value, and comparing them
by equality would report a difference on every sync without saying what moved.
"""


class InstrumentSource(Protocol):
    """Somewhere instrument definitions come from.

    One per venue. Narrow enough that a test can supply a source returning whatever it
    likes, and wide enough that the sync needs no venue vocabulary at all.
    """

    @property
    def venue(self) -> str:
        """Identifier of the venue these definitions describe."""
        ...

    async def instruments(self) -> Sequence[Instrument]:
        """Every definition this venue currently publishes for the configured universe.

        Must raise rather than return a partial list. A source that swallows a failure
        and returns what it managed to fetch would look identical to a venue that had
        delisted the rest.
        """
        ...


@final
@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field of one instrument, moved.

    Attributes:
        instrument_id: Which instrument.
        field_name: Which field.
        before: The stored value, rendered.
        after: The venue's current value, rendered.
    """

    instrument_id: InstrumentId
    field_name: str
    before: str
    after: str

    def __str__(self) -> str:
        return f"{self.instrument_id}.{self.field_name}: {self.before} -> {self.after}"


@final
@dataclass(slots=True)
class SyncReport:
    """What one sync found.

    Attributes:
        added: Instruments the venue publishes that were not stored.
        changed: Fields that moved, one entry per field rather than per instrument, so
            an alert names the thing that changed.
        unchanged: Instruments whose compared fields all matched.
        absent: Instruments stored for this venue that it no longer publishes. Left in
            place, never deleted.
    """

    venue: str
    added: list[InstrumentId] = field(default_factory=list)
    changed: list[FieldChange] = field(default_factory=list)
    unchanged: list[InstrumentId] = field(default_factory=list)
    absent: list[InstrumentId] = field(default_factory=list)

    @property
    def moved(self) -> bool:
        """Whether anything changed. A quiet sync is the expected case."""
        return bool(self.added or self.changed or self.absent)

    def summary(self) -> str:
        return (
            f"{self.venue}: {len(self.added)} added, {len(self.changed)} fields changed, "
            f"{len(self.unchanged)} unchanged, {len(self.absent)} absent"
        )


@final
class RegistrySync:
    """Refreshes stored instrument definitions from their venues."""

    __slots__ = ("_audit", "_repository")

    def __init__(self, repository: InstrumentRepository, *, audit: AuditLog | None = None) -> None:
        self._repository = repository
        self._audit = audit

    async def sync(
        self, source: InstrumentSource, *, correlation_id: str | None = None
    ) -> SyncReport:
        """Fetch a venue's definitions, compare them to what is stored, and store them.

        The comparison happens before the write, because after the write there is
        nothing left to compare against. That ordering is the whole reason this is a
        component rather than a call to ``upsert_many``.

        Args:
            source: The venue to refresh from.
            correlation_id: Trace id for the audit entries. One is generated when the
                caller has none.

        Returns:
            What moved. A report with :attr:`SyncReport.moved` false means the venue's
            definitions match what was already stored.
        """
        published = await source.instruments()
        report = SyncReport(venue=source.venue)

        stored = {item.id: item for item in await self._repository.list_for_venue(source.venue)}
        seen: set[InstrumentId] = set()

        for instrument in published:
            seen.add(instrument.id)
            previous = stored.get(instrument.id)
            if previous is None:
                report.added.append(instrument.id)
                continue
            changes = _differences(previous, instrument)
            if changes:
                report.changed.extend(changes)
            else:
                report.unchanged.append(instrument.id)

        report.absent = sorted(key for key in stored if key not in seen)

        # Written after the comparison and in one transaction, so a partial refresh
        # never lands: either every definition is current or none of them changed.
        await self._repository.upsert_many(published)

        await self._record(report, correlation_id)
        return report

    async def _record(self, report: SyncReport, correlation_id: str | None) -> None:
        """Log, and audit anything that moved.

        A quiet sync is not audited. The audit log is for decisions and changes, and an
        entry saying nothing happened, written every refresh, buries the ones that say
        something did.
        """
        logger.info(
            "instrument registry synced",
            venue=report.venue,
            added=len(report.added),
            changed=len(report.changed),
            unchanged=len(report.unchanged),
            absent=len(report.absent),
        )
        for change in report.changed:
            logger.warning(
                "venue changed an instrument definition",
                venue=report.venue,
                instrument=str(change.instrument_id),
                field=change.field_name,
                before=change.before,
                after=change.after,
            )
        for missing in report.absent:
            logger.warning(
                "instrument is stored but no longer published by its venue",
                venue=report.venue,
                instrument=str(missing),
            )

        if self._audit is None or not report.moved:
            return

        with correlation_scope(correlation_id) as trace:
            await self._audit.append(
                correlation_id=trace,
                category=AuditCategory.SYSTEM,
                actor="marketdata.registry",
                action="instruments_synced",
                summary=report.summary(),
                payload={
                    "venue": report.venue,
                    "added": [str(item) for item in report.added],
                    "changed": [str(change) for change in report.changed],
                    "absent": [str(item) for item in report.absent],
                },
            )


def _differences(before: Instrument, after: Instrument) -> list[FieldChange]:
    """Fields of :data:`COMPARED_FIELDS` whose value moved between two definitions."""
    changes: list[FieldChange] = []
    for name in COMPARED_FIELDS:
        old = getattr(before, name)
        new = getattr(after, name)
        if _equal(old, new):
            continue
        changes.append(
            FieldChange(
                instrument_id=after.id,
                field_name=name,
                before=_render(old),
                after=_render(new),
            )
        )
    return changes


def _equal(left: object, right: object) -> bool:
    """Compare two field values, treating numerically equal Decimals as unchanged.

    ``Decimal("1.0")`` and ``Decimal("1.00")`` are the same size and a different number
    of trailing zeros. A venue that re-renders its own metadata would otherwise report a
    change on every field it touched, and an alert that fires without a cause stops being
    read.
    """
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return left.compare(right) == 0
    return bool(left == right)


def _render(value: object) -> str:
    return "none" if value is None else str(value)


async def sync_all(
    sync: RegistrySync, sources: Iterable[InstrumentSource]
) -> tuple[SyncReport, ...]:
    """Refresh several venues, reporting each.

    A failure at one venue does not silently skip the others: it propagates, because a
    registry refreshed for one venue and not another is a state nothing downstream can
    reason about, and it is better to fail the refresh than to half apply it.
    """
    return tuple([await sync.sync(source) for source in sources])
