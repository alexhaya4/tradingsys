"""Unit tests for the audit log's hashing and chain verification.

These cover the parts that are pure: canonical serialisation, the digest, and chain
linkage. The database behaviour (append-only enforcement, sequence assignment under
concurrency) is exercised in test_integration.py against a real PostgreSQL.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tradingsys.core.errors import PersistenceError
from tradingsys.persistence.audit import (
    GENESIS_HASH,
    AuditCategory,
    AuditEntry,
    canonical_json,
    compute_entry_hash,
    verify_chain,
)

NOW = datetime(2025, 3, 5, 12, 0, tzinfo=UTC)


def entry(sequence: int = 1, previous_hash: str = GENESIS_HASH, **overrides: Any) -> AuditEntry:
    defaults: dict[str, Any] = {
        "sequence": sequence,
        "ts": NOW,
        "correlation_id": "corr-1",
        "category": AuditCategory.RISK,
        "actor": "risk.position_sizer",
        "action": "rejected_order",
        "summary": "position would exceed the per-instrument exposure limit",
        "payload": {"limit": "10000", "requested": "12500"},
        "instrument_id": "fxbroker:EUR/USD",
        "previous_hash": previous_hash,
    }
    defaults.update(overrides)
    return AuditEntry(**defaults)


def chain(length: int) -> list[AuditEntry]:
    """A valid chain of ``length`` entries."""
    entries: list[AuditEntry] = []
    previous = GENESIS_HASH
    for index in range(1, length + 1):
        current = entry(
            sequence=index,
            previous_hash=previous,
            ts=NOW + timedelta(seconds=index),
            summary=f"decision {index}",
        )
        entries.append(current)
        previous = current.entry_hash
    return entries


class TestCanonicalJson:
    def test_key_order_does_not_change_the_output(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_output_is_compact(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_decimals_keep_their_exact_representation(self) -> None:
        rendered = canonical_json({"price": Decimal("1.08500")})
        assert rendered == '{"price":"1.08500"}'

    def test_nested_structures_are_sorted_throughout(self) -> None:
        first = canonical_json({"outer": {"z": 1, "a": 2}})
        second = canonical_json({"outer": {"a": 2, "z": 1}})
        assert first == second

    def test_non_ascii_is_preserved(self) -> None:
        assert canonical_json({"note": "café"}) == '{"note":"café"}'

    def test_unserialisable_payloads_are_rendered_through_str(self) -> None:
        # default=str keeps the log writable rather than losing a decision to a
        # serialisation error at the worst moment.
        assert canonical_json({"when": NOW}) == '{"when":"2025-03-05 12:00:00+00:00"}'

    def test_circular_payloads_are_reported(self) -> None:
        payload: dict[str, object] = {}
        payload["self"] = payload
        with pytest.raises(PersistenceError, match="not serialisable"):
            canonical_json(payload)


class TestTheDigestIsPinned:
    """The one test here that is not self-referential.

    Every other hash test compares one output of ``compute_entry_hash`` against
    another, so all of them would keep passing if the canonical form changed: reorder
    the fields, swap the separator, drop the timestamp, and the chain stays internally
    consistent while every previously written digest becomes unverifiable.

    This pins the algorithm to a value computed once and written down. If it fails, the
    hash input changed, and either that was deliberate, in which case every stored
    audit log needs rehashing and a migration, or it was an accident.
    """

    GOLDEN = "dc599ee5046a3fb52f02774ffb510f273eda76d22bb25f2f8ec1b2e94af14d62"

    def test_a_known_entry_hashes_to_a_known_digest(self) -> None:
        digest = compute_entry_hash(
            sequence=1,
            ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            correlation_id="fixed-correlation",
            category=AuditCategory.RISK,
            actor="risk.position_sizer",
            action="rejected_order",
            summary="exposure limit exceeded",
            payload={"limit": "10000", "requested": "12500"},
            instrument_id="fxbroker:EUR/USD",
            previous_hash=GENESIS_HASH,
        )
        assert digest == self.GOLDEN

    def test_the_same_entry_built_through_the_dataclass_agrees(self) -> None:
        record = AuditEntry(
            sequence=1,
            ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            correlation_id="fixed-correlation",
            category=AuditCategory.RISK,
            actor="risk.position_sizer",
            action="rejected_order",
            summary="exposure limit exceeded",
            payload={"limit": "10000", "requested": "12500"},
            instrument_id="fxbroker:EUR/USD",
        )
        assert record.entry_hash == self.GOLDEN


class TestEntryHash:
    def test_is_deterministic(self) -> None:
        assert entry().entry_hash == entry().entry_hash

    def test_is_a_sha256_hex_digest(self) -> None:
        digest = entry().entry_hash
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("sequence", 2),
            ("ts", NOW + timedelta(seconds=1)),
            ("correlation_id", "corr-2"),
            ("category", AuditCategory.ORDER),
            ("actor", "risk.other"),
            ("action", "accepted_order"),
            ("summary", "something else"),
            ("payload", {"limit": "10000", "requested": "12501"}),
            ("instrument_id", "fxbroker:GBP/USD"),
            ("previous_hash", "a" * 64),
        ],
    )
    def test_every_field_changes_the_hash(self, field_name: str, value: Any) -> None:
        changed: dict[str, Any] = {field_name: value}
        assert entry().entry_hash != entry(**changed).entry_hash

    def test_field_contents_cannot_be_shifted_across_a_boundary(self) -> None:
        # Concatenating without a separator, these two would hash identical input.
        # The unit separator between fields is what keeps them distinct.
        def digest(actor: str, action: str) -> str:
            return compute_entry_hash(
                sequence=1,
                ts=NOW,
                correlation_id="corr-1",
                category=AuditCategory.RISK,
                actor=actor,
                action=action,
                summary="summary",
                payload={},
                instrument_id=None,
                previous_hash=GENESIS_HASH,
            )

        assert digest("risk", "sized") != digest("risksized", "")
        assert digest("ri", "sksized") != digest("risksized", "")

    def test_payload_key_order_does_not_change_the_hash(self) -> None:
        first = entry(payload={"a": 1, "b": 2})
        second = entry(payload={"b": 2, "a": 1})
        assert first.entry_hash == second.entry_hash

    def test_timestamp_offset_does_not_change_the_hash(self) -> None:
        tokyo = NOW.astimezone(ZoneInfo("Asia/Tokyo"))
        assert entry(ts=tokyo).entry_hash == entry().entry_hash


class TestEntryValidation:
    def test_hash_is_computed_when_absent(self) -> None:
        assert entry().entry_hash

    def test_a_matching_supplied_hash_is_accepted(self) -> None:
        original = entry()
        rebuilt = entry(entry_hash=original.entry_hash)
        assert rebuilt == original

    def test_a_tampered_entry_is_rejected_on_construction(self) -> None:
        original = entry()
        with pytest.raises(PersistenceError, match="has been altered"):
            entry(summary="a different story", entry_hash=original.entry_hash)

    def test_a_tampered_payload_is_rejected(self) -> None:
        original = entry()
        with pytest.raises(PersistenceError, match="has been altered"):
            entry(payload={"limit": "999999"}, entry_hash=original.entry_hash)

    def test_sequence_starts_at_one(self) -> None:
        with pytest.raises(PersistenceError, match="must start at 1"):
            entry(sequence=0)

    @pytest.mark.parametrize("field_name", ["correlation_id", "actor", "action", "summary"])
    def test_required_text_must_not_be_blank(self, field_name: str) -> None:
        blank: dict[str, Any] = {field_name: "   "}
        with pytest.raises(PersistenceError, match=f"{field_name} must not be empty"):
            entry(**blank)

    def test_previous_hash_must_be_a_digest(self) -> None:
        with pytest.raises(PersistenceError, match="64 character hex digest"):
            entry(previous_hash="short")

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(Exception, match="timezone aware"):
            entry(ts=datetime(2025, 3, 5, 12, 0))  # noqa: DTZ001

    def test_entries_are_frozen(self) -> None:
        record = entry()
        with pytest.raises(AttributeError):
            record.summary = "changed"  # type: ignore[misc]

    def test_instrument_is_optional(self) -> None:
        assert entry(instrument_id=None).instrument_id is None


class TestChainLinkage:
    def test_a_valid_chain_verifies(self) -> None:
        report = verify_chain(chain(5))
        assert report.is_intact
        assert report.entries_checked == 5
        assert report.first_sequence == 1
        assert report.last_sequence == 5
        report.raise_if_broken()

    def test_an_empty_chain_verifies(self) -> None:
        report = verify_chain([])
        assert report.is_intact
        assert report.entries_checked == 0
        assert report.first_sequence is None

    def test_a_single_entry_verifies(self) -> None:
        assert verify_chain([entry()]).is_intact

    def test_links_to_predicate(self) -> None:
        entries = chain(2)
        assert entries[1].links_to(entries[0])
        assert not entries[0].links_to(entries[1])

    def test_a_removed_entry_breaks_the_chain(self) -> None:
        entries = chain(5)
        report = verify_chain([*entries[:2], *entries[3:]])
        assert not report.is_intact
        assert report.broken_at == 4
        assert "missing" in (report.reason or "")

    def test_a_relinked_entry_breaks_the_chain(self) -> None:
        # Rebuilding entry 3 with different contents changes its hash, so entry 4 no
        # longer points at it.
        entries = chain(4)
        forged = entry(
            sequence=3,
            previous_hash=entries[1].entry_hash,
            ts=entries[2].ts,
            summary="a decision that never happened",
        )
        report = verify_chain([entries[0], entries[1], forged, entries[3]])
        assert not report.is_intact
        assert report.broken_at == 4
        assert "does not match" in (report.reason or "")

    def test_a_reordered_chain_is_detected(self) -> None:
        entries = chain(3)
        report = verify_chain([entries[0], entries[2], entries[1]])
        assert not report.is_intact

    def test_raise_if_broken_names_the_sequence(self) -> None:
        entries = chain(3)
        report = verify_chain([entries[0], entries[2]])
        with pytest.raises(PersistenceError, match="broken at sequence 3"):
            report.raise_if_broken()

    def test_replacing_the_tail_still_breaks_verification_against_the_head(self) -> None:
        # An attacker who rewrites the last entry and recomputes its hash produces a
        # chain that is internally consistent but no longer matches a hash recorded
        # elsewhere, which is why the head hash is what an external witness stores.
        entries = chain(3)
        forged_tail = entry(sequence=3, previous_hash=entries[1].entry_hash, summary="rewritten")
        rewritten = [entries[0], entries[1], forged_tail]
        assert verify_chain(rewritten).is_intact
        assert forged_tail.entry_hash != entries[2].entry_hash

    def test_a_chain_not_starting_at_one_is_still_checked_internally(self) -> None:
        entries = chain(5)
        report = verify_chain(entries[2:])
        assert report.is_intact
        assert report.first_sequence == 3


class TestReplaceIsRejected:
    def test_dataclass_replace_recomputes_and_rejects_a_stale_hash(self) -> None:
        # dataclasses.replace re-runs __post_init__, which catches an edit that keeps
        # the old digest.
        original = entry()
        with pytest.raises(PersistenceError, match="has been altered"):
            replace(original, summary="edited")
