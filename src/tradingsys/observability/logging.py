"""Structured logging.

Every log line is a JSON object with a UTC ISO 8601 timestamp, a level, the service
name, the environment, and the correlation ID of the work in progress. Development can
switch to a human readable renderer, but production is forced to JSON by configuration
validation, because a console formatted line loses its structured fields on the way
into log storage.

Logging from libraries that use the standard library is routed through the same
pipeline, so a warning from asyncpg or uvicorn appears in the same format with the same
correlation ID rather than as a stray unstructured line.
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from tradingsys.config.settings import LogFormat, LogLevel
from tradingsys.observability.correlation import current_correlation_id

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger

    from tradingsys.config.settings import ObservabilitySettings, Settings

__all__ = ["configure_logging", "get_logger", "reset_logging"]

_STDLIB_LEVELS: dict[LogLevel, int] = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
    LogLevel.CRITICAL: logging.CRITICAL,
}

_NOISY_LIBRARIES = ("asyncio", "uvicorn.access")
"""Libraries whose default output is duplicated by our own.

uvicorn's access log repeats what our middleware already records, with no correlation
ID; asyncio's debug chatter is not useful at info level.
"""


def add_correlation_id(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Attach the current correlation ID, when there is one."""
    identifier = current_correlation_id()
    if identifier is not None:
        event_dict["correlation_id"] = identifier
    return event_dict


def render_decimals(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Render Decimal values as exact strings.

    The JSON renderer would otherwise convert a Decimal through float and log a price
    that differs from the one the system acted on.
    """
    for key, value in event_dict.items():
        if isinstance(value, Decimal):
            event_dict[key] = str(value)
    return event_dict


def configure_logging(
    settings: ObservabilitySettings, *, environment: str, cache_loggers: bool = True
) -> None:
    """Configure structlog and the standard library for this process.

    Safe to call more than once; the last call wins. Tests pass
    ``cache_loggers=False`` so that a later reconfiguration takes effect for loggers
    that were already obtained.

    Args:
        settings: The observability section of the configuration.
        environment: Deployment environment name, added to every line.
        cache_loggers: Whether structlog may cache bound loggers on first use.
    """
    level = _STDLIB_LEVELS[settings.log_level]

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        add_correlation_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        render_decimals,
    ]

    renderer: Any
    if settings.log_format is LogFormat.JSON:
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=cache_loggers,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    structlog.contextvars.bind_contextvars(service=settings.service_name, environment=environment)


def configure_from_settings(settings: Settings, *, cache_loggers: bool = True) -> None:
    """Configure logging from the whole settings object."""
    configure_logging(
        settings.observability,
        environment=settings.app.environment.value,
        cache_loggers=cache_loggers,
    )


def reset_logging() -> None:
    """Undo configuration, returning structlog and the root logger to their defaults.

    Used between tests so that one test's configuration cannot affect another's.
    """
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)


def get_logger(name: str, **initial_values: object) -> structlog.stdlib.BoundLogger:
    """A logger bound to ``name``, plus any values to attach to every line.

    Args:
        name: Dotted module or component name, for example ``execution.router``.
        initial_values: Fields bound to every line from this logger.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_values:
        return logger.bind(**initial_values)
    return logger


def bind_context(**values: object) -> None:
    """Bind values to every log line emitted by the current task."""
    structlog.contextvars.bind_contextvars(**values)


def unbind_context(*keys: str) -> None:
    """Remove previously bound context values."""
    structlog.contextvars.unbind_contextvars(*keys)
