"""Tests for structured logging and correlation IDs.

Log output is parsed back from captured stdout, so what is asserted is the bytes a log
collector would actually receive, not an intermediate structlog object.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from tradingsys.config.settings import LogFormat, LogLevel, ObservabilitySettings
from tradingsys.observability.correlation import (
    bind_correlation_id,
    correlation_id,
    current_correlation_id,
    new_correlation_id,
    require_correlation_id,
)
from tradingsys.observability.logging import (
    bind_context,
    configure_logging,
    get_logger,
    reset_logging,
    unbind_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def observability(**overrides: Any) -> ObservabilitySettings:
    defaults: dict[str, Any] = {
        "service_name": "tradingsys",
        "log_level": LogLevel.DEBUG,
        "log_format": LogFormat.JSON,
        "http_host": "127.0.0.1",
        "http_port": 8000,
        "health_path": "/health",
        "ready_path": "/ready",
        "metrics_path": "/metrics",
        "readiness_timeout_seconds": 3.0,
    }
    defaults.update(overrides)
    return ObservabilitySettings(**defaults)


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    """Every test starts and ends with logging unconfigured."""
    reset_logging()
    yield
    reset_logging()


def emit(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Read back every JSON line written to stdout."""
    captured = capsys.readouterr().out
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


class TestJsonOutput:
    def test_a_line_is_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        get_logger("test.module").info("something happened")
        lines = emit(capsys)
        assert len(lines) == 1
        assert lines[0]["event"] == "something happened"

    def test_standard_fields_are_present(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="staging", cache_loggers=False)
        get_logger("test.module").warning("careful")
        line = emit(capsys)[0]
        assert line["level"] == "warning"
        assert line["logger"] == "test.module"
        assert line["service"] == "tradingsys"
        assert line["environment"] == "staging"
        assert line["timestamp"].endswith("Z")

    def test_timestamps_are_utc_iso8601(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        get_logger("t").info("now")
        stamp = emit(capsys)[0]["timestamp"]
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None

    def test_extra_fields_are_included(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        get_logger("t").info("order placed", venue="fxbroker", quantity=1000)
        line = emit(capsys)[0]
        assert line["venue"] == "fxbroker"
        assert line["quantity"] == 1000

    def test_decimals_are_logged_exactly(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Through float, 1.08500 would render as 1.085 and 0.1 + 0.2 as 0.30000000000000004.
        configure_logging(observability(), environment="test", cache_loggers=False)
        get_logger("t").info("priced", price=Decimal("1.08500"), size=Decimal("0.1"))
        line = emit(capsys)[0]
        assert line["price"] == "1.08500"
        assert line["size"] == "0.1"

    def test_bound_values_appear_on_every_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        logger = get_logger("t", component="router")
        logger.info("first")
        logger.info("second")
        lines = emit(capsys)
        assert all(line["component"] == "router" for line in lines)

    def test_exceptions_are_rendered(self, capsys: pytest.CaptureFixture[str]) -> None:
        def explode() -> None:
            raise ValueError("boom")

        configure_logging(observability(), environment="test", cache_loggers=False)
        try:
            explode()
        except ValueError:
            get_logger("t").exception("failed while pricing")
        line = emit(capsys)[0]
        assert "ValueError: boom" in line["exception"]

    def test_keys_are_sorted_for_stable_diffs(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        get_logger("t").info("e", zebra=1, alpha=2)
        raw = capsys.readouterr().out
        assert raw.index('"alpha"') < raw.index('"zebra"')


class TestLevels:
    def test_below_the_configured_level_is_dropped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(
            observability(log_level=LogLevel.WARNING), environment="test", cache_loggers=False
        )
        logger = get_logger("t")
        logger.debug("invisible")
        logger.info("also invisible")
        logger.warning("visible")
        lines = emit(capsys)
        assert [line["event"] for line in lines] == ["visible"]

    def test_debug_level_lets_everything_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(
            observability(log_level=LogLevel.DEBUG), environment="test", cache_loggers=False
        )
        get_logger("t").debug("visible")
        assert len(emit(capsys)) == 1


class TestConsoleFormat:
    def test_console_output_is_not_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(
            observability(log_format=LogFormat.CONSOLE), environment="test", cache_loggers=False
        )
        get_logger("t").info("readable line", venue="fxbroker")
        raw = capsys.readouterr().out
        assert "readable line" in raw
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw.splitlines()[0])


class TestStdlibBridging:
    def test_library_logging_is_reformatted(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        logging.getLogger("some.library").warning("a library complained")
        line = emit(capsys)[0]
        assert line["event"] == "a library complained"
        assert line["level"] == "warning"
        assert line["logger"] == "some.library"

    def test_library_logging_carries_the_correlation_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        with correlation_id("corr-lib"):
            logging.getLogger("some.library").error("failed")
        assert emit(capsys)[0]["correlation_id"] == "corr-lib"


class TestCorrelationIds:
    def test_absent_outside_a_scope(self) -> None:
        assert current_correlation_id() is None

    def test_appears_in_the_log_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        with correlation_id("corr-1"):
            get_logger("t").info("inside")
        assert emit(capsys)[0]["correlation_id"] == "corr-1"

    def test_absent_from_lines_outside_a_scope(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        get_logger("t").info("outside")
        assert "correlation_id" not in emit(capsys)[0]

    def test_a_generated_id_is_used_when_none_is_given(self) -> None:
        with correlation_id() as generated:
            assert current_correlation_id() == generated
            assert len(generated) == 32

    def test_the_scope_unbinds_on_exit(self) -> None:
        with correlation_id("corr-1"):
            pass
        assert current_correlation_id() is None

    def test_the_scope_unbinds_after_an_exception(self) -> None:
        with pytest.raises(RuntimeError), correlation_id("corr-1"):
            raise RuntimeError("boom")
        assert current_correlation_id() is None

    def test_scopes_nest_and_restore(self) -> None:
        with correlation_id("outer"):
            with correlation_id("inner"):
                assert current_correlation_id() == "inner"
            assert current_correlation_id() == "outer"

    def test_require_generates_one_when_missing(self) -> None:
        identifier = require_correlation_id()
        assert identifier is not None
        assert current_correlation_id() == identifier

    def test_require_reuses_an_existing_id(self) -> None:
        with correlation_id("corr-1"):
            assert require_correlation_id() == "corr-1"

    def test_bind_and_new(self) -> None:
        bind_correlation_id("bound")
        assert current_correlation_id() == "bound"
        generated = new_correlation_id()
        assert current_correlation_id() == generated
        assert generated != "bound"

    async def test_concurrent_tasks_keep_separate_ids(self) -> None:
        seen: dict[str, str | None] = {}

        async def work(identifier: str) -> None:
            with correlation_id(identifier):
                await asyncio.sleep(0)
                seen[identifier] = current_correlation_id()

        await asyncio.gather(work("a"), work("b"), work("c"))
        assert seen == {"a": "a", "b": "b", "c": "c"}

    async def test_a_child_task_inherits_the_id(self) -> None:
        captured: list[str | None] = []

        async def child() -> None:
            captured.append(current_correlation_id())

        with correlation_id("parent"):
            await asyncio.create_task(child())
        assert captured == ["parent"]


class TestContextBinding:
    def test_bound_context_appears_on_every_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        bind_context(strategy="mean_reversion")
        get_logger("t").info("first")
        unbind_context("strategy")
        get_logger("t").info("second")
        lines = emit(capsys)
        assert lines[0]["strategy"] == "mean_reversion"
        assert "strategy" not in lines[1]


class TestReconfiguration:
    def test_reconfiguring_replaces_the_handler(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        configure_logging(observability(), environment="test", cache_loggers=False)
        get_logger("t").info("once")
        assert len(emit(capsys)) == 1, "a duplicated handler would log the line twice"

    def test_reset_removes_handlers(self) -> None:
        configure_logging(observability(), environment="test", cache_loggers=False)
        reset_logging()
        assert logging.getLogger().handlers == []
