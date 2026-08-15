"""Reading Dukascopy's .bi5 tick archives.

Dukascopy publishes historical ticks as one LZMA compressed file per instrument per
UTC hour. Each decompressed file is a flat array of 20 byte big endian records:

===========  ======  =========================================================
Offset       Type    Meaning
===========  ======  =========================================================
0            uint32  Milliseconds elapsed since the start of the file's hour
4            uint32  Ask, as an integer in the instrument's smallest price unit
8            uint32  Bid, same scaling
12           float32 Ask volume
16           float32 Bid volume
===========  ======  =========================================================

Two properties of that layout drive the code below.

The prices are integers. The instrument's decimal places are *not* in the file, so the
caller supplies them from the instrument registry; there is no sensible default, since
guessing five would silently misprice every JPY pair by a factor of a hundred. Scaling
an integer by a power of ten is exact, so the stored price is the published price with
nothing lost.

The volumes are binary32. They are converted through
:func:`~tradingsys.core.numeric.from_binary32`, which expands them exactly rather than
rounding, so the stored value converts back to the identical 32 bit pattern. What those
volumes are denominated in is a separate question that the feed does not answer, and
this module deliberately does not guess: see :data:`VOLUME_UNIT_UNVERIFIED`.

This reader is written in house rather than taken from a package because it is the
provenance boundary for every piece of research data in the system, and because the
failure mode that matters is not an exception. Dukascopy answers a burst of parallel
requests with HTTP 503 and an HTML body. A reader that hands whatever it received to a
decompressor treats that page as a missing hour, and the resulting hole is
indistinguishable from a genuinely quiet market. Every payload is therefore validated
before it is decoded, and anything unexpected raises.

Dukascopy data is research only. It is a different liquidity pool from our execution
venue, so its spreads are not the spreads we would pay, and
:mod:`tradingsys.core.provenance` enforces that structurally rather than by convention.
"""

from __future__ import annotations

import lzma
import re
import struct
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final, final

from tradingsys.core.clock import ensure_utc
from tradingsys.core.errors import DomainError
from tradingsys.core.numeric import exact_context, from_binary32

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "FEED_BASE_URL",
    "MAX_DIGITS",
    "RECORD_SIZE",
    "VOLUME_UNIT_UNVERIFIED",
    "Bi5DecodeError",
    "DukascopyTick",
    "decode_hour",
    "decompress_bi5",
    "hour_url",
]

FEED_BASE_URL: Final = "https://datafeed.dukascopy.com/datafeed"

RECORD_SIZE: Final = 20
_RECORD = struct.Struct(">IIIff")

_LZMA_ALONE_MAGIC: Final = b"\x5d\x00\x00"
"""First three bytes of the LZMA1 "alone" container Dukascopy uses.

0x5d is the standard properties byte for lc=3, lp=0, pb=2, and the two bytes after it
begin the little endian dictionary size. It is not a strong magic number, but it is
enough to reject an HTML error page, which is the payload that actually turns up.
"""

_MILLISECONDS_PER_HOUR: Final = 3_600_000
MAX_DIGITS: Final = 12
"""Upper bound on price decimal places, well past any instrument Dukascopy carries."""

_SYMBOL = re.compile(r"^[A-Z0-9]{3,20}$")

VOLUME_UNIT_UNVERIFIED: Final = (
    "Dukascopy does not document the unit of its tick volumes and we have not "
    "confirmed it against a published definition. Treat the figure as an unnamed "
    "quantity: comparable between Dukascopy ticks, and not comparable with volumes "
    "from any other source until the unit has been established."
)
"""Why the volume fields carry no unit.

Widely repeated on forums as "millions of base currency", which is plausible given the
magnitudes but is not a source. Until it is confirmed, or cross checked against our own
venue over an overlapping window, nothing may scale these numbers or compare them with
volumes from elsewhere.
"""


class Bi5DecodeError(DomainError):
    """A .bi5 payload was absent, truncated, or not a .bi5 payload at all."""


@final
@dataclass(frozen=True, slots=True)
class DukascopyTick:
    """One quote update from a Dukascopy tick archive.

    Attributes:
        ts: Instant of the quote, UTC, to the millisecond the feed reported.
        bid: Bid price, exactly as published once the integer is scaled.
        ask: Ask price, same.
        bid_volume: Bid side volume, exact expansion of the published binary32. See
            :data:`VOLUME_UNIT_UNVERIFIED` for what it is not safe to assume about it.
        ask_volume: Ask side volume, same.
    """

    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_volume: Decimal
    ask_volume: Decimal

    @property
    def spread(self) -> Decimal:
        """Ask minus bid. Negative for a crossed quote, which is not an error here."""
        return self.ask - self.bid

    @property
    def is_crossed(self) -> bool:
        """Whether the bid is above the ask.

        Real archives contain these, usually a few milliseconds either side of a data
        release. Rejecting them would discard the most interesting hour of the month,
        so they are decoded and flagged, and it is the data quality report's job to
        count them.
        """
        return self.bid > self.ask


def hour_url(symbol: str, hour: datetime, *, base_url: str = FEED_BASE_URL) -> str:
    """The feed URL for one instrument hour.

    The month in the path is **zero based**: January is 00 and December is 11. The day
    and hour are not. This is the single most common way to read Dukascopy data from
    the wrong month, so it is asserted in the tests rather than only described here.

    Args:
        symbol: Dukascopy instrument code, uppercase, for example ``EURUSD``.
        hour: The hour to fetch, UTC and on the hour.
        base_url: Feed root, overridable so tests need no network.

    Raises:
        DomainError: The symbol is not a plausible instrument code, or the instant is
            naive or is not exactly on the hour.
    """
    if not _SYMBOL.match(symbol):
        raise DomainError(
            f"{symbol!r} is not a Dukascopy instrument code; expected uppercase "
            f"alphanumerics such as 'EURUSD'"
        )
    hour = _require_exact_hour(hour)
    return (
        f"{base_url.rstrip('/')}/{symbol}/{hour.year:04d}/{hour.month - 1:02d}/"
        f"{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
    )


def decompress_bi5(payload: bytes) -> bytes:
    """Validate and decompress one .bi5 body.

    Args:
        payload: Exactly the bytes the feed returned.

    Returns:
        The decompressed record array, empty for an hour the feed reports as empty.

    Raises:
        Bi5DecodeError: The payload is not LZMA, does not decompress, or does not
            decompress to a whole number of records.
    """
    if not payload:
        # Dukascopy returns a zero length body for an hour it holds nothing for, which
        # is every hour of every weekend. That is an answer, not a failure.
        return b""
    if not payload.startswith(_LZMA_ALONE_MAGIC):
        raise Bi5DecodeError(
            f"payload is not an LZMA stream: it begins {payload[:16]!r}. An HTML error "
            f"page looks like this, and the feed serves one under load; treating it as "
            f"an empty hour would record a hole that never existed."
        )
    try:
        decompressed = lzma.decompress(payload, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError as error:
        raise Bi5DecodeError(f"payload did not decompress: {error}") from error
    if len(decompressed) % RECORD_SIZE:
        raise Bi5DecodeError(
            f"decompressed to {len(decompressed)} bytes, which is not a whole number of "
            f"{RECORD_SIZE} byte records; the file is truncated or is not a tick archive"
        )
    return decompressed


def decode_hour(payload: bytes, *, hour: datetime, digits: int) -> tuple[DukascopyTick, ...]:
    """Decode one hourly .bi5 body into ticks.

    Args:
        payload: Exactly the bytes the feed returned, still compressed.
        hour: The hour the file covers, UTC and on the hour. Record timestamps are
            offsets from it, so passing the wrong hour misdates every tick and nothing
            in the file would contradict it.
        digits: Decimal places in the instrument's prices, from the instrument
            registry. Five for most FX pairs and three for JPY quoted ones, but this
            is a property of the instrument and is never assumed here.

    Returns:
        Ticks in the order the file stores them, which is ascending by time.

    Raises:
        Bi5DecodeError: The payload is invalid, or a record is.
        DomainError: ``hour`` is naive or not on the hour, or ``digits`` is out of range.
    """
    hour = _require_exact_hour(hour)
    if not 0 <= digits <= MAX_DIGITS:
        raise DomainError(f"digits must be between 0 and {MAX_DIGITS}, got {digits}")

    decompressed = decompress_bi5(payload)
    ticks: list[DukascopyTick] = []
    previous_offset = -1
    with exact_context():
        for index, (offset_ms, raw_ask, raw_bid, ask_volume, bid_volume) in enumerate(
            _RECORD.iter_unpack(decompressed)
        ):
            if offset_ms >= _MILLISECONDS_PER_HOUR:
                raise Bi5DecodeError(
                    f"record {index} is {offset_ms} ms into the hour, past the end of "
                    f"it; the file is misaligned or is not a tick archive"
                )
            if offset_ms < previous_offset:
                raise Bi5DecodeError(
                    f"record {index} at {offset_ms} ms goes back in time from "
                    f"{previous_offset} ms; the file is corrupt"
                )
            if not raw_bid or not raw_ask:
                raise Bi5DecodeError(
                    f"record {index} has a zero price: bid {raw_bid}, ask {raw_ask}"
                )
            previous_offset = offset_ms
            ticks.append(
                DukascopyTick(
                    ts=hour + timedelta(milliseconds=offset_ms),
                    bid=_scale(raw_bid, digits),
                    ask=_scale(raw_ask, digits),
                    bid_volume=_volume(bid_volume, index=index, side="bid"),
                    ask_volume=_volume(ask_volume, index=index, side="ask"),
                )
            )
    return tuple(ticks)


def _scale(raw: int, digits: int) -> Decimal:
    """An integer price in the smallest price unit, as a decimal price.

    Exact: scaling by a power of ten only moves the decimal exponent, so no digit of
    the published value is lost or invented.
    """
    return Decimal(raw).scaleb(-digits)


def _volume(value: float, *, index: int, side: str) -> Decimal:
    try:
        volume = from_binary32(value, what=f"record {index} {side} volume")
    except DomainError as error:
        raise Bi5DecodeError(str(error)) from error
    if volume < 0:
        raise Bi5DecodeError(f"record {index} has a negative {side} volume: {value!r}")
    return volume


def _require_exact_hour(instant: datetime) -> datetime:
    instant = ensure_utc(instant, what="hour")
    if (instant.minute, instant.second, instant.microsecond) != (0, 0, 0):
        raise DomainError(
            f"a .bi5 file covers exactly one hour, so the hour must be on the hour; "
            f"got {instant.isoformat()}"
        )
    return instant
