"""Whether an instrument can be traded at the risk the system claims to take.

`SPEC.md` section 6 says one percent of the account per trade, and that where one
percent cannot produce a size at or above the venue's minimum the instrument is
excluded rather than the limit raised. This module is that rule, made arithmetic.

It answers with two separate reasons for exclusion, because they are different
problems:

**Below the minimum.** The size one percent implies is smaller than the venue will
accept. Trading anyway means taking more risk than the limit allows, on every trade.

**Too coarse.** The size is above the minimum but the quantity step is a large fraction
of it, so the position actually sent can only be a few discrete sizes and the realised
risk lands well away from the intended one. A limit that is approximated to within a
third is not being enforced. That is BTC/USDT perpetual on a 200 USD account: three
usable sizes, and up to 31 percent off.

The threshold is a parameter rather than a constant, and everything else comes from
venue metadata and the current price, so the answer changes on its own as the account
grows or the venue changes a step. An exclusion hardcoded from one afternoon's prices
outlives its reason and nobody notices.

Conversion is explicit. Risk per unit is computed in the instrument's quote currency,
and turning that into account currency needs a rate that is a market fact rather than
a detail this module can infer, so callers pass it and a mismatch without one raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, final

from tradingsys.core.errors import DomainError
from tradingsys.core.numeric import exact_context, to_decimal
from tradingsys.core.rounding import Rounding

if TYPE_CHECKING:
    from tradingsys.core.instrument import Instrument
    from tradingsys.core.money import Money
    from tradingsys.core.numeric import Numeric

__all__ = [
    "Eligibility",
    "ExclusionReason",
    "evaluate_eligibility",
]


class ExclusionReason(StrEnum):
    """Why an instrument cannot be traded at the configured risk."""

    BELOW_MINIMUM_SIZE = "below_minimum_size"
    """The intended size is under the venue's minimum quantity."""

    BELOW_MINIMUM_NOTIONAL = "below_minimum_notional"
    """The intended size clears the quantity minimum but not the cash one."""

    QUANTISATION_TOO_COARSE = "quantisation_too_coarse"
    """The quantity step is too large a fraction of the intended size, so realised risk
    cannot be held close enough to the limit."""


@final
@dataclass(frozen=True, slots=True)
class Eligibility:
    """The sizing arithmetic for one instrument, and the verdict that follows.

    Attributes:
        instrument: What was evaluated.
        price: The price the verdict was reached at, kept because every figure below
            depends on it and a verdict without its price cannot be checked later.
        risk_budget: Account currency the trade is allowed to lose.
        intended_quantity: Exact size the budget implies, before the venue's step.
        tradeable_quantity: That size rounded down to the step. Zero when the budget
            cannot buy a single step.
        realised_risk: What the tradeable size actually risks, in account currency.
        worst_case_deviation: One step as a fraction of the intended size. The bound on
            how far realised risk can sit from intended for this instrument at this
            account size, whichever way the rounding happens to fall.
        widest_affordable_stop: The largest stop distance, as a fraction of price, at
            which the venue's minimum position still fits inside the risk budget. This
            is the strategy constraint rather than the eligibility verdict: a stop
            wider than this needs a position smaller than the venue accepts, so it
            rules out every strategy whose stops are wider, at this balance.
        reason: Why it was excluded, or ``None`` when it was not.
    """

    instrument: Instrument
    price: Decimal
    risk_budget: Decimal
    intended_quantity: Decimal
    tradeable_quantity: Decimal
    realised_risk: Decimal
    worst_case_deviation: Decimal
    widest_affordable_stop: Decimal
    reason: ExclusionReason | None

    @property
    def eligible(self) -> bool:
        return self.reason is None

    def explain(self) -> str:
        """One line for the exclusion report, in the terms the decision was made in."""
        name = str(self.instrument.id)
        if self.reason is None:
            return (
                f"{name}: tradeable at {self.tradeable_quantity} "
                f"({self.worst_case_deviation:.1%} worst case deviation from intended risk)"
            )
        if self.reason is ExclusionReason.BELOW_MINIMUM_SIZE:
            return (
                f"{name}: excluded, {self.intended_quantity} is below the venue minimum "
                f"of {self.instrument.min_quantity}"
            )
        if self.reason is ExclusionReason.BELOW_MINIMUM_NOTIONAL:
            return (
                f"{name}: excluded, the intended size is worth less than the venue's "
                f"minimum notional of {self.instrument.min_notional}"
            )
        return (
            f"{name}: excluded, one step of {self.instrument.quantity_increment} is "
            f"{self.worst_case_deviation:.1%} of the intended size, so realised risk "
            f"cannot be held near the limit"
        )


def evaluate_eligibility(
    instrument: Instrument,
    *,
    price: Numeric,
    equity: Money,
    risk_fraction: Numeric,
    stop_distance: Numeric,
    max_risk_deviation: Numeric,
    quote_to_account_rate: Numeric | None = None,
) -> Eligibility:
    """Decide whether ``instrument`` can be traded within the risk limit.

    Args:
        instrument: The instrument, with the venue's own step and minimum sizes.
        price: Current price, in the instrument's quote currency.
        equity: Account equity, in the account currency.
        risk_fraction: Fraction of equity a single trade may lose, for example
            ``Decimal("0.01")``.
        stop_distance: Distance to the stop as a fraction of price. Required, because
            risk per unit is meaningless without it and a default here would be an
            invented trading decision.
        max_risk_deviation: Largest acceptable gap between intended and realised risk,
            as a fraction. Above this the instrument is excluded as too coarse.
        quote_to_account_rate: Units of account currency per unit of quote currency.
            Required when the two differ, for example a JPY quoted pair on a USD
            account. Omitted when they are the same.

    Raises:
        DomainError: A fraction is not positive, the price is not positive, or the
            quote and account currencies differ with no rate supplied.
    """
    price_value = _positive(price, "price")
    risk = _positive(risk_fraction, "risk_fraction")
    stop = _positive(stop_distance, "stop_distance")
    deviation_limit = _positive(max_risk_deviation, "max_risk_deviation")
    rate = _conversion_rate(instrument, equity, quote_to_account_rate)

    with exact_context():
        budget = equity.amount * risk
        # Risk per unit: how much of the account one unit loses if the stop is hit.
        # contract_size carries lot sized venues, where one unit of quantity is many
        # units of the base asset.
        risk_per_unit = stop * price_value * instrument.contract_size * rate
        intended = budget / risk_per_unit
        tradeable = instrument.quantize_quantity(intended, Rounding.DOWN)
        realised = tradeable * risk_per_unit
        # The step as a fraction of the intended size, which is the worst the rounding
        # can be at this account size. Reported for eligible instruments too: it is the
        # precision the risk limit is actually being held to.
        worst_case = instrument.quantity_increment / intended
        # The stop ceiling this balance imposes. Derived from the same budget, so it
        # moves with the account rather than being a figure written down once: at twice
        # the equity the ceiling is twice as wide, which is the whole of what capital
        # independence means in practice.
        widest_stop = budget / (
            instrument.min_quantity * price_value * instrument.contract_size * rate
        )

    reason = _reason(
        instrument,
        tradeable=tradeable,
        price=price_value,
        worst_case=worst_case,
        deviation_limit=deviation_limit,
    )
    return Eligibility(
        instrument=instrument,
        price=price_value,
        risk_budget=budget,
        intended_quantity=intended,
        tradeable_quantity=tradeable,
        realised_risk=realised,
        worst_case_deviation=worst_case,
        widest_affordable_stop=widest_stop,
        reason=reason,
    )


def _reason(
    instrument: Instrument,
    *,
    tradeable: Decimal,
    price: Decimal,
    worst_case: Decimal,
    deviation_limit: Decimal,
) -> ExclusionReason | None:
    if tradeable < instrument.min_quantity:
        return ExclusionReason.BELOW_MINIMUM_SIZE
    if instrument.min_notional is not None:
        with exact_context():
            notional = tradeable * price * instrument.contract_size
        if notional < instrument.min_notional.amount:
            return ExclusionReason.BELOW_MINIMUM_NOTIONAL
    if worst_case > deviation_limit:
        return ExclusionReason.QUANTISATION_TOO_COARSE
    return None


def _conversion_rate(
    instrument: Instrument, equity: Money, quote_to_account_rate: Numeric | None
) -> Decimal:
    same_currency = instrument.quote_currency == equity.currency
    if quote_to_account_rate is None:
        if not same_currency:
            raise DomainError(
                f"{instrument.id}: risk is priced in {instrument.quote_currency.code} but the "
                f"account is in {equity.currency.code}, so a conversion rate is required. "
                f"Assuming parity would misstate the risk on every trade in this instrument."
            )
        return Decimal(1)
    return _positive(quote_to_account_rate, "quote_to_account_rate")


def _positive(value: Numeric, what: str) -> Decimal:
    number = to_decimal(value, what=what)
    if number <= 0:
        raise DomainError(f"{what} must be positive, got {number}")
    return number
