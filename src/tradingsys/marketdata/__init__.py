"""Market data ingestion: gap detection, backfill, and the venue readers.

Nothing here talks to a venue directly. The adapters live in
:mod:`tradingsys.venues`; this package is the machinery that decides what to fetch,
notices what is missing, and writes what arrives.
"""

from tradingsys.marketdata.gaps import (
    Gap,
    coverage_from_timestamps,
    find_gaps,
    merge_coverage,
)

__all__ = ["Gap", "coverage_from_timestamps", "find_gaps", "merge_coverage"]
