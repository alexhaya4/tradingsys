"""Tests for trading schedules.

The interesting cases are the ones that break naive implementations: a session that
wraps the week boundary, the two days a year when the wall clock boundary moves
relative to UTC, and holidays that interrupt an already open session.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tradingsys.core.errors import DomainError
from tradingsys.core.schedule import TradingSchedule, Weekday, WeeklySession

NEW_YORK = ZoneInfo("America/New_York")

FOREX_SESSION = WeeklySession(
    open_day=Weekday.SUNDAY,
    open_time=time(17, 0),
    close_day=Weekday.FRIDAY,
    close_time=time(17, 0),
)


def forex_schedule(holidays: tuple[date, ...] = ()) -> TradingSchedule:
    return TradingSchedule.weekly(
        timezone="America/New_York", sessions=[FOREX_SESSION], holidays=holidays
    )


class TestWeeklySession:
    def test_span_of_a_wrapping_session(self) -> None:
        assert FOREX_SESSION.span == timedelta(days=5)
        assert FOREX_SESSION.spans_days == 5

    def test_span_within_a_single_day(self) -> None:
        session = WeeklySession(Weekday.MONDAY, time(9, 30), Weekday.MONDAY, time(16, 0))
        assert session.span == timedelta(hours=6, minutes=30)
        assert session.spans_days == 0

    def test_session_closing_at_its_own_opening_time_covers_a_full_week(self) -> None:
        session = WeeklySession(Weekday.MONDAY, time(0, 0), Weekday.MONDAY, time(0, 0))
        assert session.span == timedelta(days=7)

    def test_rejects_aware_times(self) -> None:
        with pytest.raises(DomainError, match="naive wall clock time"):
            WeeklySession(Weekday.MONDAY, time(9, 0, tzinfo=UTC), Weekday.MONDAY, time(17, 0))

    def test_rejects_microseconds(self) -> None:
        with pytest.raises(DomainError, match="microseconds"):
            WeeklySession(Weekday.MONDAY, time(9, 0, 0, 1), Weekday.MONDAY, time(17, 0))

    def test_mapping_round_trip(self) -> None:
        assert WeeklySession.from_mapping(FOREX_SESSION.to_mapping()) == FOREX_SESSION

    def test_from_mapping_rejects_missing_keys(self) -> None:
        with pytest.raises(DomainError, match="missing keys"):
            WeeklySession.from_mapping({"open_day": "MONDAY"})

    def test_from_mapping_rejects_bad_weekday(self) -> None:
        payload = dict(FOREX_SESSION.to_mapping())
        payload["open_day"] = "FUNDAY"
        with pytest.raises(DomainError, match="not a valid weekday name"):
            WeeklySession.from_mapping(payload)

    def test_from_mapping_accepts_weekday_numbers(self) -> None:
        payload: dict[str, object] = dict(FOREX_SESSION.to_mapping())
        payload["open_day"] = 6
        assert WeeklySession.from_mapping(payload).open_day is Weekday.SUNDAY

    def test_from_mapping_rejects_bad_time(self) -> None:
        payload = dict(FOREX_SESSION.to_mapping())
        payload["close_time"] = "25:00"
        with pytest.raises(DomainError, match="ISO 8601 time"):
            WeeklySession.from_mapping(payload)


class TestValidation:
    def test_rejects_unknown_timezone(self) -> None:
        with pytest.raises(DomainError, match="not a known IANA timezone"):
            TradingSchedule.weekly(timezone="Mars/Olympus", sessions=[FOREX_SESSION])

    def test_rejects_sessions_on_a_continuous_schedule(self) -> None:
        with pytest.raises(DomainError, match="must not declare weekly sessions"):
            TradingSchedule(timezone="UTC", sessions=(FOREX_SESSION,), always_open=True)

    def test_rejects_a_schedule_with_no_sessions(self) -> None:
        with pytest.raises(DomainError, match="at least one weekly session"):
            TradingSchedule(timezone="UTC")

    def test_rejects_overlapping_sessions(self) -> None:
        with pytest.raises(DomainError, match="sessions overlap"):
            TradingSchedule.weekly(
                timezone="UTC",
                sessions=[
                    WeeklySession(Weekday.MONDAY, time(9, 0), Weekday.MONDAY, time(17, 0)),
                    WeeklySession(Weekday.MONDAY, time(16, 0), Weekday.MONDAY, time(18, 0)),
                ],
            )

    def test_rejects_sessions_overlapping_across_the_week_boundary(self) -> None:
        with pytest.raises(DomainError, match="sessions overlap"):
            TradingSchedule.weekly(
                timezone="UTC",
                sessions=[
                    WeeklySession(Weekday.SUNDAY, time(20, 0), Weekday.MONDAY, time(4, 0)),
                    WeeklySession(Weekday.MONDAY, time(2, 0), Weekday.MONDAY, time(6, 0)),
                ],
            )

    def test_accepts_adjacent_sessions(self) -> None:
        schedule = TradingSchedule.weekly(
            timezone="UTC",
            sessions=[
                WeeklySession(Weekday.MONDAY, time(9, 0), Weekday.MONDAY, time(12, 0)),
                WeeklySession(Weekday.MONDAY, time(12, 0), Weekday.MONDAY, time(17, 0)),
            ],
        )
        assert len(schedule.sessions) == 2

    def test_rejects_datetime_holidays(self) -> None:
        with pytest.raises(DomainError, match="plain dates"):
            TradingSchedule.weekly(
                timezone="UTC",
                sessions=[FOREX_SESSION],
                holidays=[datetime(2025, 1, 1, tzinfo=UTC)],
            )

    def test_naive_query_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            forex_schedule().is_open(datetime(2025, 3, 5, 12, 0))  # noqa: DTZ001


class TestForexWeek:
    @pytest.mark.parametrize(
        ("local", "expected"),
        [
            (datetime(2025, 3, 5, 12, 0, tzinfo=NEW_YORK), True),  # Wednesday midday
            (datetime(2025, 3, 7, 16, 59, tzinfo=NEW_YORK), True),  # a minute before the close
            (datetime(2025, 3, 7, 17, 0, tzinfo=NEW_YORK), False),  # Friday close is exclusive
            (datetime(2025, 3, 8, 12, 0, tzinfo=NEW_YORK), False),  # Saturday
            (datetime(2025, 3, 9, 16, 59, tzinfo=NEW_YORK), False),  # Sunday, before the open
            (datetime(2025, 3, 9, 17, 0, tzinfo=NEW_YORK), True),  # Sunday open is inclusive
            (datetime(2025, 3, 10, 3, 0, tzinfo=NEW_YORK), True),  # inside the wrapped session
        ],
    )
    def test_open_and_closed_moments(self, local: datetime, expected: bool) -> None:
        assert forex_schedule().is_open(local) is expected

    def test_evaluated_correctly_from_a_utc_instant(self) -> None:
        # 21:59 UTC on Friday is 16:59 New York in March: still open.
        assert forex_schedule().is_open(datetime(2025, 3, 7, 21, 59, tzinfo=UTC))
        assert not forex_schedule().is_open(datetime(2025, 3, 7, 22, 0, tzinfo=UTC))

    def test_boundary_moves_with_daylight_saving(self) -> None:
        schedule = forex_schedule()
        # The week of 2025-01-10 is standard time: the Friday close is 22:00 UTC.
        assert schedule.is_open(datetime(2025, 1, 10, 21, 59, tzinfo=UTC))
        assert not schedule.is_open(datetime(2025, 1, 10, 22, 0, tzinfo=UTC))
        # The week of 2025-07-11 is daylight time: the same local close is 21:00 UTC.
        assert schedule.is_open(datetime(2025, 7, 11, 20, 59, tzinfo=UTC))
        assert not schedule.is_open(datetime(2025, 7, 11, 21, 0, tzinfo=UTC))

    def test_session_open_across_the_spring_forward_transition(self) -> None:
        # US clocks jump from 02:00 to 03:00 local on 2025-03-09, inside the weekend gap,
        # and the following week must still be evaluated correctly.
        schedule = forex_schedule()
        assert schedule.is_open(datetime(2025, 3, 9, 21, 0, tzinfo=UTC))  # 17:00 EDT Sunday
        assert not schedule.is_open(datetime(2025, 3, 9, 20, 59, tzinfo=UTC))

    def test_session_open_across_the_fall_back_transition(self) -> None:
        schedule = forex_schedule()
        # 2025-11-02 clocks go back; the Sunday open is 17:00 EST, which is 22:00 UTC.
        assert schedule.is_open(datetime(2025, 11, 2, 22, 0, tzinfo=UTC))
        assert not schedule.is_open(datetime(2025, 11, 2, 21, 59, tzinfo=UTC))


class TestHolidays:
    def test_holiday_closes_an_otherwise_open_day(self) -> None:
        schedule = forex_schedule(holidays=(date(2025, 12, 25),))
        assert not schedule.is_open(datetime(2025, 12, 25, 12, 0, tzinfo=NEW_YORK))
        assert schedule.is_open(datetime(2025, 12, 24, 12, 0, tzinfo=NEW_YORK))

    def test_holiday_is_evaluated_in_venue_local_time(self) -> None:
        schedule = forex_schedule(holidays=(date(2025, 12, 25),))
        # 03:00 UTC on the 25th is still the 24th in New York, so trading continues.
        assert schedule.is_open(datetime(2025, 12, 25, 3, 0, tzinfo=UTC))
        assert not schedule.is_open(datetime(2025, 12, 25, 15, 0, tzinfo=UTC))

    def test_holiday_closes_a_continuous_venue(self) -> None:
        schedule = TradingSchedule.continuous(holidays=[date(2025, 6, 1)])
        assert not schedule.is_open(datetime(2025, 6, 1, 12, 0, tzinfo=UTC))
        assert schedule.is_open(datetime(2025, 6, 2, 12, 0, tzinfo=UTC))


class TestContinuous:
    def test_always_open(self) -> None:
        schedule = TradingSchedule.continuous()
        for instant in (
            datetime(2025, 3, 8, 3, 0, tzinfo=UTC),
            datetime(2025, 12, 25, 0, 0, tzinfo=UTC),
            datetime(2025, 7, 4, 23, 59, tzinfo=UTC),
        ):
            assert schedule.is_open(instant)

    def test_next_close_is_none_without_holidays(self) -> None:
        assert TradingSchedule.continuous().next_close(datetime(2025, 5, 1, tzinfo=UTC)) is None

    def test_next_close_is_the_holiday_start(self) -> None:
        schedule = TradingSchedule.continuous(holidays=[date(2025, 6, 1)])
        closes = schedule.next_close(datetime(2025, 5, 30, 12, 0, tzinfo=UTC))
        assert closes == datetime(2025, 6, 1, 0, 0, tzinfo=ZoneInfo("UTC"))

    def test_next_open_during_a_holiday_is_the_following_midnight(self) -> None:
        schedule = TradingSchedule.continuous(holidays=[date(2025, 6, 1)])
        opens = schedule.next_open(datetime(2025, 6, 1, 12, 0, tzinfo=UTC))
        assert opens == datetime(2025, 6, 2, 0, 0, tzinfo=ZoneInfo("UTC"))


class TestNextOpenAndClose:
    def test_next_open_when_already_open_is_now(self) -> None:
        now = datetime(2025, 3, 5, 12, 0, tzinfo=NEW_YORK)
        assert forex_schedule().next_open(now) == now

    def test_next_open_from_a_saturday(self) -> None:
        opens = forex_schedule().next_open(datetime(2025, 3, 8, 12, 0, tzinfo=NEW_YORK))
        assert opens == datetime(2025, 3, 9, 17, 0, tzinfo=NEW_YORK)

    def test_next_open_skips_a_holiday(self) -> None:
        schedule = forex_schedule(holidays=(date(2025, 3, 9), date(2025, 3, 10)))
        opens = schedule.next_open(datetime(2025, 3, 8, 12, 0, tzinfo=NEW_YORK))
        assert opens is not None
        assert opens.astimezone(NEW_YORK).date() == date(2025, 3, 16)

    def test_next_close_from_inside_the_session(self) -> None:
        closes = forex_schedule().next_close(datetime(2025, 3, 5, 12, 0, tzinfo=NEW_YORK))
        assert closes == datetime(2025, 3, 7, 17, 0, tzinfo=NEW_YORK)

    def test_next_close_when_closed_is_none(self) -> None:
        assert forex_schedule().next_close(datetime(2025, 3, 8, 12, 0, tzinfo=NEW_YORK)) is None

    def test_next_close_stops_at_an_intervening_holiday(self) -> None:
        schedule = forex_schedule(holidays=(date(2025, 3, 6),))
        closes = schedule.next_close(datetime(2025, 3, 5, 12, 0, tzinfo=NEW_YORK))
        assert closes == datetime(2025, 3, 6, 0, 0, tzinfo=NEW_YORK)


class TestMultipleSessions:
    def test_an_exchange_with_a_lunch_break(self) -> None:
        schedule = TradingSchedule.weekly(
            timezone="Asia/Tokyo",
            sessions=[
                WeeklySession(day, time(9, 0), day, time(11, 30))
                for day in (Weekday.MONDAY, Weekday.TUESDAY)
            ]
            + [
                WeeklySession(day, time(12, 30), day, time(15, 0))
                for day in (Weekday.MONDAY, Weekday.TUESDAY)
            ],
        )
        tokyo = ZoneInfo("Asia/Tokyo")
        assert schedule.is_open(datetime(2025, 3, 3, 10, 0, tzinfo=tokyo))
        assert not schedule.is_open(datetime(2025, 3, 3, 12, 0, tzinfo=tokyo))
        assert schedule.is_open(datetime(2025, 3, 3, 14, 0, tzinfo=tokyo))
        assert not schedule.is_open(datetime(2025, 3, 5, 10, 0, tzinfo=tokyo))


class TestSerialisation:
    def test_round_trip(self) -> None:
        schedule = forex_schedule(holidays=(date(2025, 12, 25), date(2026, 1, 1)))
        assert TradingSchedule.from_mapping(schedule.to_mapping()) == schedule

    def test_continuous_round_trip(self) -> None:
        schedule = TradingSchedule.continuous(holidays=[date(2025, 6, 1)])
        assert TradingSchedule.from_mapping(schedule.to_mapping()) == schedule

    def test_mapping_is_json_safe(self) -> None:
        payload = json.dumps(forex_schedule(holidays=(date(2025, 12, 25),)).to_mapping())
        assert TradingSchedule.from_mapping(json.loads(payload)) == forex_schedule(
            holidays=(date(2025, 12, 25),)
        )

    def test_from_mapping_requires_a_timezone(self) -> None:
        with pytest.raises(DomainError, match="missing 'timezone'"):
            TradingSchedule.from_mapping({"sessions": []})

    def test_from_mapping_rejects_a_non_list_of_sessions(self) -> None:
        with pytest.raises(DomainError, match="'sessions' must be a list"):
            TradingSchedule.from_mapping({"timezone": "UTC", "sessions": {}})

    def test_from_mapping_rejects_a_bad_holiday(self) -> None:
        with pytest.raises(DomainError, match="ISO 8601 date"):
            TradingSchedule.from_mapping(
                {"timezone": "UTC", "always_open": True, "holidays": ["not-a-date"]}
            )


class TestLocalDate:
    def test_local_date_uses_the_schedule_timezone(self) -> None:
        schedule = forex_schedule()
        assert schedule.local_date(datetime(2025, 3, 5, 2, 0, tzinfo=UTC)) == date(2025, 3, 4)


class TestOpenIntervals:
    """The window view of a schedule, which is what gap detection consumes."""

    def test_a_window_inside_a_session_is_returned_whole(self) -> None:
        schedule = forex_schedule()
        window = (datetime(2025, 3, 5, 8, tzinfo=UTC), datetime(2025, 3, 5, 16, tzinfo=UTC))
        assert schedule.open_intervals(*window) == (window,)

    def test_the_weekend_is_excluded(self) -> None:
        schedule = forex_schedule()
        intervals = schedule.open_intervals(
            datetime(2025, 3, 7, 12, tzinfo=UTC), datetime(2025, 3, 10, 12, tzinfo=UTC)
        )
        assert intervals == (
            (datetime(2025, 3, 7, 12, tzinfo=UTC), datetime(2025, 3, 7, 22, tzinfo=UTC)),
            (datetime(2025, 3, 9, 21, tzinfo=UTC), datetime(2025, 3, 10, 12, tzinfo=UTC)),
        )

    def test_a_continuous_venue_returns_the_window(self) -> None:
        schedule = TradingSchedule.continuous()
        window = (datetime(2025, 3, 7, 12, tzinfo=UTC), datetime(2025, 3, 10, 12, tzinfo=UTC))
        assert schedule.open_intervals(*window) == (window,)

    def test_a_continuous_venue_is_split_by_a_holiday(self) -> None:
        schedule = TradingSchedule.continuous(timezone="UTC", holidays=[date(2025, 3, 8)])
        intervals = schedule.open_intervals(
            datetime(2025, 3, 7, 12, tzinfo=UTC), datetime(2025, 3, 9, 12, tzinfo=UTC)
        )
        assert intervals == (
            (datetime(2025, 3, 7, 12, tzinfo=UTC), datetime(2025, 3, 8, tzinfo=UTC)),
            (datetime(2025, 3, 9, tzinfo=UTC), datetime(2025, 3, 9, 12, tzinfo=UTC)),
        )

    def test_touching_sessions_are_reported_as_one_interval(self) -> None:
        # A venue with a morning and an afternoon session that meet at noon quotes
        # continuously across the boundary. Reporting two intervals would invite a
        # caller to treat the seam as closed.
        schedule = TradingSchedule.weekly(
            timezone="UTC",
            sessions=[
                WeeklySession(
                    open_day=Weekday.MONDAY,
                    open_time=time(8, 0),
                    close_day=Weekday.MONDAY,
                    close_time=time(12, 0),
                ),
                WeeklySession(
                    open_day=Weekday.MONDAY,
                    open_time=time(12, 0),
                    close_day=Weekday.MONDAY,
                    close_time=time(16, 0),
                ),
            ],
        )
        intervals = schedule.open_intervals(
            datetime(2025, 3, 3, tzinfo=UTC), datetime(2025, 3, 4, tzinfo=UTC)
        )
        assert intervals == (
            (datetime(2025, 3, 3, 8, tzinfo=UTC), datetime(2025, 3, 3, 16, tzinfo=UTC)),
        )

    def test_a_session_already_open_at_the_window_start_is_clipped_not_dropped(self) -> None:
        # The session opened on Sunday evening. A window starting on Wednesday must
        # still see it, which is why expansion begins a week early.
        schedule = forex_schedule()
        intervals = schedule.open_intervals(
            datetime(2025, 3, 5, tzinfo=UTC), datetime(2025, 3, 5, 6, tzinfo=UTC)
        )
        assert intervals == (
            (datetime(2025, 3, 5, tzinfo=UTC), datetime(2025, 3, 5, 6, tzinfo=UTC)),
        )

    def test_an_empty_window_has_no_intervals(self) -> None:
        instant = datetime(2025, 3, 5, tzinfo=UTC)
        assert forex_schedule().open_intervals(instant, instant) == ()

    def test_a_reversed_window_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="is before start"):
            forex_schedule().open_intervals(
                datetime(2025, 3, 6, tzinfo=UTC), datetime(2025, 3, 5, tzinfo=UTC)
            )

    def test_naive_bounds_are_rejected(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            forex_schedule().open_intervals(datetime(2025, 3, 5), datetime(2025, 3, 6))  # noqa: DTZ001

    def test_a_window_entirely_inside_a_holiday_is_empty(self) -> None:
        schedule = TradingSchedule.continuous(timezone="UTC", holidays=[date(2025, 3, 8)])
        assert (
            schedule.open_intervals(
                datetime(2025, 3, 8, 2, tzinfo=UTC), datetime(2025, 3, 8, 20, tzinfo=UTC)
            )
            == ()
        )


class TestHolidayParsing:
    def test_a_date_object_is_accepted(self) -> None:
        schedule = TradingSchedule.from_mapping(
            {"timezone": "UTC", "always_open": True, "holidays": [date(2025, 12, 25)]}
        )
        assert schedule.holidays == frozenset({date(2025, 12, 25)})

    def test_a_datetime_is_rejected(self) -> None:
        # datetime is a subclass of date, so an unguarded isinstance check would accept
        # this and then store a value that never compares equal to a local date.
        with pytest.raises(DomainError, match="not a datetime"):
            TradingSchedule.from_mapping(
                {
                    "timezone": "UTC",
                    "always_open": True,
                    "holidays": [datetime(2025, 12, 25, tzinfo=UTC)],
                }
            )

    def test_a_non_date_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="cannot interpret"):
            TradingSchedule.from_mapping(
                {"timezone": "UTC", "always_open": True, "holidays": [20251225]}
            )

    def test_from_mapping_rejects_a_non_list_of_holidays(self) -> None:
        with pytest.raises(DomainError, match="'holidays' must be a list"):
            TradingSchedule.from_mapping({"timezone": "UTC", "holidays": {}})
