"""Repositories for instruments and market data.

These own the SQL for the tables the migrations create. Query construction and row
mapping are separated from execution so that both can be tested without a database:
the mapping functions are pure, and the statements are module level constants that a
test can inspect.

Storage conventions:

* Prices, sizes, and volumes are ``NUMERIC``, read back as Decimal.
* Timestamps are ``TIMESTAMPTZ``, always UTC.
* Bars and ticks are keyed by the surrogate ``instrument_id`` of the instruments table,
  not by symbol, because the same symbol exists at several venues.
* Writes are idempotent. Re-ingesting an overlapping range updates rather than
  duplicating, which is what makes a backfill safe to re-run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, final

from tradingsys.core.errors import PersistenceError
from tradingsys.core.instrument import Instrument, InstrumentId
from tradingsys.core.provenance import CalibrationTicks, ProvenanceError, TickSource
from tradingsys.venues.enums import CandlePrice, Granularity
from tradingsys.venues.models import Candle, Quote

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    import asyncpg

    from tradingsys.core.currency import CurrencyRegistry
    from tradingsys.persistence.database import Database

__all__ = [
    "InstrumentRepository",
    "MarketDataRepository",
    "candle_from_row",
    "instrument_args",
    "quote_from_row",
]


UPSERT_INSTRUMENT: Final = """
INSERT INTO instruments (
    venue, symbol, venue_symbol, asset_class, base_currency, quote_currency, settlement_currency,
    price_increment, price_precision, pip_size, quantity_unit, contract_size,
    quantity_increment, min_quantity, max_quantity, min_notional, max_leverage,
    financing, schedule, status, updated_at
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18,
    $19, $20, now()
)
ON CONFLICT (venue, symbol) DO UPDATE SET
    venue_symbol = excluded.venue_symbol,
    asset_class = excluded.asset_class,
    base_currency = excluded.base_currency,
    quote_currency = excluded.quote_currency,
    settlement_currency = excluded.settlement_currency,
    price_increment = excluded.price_increment,
    price_precision = excluded.price_precision,
    pip_size = excluded.pip_size,
    quantity_unit = excluded.quantity_unit,
    contract_size = excluded.contract_size,
    quantity_increment = excluded.quantity_increment,
    min_quantity = excluded.min_quantity,
    max_quantity = excluded.max_quantity,
    min_notional = excluded.min_notional,
    max_leverage = excluded.max_leverage,
    financing = excluded.financing,
    schedule = excluded.schedule,
    status = excluded.status,
    updated_at = now()
RETURNING id
"""

INSTRUMENT_COLUMNS: Final = """
id, venue, symbol, venue_symbol, asset_class, base_currency, quote_currency, settlement_currency,
price_increment, price_precision, pip_size, quantity_unit, contract_size,
quantity_increment, min_quantity, max_quantity, min_notional, max_leverage,
financing, schedule, status
"""

UPSERT_BAR: Final = """
INSERT INTO ohlcv_bars (
    instrument_id, granularity, price_component, ts, open, high, low, close,
    tick_count, traded_volume, complete
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (instrument_id, granularity, price_component, ts) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    tick_count = excluded.tick_count,
    traded_volume = excluded.traded_volume,
    complete = excluded.complete
WHERE ohlcv_bars.complete = false
"""
"""Re-ingesting a bar updates it only while it was incomplete.

A bar the venue has already closed is immutable history. Silently overwriting one
would let a late correction rewrite a decision's inputs after the fact, so the update
is restricted rather than unconditional.
"""

INSERT_TICK: Final = """
INSERT INTO ticks (instrument_id, source, ts, bid, ask, bid_size, ask_size, tradeable)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (instrument_id, source, ts) DO NOTHING
"""


INSTRUMENT_ARG_ORDER: Final = (
    "venue",
    "symbol",
    "venue_symbol",
    "asset_class",
    "base_currency",
    "quote_currency",
    "settlement_currency",
    "price_increment",
    "price_precision",
    "pip_size",
    "quantity_unit",
    "contract_size",
    "quantity_increment",
    "min_quantity",
    "max_quantity",
    "min_notional",
    "max_leverage",
    "financing",
    "schedule",
    "status",
)
"""Order of the placeholders in :data:`UPSERT_INSTRUMENT`.

Declared once so the statement and its arguments cannot drift apart.
"""


def instrument_args(instrument: Instrument) -> tuple[object, ...]:
    """Arguments for :data:`UPSERT_INSTRUMENT`, in placeholder order."""
    mapping = instrument.to_mapping()
    return tuple(mapping[key] for key in INSTRUMENT_ARG_ORDER)


@final
class InstrumentRepository:
    """Stores and retrieves instrument definitions."""

    __slots__ = ("_currencies", "_database")

    def __init__(self, database: Database, currencies: CurrencyRegistry) -> None:
        self._database = database
        self._currencies = currencies

    async def upsert(self, instrument: Instrument) -> int:
        """Store an instrument, replacing any previous definition, and return its row id.

        Definitions change: an exchange alters a step size, a broker changes a leverage
        cap. Overwriting is correct, and the audit log is where the change is recorded.
        """
        row_id = await self._database.fetchval(UPSERT_INSTRUMENT, *instrument_args(instrument))
        if row_id is None:  # pragma: no cover - RETURNING always yields a row here
            raise PersistenceError(f"upsert of {instrument.id} returned no id")
        return int(row_id)

    async def upsert_many(self, instruments: Iterable[Instrument]) -> int:
        """Store several definitions in one transaction and return how many were written.

        One transaction so that a partial refresh of a venue's instrument list never
        lands: either every definition is current or none of them changed.
        """
        written = 0
        async with self._database.transaction() as connection:
            for instrument in instruments:
                await connection.fetchval(UPSERT_INSTRUMENT, *instrument_args(instrument))
                written += 1
        return written

    async def get(self, instrument_id: InstrumentId) -> Instrument | None:
        """One instrument, or ``None`` if it has never been stored."""
        row = await self._database.fetchrow(
            f"SELECT {INSTRUMENT_COLUMNS} FROM instruments WHERE venue = $1 AND symbol = $2",
            instrument_id.venue,
            instrument_id.symbol,
        )
        return None if row is None else self.to_instrument(row)

    async def row_id(self, instrument_id: InstrumentId) -> int:
        """The surrogate key used by the bar and tick tables.

        Raises:
            PersistenceError: The instrument has not been stored, so no bar or tick
                can reference it.
        """
        value = await self._database.fetchval(
            "SELECT id FROM instruments WHERE venue = $1 AND symbol = $2",
            instrument_id.venue,
            instrument_id.symbol,
        )
        if value is None:
            raise PersistenceError(
                f"instrument {instrument_id} is not in the database; store its definition "
                f"before writing market data for it"
            )
        return int(value)

    async def list_for_venue(self, venue: str) -> Sequence[Instrument]:
        """Every stored instrument at one venue, ordered by symbol."""
        rows = await self._database.fetch(
            f"SELECT {INSTRUMENT_COLUMNS} FROM instruments WHERE venue = $1 ORDER BY symbol",
            venue,
        )
        return tuple(self.to_instrument(row) for row in rows)

    def to_instrument(self, row: asyncpg.Record) -> Instrument:
        """Rebuild an instrument from a database row."""
        return Instrument.from_mapping(
            {
                "venue": row["venue"],
                "symbol": row["symbol"],
                "venue_symbol": row["venue_symbol"],
                "asset_class": row["asset_class"],
                "base_currency": row["base_currency"],
                "quote_currency": row["quote_currency"],
                "settlement_currency": row["settlement_currency"],
                "price_increment": row["price_increment"],
                "price_precision": row["price_precision"],
                "pip_size": row["pip_size"],
                "quantity_unit": row["quantity_unit"],
                "contract_size": row["contract_size"],
                "quantity_increment": row["quantity_increment"],
                "min_quantity": row["min_quantity"],
                "max_quantity": row["max_quantity"],
                "min_notional": row["min_notional"],
                "max_leverage": row["max_leverage"],
                "financing": row["financing"],
                "schedule": row["schedule"],
                "status": row["status"],
            },
            self._currencies,
        )


@final
class MarketDataRepository:
    """Stores and retrieves bars and ticks."""

    __slots__ = ("_database",)

    def __init__(self, database: Database) -> None:
        self._database = database

    async def store_bars(self, instrument_row_id: int, bars: Sequence[Candle]) -> int:
        """Write bars, updating any that were previously stored as incomplete.

        Returns the number of bars submitted. All are written in one transaction, so a
        partial range never lands.
        """
        if not bars:
            return 0
        rows = [
            (
                instrument_row_id,
                bar.granularity.value,
                bar.price.value,
                bar.start,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.tick_count,
                bar.traded_volume,
                bar.complete,
            )
            for bar in bars
        ]
        async with self._database.transaction() as connection:
            await connection.executemany(UPSERT_BAR, rows)
        return len(rows)

    async def fetch_bars(
        self,
        instrument_row_id: int,
        granularity: Granularity,
        price: CandlePrice,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[Candle]:
        """Bars in ascending time order over a half open range."""
        query, args = build_bar_query(
            instrument_row_id, granularity, price, start=start, end=end, limit=limit
        )
        rows = await self._database.fetch(query, *args)
        return tuple(candle_from_row(row) for row in rows)

    async def store_ticks(
        self, instrument_row_id: int, source: TickSource, ticks: Sequence[Quote]
    ) -> int:
        """Write ticks from one source, ignoring any already stored for the same instant.

        A duplicate timestamp from the same source is a repeat of the same observation,
        so it is dropped rather than overwritten: the first value received is the one
        decisions were made on. Two different sources quoting the same instant are two
        separate observations and both are kept.
        """
        if not ticks:
            return 0
        rows = [
            (
                instrument_row_id,
                source.value,
                tick.ts,
                tick.bid,
                tick.ask,
                tick.bid_size,
                tick.ask_size,
                tick.tradeable,
            )
            for tick in ticks
        ]
        async with self._database.transaction() as connection:
            await connection.executemany(INSERT_TICK, rows)
        return len(rows)

    async def fetch_ticks(
        self,
        instrument_row_id: int,
        source: TickSource,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[Quote]:
        """Ticks from one source in ascending time order over a half open range.

        The source is required rather than optional. Ticks from two liquidity pools
        interleaved into one series would be meaningless, and defaulting to "all of
        them" would make that the easy thing to do by accident.
        """
        query, args = build_tick_query(instrument_row_id, source, start=start, end=end, limit=limit)
        rows = await self._database.fetch(query, *args)
        return tuple(quote_from_row(row) for row in rows)

    async def fetch_calibration_ticks(
        self,
        instrument_id: InstrumentId,
        instrument_row_id: int,
        source: TickSource,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> CalibrationTicks[Quote]:
        """Ticks eligible for cost model calibration.

        Reads the ``execution_venue_ticks`` view rather than ``ticks``, so research
        data is filtered out in the database as well as refused by the return type.
        Two independent barriers, because getting this wrong produces a backtest that
        looks right.

        Raises:
            ProvenanceError: ``source`` is a research source.
        """
        if not source.is_execution_venue:
            raise ProvenanceError(
                f"{source.value} is {source.provenance.value} and cannot be calibrated "
                f"on. Its spreads are not the spreads we will pay."
            )
        query, args = build_tick_query(
            instrument_row_id, source, start=start, end=end, limit=limit, calibration=True
        )
        rows = await self._database.fetch(query, *args)
        return CalibrationTicks.of(
            instrument_id, source, tuple(quote_from_row(row) for row in rows)
        )

    async def latest_bar_start(
        self, instrument_row_id: int, granularity: Granularity, price: CandlePrice
    ) -> datetime | None:
        """Start of the most recent stored bar, or ``None`` when there are none.

        This is the resume point for a backfill.
        """
        value = await self._database.fetchval(
            "SELECT max(ts) FROM ohlcv_bars WHERE instrument_id = $1 AND granularity = $2 "
            "AND price_component = $3",
            instrument_row_id,
            granularity.value,
            price.value,
        )
        return None if value is None else value


def build_bar_query(
    instrument_row_id: int,
    granularity: Granularity,
    price: CandlePrice,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> tuple[str, list[object]]:
    """Build the bar selection statement and its arguments.

    Separated from execution so the parameter numbering, the half open range, and the
    ordering can be asserted without a database.
    """
    args: list[object] = [instrument_row_id, granularity.value, price.value]
    conditions = ["instrument_id = $1", "granularity = $2", "price_component = $3"]
    if start is not None:
        args.append(start)
        conditions.append(f"ts >= ${len(args)}")
    if end is not None:
        args.append(end)
        conditions.append(f"ts < ${len(args)}")
    suffix = ""
    if limit is not None:
        if limit < 1:
            raise PersistenceError(f"limit must be at least 1, got {limit}")
        args.append(limit)
        suffix = f" LIMIT ${len(args)}"
    query = (
        "SELECT instrument_id, venue, symbol, granularity, price_component, ts, open, high, "
        "low, close, tick_count, traded_volume, complete FROM ohlcv_bars_with_instrument WHERE "
        f"{' AND '.join(conditions)} ORDER BY ts ASC{suffix}"
    )
    return query, args


def build_tick_query(
    instrument_row_id: int,
    source: TickSource,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    calibration: bool = False,
) -> tuple[str, list[object]]:
    """Build the tick selection statement and its arguments.

    Args:
        instrument_row_id: Surrogate key of the instrument.
        source: Which feed to read. Never optional: interleaving two liquidity pools
            into one series would be meaningless.
        start: Inclusive lower bound.
        end: Exclusive upper bound.
        limit: Maximum rows.
        calibration: Read the execution venue view instead of the base table, so that
            research rows cannot be returned even if the source filter were wrong.
    """
    args: list[object] = [instrument_row_id, source.value]
    conditions = ["instrument_id = $1", "source = $2"]
    if start is not None:
        args.append(start)
        conditions.append(f"ts >= ${len(args)}")
    if end is not None:
        args.append(end)
        conditions.append(f"ts < ${len(args)}")
    suffix = ""
    if limit is not None:
        if limit < 1:
            raise PersistenceError(f"limit must be at least 1, got {limit}")
        args.append(limit)
        suffix = f" LIMIT ${len(args)}"
    table = "execution_venue_ticks" if calibration else "ticks_with_instrument"
    query = (
        "SELECT instrument_id, venue, symbol, source, ts, bid, ask, bid_size, ask_size, "
        f"tradeable FROM {table} WHERE {' AND '.join(conditions)} ORDER BY ts ASC{suffix}"
    )
    return query, args


def candle_from_row(row: asyncpg.Record) -> Candle:
    """Rebuild a bar from a row of the bar view."""
    return Candle(
        instrument_id=InstrumentId(venue=row["venue"], symbol=row["symbol"]),
        granularity=Granularity(row["granularity"]),
        price=CandlePrice(row["price_component"]),
        start=row["ts"],
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        tick_count=None if row["tick_count"] is None else int(row["tick_count"]),
        traded_volume=row["traded_volume"],
        complete=bool(row["complete"]),
    )


def quote_from_row(row: asyncpg.Record) -> Quote:
    """Rebuild a tick from a row of the tick view."""
    return Quote(
        instrument_id=InstrumentId(venue=row["venue"], symbol=row["symbol"]),
        ts=row["ts"],
        bid=row["bid"],
        ask=row["ask"],
        bid_size=row["bid_size"],
        ask_size=row["ask_size"],
        tradeable=bool(row["tradeable"]),
    )
