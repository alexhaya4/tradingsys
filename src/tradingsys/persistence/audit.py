"""The append-only audit log.

Every decision the system makes is recorded here: what was decided, by which
component, under which correlation ID, and with the inputs that led to it. The log is
the record of record for reconstructing why a position exists.

Two mechanisms make it trustworthy rather than merely well intentioned.

*Append only in the database.* A trigger raises on ``UPDATE`` and ``DELETE``, so a
mistake in application code cannot rewrite history.

*Hash chained.* Each entry carries the SHA-256 of its own canonical form together with
the previous entry's hash. Altering or removing any entry breaks every hash after it,
which :meth:`AuditLog.verify` detects. The chain is computed under a transaction level
advisory lock so that concurrent writers cannot interleave and produce two entries
claiming the same predecessor.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, final

from tradingsys.core.clock import ensure_utc
from tradingsys.core.errors import PersistenceError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import asyncpg

    from tradingsys.core.clock import Clock
    from tradingsys.core.instrument import InstrumentId
    from tradingsys.persistence.database import Database

__all__ = [
    "GENESIS_HASH",
    "AuditCategory",
    "AuditEntry",
    "AuditLog",
    "ChainReport",
    "canonical_json",
    "compute_entry_hash",
    "verify_chain",
]

GENESIS_HASH: Final = "0" * 64
"""Stands in for the previous hash of the very first entry."""

_FIELD_SEPARATOR: Final = "\x1f"
"""ASCII unit separator, used between hashed fields.

A character that cannot appear in JSON output or in an identifier, so no combination
of field values can be rearranged to produce the same hash input.
"""

AUDIT_LOCK_KEY: Final = zlib.crc32(b"tradingsys.audit_log")
"""Advisory lock key serialising audit appends.

Derived from the table name rather than chosen arbitrarily, so a second component that
needs an advisory lock will not collide with this one by accident.
"""


class AuditCategory(StrEnum):
    """What kind of decision an entry records."""

    SYSTEM = "system"
    """Process lifecycle: startup, shutdown, configuration loaded."""

    MARKET_DATA = "market_data"
    """A judgement about incoming data: a gap detected, a stale feed, a bad tick."""

    SIGNAL = "signal"
    """A strategy's view, before any risk or sizing decision."""

    RISK = "risk"
    """A risk decision, including refusals. A blocked order is a decision and is
    recorded as one."""

    ORDER = "order"
    """An order intent, submission, amendment, cancellation, or venue response."""

    POSITION = "position"
    """A change in exposure, including venue-initiated ones such as a stop out."""

    RECONCILIATION = "reconciliation"
    """A difference found between our state and a venue's, and what was done about it."""


@final
@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One immutable record.

    Attributes:
        sequence: Position in the chain, starting at 1 and contiguous. Assigned by
            the database at append time.
        ts: When the decision was made, UTC.
        correlation_id: Ties this entry to every log line and downstream decision from
            the same originating event.
        category: What kind of decision this is.
        actor: The component that decided, for example ``risk.position_sizer``.
        action: A short, stable verb phrase, for example ``rejected_order``. Stable
            enough to aggregate on.
        summary: A human readable sentence for an operator reading the log.
        payload: The inputs and outputs of the decision. Must be JSON serialisable
            and must never contain a secret.
        instrument_id: The instrument concerned, when there is exactly one.
        previous_hash: The preceding entry's hash, or :data:`GENESIS_HASH`.
        entry_hash: SHA-256 over this entry's canonical form.
    """

    sequence: int
    ts: datetime
    correlation_id: str
    category: AuditCategory
    actor: str
    action: str
    summary: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    instrument_id: str | None = None
    previous_hash: str = GENESIS_HASH
    entry_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts, what="audit timestamp"))
        if self.sequence < 1:
            raise PersistenceError(f"audit sequence must start at 1, got {self.sequence}")
        for name in ("correlation_id", "actor", "action", "summary"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise PersistenceError(f"audit entry {name} must not be empty")
        if len(self.previous_hash) != len(GENESIS_HASH):
            raise PersistenceError(
                f"previous_hash must be a 64 character hex digest, got {self.previous_hash!r}"
            )
        computed = compute_entry_hash(
            sequence=self.sequence,
            ts=self.ts,
            correlation_id=self.correlation_id,
            category=self.category,
            actor=self.actor,
            action=self.action,
            summary=self.summary,
            payload=self.payload,
            instrument_id=self.instrument_id,
            previous_hash=self.previous_hash,
        )
        if not self.entry_hash:
            object.__setattr__(self, "entry_hash", computed)
        elif self.entry_hash != computed:
            raise PersistenceError(
                f"audit entry {self.sequence} carries hash {self.entry_hash} but its contents "
                f"hash to {computed}; the entry has been altered"
            )

    def links_to(self, previous: AuditEntry) -> bool:
        """Whether this entry is the immediate successor of ``previous``."""
        return self.previous_hash == previous.entry_hash and self.sequence == previous.sequence + 1


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialise a payload to a byte-for-byte reproducible JSON string.

    Keys are sorted, separators are fixed, and non-JSON types are rendered through
    ``str``. Decimals therefore keep their exact decimal representation rather than
    being converted through float.

    Raises:
        PersistenceError: The payload cannot be serialised.
    """
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
        )
    except (TypeError, ValueError) as exc:
        raise PersistenceError(f"audit payload is not serialisable: {exc}") from exc


def compute_entry_hash(
    *,
    sequence: int,
    ts: datetime,
    correlation_id: str,
    category: AuditCategory | str,
    actor: str,
    action: str,
    summary: str,
    payload: Mapping[str, Any],
    instrument_id: str | None,
    previous_hash: str,
) -> str:
    """SHA-256 over an entry's canonical form.

    Every field that carries meaning is included, joined by a separator that cannot
    occur in any of them, so two different entries cannot produce the same digest by
    shifting content across a field boundary.
    """
    parts = (
        str(sequence),
        ensure_utc(ts, what="audit timestamp").isoformat(),
        correlation_id,
        str(category),
        actor,
        action,
        summary,
        instrument_id or "",
        canonical_json(payload),
        previous_hash,
    )
    joined = _FIELD_SEPARATOR.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@final
@dataclass(frozen=True, slots=True)
class ChainReport:
    """The result of verifying a range of the chain."""

    entries_checked: int
    first_sequence: int | None
    last_sequence: int | None
    broken_at: int | None = None
    reason: str | None = None

    @property
    def is_intact(self) -> bool:
        return self.broken_at is None

    def raise_if_broken(self) -> None:
        """Raise if the chain does not verify.

        Raises:
            PersistenceError: The chain is broken, with the sequence and the reason.
        """
        if self.broken_at is not None:
            raise PersistenceError(
                f"audit chain is broken at sequence {self.broken_at}: {self.reason}"
            )


def verify_chain(entries: Sequence[AuditEntry]) -> ChainReport:
    """Check that a contiguous run of entries forms an unbroken chain.

    Entries must be supplied in ascending sequence order. Construction of each
    :class:`AuditEntry` has already verified its own hash, so what is checked here is
    the linkage between them: contiguous sequence numbers and matching hashes.
    """
    if not entries:
        return ChainReport(entries_checked=0, first_sequence=None, last_sequence=None)
    first, last = entries[0], entries[-1]
    for previous, current in pairwise(entries):
        if current.sequence != previous.sequence + 1:
            return ChainReport(
                entries_checked=len(entries),
                first_sequence=first.sequence,
                last_sequence=last.sequence,
                broken_at=current.sequence,
                reason=(
                    f"sequence jumps from {previous.sequence} to {current.sequence}; "
                    f"an entry is missing"
                ),
            )
        if current.previous_hash != previous.entry_hash:
            return ChainReport(
                entries_checked=len(entries),
                first_sequence=first.sequence,
                last_sequence=last.sequence,
                broken_at=current.sequence,
                reason=(
                    f"previous_hash {current.previous_hash[:12]} does not match the hash of "
                    f"entry {previous.sequence} ({previous.entry_hash[:12]})"
                ),
            )
    return ChainReport(
        entries_checked=len(entries),
        first_sequence=first.sequence,
        last_sequence=last.sequence,
    )


_INSERT_SQL: Final = """
INSERT INTO audit_log (
    sequence, ts, correlation_id, category, actor, action, summary,
    instrument_id, payload, previous_hash, entry_hash
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

_SELECT_COLUMNS: Final = """
sequence, ts, correlation_id, category, actor, action, summary,
instrument_id, payload, previous_hash, entry_hash
"""


@final
class AuditLog:
    """Reads and appends to the audit log."""

    __slots__ = ("_clock", "_database")

    def __init__(self, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def append(
        self,
        *,
        correlation_id: str,
        category: AuditCategory,
        actor: str,
        action: str,
        summary: str,
        payload: Mapping[str, Any] | None = None,
        instrument_id: InstrumentId | str | None = None,
        ts: datetime | None = None,
    ) -> AuditEntry:
        """Record one decision and return the stored entry.

        The sequence number and previous hash are resolved inside a transaction that
        holds an advisory lock, so two concurrent callers produce two consecutive
        entries rather than a fork.

        Raises:
            PersistenceError: The entry could not be written.
        """
        moment = self._clock.now() if ts is None else ts
        instrument = None if instrument_id is None else str(instrument_id)
        body = dict(payload or {})
        async with self._database.transaction() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock($1)", AUDIT_LOCK_KEY)
            head = await connection.fetchrow(
                "SELECT sequence, entry_hash FROM audit_log ORDER BY sequence DESC LIMIT 1"
            )
            sequence = 1 if head is None else int(head["sequence"]) + 1
            previous_hash = GENESIS_HASH if head is None else str(head["entry_hash"])
            entry = AuditEntry(
                sequence=sequence,
                ts=moment,
                correlation_id=correlation_id,
                category=category,
                actor=actor,
                action=action,
                summary=summary,
                payload=body,
                instrument_id=instrument,
                previous_hash=previous_hash,
            )
            await connection.execute(
                _INSERT_SQL,
                entry.sequence,
                entry.ts,
                entry.correlation_id,
                entry.category.value,
                entry.actor,
                entry.action,
                entry.summary,
                entry.instrument_id,
                dict(entry.payload),
                entry.previous_hash,
                entry.entry_hash,
            )
        return entry

    async def head(self) -> AuditEntry | None:
        """The most recent entry, or ``None`` when the log is empty."""
        row = await self._database.fetchrow(
            f"SELECT {_SELECT_COLUMNS} FROM audit_log ORDER BY sequence DESC LIMIT 1"
        )
        return None if row is None else entry_from_row(row)

    async def read(
        self,
        *,
        start_sequence: int | None = None,
        end_sequence: int | None = None,
        correlation_id: str | None = None,
        category: AuditCategory | None = None,
        limit: int | None = None,
    ) -> Sequence[AuditEntry]:
        """Entries in ascending sequence order, filtered by the given criteria.

        The sequence range is inclusive at both ends, matching how an operator reads
        "entries 100 to 200".
        """
        conditions: list[str] = []
        args: list[object] = []
        if start_sequence is not None:
            args.append(start_sequence)
            conditions.append(f"sequence >= ${len(args)}")
        if end_sequence is not None:
            args.append(end_sequence)
            conditions.append(f"sequence <= ${len(args)}")
        if correlation_id is not None:
            args.append(correlation_id)
            conditions.append(f"correlation_id = ${len(args)}")
        if category is not None:
            args.append(category.value)
            conditions.append(f"category = ${len(args)}")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        suffix = ""
        if limit is not None:
            args.append(limit)
            suffix = f" LIMIT ${len(args)}"
        query = f"SELECT {_SELECT_COLUMNS} FROM audit_log{where} ORDER BY sequence ASC{suffix}"
        rows = await self._database.fetch(query, *args)
        return tuple(entry_from_row(row) for row in rows)

    async def verify(
        self, *, start_sequence: int | None = None, end_sequence: int | None = None
    ) -> ChainReport:
        """Recompute and check the hash chain over a range.

        Verifying from the beginning proves the whole log. Verifying a later range
        proves it internally consistent, and links it to the rest only if the entry
        before ``start_sequence`` is included, so callers checking a tail should start
        one entry earlier.
        """
        entries = await self.read(start_sequence=start_sequence, end_sequence=end_sequence)
        return verify_chain(entries)

    async def count(self) -> int:
        """Number of entries in the log."""
        value = await self._database.fetchval("SELECT count(*) FROM audit_log")
        return int(value or 0)


def entry_from_row(row: asyncpg.Record) -> AuditEntry:
    """Rebuild an entry from a database row, re-verifying its hash.

    Because :class:`AuditEntry` recomputes the digest on construction, reading an entry
    that was tampered with in place raises here rather than returning quietly.

    Raises:
        PersistenceError: The stored hash does not match the stored contents.
    """
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return AuditEntry(
        sequence=int(row["sequence"]),
        ts=row["ts"],
        correlation_id=str(row["correlation_id"]),
        category=AuditCategory(row["category"]),
        actor=str(row["actor"]),
        action=str(row["action"]),
        summary=str(row["summary"]),
        payload=payload or {},
        instrument_id=row["instrument_id"],
        previous_hash=str(row["previous_hash"]),
        entry_hash=str(row["entry_hash"]),
    )
