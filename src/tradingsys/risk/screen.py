"""The tradeable instrument set at the current account balance.

`SPEC.md` section 6.1 requires that the system accept any account size, that
eligibility be evaluated against the balance the venue reports rather than a
configured constant, and that instruments enter and leave the tradeable set as the
balance moves, without a code change and without a restart.

This module is that requirement, made a component. It holds no capital figure. It is
handed a balance and prices, and it answers. Calling it again with a different balance
gives a different answer, which is the entire point: there is no cached verdict to go
stale and no threshold to cross.

**Transitions are first class.** A silently changing instrument universe is
indistinguishable from a defect, so comparing two evaluations yields the instruments
that entered and left, and the caller is expected to log and audit them. An instrument
that becomes tradeable because the account grew is a legitimate and important event;
so is one that stops being tradeable because the account shrank, and the second is the
one an operator must not learn about by noticing that orders stopped.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, final

from tradingsys.core.errors import DomainError
from tradingsys.risk.eligibility import evaluate_eligibility

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from tradingsys.core.instrument import Instrument, InstrumentId
    from tradingsys.core.money import Money
    from tradingsys.core.numeric import Numeric
    from tradingsys.risk.eligibility import Eligibility

__all__ = [
    "InstrumentScreen",
    "ScreenResult",
    "Transition",
    "TransitionKind",
    "format_stop_ceilings",
]


class TransitionKind(StrEnum):
    """What changed about an instrument between two evaluations."""

    ENTERED = "entered"
    """It was not tradeable at the previous balance and is now."""

    LEFT = "left"
    """It was tradeable at the previous balance and is not now."""


@final
@dataclass(frozen=True, slots=True)
class Transition:
    """One instrument changing eligibility between two evaluations.

    Attributes:
        instrument_id: Which instrument moved.
        kind: Which way it moved.
        detail: The screen's own explanation at the new balance, so the audit entry
            carries the reason rather than only the fact.
    """

    instrument_id: InstrumentId
    kind: TransitionKind
    detail: str


@final
@dataclass(frozen=True, slots=True)
class ScreenResult:
    """The verdict for every instrument at one balance.

    Attributes:
        equity: The balance this was evaluated at, as reported by the venue.
        assessments: One entry per instrument offered, keyed by instrument id.
    """

    equity: Money
    assessments: Mapping[InstrumentId, Eligibility]

    @property
    def tradeable(self) -> tuple[InstrumentId, ...]:
        """Instrument ids that can be traded at this balance, in sorted order."""
        return tuple(sorted(key for key, value in self.assessments.items() if value.eligible))

    @property
    def excluded(self) -> tuple[InstrumentId, ...]:
        return tuple(sorted(key for key, value in self.assessments.items() if not value.eligible))

    def stop_ceiling(self, instrument_id: InstrumentId) -> Decimal:
        """The widest stop this balance affords on one instrument, as a fraction of price.

        Raises:
            KeyError: The instrument was not part of this evaluation.
        """
        return self.assessments[instrument_id].widest_affordable_stop

    def explain(self) -> tuple[str, ...]:
        """One line per instrument, in the terms the decision was made in."""
        return tuple(self.assessments[key].explain() for key in sorted(self.assessments))

    def transitions(self, previous: ScreenResult | None) -> tuple[Transition, ...]:
        """What entered and left the tradeable set since ``previous``.

        A first evaluation has no previous result and therefore no transitions: every
        instrument's state is new information rather than a change, and reporting the
        whole universe as having entered would drown the events that matter.

        Instruments absent from one side are skipped rather than treated as a
        transition, because an instrument that was not evaluated has not changed
        eligibility; it was simply not asked about.
        """
        if previous is None:
            return ()
        moved: list[Transition] = []
        for key in sorted(self.assessments):
            after = self.assessments[key]
            before = previous.assessments.get(key)
            if before is None or before.eligible == after.eligible:
                continue
            kind = TransitionKind.ENTERED if after.eligible else TransitionKind.LEFT
            moved.append(Transition(instrument_id=key, kind=kind, detail=after.explain()))
        return tuple(moved)


@final
@dataclass(frozen=True, slots=True)
class InstrumentScreen:
    """Risk policy, expressed only as fractions.

    Every field here is dimensionless. There is deliberately no account size, no
    currency amount, and no minimum balance: a policy that carried one would be a
    policy that is correct at exactly one account size and silently wrong at every
    other.

    Attributes:
        risk_fraction: Fraction of equity a single trade may lose.
        max_risk_deviation: Largest acceptable gap between intended and realised risk,
            above which an instrument is excluded as too coarsely quantised.
        reference_stop: Stop distance, as a fraction of price, at which the eligibility
            verdict is reported. It is a reporting choice rather than a trading
            decision: the stop ceiling on each result is the figure that actually
            constrains strategy design.
    """

    risk_fraction: Decimal
    max_risk_deviation: Decimal
    reference_stop: Decimal

    def __post_init__(self) -> None:
        for name in ("risk_fraction", "max_risk_deviation", "reference_stop"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise DomainError(f"{name} must be a positive finite Decimal, got {value!r}")
            if value >= 1:
                raise DomainError(
                    f"{name} is {value}, which is not a fraction. Every risk policy value "
                    f"here is a fraction of equity or of price; an absolute amount would "
                    f"be correct at one account size and wrong at every other."
                )

    def evaluate(
        self,
        instruments: Iterable[Instrument],
        *,
        equity: Money,
        prices: Mapping[InstrumentId, Numeric],
        quote_rates: Mapping[InstrumentId, Numeric] | None = None,
    ) -> ScreenResult:
        """Assess every instrument at the balance the venue reports.

        Args:
            instruments: The candidate universe.
            equity: Account balance, from the venue rather than from configuration.
            prices: Current price per instrument, in that instrument's quote currency.
            quote_rates: Units of account currency per unit of quote currency, for
                instruments whose quote differs from the account currency. Required
                for those and rejected as unnecessary for the others by the
                eligibility screen itself.

        Raises:
            DomainError: An instrument has no price, or equity is not positive.
        """
        if equity.amount <= 0:
            raise DomainError(
                f"equity is {equity}, so no position can be sized. A balance of zero or "
                f"less is an account state to halt on, not a small account to trade."
            )
        rates = quote_rates or {}
        assessments: dict[InstrumentId, Eligibility] = {}
        for instrument in instruments:
            price = prices.get(instrument.id)
            if price is None:
                raise DomainError(
                    f"{instrument.id} has no price, so its eligibility cannot be "
                    f"evaluated. Screening it out for lack of a price would look "
                    f"identical to screening it out on risk grounds."
                )
            assessments[instrument.id] = evaluate_eligibility(
                instrument,
                price=price,
                equity=equity,
                risk_fraction=self.risk_fraction,
                stop_distance=self.reference_stop,
                max_risk_deviation=self.max_risk_deviation,
                quote_to_account_rate=rates.get(instrument.id),
            )
        return ScreenResult(equity=equity, assessments=assessments)


def format_stop_ceilings(
    result: ScreenResult, instruments: Sequence[Instrument]
) -> tuple[str, ...]:
    """The strategy implication, one line per instrument.

    The eligibility verdict answers whether an instrument can be traded. This answers
    the question that follows it, which is what can be traded on it: the widest stop
    the balance affords, in the instrument's own pips, because a stop ceiling is only
    meaningful against the distance a strategy actually needs.
    """
    lines: list[str] = []
    for instrument in sorted(instruments, key=lambda item: item.id):
        assessment = result.assessments.get(instrument.id)
        if assessment is None:
            continue
        ceiling = assessment.widest_affordable_stop
        if instrument.pip_size is None or instrument.pip_size <= 0:
            lines.append(f"{instrument.id}: widest affordable stop {ceiling:.4%} of price")
            continue
        price = assessment.price
        pips = (ceiling * price) / instrument.pip_size
        lines.append(
            f"{instrument.id}: widest affordable stop {ceiling:.4%} of price, "
            f"{pips:.1f} pips at {price}"
        )
    return tuple(lines)
