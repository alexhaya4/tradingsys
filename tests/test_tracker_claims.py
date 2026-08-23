"""Makes some false claims in ``PROGRESS.md`` fail something.

**Why this file exists.** The tracker has now carried a false claim three times: that CI
was enforcing gates during a period when the workflow had never triggered once, that a
module was complete while nothing constructed it, and that two components were not
started when both were built. The code has not drifted in the same way, and the reason is
structural rather than a matter of care. Every other claim this project makes is enforced
by something that runs: types are checked, lints are checked, behaviour is tested, the
verification path is executed by CI, the host is converged by a script. ``PROGRESS.md`` is
the only artifact updated by intention, so nothing fails when it is wrong.

**What these rules catch.** A stale path, and a deliverable claimed absent while its class
exists. Those are the two cheapest kinds of drift and they cover two of the three
historical instances.

**What they deliberately do not attempt.** They cannot catch a row that says Complete
while the component is unreached. Reachability is what the constructed-by column exists to
state, and a test that inferred it would be a second and weaker implementation of the call
graph. They also do not apply to ``docs/DECISIONS.md``, which is append-only: a decision
naming a file that was later deleted is still an accurate record of that decision, and a
gate there would force editing history to satisfy a test.

So this is a floor, not a guarantee. The tracker is still mostly trusted prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROGRESS = REPO_ROOT / "PROGRESS.md"
SRC = REPO_ROOT / "src"

# Paths, as opposed to prose in backticks. Anchored on the directories this repository
# actually has, so that a sentence mentioning `orderbook.1` or `ETH/USDT` is not read as a
# filename and reported as missing.
PATH_PATTERN = re.compile(
    r"`((?:src|tests|scripts|deploy|config|migrations|docs)/[A-Za-z0-9_./{}-]+)`"
)

# A brace expansion in a path, which the tracker uses to name several files at once:
# `venues/bybit/{instruments,rest}.py`.
BRACE_PATTERN = re.compile(r"^(.*)\{([^}]+)\}(.*)$")

CLASS_PATTERN = re.compile(r"`([A-Z][A-Za-z0-9]+)`")


def expand(path: str) -> list[str]:
    match = BRACE_PATTERN.match(path)
    if not match:
        return [path]
    prefix, options, suffix = match.groups()
    return [f"{prefix}{option}{suffix}" for option in options.split(",")]


def declared_paths() -> list[str]:
    found: list[str] = []
    for raw in PATH_PATTERN.findall(PROGRESS.read_text()):
        found.extend(expand(raw))
    return sorted(set(found))


def source_text() -> str:
    return "\n".join(path.read_text() for path in sorted(SRC.rglob("*.py")))


@pytest.mark.parametrize("path", declared_paths())
def test_every_path_the_tracker_names_exists(path: str) -> None:
    """A renamed or deleted module described as though it were still there."""
    candidate = REPO_ROOT / path
    # The tracker names modules both from the repository root and from inside the
    # package, because a table of "where the code lives" reads better the second way.
    package_relative = SRC / "tradingsys" / path
    assert candidate.exists() or package_relative.exists(), (
        f"PROGRESS.md names {path}, which does not exist. Either the file moved and the "
        f"tracker did not, or the tracker is describing something that was never built."
    )


class TestTheTaskTable:
    """The table whose columns the director required: what constructs this, and what it
    feeds. A row that cannot name both has an unbuilt path, and the point of the columns
    is that the gap is visible in the row rather than discoverable by tracing calls."""

    def rows(self) -> list[list[str]]:
        collected: list[list[str]] = []
        in_table = False
        for line in PROGRESS.read_text().splitlines():
            if line.startswith("| Task | Status | Constructed by | Feeds |"):
                in_table = True
                continue
            if in_table:
                if not line.startswith("|"):
                    in_table = False
                    continue
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                if len(cells) >= 4 and not set(cells[0]) <= {"-", " "}:
                    collected.append(cells)
        assert collected, "the task status table moved or changed shape; this gate is blind"
        return collected

    def test_every_row_names_what_constructs_it_and_what_it_feeds(self) -> None:
        incomplete = [row[0] for row in self.rows() if not row[2] or not row[3]]
        assert not incomplete, (
            f"these rows leave a column empty: {incomplete}. A row that cannot name both "
            f"has an unbuilt path. NOTHING is a finding and is written as one."
        )

    def test_nothing_claimed_not_started_already_exists(self) -> None:
        """The rule that would have caught `BybitInstrumentSource`.

        Scoped to the Task column on purpose. The other columns legitimately reference
        things that do exist, most often the component a not-started task will one day be
        constructed by, and a rule that fired on those would be a false positive machine.
        """
        source = source_text()
        built = [
            (row[0], name)
            for row in self.rows()
            if row[1].replace("*", "").startswith("Not started")
            for name in CLASS_PATTERN.findall(row[0])
            if re.search(rf"^class {name}\b", source, re.MULTILINE)
        ]
        assert not built, (
            f"these rows claim Not started and name a class that is defined in src: "
            f"{built}. Either the work landed and the row did not, or the row names "
            f"something it does not mean."
        )
