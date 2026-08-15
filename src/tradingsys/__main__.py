"""Command line entry point.

Configuration failures are reported to stderr and exit with a distinct code, because
they mean the deployment is wrong rather than the code. Everything else runs through
the application runtime.
"""

from __future__ import annotations

import asyncio
import sys

from tradingsys.app.runtime import run
from tradingsys.config import load_settings
from tradingsys.core.errors import ConfigurationError

EXIT_CONFIGURATION_ERROR = 78
"""EX_CONFIG from sysexits.h. Distinguishes a bad configuration from a crash, so a
supervisor can stop restarting a process that will never start."""


def main() -> int:
    """Load configuration and run the process."""
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(f"configuration error:\n{exc}", file=sys.stderr)  # noqa: T201
        return EXIT_CONFIGURATION_ERROR

    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
