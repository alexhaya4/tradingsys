"""Tests for gap detection.

The point of these is not that gaps are found, which is easy, but that non-gaps are
not. Weekends, holidays, and session boundaries are absences by definition. A detector
that reports them fires every Saturday, gets ignored inside a fortnight, and is then
useless on the morning a feed genuinely dies.

The daylight saving cases are deliberate. Session boundaries are wall clock times in
the venue's own zone, so the UTC instant of the Friday close moves by an hour twice a
year. Logic that fixes the boundary in UTC passes every test written in a single season
and is wrong for half of each year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.factories import CRYPTO_WEEK, FOREX_WEEK
from tradingsys.core.errors import DomainError
from tradingsys.core.schedule import Interval, TradingSchedule
from tradingsys.marketdata.gaps import (
    Gap,
    coverage_from_timestamps,
    find_gaps,
    merge_coverage,
)

MINUTE = timedelta(minutes=1)


def utc(*parts: int) -> datetime:
    """A UTC instant from year, month, day and optionally hour, minute, second.

    Variadic rather than six named parameters so that reading a case is reading a
    timestamp, which is what these tests are actually about.
    """
    year, month, day, *rest = parts
    hour, minute, second = (*rest, 0, 0, 0)[:3]
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def covering(*spans: tuple[datetime, datetime]) -> list[Interval]:
    return list(spans)


class TestWeekendsAreNotGaps:
    def test_a_silent_weekend_reports_nothing(self) -> None:
        # 2025-03-07 is a Friday. The market closes 17:00 New York, which is 22:00 UTC
        # in March, and reopens 17:00 Sunday. Holding nothing in between is correct.
        friday_close = utc(2025, 3, 7, 22)
        sunday_open = utc(2025, 3, 9, 21)  # 17:00 New York, now on daylight time
        coverage = covering(
            (utc(2025, 3, 7, 12), friday_close),
            (sunday_open, utc(2025, 3, 10, 12)),
        )
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 3, 7, 12), end=utc(2025, 3, 10, 12))
        assert gaps == ()

    def test_the_same_absence_inside_a_session_is_a_gap(self) -> None:
        # The control for the test above: an identical two day hole, positioned midweek,
        # must be reported.
        coverage = covering(
            (utc(2025, 3, 4, 12), utc(2025, 3, 4, 13)),
            (utc(2025, 3, 6, 13), utc(2025, 3, 6, 14)),
        )
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 3, 4, 12), end=utc(2025, 3, 6, 14))
        assert len(gaps) == 1
        assert gaps[0].start == utc(2025, 3, 4, 13)
        assert gaps[0].end == utc(2025, 3, 6, 13)

    def test_a_gap_spanning_a_weekend_reports_only_the_open_parts(self) -> None:
        # Friday afternoon and Monday morning are both missing. That is two gaps with
        # the weekend between them, not one long one.
        coverage = covering(
            (utc(2025, 3, 7, 10), utc(2025, 3, 7, 12)),
            (utc(2025, 3, 10, 12), utc(2025, 3, 10, 14)),
        )
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 3, 7, 10), end=utc(2025, 3, 10, 14))
        assert len(gaps) == 2
        assert gaps[0].start == utc(2025, 3, 7, 12)
        assert gaps[0].end == utc(2025, 3, 7, 22)  # Friday close, 17:00 New York
        assert gaps[1].start == utc(2025, 3, 9, 21)  # Sunday open, 17:00 New York
        assert gaps[1].end == utc(2025, 3, 10, 12)


class TestHolidaysAreNotGaps:
    def test_a_holiday_is_not_reported(self) -> None:
        schedule = TradingSchedule.weekly(
            timezone="America/New_York",
            sessions=FOREX_WEEK.sessions,
            holidays=[date(2025, 12, 25)],
        )
        coverage = covering(
            (utc(2025, 12, 24, 12), utc(2025, 12, 25, 5)),
            (utc(2025, 12, 26, 5), utc(2025, 12, 26, 12)),
        )
        gaps = find_gaps(schedule, coverage, start=utc(2025, 12, 24, 12), end=utc(2025, 12, 26, 12))
        assert gaps == ()

    def test_without_the_holiday_the_same_absence_is_a_gap(self) -> None:
        coverage = covering(
            (utc(2025, 12, 24, 12), utc(2025, 12, 25, 5)),
            (utc(2025, 12, 26, 5), utc(2025, 12, 26, 12)),
        )
        gaps = find_gaps(
            FOREX_WEEK, coverage, start=utc(2025, 12, 24, 12), end=utc(2025, 12, 26, 12)
        )
        assert gaps != ()

    def test_a_holiday_is_evaluated_in_venue_local_time(self) -> None:
        # 03:00 UTC on the 25th is still the 24th in New York, so that hour is open and
        # an absence in it is a real gap.
        schedule = TradingSchedule.weekly(
            timezone="America/New_York",
            sessions=FOREX_WEEK.sessions,
            holidays=[date(2025, 12, 25)],
        )
        coverage = covering((utc(2025, 12, 24, 12), utc(2025, 12, 25, 1)))
        gaps = find_gaps(schedule, coverage, start=utc(2025, 12, 24, 12), end=utc(2025, 12, 25, 12))
        # One gap, ending where the holiday begins locally rather than at 00:00 UTC.
        # The remaining eight hours of the window are the holiday itself and are silent.
        assert len(gaps) == 1
        assert gaps[0].start == utc(2025, 12, 25, 1)
        assert gaps[0].end == utc(2025, 12, 25, 5)  # midnight New York


class TestDaylightSaving:
    """The Friday close is 17:00 New York, which is a different UTC instant per season.

    Logic that hard codes the boundary in UTC passes in one season and is an hour wrong
    for the other half of the year. These pin both sides.
    """

    def test_the_friday_close_is_2200_utc_in_winter(self) -> None:
        # 2025-01-10, standard time. Data stopping at 21:30 leaves half an hour of open
        # session uncovered, so that is a gap.
        coverage = covering((utc(2025, 1, 10, 12), utc(2025, 1, 10, 21, 30)))
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 1, 10, 12), end=utc(2025, 1, 11, 12))
        assert len(gaps) == 1
        assert gaps[0].start == utc(2025, 1, 10, 21, 30)
        assert gaps[0].end == utc(2025, 1, 10, 22)

    def test_the_friday_close_is_2100_utc_in_summer(self) -> None:
        # 2025-07-11, daylight time. The identical 21:30 stop is now after the close,
        # so there is nothing to report. Same clock time, opposite answer.
        coverage = covering((utc(2025, 7, 11, 12), utc(2025, 7, 11, 21)))
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 7, 11, 12), end=utc(2025, 7, 12, 12))
        assert gaps == ()

    def test_a_summer_stop_before_the_close_is_still_caught(self) -> None:
        coverage = covering((utc(2025, 7, 11, 12), utc(2025, 7, 11, 20, 30)))
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 7, 11, 12), end=utc(2025, 7, 12, 12))
        assert len(gaps) == 1
        assert gaps[0].end == utc(2025, 7, 11, 21)

    def test_spring_forward_weekend(self) -> None:
        # US clocks jump 02:00 to 03:00 local on 2025-03-09, inside the weekend gap.
        # The Sunday open lands at 21:00 UTC rather than 22:00.
        coverage = covering((utc(2025, 3, 9, 21), utc(2025, 3, 10, 6)))
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 3, 8), end=utc(2025, 3, 10, 6))
        assert gaps == ()

    def test_spring_forward_open_is_not_an_hour_early(self) -> None:
        # Covering from 22:00 UTC misses the first hour of the session, which opened at
        # 21:00 UTC that week.
        coverage = covering((utc(2025, 3, 9, 22), utc(2025, 3, 10, 6)))
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 3, 8), end=utc(2025, 3, 10, 6))
        assert len(gaps) == 1
        assert gaps[0].start == utc(2025, 3, 9, 21)
        assert gaps[0].end == utc(2025, 3, 9, 22)

    def test_fall_back_weekend(self) -> None:
        # 2025-11-02, clocks go back, so the Sunday open moves to 22:00 UTC.
        coverage = covering((utc(2025, 11, 2, 22), utc(2025, 11, 3, 6)))
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 11, 1), end=utc(2025, 11, 3, 6))
        assert gaps == ()

    def test_fall_back_open_is_not_an_hour_late(self) -> None:
        # Data from 21:00 UTC is an hour before the session opened, and the hour from
        # 22:00 is covered, so there is still nothing to report.
        coverage = covering((utc(2025, 11, 2, 21), utc(2025, 11, 3, 6)))
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 11, 1), end=utc(2025, 11, 3, 6))
        assert gaps == ()

    def test_the_thursday_to_friday_span_is_an_hour_shorter_across_spring_forward(
        self,
    ) -> None:
        # A whole week of coverage either side of a transition must report nothing,
        # which fails if session expansion adds fixed 24 hour days.
        for monday in (utc(2025, 3, 10), utc(2025, 11, 3)):
            coverage = covering((monday, monday + timedelta(days=4)))
            gaps = find_gaps(FOREX_WEEK, coverage, start=monday, end=monday + timedelta(days=4))
            assert gaps == (), f"week of {monday.date()} reported {gaps}"


class TestContinuousVenues:
    def test_crypto_has_no_weekend_exemption(self) -> None:
        # The same Saturday absence that is correct for forex is a real gap here.
        coverage = covering(
            (utc(2025, 3, 7, 12), utc(2025, 3, 7, 22)),
            (utc(2025, 3, 9, 21), utc(2025, 3, 10, 12)),
        )
        gaps = find_gaps(CRYPTO_WEEK, coverage, start=utc(2025, 3, 7, 12), end=utc(2025, 3, 10, 12))
        assert len(gaps) == 1
        assert gaps[0].start == utc(2025, 3, 7, 22)
        assert gaps[0].end == utc(2025, 3, 9, 21)

    def test_a_continuous_venue_with_no_data_is_one_long_gap(self) -> None:
        gaps = find_gaps(CRYPTO_WEEK, [], start=utc(2025, 3, 1), end=utc(2025, 3, 2))
        assert len(gaps) == 1
        assert gaps[0].duration == timedelta(days=1)


class TestReporting:
    def test_short_absences_are_below_the_floor(self) -> None:
        # Ticks are irregular. Without a floor every quiet second is a gap.
        coverage = covering(
            (utc(2025, 3, 5, 12), utc(2025, 3, 5, 12, 30)),
            (utc(2025, 3, 5, 12, 30, 30), utc(2025, 3, 5, 13)),
        )
        gaps = find_gaps(
            FOREX_WEEK,
            coverage,
            start=utc(2025, 3, 5, 12),
            end=utc(2025, 3, 5, 13),
            minimum=MINUTE,
        )
        assert gaps == ()

    def test_the_floor_is_adjustable(self) -> None:
        coverage = covering(
            (utc(2025, 3, 5, 12), utc(2025, 3, 5, 12, 30)),
            (utc(2025, 3, 5, 12, 30, 30), utc(2025, 3, 5, 13)),
        )
        gaps = find_gaps(
            FOREX_WEEK,
            coverage,
            start=utc(2025, 3, 5, 12),
            end=utc(2025, 3, 5, 13),
            minimum=timedelta(seconds=10),
        )
        assert len(gaps) == 1
        assert gaps[0].duration == timedelta(seconds=30)

    def test_gaps_come_back_in_order(self) -> None:
        coverage = covering(
            (utc(2025, 3, 5, 12), utc(2025, 3, 5, 13)),
            (utc(2025, 3, 5, 14), utc(2025, 3, 5, 15)),
            (utc(2025, 3, 5, 16), utc(2025, 3, 5, 17)),
        )
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 3, 5, 12), end=utc(2025, 3, 5, 17))
        assert [gap.start for gap in gaps] == [utc(2025, 3, 5, 13), utc(2025, 3, 5, 15)]

    def test_overlapping_coverage_is_handled(self) -> None:
        # Two ingestion runs covering the same period must not produce a phantom gap.
        coverage = covering(
            (utc(2025, 3, 5, 12), utc(2025, 3, 5, 14)),
            (utc(2025, 3, 5, 13), utc(2025, 3, 5, 15)),
        )
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 3, 5, 12), end=utc(2025, 3, 5, 15))
        assert gaps == ()

    def test_unsorted_coverage_is_handled(self) -> None:
        coverage = covering(
            (utc(2025, 3, 5, 14), utc(2025, 3, 5, 15)),
            (utc(2025, 3, 5, 12), utc(2025, 3, 5, 14)),
        )
        gaps = find_gaps(FOREX_WEEK, coverage, start=utc(2025, 3, 5, 12), end=utc(2025, 3, 5, 15))
        assert gaps == ()


class TestGapValue:
    def test_a_gap_lists_the_hours_a_backfill_must_refetch(self) -> None:
        # Backfill is organised per hour, and an hour that is partly missing has to be
        # refetched whole, so the first hour is included even though it is partial.
        gap = Gap(utc(2025, 3, 5, 12, 30), utc(2025, 3, 5, 14, 15))
        assert gap.hours() == (
            utc(2025, 3, 5, 12),
            utc(2025, 3, 5, 13),
            utc(2025, 3, 5, 14),
        )

    def test_a_gap_inside_one_hour_lists_that_hour(self) -> None:
        gap = Gap(utc(2025, 3, 5, 12, 10), utc(2025, 3, 5, 12, 20))
        assert gap.hours() == (utc(2025, 3, 5, 12),)

    def test_an_empty_gap_is_refused(self) -> None:
        with pytest.raises(DomainError, match="positive duration"):
            Gap(utc(2025, 3, 5, 12), utc(2025, 3, 5, 12))

    def test_a_reversed_gap_is_refused(self) -> None:
        with pytest.raises(DomainError, match="positive duration"):
            Gap(utc(2025, 3, 5, 13), utc(2025, 3, 5, 12))

    def test_naive_bounds_are_refused(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            Gap(datetime(2025, 3, 5, 12), utc(2025, 3, 5, 13))  # noqa: DTZ001

    def test_a_gap_reads_as_a_period_in_a_log_line(self) -> None:
        # Gaps end up in alerts, where a bare repr of two datetimes is unreadable at
        # three in the morning.
        gap = Gap(utc(2025, 3, 5, 12, 30), utc(2025, 3, 5, 14))
        assert str(gap) == "2025-03-05T12:30:00+00:00 to 2025-03-05T14:00:00+00:00 (1:30:00)"


class TestCoverageFromTimestamps:
    def test_dense_timestamps_become_one_interval(self) -> None:
        moments = [utc(2025, 3, 5, 12) + timedelta(seconds=index) for index in range(10)]
        assert coverage_from_timestamps(moments, max_quiet=MINUTE) == ((moments[0], moments[-1]),)

    def test_a_quiet_period_splits_the_coverage(self) -> None:
        moments = [
            utc(2025, 3, 5, 12),
            utc(2025, 3, 5, 12, 0, 30),
            utc(2025, 3, 5, 13),
            utc(2025, 3, 5, 13, 0, 30),
        ]
        intervals = coverage_from_timestamps(moments, max_quiet=MINUTE)
        assert intervals == (
            (moments[0], moments[1]),
            (moments[2], moments[3]),
        )

    def test_unsorted_input_is_sorted(self) -> None:
        moments = [utc(2025, 3, 5, 12, 0, 30), utc(2025, 3, 5, 12)]
        assert coverage_from_timestamps(moments, max_quiet=MINUTE) == ((moments[1], moments[0]),)

    def test_no_timestamps_is_no_coverage(self) -> None:
        assert coverage_from_timestamps([], max_quiet=MINUTE) == ()

    def test_max_quiet_must_be_positive(self) -> None:
        with pytest.raises(DomainError, match="max_quiet must be positive"):
            coverage_from_timestamps([utc(2025, 3, 5, 12)], max_quiet=timedelta(0))

    def test_timestamps_feed_straight_into_gap_detection(self) -> None:
        # The two halves compose: observed ticks in, gaps out.
        ticks = [utc(2025, 3, 5, 12) + timedelta(seconds=index * 10) for index in range(6)]
        ticks += [utc(2025, 3, 5, 13) + timedelta(seconds=index * 10) for index in range(6)]
        gaps = find_gaps(
            FOREX_WEEK,
            coverage_from_timestamps(ticks, max_quiet=MINUTE),
            start=ticks[0],
            end=ticks[-1],
        )
        assert len(gaps) == 1
        assert gaps[0].start == utc(2025, 3, 5, 12, 0, 50)
        assert gaps[0].end == utc(2025, 3, 5, 13)


class TestMergeCoverage:
    def test_touching_intervals_merge(self) -> None:
        merged = merge_coverage(
            [
                (utc(2025, 3, 5, 12), utc(2025, 3, 5, 13)),
                (utc(2025, 3, 5, 13), utc(2025, 3, 5, 14)),
            ]
        )
        assert merged == ((utc(2025, 3, 5, 12), utc(2025, 3, 5, 14)),)

    def test_disjoint_intervals_stay_separate(self) -> None:
        merged = merge_coverage(
            [
                (utc(2025, 3, 5, 12), utc(2025, 3, 5, 13)),
                (utc(2025, 3, 5, 14), utc(2025, 3, 5, 15)),
            ]
        )
        assert len(merged) == 2

    def test_a_contained_interval_is_absorbed(self) -> None:
        merged = merge_coverage(
            [
                (utc(2025, 3, 5, 12), utc(2025, 3, 5, 16)),
                (utc(2025, 3, 5, 13), utc(2025, 3, 5, 14)),
            ]
        )
        assert merged == ((utc(2025, 3, 5, 12), utc(2025, 3, 5, 16)),)

    def test_a_reversed_interval_is_refused(self) -> None:
        with pytest.raises(DomainError, match="ends before it starts"):
            merge_coverage([(utc(2025, 3, 5, 13), utc(2025, 3, 5, 12))])


class TestWindowValidation:
    def test_a_reversed_window_is_refused(self) -> None:
        with pytest.raises(DomainError, match="is before start"):
            find_gaps(FOREX_WEEK, [], start=utc(2025, 3, 6), end=utc(2025, 3, 5))

    def test_a_negative_minimum_is_refused(self) -> None:
        with pytest.raises(DomainError, match="must not be negative"):
            find_gaps(
                FOREX_WEEK,
                [],
                start=utc(2025, 3, 5),
                end=utc(2025, 3, 6),
                minimum=timedelta(seconds=-1),
            )

    def test_an_empty_window_reports_nothing(self) -> None:
        assert find_gaps(FOREX_WEEK, [], start=utc(2025, 3, 5), end=utc(2025, 3, 5)) == ()


class TestALoneTickIsCoverage:
    """Found by wiring gap detection into the assembly, not by reading the module.

    An earlier version filtered zero width intervals unless every interval was zero
    width, so a lone tick on its own survived and a lone tick between two busy stretches
    vanished. The second is the reconnect case: a stream that delivers one quote and
    drops again reported no coverage at all, and the outage read as longer than it was.
    """

    def test_a_lone_tick_between_two_stretches_is_kept(self) -> None:
        base = utc(2025, 3, 5, 10)
        stamps = [
            base,
            base + timedelta(seconds=1),
            base + timedelta(minutes=5),
            base + timedelta(minutes=10),
            base + timedelta(minutes=10, seconds=1),
        ]

        covered = coverage_from_timestamps(stamps, max_quiet=timedelta(seconds=60))

        assert len(covered) == 3
        assert covered[1] == (base + timedelta(minutes=5), base + timedelta(minutes=5))

    def test_a_lone_tick_at_the_end_is_kept(self) -> None:
        base = utc(2025, 3, 5, 10)
        stamps = [base, base + timedelta(seconds=1), base + timedelta(minutes=5)]

        covered = coverage_from_timestamps(stamps, max_quiet=timedelta(seconds=60))

        assert len(covered) == 2
        assert covered[-1] == (base + timedelta(minutes=5), base + timedelta(minutes=5))

    def test_a_lone_tick_alone_is_still_kept(self) -> None:
        """The case the old code got right, pinned so the fix does not trade one for
        the other."""
        base = utc(2025, 3, 5, 10)

        covered = coverage_from_timestamps([base], max_quiet=timedelta(seconds=60))

        assert covered == ((base, base),)

    def test_the_instant_is_not_widened_to_max_quiet(self) -> None:
        """Widening would invent coverage we do not have, which is the error in the
        opposite direction and the reason the filter existed at all."""
        base = utc(2025, 3, 5, 10)

        (interval,) = coverage_from_timestamps([base], max_quiet=timedelta(minutes=5))

        assert interval[1] - interval[0] == timedelta(0)
