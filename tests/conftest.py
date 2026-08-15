"""Test-wide fixtures.

Context variables outlive an individual test when they are set without a scope, which
the correlation module deliberately allows. Clearing between tests keeps one test's
binding from appearing in another's assertions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tradingsys.observability.correlation import clear_correlation_id
from tradingsys.observability.logging import reset_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def isolated_context() -> Iterator[None]:
    """Start and end every test with no correlation ID and no logging configuration."""
    clear_correlation_id()
    yield
    clear_correlation_id()
    reset_logging()
