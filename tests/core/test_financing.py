"""Tests for carry cost conventions and their event schedules."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.factories import FOREX_WEEK
from tradingsys.core.errors import DomainError
from tradingsys.core.financing import FinancingModel, FinancingSpec
from tradingsys.core.schedule import TradingSchedule, Weekday

NEW_YORK = ZoneInfo("America/New_York")
ANCHOR = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


class TestValidation:
    def test_none_model_needs_nothing(self) -> None:
        assert FinancingSpec.none().model is FinancingModel.NONE

    def test_funding_requires_interval_and_anchor(self) -> None:
        with pytest.raises(DomainError, match="requires both funding_interval"):
            FinancingSpec(model=FinancingModel.FUNDING_RATE)
        with pytest.raises(DomainError, match="requires both funding_interval"):
            FinancingSpec(model=FinancingModel.FUNDING_RATE, funding_interval=timedelta(hours=8))

    def test_funding_interval_must_be_positive(self) -> None:
        with pytest.raises(DomainError, match="must be positive"):
            FinancingSpec(
                model=FinancingModel.FUNDING_RATE,
                funding_interval=timedelta(0),
                funding_anchor=ANCHOR,
            )

    def test_funding_interval_must_divide_a_day(self) -> None:
        with pytest.raises(DomainError, match="divide 24 hours evenly"):
            FinancingSpec.funding(interval=timedelta(hours=7), anchor=ANCHOR)

    def test_funding_anchor_must_be_aware(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            FinancingSpec.funding(
                interval=timedelta(hours=8),
                anchor=datetime(2024, 1, 1),  # noqa: DTZ001
            )

    def test_swap_requires_a_rollover_time(self) -> None:
        with pytest.raises(DomainError, match="requires a rollover_time"):
            FinancingSpec(model=FinancingModel.SWAP_POINTS)

    def test_rollover_time_must_be_naive(self) -> None:
        with pytest.raises(DomainError, match="naive wall clock time"):
            FinancingSpec.swap(rollover_time=time(17, 0, tzinfo=UTC))

    def test_swap_fields_are_rejected_on_a_funding_model(self) -> None:
        with pytest.raises(DomainError, match="does not use rollover_time"):
            FinancingSpec(
                model=FinancingModel.FUNDING_RATE,
                funding_interval=timedelta(hours=8),
                funding_anchor=ANCHOR,
                rollover_time=time(17, 0),
            )

    def test_funding_fields_are_rejected_on_a_swap_model(self) -> None:
        with pytest.raises(DomainError, match="does not use funding_interval"):
            FinancingSpec(
                model=FinancingModel.SWAP_POINTS,
                rollover_time=time(17, 0),
                funding_interval=timedelta(hours=8),
                funding_anchor=ANCHOR,
            )

    def test_borrow_rate_uses_the_funding_schedule(self) -> None:
        spec = FinancingSpec(
            model=FinancingModel.BORROW_RATE,
            funding_interval=timedelta(hours=1),
            funding_anchor=ANCHOR,
        )
        assert spec.model is FinancingModel.BORROW_RATE


class TestFundingEvents:
    def test_eight_hourly_funding_lands_on_the_expected_utc_times(self) -> None:
        spec = FinancingSpec.funding(interval=timedelta(hours=8), anchor=ANCHOR)
        events = spec.funding_events_between(
            datetime(2025, 5, 1, 0, 0, tzinfo=UTC), datetime(2025, 5, 2, 0, 0, tzinfo=UTC)
        )
        assert [event.at for event in events] == [
            datetime(2025, 5, 1, 8, 0, tzinfo=UTC),
            datetime(2025, 5, 1, 16, 0, tzinfo=UTC),
            datetime(2025, 5, 2, 0, 0, tzinfo=UTC),
        ]

    def test_start_is_exclusive_and_end_is_inclusive(self) -> None:
        spec = FinancingSpec.funding(interval=timedelta(hours=8), anchor=ANCHOR)
        first = spec.funding_events_between(
            datetime(2025, 5, 1, 0, 0, tzinfo=UTC), datetime(2025, 5, 1, 16, 0, tzinfo=UTC)
        )
        second = spec.funding_events_between(
            datetime(2025, 5, 1, 16, 0, tzinfo=UTC), datetime(2025, 5, 2, 0, 0, tzinfo=UTC)
        )
        # Chaining windows must neither repeat nor drop a settlement.
        combined = [event.at for event in (*first, *second)]
        assert combined == sorted(combined)
        assert len(set(combined)) == len(combined)

    def test_an_empty_window_yields_nothing(self) -> None:
        spec = FinancingSpec.funding(interval=timedelta(hours=8), anchor=ANCHOR)
        events = spec.funding_events_between(
            datetime(2025, 5, 1, 9, 0, tzinfo=UTC), datetime(2025, 5, 1, 15, 0, tzinfo=UTC)
        )
        assert events == ()

    def test_windows_before_the_anchor_still_resolve(self) -> None:
        spec = FinancingSpec.funding(interval=timedelta(hours=8), anchor=ANCHOR)
        events = spec.funding_events_between(
            datetime(2023, 12, 31, 0, 0, tzinfo=UTC), datetime(2023, 12, 31, 17, 0, tzinfo=UTC)
        )
        assert [event.at for event in events] == [
            datetime(2023, 12, 31, 8, 0, tzinfo=UTC),
            datetime(2023, 12, 31, 16, 0, tzinfo=UTC),
        ]

    def test_end_before_start_is_refused(self) -> None:
        spec = FinancingSpec.funding(interval=timedelta(hours=8), anchor=ANCHOR)
        with pytest.raises(DomainError, match="is before start"):
            spec.funding_events_between(
                datetime(2025, 5, 2, tzinfo=UTC), datetime(2025, 5, 1, tzinfo=UTC)
            )

    def test_naive_bounds_are_refused(self) -> None:
        spec = FinancingSpec.funding(interval=timedelta(hours=8), anchor=ANCHOR)
        with pytest.raises(DomainError, match="timezone aware"):
            spec.funding_events_between(
                datetime(2025, 5, 1),  # noqa: DTZ001
                datetime(2025, 5, 2, tzinfo=UTC),
            )

    def test_asking_a_swap_instrument_for_funding_is_an_error(self) -> None:
        with pytest.raises(DomainError, match="no funding schedule"):
            FinancingSpec.swap(rollover_time=time(17, 0)).funding_events_between(
                datetime(2025, 5, 1, tzinfo=UTC), datetime(2025, 5, 2, tzinfo=UTC)
            )


class TestRolloverEvents:
    def test_one_night_per_trading_day(self) -> None:
        spec = FinancingSpec.swap(rollover_time=time(17, 0), triple_rollover_day=None)
        events = spec.rollover_events_between(
            datetime(2025, 3, 3, 0, 0, tzinfo=NEW_YORK),
            datetime(2025, 3, 7, 23, 59, tzinfo=NEW_YORK),
            FOREX_WEEK,
        )
        assert [event.at.date() for event in events] == [
            date(2025, 3, 3),
            date(2025, 3, 4),
            date(2025, 3, 5),
            date(2025, 3, 6),
        ]
        assert {event.nights for event in events} == {1}

    def test_friday_rollover_is_excluded_because_the_market_closes_at_that_instant(self) -> None:
        # The 17:00 Friday rollover coincides with the weekly close. The session is half
        # open, so the venue is already shut at 17:00 and no charge accrues.
        spec = FinancingSpec.swap(rollover_time=time(17, 0), triple_rollover_day=None)
        events = spec.rollover_events_between(
            datetime(2025, 3, 7, 0, 0, tzinfo=NEW_YORK),
            datetime(2025, 3, 8, 23, 59, tzinfo=NEW_YORK),
            FOREX_WEEK,
        )
        assert events == ()

    def test_sunday_rollover_is_charged_because_the_week_opens_at_that_instant(self) -> None:
        # The mirror of the Friday case: the session boundary is inclusive at the open,
        # so a rollover falling exactly on it does accrue.
        spec = FinancingSpec.swap(rollover_time=time(17, 0), triple_rollover_day=None)
        events = spec.rollover_events_between(
            datetime(2025, 3, 9, 0, 0, tzinfo=NEW_YORK),
            datetime(2025, 3, 9, 23, 59, tzinfo=NEW_YORK),
            FOREX_WEEK,
        )
        assert [event.at for event in events] == [datetime(2025, 3, 9, 17, 0, tzinfo=NEW_YORK)]

    def test_wednesday_carries_three_nights(self) -> None:
        spec = FinancingSpec.swap(rollover_time=time(17, 0))
        events = spec.rollover_events_between(
            datetime(2025, 3, 3, 0, 0, tzinfo=NEW_YORK),
            datetime(2025, 3, 6, 23, 59, tzinfo=NEW_YORK),
            FOREX_WEEK,
        )
        nights = {event.at.date(): event.nights for event in events}
        assert nights[date(2025, 3, 5)] == 3
        assert nights[date(2025, 3, 4)] == 1

    def test_weekends_accrue_nothing(self) -> None:
        spec = FinancingSpec.swap(rollover_time=time(12, 0))
        events = spec.rollover_events_between(
            datetime(2025, 3, 8, 0, 0, tzinfo=NEW_YORK),
            datetime(2025, 3, 9, 23, 59, tzinfo=NEW_YORK),
            FOREX_WEEK,
        )
        assert events == ()

    def test_holidays_accrue_nothing(self) -> None:
        schedule = TradingSchedule.weekly(
            timezone="America/New_York",
            sessions=FOREX_WEEK.sessions,
            holidays=[date(2025, 3, 4)],
        )
        spec = FinancingSpec.swap(rollover_time=time(12, 0), triple_rollover_day=None)
        events = spec.rollover_events_between(
            datetime(2025, 3, 3, 0, 0, tzinfo=NEW_YORK),
            datetime(2025, 3, 5, 23, 59, tzinfo=NEW_YORK),
            schedule,
        )
        assert [event.at.date() for event in events] == [date(2025, 3, 3), date(2025, 3, 5)]

    def test_rollover_time_is_wall_clock_across_a_dst_change(self) -> None:
        spec = FinancingSpec.swap(rollover_time=time(17, 0), triple_rollover_day=None)
        march = spec.rollover_events_between(
            datetime(2025, 3, 3, 0, 0, tzinfo=UTC),
            datetime(2025, 3, 4, 0, 0, tzinfo=UTC),
            FOREX_WEEK,
        )
        july = spec.rollover_events_between(
            datetime(2025, 7, 7, 0, 0, tzinfo=UTC),
            datetime(2025, 7, 8, 0, 0, tzinfo=UTC),
            FOREX_WEEK,
        )
        assert march[0].at.utcoffset() == timedelta(hours=-5)
        assert july[0].at.utcoffset() == timedelta(hours=-4)
        assert march[0].at.astimezone(NEW_YORK).hour == 17
        assert july[0].at.astimezone(NEW_YORK).hour == 17

    def test_asking_a_funding_instrument_for_rollovers_is_an_error(self) -> None:
        spec = FinancingSpec.funding(interval=timedelta(hours=8), anchor=ANCHOR)
        with pytest.raises(DomainError, match="no daily rollover"):
            spec.rollover_events_between(
                datetime(2025, 3, 3, tzinfo=UTC), datetime(2025, 3, 4, tzinfo=UTC), FOREX_WEEK
            )

    def test_absurd_ranges_are_refused(self) -> None:
        spec = FinancingSpec.swap(rollover_time=time(17, 0))
        with pytest.raises(DomainError, match="safety limit"):
            spec.rollover_events_between(
                datetime(1990, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC), FOREX_WEEK
            )


class TestSerialisation:
    @pytest.mark.parametrize(
        "spec",
        [
            FinancingSpec.none(),
            FinancingSpec.swap(rollover_time=time(17, 0)),
            FinancingSpec.swap(rollover_time=time(0, 0), triple_rollover_day=None),
            FinancingSpec.funding(interval=timedelta(hours=8), anchor=ANCHOR),
        ],
    )
    def test_round_trip(self, spec: FinancingSpec) -> None:
        assert FinancingSpec.from_mapping(spec.to_mapping()) == spec

    def test_mapping_is_json_safe(self) -> None:
        spec = FinancingSpec.swap(rollover_time=time(17, 0), triple_rollover_day=Weekday.WEDNESDAY)
        assert FinancingSpec.from_mapping(json.loads(json.dumps(spec.to_mapping()))) == spec

    def test_from_mapping_requires_a_model(self) -> None:
        with pytest.raises(DomainError, match="missing 'model'"):
            FinancingSpec.from_mapping({})

    def test_from_mapping_rejects_an_unknown_model(self) -> None:
        with pytest.raises(DomainError, match="not a known financing model"):
            FinancingSpec.from_mapping({"model": "vibes"})
