"""Exception hierarchy shared by every layer of the system.

Every error raised deliberately by this package derives from :class:`TradingSysError`
so that callers can distinguish our failures from bugs in third party libraries.
"""

from __future__ import annotations

__all__ = [
    "ConfigurationError",
    "CurrencyMismatchError",
    "DomainError",
    "InstrumentDefinitionError",
    "InvalidPriceError",
    "InvalidQuantityError",
    "PersistenceError",
    "PipNotDefinedError",
    "SecretInConfigFileError",
    "TradingSysError",
    "UnknownCurrencyError",
]


class TradingSysError(Exception):
    """Base class for all errors raised by tradingsys."""


class ConfigurationError(TradingSysError):
    """Configuration is missing, malformed, or internally inconsistent.

    Raised at startup only. The process must not continue past this error.
    """


class SecretInConfigFileError(ConfigurationError):
    """A secret-typed field was supplied by a configuration file.

    Secrets are accepted from environment variables and the secrets directory
    only, never from a file that could be committed to version control.
    """


class DomainError(TradingSysError):
    """Base class for violations of trading domain invariants."""


class UnknownCurrencyError(DomainError):
    """A currency code has no definition in the currency registry."""


class CurrencyMismatchError(DomainError):
    """An operation combined two :class:`~tradingsys.core.money.Money` values of
    different currencies."""


class InstrumentDefinitionError(DomainError):
    """An instrument definition is internally inconsistent."""


class InvalidQuantityError(DomainError):
    """A quantity violates an instrument's minimum, maximum, or step size."""


class InvalidPriceError(DomainError):
    """A price violates an instrument's tick size or sign constraints."""


class PipNotDefinedError(DomainError):
    """A pip-denominated calculation was requested for an instrument that has no
    pip convention, for example a crypto pair quoted purely in tick sizes."""


class PersistenceError(TradingSysError):
    """Base class for storage layer failures."""
