"""Fixtures for tests that need a real PostgreSQL with TimescaleDB.

Integration tests connect to the database defined by the ``test`` configuration
environment, which matches the compose stack. They **fail** rather than skip when the
database is unreachable: a suite that quietly skips its only real storage coverage
reports green while testing nothing.

Run them through the one verification path::

    scripts/verify.sh

The settings themselves come from the shared ``live_settings`` fixture in the parent
conftest, so a missing credential is reported once rather than once per test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass
