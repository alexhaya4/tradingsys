"""The shipped timings must survive an idle connection.

This exists because the first draft did not. The read deadline was 20 seconds and the
venue heartbeats every 30, so any connection that went quiet, which is the normal state
of a forex socket over a weekend, would have been declared dead and reconnected in a
loop. No unit test could have caught it: the scripted peer sends whatever the test tells
it to, so it only revealed itself against the real venue.

What is guarded here is the relationship between the numbers, not the numbers
themselves. Either may be tuned; the deadline may not be brought back under the
interval the venue actually sends at.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tradingsys.venues.ctrader.connection import VENUE_HEARTBEAT_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_CONFIG = REPO_ROOT / "config" / "base.toml"


def forex_settings() -> dict[str, object]:
    with BASE_CONFIG.open("rb") as handle:
        return dict(tomllib.load(handle)["venues"]["forex"])


class TestTheIdleConnectionSurvives:
    def test_the_read_deadline_tolerates_two_missed_venue_heartbeats(self) -> None:
        deadline = forex_settings()["stream_read_timeout_seconds"]
        assert isinstance(deadline, float)
        required = 3 * VENUE_HEARTBEAT_SECONDS
        assert deadline >= required, (
            f"stream_read_timeout_seconds is {deadline}s, but the venue only sends a "
            f"heartbeat every {VENUE_HEARTBEAT_SECONDS}s. A deadline below {required}s "
            f"declares a healthy idle connection dead before two consecutive heartbeats "
            f"could have been missed, and an idle socket is the normal weekend state of "
            f"a forex connection."
        )

    def test_our_heartbeat_is_frequent_enough_for_the_venues_idle_timeout(self) -> None:
        # The venue closes a connection that has been silent for longer than 30 seconds.
        # Sending at that boundary leaves no room for a single delayed write.
        interval = forex_settings()["heartbeat_interval_seconds"]
        assert isinstance(interval, float)
        assert interval <= VENUE_HEARTBEAT_SECONDS / 2, (
            f"heartbeat_interval_seconds is {interval}s, which leaves no margin before "
            f"the venue's {VENUE_HEARTBEAT_SECONDS}s idle timeout"
        )

    def test_the_read_deadline_is_longer_than_our_own_heartbeat_interval(self) -> None:
        # Our heartbeats are writes and do not reset the read deadline. If the deadline
        # were the shorter of the two, the connection would die between our own sends.
        settings = forex_settings()
        deadline = settings["stream_read_timeout_seconds"]
        interval = settings["heartbeat_interval_seconds"]
        assert isinstance(deadline, float)
        assert isinstance(interval, float)
        assert deadline > interval
