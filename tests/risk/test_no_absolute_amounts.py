"""No risk limit may carry an absolute currency amount. `SPEC.md` section 6.1.

A limit written in currency is correct at exactly one account size and silently wrong
at every other, and it fails by producing plausible numbers rather than by raising. It
cannot be caught by testing the arithmetic, because the arithmetic is right; it is the
constant that is wrong.

So it is caught structurally instead. The risk package is parsed and any constant that
could be an amount of money is rejected. Dimensionless numbers are allowed, because
fractions, counts, and array indices are what this code is legitimately made of.

This is a scan rather than a rule anyone has to remember, which is the point: the
CI-never-ran defect established that a control nobody runs is not a control, and the
same applies to a convention nobody enforces.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

RISK_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "tradingsys" / "risk"

MONEY_CONSTRUCTORS = frozenset({"Money"})
"""Calls that produce an amount in a currency. A literal argument to one of these is an
absolute amount by definition, whatever it is named."""

DIMENSIONLESS = frozenset({"0", "1", "2", "100"})
"""Numbers that cannot be an account size in this codebase.

Zero and one are identities, two appears in halving and doubling, and a hundred is the
conversion between a venue's hundredths and whole units. Anything else written as a
Decimal literal at module scope is asked to justify itself.
"""


def risk_modules() -> list[Path]:
    return sorted(path for path in RISK_PACKAGE.rglob("*.py") if path.name != "__init__.py")


def test_the_risk_package_has_modules_to_scan() -> None:
    # A scan over an empty directory passes and proves nothing, which is the failure
    # mode this whole file exists to avoid.
    assert risk_modules(), f"no modules found under {RISK_PACKAGE}"


@pytest.mark.parametrize("module", risk_modules(), ids=lambda path: path.name)
class TestNoAbsoluteAmounts:
    def test_no_money_is_constructed_from_a_literal(self, module: Path) -> None:
        # The arguments are walked rather than inspected directly. An earlier version of
        # this test looked only at immediate arguments and was defeated by
        # `Money(Decimal("200"), currency)`, which wraps the literal one call deep. That
        # hole was found by writing the mutation, not by reading the test.
        tree = ast.parse(module.read_text(), filename=str(module))
        offences: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in MONEY_CONSTRUCTORS:
                continue
            for argument in node.args:
                for inner in ast.walk(argument):
                    if (
                        isinstance(inner, ast.Constant)
                        and not isinstance(inner.value, bool)
                        and isinstance(inner.value, int | float | str)
                        and str(inner.value) not in DIMENSIONLESS
                    ):
                        offences.append(f"line {node.lineno}: {ast.unparse(node)}")
        assert not offences, (
            f"{module.name} constructs a currency amount from a literal. Every risk "
            f"limit is a fraction of equity, and an absolute amount is correct at one "
            f"account size and wrong at every other: {offences}"
        )

    def test_no_module_level_decimal_constant_could_be_an_amount(self, module: Path) -> None:
        # Module level is where a threshold would live. A Decimal built from a literal
        # there is either dimensionless or it is a quantity of something, and the second
        # is what section 6.1 forbids.
        tree = ast.parse(module.read_text(), filename=str(module))
        offences: list[str] = []
        for node in tree.body:
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            value = node.value
            if value is None:
                continue
            for inner in ast.walk(value):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "Decimal"
                    and inner.args
                    and isinstance(inner.args[0], ast.Constant)
                    and str(inner.args[0].value) not in DIMENSIONLESS
                ):
                    offences.append(f"line {node.lineno}: {ast.unparse(node)}")
        assert not offences, (
            f"{module.name} defines a Decimal constant at module scope. If it is an "
            f"amount of money it is forbidden by SPEC 6.1; if it is genuinely "
            f"dimensionless, add it to DIMENSIONLESS in this test with a reason: "
            f"{offences}"
        )

    def test_no_default_argument_carries_a_number(self, module: Path) -> None:
        # A default is the quietest place for an assumption to live: it applies whenever
        # a caller does not think about it, which is exactly when it should not.
        tree = ast.parse(module.read_text(), filename=str(module))
        offences: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            defaults = [*node.args.defaults, *(node.args.kw_defaults or [])]
            for default in defaults:
                if (
                    isinstance(default, ast.Constant)
                    and isinstance(default.value, int | float)
                    and not isinstance(default.value, bool)
                    and str(default.value) not in DIMENSIONLESS
                ):
                    offences.append(f"line {node.lineno}: {node.name} defaults to {default.value}")
        assert not offences, (
            f"{module.name} has a numeric default argument. A risk parameter with a "
            f"default is a trading decision nobody made: {offences}"
        )
