"""Tests for the exact numeric gate and the clock."""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo

import pytest

from tradingsys.core.clock import FixedClock, SystemClock, ensure_utc, new_id, utc_now
from tradingsys.core.errors import DomainError
from tradingsys.core.numeric import (
    ARITHMETIC_PRECISION,
    exact_context,
    from_binary32,
    to_decimal,
)
from tradingsys.core.rounding import Rounding


class TestToDecimal:
    def test_accepts_exact_types(self) -> None:
        assert to_decimal(Decimal("1.5")) == Decimal("1.5")
        assert to_decimal(7) == Decimal(7)
        assert to_decimal("0.001") == Decimal("0.001")

    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="must not be a float"):
            to_decimal(1.5)

    def test_rejects_bool(self) -> None:
        # bool is a subclass of int; treating True as 1 would hide a real bug.
        with pytest.raises(TypeError, match="got a bool"):
            to_decimal(True)

    def test_rejects_unsupported_types(self) -> None:
        with pytest.raises(TypeError, match="must be a Decimal, int, or str"):
            to_decimal(None)
        with pytest.raises(TypeError, match="must be a Decimal, int, or str"):
            to_decimal([1])

    def test_rejects_unparseable_strings(self) -> None:
        with pytest.raises(DomainError, match="not a valid decimal number"):
            to_decimal("1.2.3")

    def test_rejects_non_finite(self) -> None:
        for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with pytest.raises(DomainError, match="must be finite"):
                to_decimal(value)

    def test_label_appears_in_the_error(self) -> None:
        with pytest.raises(TypeError, match="tick size must not be a float"):
            to_decimal(0.1, what="tick size")

    def test_preserves_trailing_zeros(self) -> None:
        assert str(to_decimal("1.500")) == "1.500"


class TestExactContext:
    def test_widens_precision_inside_the_block(self) -> None:
        with exact_context() as ctx:
            assert ctx.prec == ARITHMETIC_PRECISION
            wide = Decimal(1) / Decimal(3)
        assert len(str(wide).split(".")[1]) == ARITHMETIC_PRECISION

    def test_restores_the_ambient_context(self) -> None:
        before = getcontext().prec
        with exact_context():
            pass
        assert getcontext().prec == before

    def test_does_not_leak_on_an_exception(self) -> None:
        before = getcontext().prec
        with pytest.raises(ZeroDivisionError), exact_context():
            _ = 1 / 0
        assert getcontext().prec == before


class TestRounding:
    def test_values_are_the_stdlib_constants(self) -> None:
        assert Decimal("1.005").quantize(Decimal("0.01"), rounding=Rounding.HALF_UP.value) == (
            Decimal("1.01")
        )

    def test_directions_differ_as_documented(self) -> None:
        value = Decimal("-1.5")
        assert value.quantize(Decimal(1), rounding=Rounding.DOWN.value) == Decimal(-1)
        assert value.quantize(Decimal(1), rounding=Rounding.UP.value) == Decimal(-2)
        assert value.quantize(Decimal(1), rounding=Rounding.FLOOR.value) == Decimal(-2)
        assert value.quantize(Decimal(1), rounding=Rounding.CEILING.value) == Decimal(-1)


class TestClock:
    def test_utc_now_is_aware_and_utc(self) -> None:
        now = utc_now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_system_clock_advances(self) -> None:
        clock = SystemClock()
        first = clock.now()
        second = clock.now()
        assert second >= first

    def test_ensure_utc_converts(self) -> None:
        tokyo = datetime(2025, 3, 5, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        assert ensure_utc(tokyo) == datetime(2025, 3, 5, 0, 0, tzinfo=UTC)

    def test_ensure_utc_rejects_naive(self) -> None:
        with pytest.raises(DomainError, match="must be timezone aware"):
            ensure_utc(datetime(2025, 3, 5))  # noqa: DTZ001

    def test_fixed_clock_is_stable(self) -> None:
        clock = FixedClock(datetime(2025, 3, 5, 12, 0, tzinfo=UTC))
        assert clock.now() == clock.now() == datetime(2025, 3, 5, 12, 0, tzinfo=UTC)

    def test_fixed_clock_normalises_to_utc(self) -> None:
        clock = FixedClock(datetime(2025, 3, 5, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
        assert clock.now() == datetime(2025, 3, 5, 0, 0, tzinfo=UTC)

    def test_fixed_clock_can_be_moved(self) -> None:
        clock = FixedClock(datetime(2025, 3, 5, 12, 0, tzinfo=UTC))
        clock.set(datetime(2025, 3, 6, 12, 0, tzinfo=UTC))
        assert clock.now() == datetime(2025, 3, 6, 12, 0, tzinfo=UTC)

    def test_fixed_clock_advance(self) -> None:
        clock = FixedClock(datetime(2025, 3, 5, 12, 0, tzinfo=UTC))
        assert clock.advance(90) == datetime(2025, 3, 5, 12, 1, 30, tzinfo=UTC)

    def test_fixed_clock_refuses_to_run_backwards(self) -> None:
        clock = FixedClock(datetime(2025, 3, 5, 12, 0, tzinfo=UTC))
        with pytest.raises(DomainError, match="backwards"):
            clock.advance(-1)

    def test_fixed_clock_rejects_naive_construction(self) -> None:
        with pytest.raises(DomainError, match="timezone aware"):
            FixedClock(datetime(2025, 3, 5, 12, 0))  # noqa: DTZ001


class TestIdentifiers:
    def test_new_id_is_hex_and_url_safe(self) -> None:
        value = new_id()
        assert len(value) == 32
        assert value.isalnum()

    def test_new_id_is_unique(self) -> None:
        assert len({new_id() for _ in range(1000)}) == 1000


class TestFromBinary32:
    """The one sanctioned float door, for fields a venue publishes as 32 bit binary."""

    def test_the_expansion_is_exact_not_rounded(self) -> None:
        # 0.9 is not representable in binary. This is the exact value of the 32 bit
        # pattern, so it looks wrong and is right; anything shorter would be a value
        # the source never sent.
        assert from_binary32(struct.unpack(">f", struct.pack(">f", 0.9))[0]) == Decimal(
            "0.89999997615814208984375"
        )

    def test_the_result_converts_back_to_the_same_bits(self) -> None:
        for sample in (0.0, 1.0, 0.9, 5.04, 2.7, 1e-30, 3.4e38):
            value: float = struct.unpack(">f", struct.pack(">f", sample))[0]
            assert struct.pack(">f", float(from_binary32(value))) == struct.pack(">f", value)

    def test_an_exactly_representable_value_keeps_its_familiar_form(self) -> None:
        assert from_binary32(1.25) == Decimal("1.25")

    def test_negative_values_are_converted(self) -> None:
        # Sign is the caller's business; this gate only rules on exactness.
        assert from_binary32(-1.5) == Decimal("-1.5")

    def test_a_double_that_is_not_a_binary32_is_refused(self) -> None:
        # 0.1 as a double is a different number from 0.1 as a binary32, so a value that
        # fails this round trip did not come from where the caller thinks it did.
        with pytest.raises(DomainError, match="not exactly representable in binary32"):
            from_binary32(0.1)

    def test_a_value_beyond_the_binary32_range_is_refused(self) -> None:
        with pytest.raises(DomainError, match="outside the binary32 range"):
            from_binary32(1e39)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_values_are_refused(self, value: float) -> None:
        with pytest.raises(DomainError, match="must be finite"):
            from_binary32(value)

    @pytest.mark.parametrize("value", [1, Decimal("1.5"), "1.5", True])
    def test_non_floats_are_refused(self, value: object) -> None:
        # Not a convenience converter: an int or a Decimal is already exact and belongs
        # in to_decimal, and accepting one here would make this function a place where
        # anything at all can be turned into a Decimal.
        with pytest.raises(TypeError, match="must be a float unpacked from a binary32"):
            from_binary32(value)  # type: ignore[arg-type]

    def test_the_error_names_the_field(self) -> None:
        with pytest.raises(DomainError, match="record 3 bid volume"):
            from_binary32(0.1, what="record 3 bid volume")
