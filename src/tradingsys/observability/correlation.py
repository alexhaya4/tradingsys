"""Correlation IDs.

A correlation ID ties everything that follows from one originating event: the tick that
triggered a signal, the risk decision, the order, the venue's response, and every log
line and audit entry along the way. Without one, reconstructing why a position exists
means guessing from timestamps.

The current ID lives in a :class:`~contextvars.ContextVar`, which is per-task in asyncio
rather than per-thread. Each concurrently handled event therefore carries its own ID
without any explicit plumbing, and a spawned task inherits the context of whatever
created it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from tradingsys.core.clock import new_id

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "CORRELATION_ID_HEADER",
    "bind_correlation_id",
    "clear_correlation_id",
    "correlation_id",
    "current_correlation_id",
    "new_correlation_id",
    "require_correlation_id",
]

CORRELATION_ID_HEADER = "X-Correlation-ID"
"""Header used to accept and echo an ID across a process boundary."""

_correlation_id: ContextVar[str | None] = ContextVar("tradingsys_correlation_id", default=None)


def current_correlation_id() -> str | None:
    """The ID bound to the current task, or ``None`` outside any correlated work."""
    return _correlation_id.get()


def require_correlation_id() -> str:
    """The ID bound to the current task, generating one if there is none.

    Used where an ID is mandatory, such as writing an audit entry. Generating rather
    than raising means a missing binding degrades to an uncorrelated record instead of
    losing the record entirely.
    """
    existing = _correlation_id.get()
    if existing is not None:
        return existing
    generated = new_id()
    _correlation_id.set(generated)
    return generated


@contextmanager
def correlation_id(value: str | None = None) -> Iterator[str]:
    """Bind a correlation ID for the duration of a block.

    Args:
        value: The ID to bind. When omitted a new one is generated, which is what an
            entry point does for an event that did not arrive with one.

    Yields:
        The bound ID.
    """
    resolved = value if value is not None else new_id()
    token = _correlation_id.set(resolved)
    try:
        yield resolved
    finally:
        _correlation_id.reset(token)


def bind_correlation_id(value: str) -> None:
    """Bind an ID for the rest of the current context, without a scope.

    Prefer :func:`correlation_id`, which unbinds again. This exists for entry points
    whose scope is the whole task, such as a stream consumer that binds once per
    received message.
    """
    _correlation_id.set(value)


def new_correlation_id() -> str:
    """Generate and bind a fresh ID, returning it."""
    generated = new_id()
    _correlation_id.set(generated)
    return generated


def clear_correlation_id() -> None:
    """Unbind the current ID.

    The scopeless setters above deliberately outlive the call that made them, so any
    long-lived loop that binds per message must clear afterwards. Otherwise the next
    message processed without its own ID inherits the previous one, and two unrelated
    decisions appear in the log as one trace.
    """
    _correlation_id.set(None)
