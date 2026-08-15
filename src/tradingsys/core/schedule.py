"""Trading schedules: when a venue accepts orders.

Two regimes have to coexist in one type.

Forex trades a single continuous session that opens Sunday evening and closes Friday
evening in the venue's reference timezone, pauses for weekends, and skips a handful of
holidays. Because those boundaries are defined in wall clock time, the corresponding
UTC instants move by an hour twice a year, so schedules are evaluated in a named IANA
timezone rather than as fixed UTC offsets.

Crypto trades continuously with no boundary at all.

Both are modelled as a set of :class:`WeeklySession` intervals over a repeating week,
plus an optional holiday calendar. A session may span days and may wrap the week
boundary, which is what makes the single Sunday to Friday forex week expressible as one
interval instead of five.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import IntEnum
from typing import Any, Self, final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tradingsys.core.errors import DomainError

__all__ = [
    "TradingSchedule",
    "Weekday",
    "WeeklySession",
]

_DAYS_IN_WEEK = 7
_LOOKAHEAD_DAYS = 21
"""How far :meth:`TradingSchedule.next_open` and :meth:`TradingSchedule.next_close`
scan before giving up. Three weeks covers any weekly schedule plus a run of holidays.
"""


class Weekday(IntEnum):
    """Days of the week, numbered to match :meth:`datetime.date.weekday`."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


@final
@dataclass(frozen=True, slots=True)
class WeeklySession:
    """A recurring interval in the trading week, half open as ``[open, close)``.

    The interval runs forward from ``open_day`` at ``open_time`` to the next
    occurrence of ``close_day`` at ``close_time``. When the close does not come
    later in the same week, the session wraps the week boundary: a session opening
    Sunday 17:00 and closing Friday 17:00 spans five days, not two.

    Times are wall clock times in the owning schedule's timezone.
    """

    open_day: Weekday
    open_time: time
    close_day: Weekday
    close_time: time

    def __post_init__(self) -> None:
        for label, value in (("open_time", self.open_time), ("close_time", self.close_time)):
            if value.tzinfo is not None:
                raise DomainError(
                    f"{label} must be a naive wall clock time; the timezone belongs to "
                    f"the schedule, not the session"
                )
            if value.microsecond:
                raise DomainError(f"{label} must not carry microseconds, got {value!r}")

    @property
    def span(self) -> timedelta:
        """Duration of one occurrence of the session."""
        days = (int(self.close_day) - int(self.open_day)) % _DAYS_IN_WEEK
        if days == 0 and self.close_time <= self.open_time:
            days = _DAYS_IN_WEEK
        open_delta = timedelta(
            hours=self.open_time.hour, minutes=self.open_time.minute, seconds=self.open_time.second
        )
        close_delta = timedelta(
            hours=self.close_time.hour,
            minutes=self.close_time.minute,
            seconds=self.close_time.second,
        )
        return timedelta(days=days) + close_delta - open_delta

    @property
    def spans_days(self) -> int:
        """Number of calendar days between the opening date and the closing date."""
        days = (int(self.close_day) - int(self.open_day)) % _DAYS_IN_WEEK
        if days == 0 and self.close_time <= self.open_time:
            days = _DAYS_IN_WEEK
        return days

    def to_mapping(self) -> dict[str, str]:
        """Serialise for storage in a JSONB column."""
        return {
            "open_day": self.open_day.name,
            "open_time": self.open_time.isoformat(),
            "close_day": self.close_day.name,
            "close_time": self.close_time.isoformat(),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild a session from :meth:`to_mapping` output."""
        missing = {"open_day", "open_time", "close_day", "close_time"} - set(data)
        if missing:
            raise DomainError(f"session mapping is missing keys: {sorted(missing)}")
        return cls(
            open_day=_parse_weekday(data["open_day"]),
            open_time=_parse_time(data["open_time"], "open_time"),
            close_day=_parse_weekday(data["close_day"]),
            close_time=_parse_time(data["close_time"], "close_time"),
        )


def _parse_weekday(value: object) -> Weekday:
    if isinstance(value, Weekday):
        return value
    if isinstance(value, int):
        try:
            return Weekday(value)
        except ValueError:
            raise DomainError(f"{value} is not a valid weekday number (0=Monday)") from None
    if isinstance(value, str):
        try:
            return Weekday[value.strip().upper()]
        except KeyError:
            raise DomainError(f"{value!r} is not a valid weekday name") from None
    raise DomainError(f"cannot interpret {value!r} as a weekday")


def _as_holiday_set(value: object) -> frozenset[date]:
    """Normalise a holiday collection, rejecting datetimes and non-dates.

    Accepts any iterable so that a schedule built from deserialised data with a plain
    ``set`` or ``list`` is corrected rather than silently stored in the wrong type.
    """
    if not isinstance(value, Iterable):
        raise DomainError(f"holidays must be an iterable of dates, got {value!r}")
    holidays: set[date] = set()
    for holiday in value:
        if isinstance(holiday, datetime) or not isinstance(holiday, date):
            raise DomainError(
                f"holidays must be plain dates, got {holiday!r}; a datetime would make "
                f"the closure depend on a time of day that the calendar does not carry"
            )
        holidays.add(holiday)
    return frozenset(holidays)


def _parse_time(value: object, label: str) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            return time.fromisoformat(value)
        except ValueError:
            raise DomainError(f"{label} {value!r} is not an ISO 8601 time") from None
    raise DomainError(f"cannot interpret {value!r} as {label}")


@final
@dataclass(frozen=True, slots=True)
class TradingSchedule:
    """A venue's weekly opening hours in a named timezone.

    Attributes:
        timezone: IANA timezone name in which session times are expressed, for
            example ``America/New_York`` for the forex week.
        sessions: Recurring weekly intervals. Empty when ``always_open`` is set.
        holidays: Local dates on which the venue does not trade. A holiday closes
            the whole local date, including any session that would otherwise span it.
        always_open: True for continuously traded venues. Holidays still apply, so a
            24/7 venue with a scheduled maintenance date can be expressed.
    """

    timezone: str
    sessions: tuple[WeeklySession, ...] = ()
    holidays: frozenset[date] = field(default_factory=frozenset)
    always_open: bool = False

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DomainError(f"{self.timezone!r} is not a known IANA timezone: {exc}") from exc
        if self.always_open and self.sessions:
            raise DomainError(
                "a continuously open schedule must not declare weekly sessions; "
                "the two would contradict each other"
            )
        if not self.always_open and not self.sessions:
            raise DomainError(
                "a schedule must declare at least one weekly session, or set "
                "always_open for a 24/7 venue"
            )
        object.__setattr__(self, "holidays", _as_holiday_set(self.holidays))
        self._reject_overlaps()

    def _reject_overlaps(self) -> None:
        """Fail on sessions that overlap, which would make open/close events ambiguous."""
        intervals: list[tuple[int, int, WeeklySession]] = []
        for session in self.sessions:
            start = _week_offset_seconds(session.open_day, session.open_time)
            end = start + int(session.span.total_seconds())
            intervals.append((start, end, session))
        week_seconds = _DAYS_IN_WEEK * 24 * 3600
        for i, (start_a, end_a, session_a) in enumerate(intervals):
            for start_b, end_b, session_b in intervals[i + 1 :]:
                for shift in (-week_seconds, 0, week_seconds):
                    if start_a + shift < end_b and start_b < end_a + shift:
                        raise DomainError(
                            f"sessions overlap and would make opening times ambiguous: "
                            f"{session_a} and {session_b}"
                        )

    # ------------------------------------------------------------------
    # constructors
    # ------------------------------------------------------------------

    @classmethod
    def continuous(cls, timezone: str = "UTC", holidays: Iterable[date] = ()) -> Self:
        """A 24/7 schedule, as used by crypto spot and perpetual venues."""
        return cls(timezone=timezone, sessions=(), holidays=frozenset(holidays), always_open=True)

    @classmethod
    def weekly(
        cls,
        timezone: str,
        sessions: Sequence[WeeklySession],
        holidays: Iterable[date] = (),
    ) -> Self:
        """A schedule built from explicit weekly sessions."""
        return cls(
            timezone=timezone,
            sessions=tuple(sessions),
            holidays=frozenset(holidays),
            always_open=False,
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def local_date(self, instant: datetime) -> date:
        """The venue-local calendar date containing ``instant``."""
        return self._require_aware(instant).astimezone(self.zone).date()

    def is_open(self, instant: datetime) -> bool:
        """Whether the venue is trading at ``instant``.

        Args:
            instant: A timezone aware datetime. Naive datetimes are rejected rather
                than assumed to be UTC.
        """
        instant = self._require_aware(instant)
        if self.local_date(instant) in self.holidays:
            return False
        if self.always_open:
            return True
        return any(start <= instant < end for start, end in self._occurrences_around(instant))

    def next_open(self, instant: datetime) -> datetime | None:
        """The next instant at which trading starts, or ``None`` within the lookahead.

        Returns ``instant`` itself if the venue is already open, so callers can treat
        the result as "the start of the tradeable window covering or following now".
        """
        instant = self._require_aware(instant)
        if self.is_open(instant):
            return instant
        if self.always_open:
            return self._first_non_holiday_start(instant)
        candidates = sorted(
            start
            for start, _end in self._occurrences_forward(instant)
            if start >= instant and self.local_date(start) not in self.holidays
        )
        return candidates[0] if candidates else None

    def next_close(self, instant: datetime) -> datetime | None:
        """The end of the tradeable window containing ``instant``.

        Returns ``None`` when the venue is not open at ``instant``, or when it never
        closes within the lookahead window.
        """
        instant = self._require_aware(instant)
        if not self.is_open(instant):
            return None
        if self.always_open:
            return self._next_holiday_start(instant)
        for start, end in sorted(self._occurrences_around(instant)):
            if start <= instant < end:
                holiday_start = self._next_holiday_start(instant)
                if holiday_start is not None and holiday_start < end:
                    return holiday_start
                return end
        return None

    # ------------------------------------------------------------------
    # occurrence expansion
    # ------------------------------------------------------------------

    def _occurrences_around(self, instant: datetime) -> list[tuple[datetime, datetime]]:
        """Concrete session windows that could contain ``instant``.

        Sessions are expanded over the surrounding fortnight so that a window opened
        in the previous week and still running is found.
        """
        anchor = instant.astimezone(self.zone).date()
        return self._expand(anchor - timedelta(days=_DAYS_IN_WEEK + 1), days=_DAYS_IN_WEEK * 2 + 2)

    def _occurrences_forward(self, instant: datetime) -> list[tuple[datetime, datetime]]:
        anchor = instant.astimezone(self.zone).date()
        return self._expand(anchor - timedelta(days=1), days=_LOOKAHEAD_DAYS)

    def _expand(self, start_date: date, days: int) -> list[tuple[datetime, datetime]]:
        """Materialise every session occurrence opening within ``days`` of ``start_date``."""
        zone = self.zone
        windows: list[tuple[datetime, datetime]] = []
        for offset in range(days):
            day = start_date + timedelta(days=offset)
            for session in self.sessions:
                if day.weekday() != int(session.open_day):
                    continue
                open_at = datetime.combine(day, session.open_time, tzinfo=zone)
                close_day = day + timedelta(days=session.spans_days)
                close_at = datetime.combine(close_day, session.close_time, tzinfo=zone)
                windows.append((open_at, close_at))
        return windows

    def _next_holiday_start(self, instant: datetime) -> datetime | None:
        """Midnight local time at the start of the next holiday, if one is near."""
        zone = self.zone
        current = instant.astimezone(zone).date()
        for offset in range(_LOOKAHEAD_DAYS):
            day = current + timedelta(days=offset)
            if day in self.holidays:
                start = datetime.combine(day, time.min, tzinfo=zone)
                if start > instant:
                    return start
        return None

    def _first_non_holiday_start(self, instant: datetime) -> datetime | None:
        """For a continuous venue sitting inside a holiday, the next trading midnight."""
        zone = self.zone
        current = instant.astimezone(zone).date()
        for offset in range(_LOOKAHEAD_DAYS):
            day = current + timedelta(days=offset)
            if day not in self.holidays:
                start = datetime.combine(day, time.min, tzinfo=zone)
                return max(start, instant)
        return None

    @staticmethod
    def _require_aware(instant: datetime) -> datetime:
        if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
            raise DomainError(
                "schedule queries require a timezone aware datetime; a naive value "
                "would silently be interpreted in the host's local zone"
            )
        return instant

    # ------------------------------------------------------------------
    # serialisation
    # ------------------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        """Serialise for storage in a JSONB column."""
        return {
            "timezone": self.timezone,
            "always_open": self.always_open,
            "sessions": [session.to_mapping() for session in self.sessions],
            "holidays": [day.isoformat() for day in sorted(self.holidays)],
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild a schedule from :meth:`to_mapping` output."""
        if "timezone" not in data:
            raise DomainError("schedule mapping is missing 'timezone'")
        raw_sessions = data.get("sessions", ())
        if not isinstance(raw_sessions, list | tuple):
            raise DomainError(f"schedule 'sessions' must be a list, got {type(raw_sessions)}")
        raw_holidays = data.get("holidays", ())
        if not isinstance(raw_holidays, list | tuple):
            raise DomainError(f"schedule 'holidays' must be a list, got {type(raw_holidays)}")
        return cls(
            timezone=str(data["timezone"]),
            sessions=tuple(WeeklySession.from_mapping(item) for item in raw_sessions),
            holidays=frozenset(_parse_date(item) for item in raw_holidays),
            always_open=bool(data.get("always_open", False)),
        )


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        raise DomainError(f"holiday must be a date, not a datetime: {value!r}")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise DomainError(f"holiday {value!r} is not an ISO 8601 date") from None
    raise DomainError(f"cannot interpret {value!r} as a date")


def _week_offset_seconds(day: Weekday, at: time) -> int:
    """Seconds from Monday 00:00 to ``day`` at ``at``, used for overlap checks."""
    return int(day) * 24 * 3600 + at.hour * 3600 + at.minute * 60 + at.second
