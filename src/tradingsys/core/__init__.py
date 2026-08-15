"""Core domain types shared by every other layer.

Nothing in this package imports from configuration, persistence, venues, or the
runtime. It is pure, synchronous, and side effect free, which is what makes it cheap
to test and safe to use inside an event loop.
"""

from tradingsys.core.clock import Clock, FixedClock, SystemClock, ensure_utc, new_id, utc_now
from tradingsys.core.currency import Currency, CurrencyKind, CurrencyRegistry, default_registry
from tradingsys.core.errors import (
    ConfigurationError,
    CurrencyMismatchError,
    DomainError,
    InstrumentDefinitionError,
    InvalidPriceError,
    InvalidQuantityError,
    PersistenceError,
    PipNotDefinedError,
    SecretInConfigFileError,
    TradingSysError,
    UnknownCurrencyError,
)
from tradingsys.core.financing import (
    FinancingModel,
    FinancingSpec,
    FundingEvent,
    RolloverEvent,
)
from tradingsys.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentStatus,
    QuantityUnit,
)
from tradingsys.core.money import Money
from tradingsys.core.numeric import Numeric, to_decimal
from tradingsys.core.rounding import Rounding
from tradingsys.core.schedule import TradingSchedule, Weekday, WeeklySession

__all__ = [
    "AssetClass",
    "Clock",
    "ConfigurationError",
    "Currency",
    "CurrencyKind",
    "CurrencyMismatchError",
    "CurrencyRegistry",
    "DomainError",
    "FinancingModel",
    "FinancingSpec",
    "FixedClock",
    "FundingEvent",
    "Instrument",
    "InstrumentDefinitionError",
    "InstrumentId",
    "InstrumentStatus",
    "InvalidPriceError",
    "InvalidQuantityError",
    "Money",
    "Numeric",
    "PersistenceError",
    "PipNotDefinedError",
    "QuantityUnit",
    "RolloverEvent",
    "Rounding",
    "SecretInConfigFileError",
    "SystemClock",
    "TradingSchedule",
    "TradingSysError",
    "UnknownCurrencyError",
    "Weekday",
    "WeeklySession",
    "default_registry",
    "ensure_utc",
    "new_id",
    "to_decimal",
    "utc_now",
]
