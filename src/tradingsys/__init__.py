"""tradingsys: foundation for an automated multi-venue trading system."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tradingsys")
except PackageNotFoundError:  # pragma: no cover - only hit in a non-installed checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
