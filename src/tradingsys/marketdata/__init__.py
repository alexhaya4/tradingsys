"""Market data ingestion: gap detection, backfill, and the venue readers.

Nothing here talks to a venue directly. The adapters live in
:mod:`tradingsys.venues`; this package is the machinery that decides what to fetch,
notices what is missing, and writes what arrives.
"""

from tradingsys.marketdata.dukascopy import (
    Bi5DecodeError,
    DukascopyTick,
    decode_hour,
    hour_url,
)
from tradingsys.marketdata.gaps import (
    Gap,
    coverage_from_timestamps,
    find_gaps,
    merge_coverage,
)

__all__ = [
    "Bi5DecodeError",
    "DukascopyTick",
    "Gap",
    "coverage_from_timestamps",
    "decode_hour",
    "find_gaps",
    "hour_url",
    "merge_coverage",
]
