"""Risk limits and the checks that enforce them.

Phase 5 builds the full risk engine. What lives here now is the part phase 2 needs:
deciding whether an instrument can be traded at all at the configured risk, which
determines which instruments the registry marks tradeable.
"""

from tradingsys.risk.eligibility import Eligibility, ExclusionReason, evaluate_eligibility

__all__ = ["Eligibility", "ExclusionReason", "evaluate_eligibility"]
