"""asyncpg connection pool management.

Two settings here are load bearing and easy to get wrong.

``NUMERIC`` columns come back as :class:`~decimal.Decimal` by default in asyncpg, which
is what we want and why every price and quantity column uses ``NUMERIC`` rather than
``DOUBLE PRECISION``. A codec is registered anyway so that a future change to the
driver's defaults cannot silently start handing us floats.

The prepared statement cache is disabled by default in configuration, because PgBouncer
in transaction pooling mode cannot support per-connection prepared statements. Running
against a direct connection is faster with the cache on, so it stays configurable
rather than hard coded either way.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self, final

import asyncpg

from tradingsys.core.errors import PersistenceError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from types import TracebackType

    from tradingsys.config.settings import DatabaseSettings

__all__ = ["Database"]


@final
class Database:
    """An owned asyncpg pool with the codecs and timeouts this system needs."""

    __slots__ = ("_pool", "_settings")

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool[asyncpg.Record] | None = None

    @property
    def settings(self) -> DatabaseSettings:
        return self._settings

    @property
    def is_connected(self) -> bool:
        return self._pool is not None and not self._pool.is_closing()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the pool and verify the connection.

        Calling this twice is a no-op rather than an error, so that a component that
        can be started from more than one path stays simple.

        Raises:
            PersistenceError: The database is unreachable or rejected the credentials.
        """
        if self._pool is not None:
            return
        try:
            self._pool = await asyncpg.create_pool(
                dsn=self._settings.dsn(reveal_password=True),
                min_size=self._settings.min_pool_size,
                max_size=self._settings.max_pool_size,
                timeout=self._settings.connect_timeout_seconds,
                command_timeout=self._settings.command_timeout_seconds,
                statement_cache_size=self._settings.statement_cache_size,
                init=_configure_connection,
                server_settings={"application_name": "tradingsys"},
            )
        except (OSError, asyncpg.PostgresError) as exc:
            raise PersistenceError(f"could not connect to {self._settings.dsn()}: {exc}") from exc
        if self._pool is None:  # pragma: no cover - defensive, asyncpg returns a pool
            raise PersistenceError(f"asyncpg returned no pool for {self._settings.dsn()}")

    async def close(self) -> None:
        """Close the pool, waiting for in-flight queries to finish.

        Safe to call when never connected.
        """
        if self._pool is None:
            return
        pool, self._pool = self._pool, None
        await pool.close()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # access
    # ------------------------------------------------------------------

    @property
    def pool(self) -> asyncpg.Pool[asyncpg.Record]:
        """The live pool.

        Raises:
            PersistenceError: :meth:`connect` has not been called.
        """
        if self._pool is None:
            raise PersistenceError(
                "the database pool is not open; call connect() during startup before "
                "issuing queries"
            )
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection[asyncpg.Record]]:
        """Borrow a connection from the pool for the duration of a block."""
        async with self.pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection[asyncpg.Record]]:
        """Borrow a connection and run the block inside one transaction.

        The transaction commits when the block completes and rolls back if it raises.
        """
        async with self.pool.acquire() as connection, connection.transaction():
            yield connection

    # ------------------------------------------------------------------
    # convenience wrappers
    # ------------------------------------------------------------------

    async def execute(self, query: str, *args: object) -> str:
        """Run a statement and return its status tag."""
        tag: str = await self.pool.execute(query, *args)
        return tag

    async def fetch(self, query: str, *args: object) -> Sequence[asyncpg.Record]:
        """Run a query and return every row."""
        rows: Sequence[asyncpg.Record] = await self.pool.fetch(query, *args)
        return rows

    async def fetchrow(self, query: str, *args: object) -> asyncpg.Record | None:
        """Run a query and return the first row, or ``None``."""
        return await self.pool.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: object) -> Any:
        """Run a query and return the first column of the first row."""
        return await self.pool.fetchval(query, *args)

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------

    async def check(self) -> None:
        """Verify the database answers.

        Raises:
            PersistenceError: The database did not respond correctly.
        """
        try:
            value = await self.fetchval("SELECT 1")
        except (OSError, asyncpg.PostgresError) as exc:
            raise PersistenceError(f"database health check failed: {exc}") from exc
        if value != 1:
            raise PersistenceError(f"database health check returned {value!r}, expected 1")

    async def timescale_version(self) -> str | None:
        """The installed TimescaleDB extension version, or ``None`` if absent.

        Used at startup to fail loudly when the hypertables the schema depends on
        cannot exist.
        """
        result = await self.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
        )
        return None if result is None else str(result)

    async def missing_tables(self, required: Sequence[str]) -> tuple[str, ...]:
        """Which of ``required`` tables do not exist, in the order given.

        Checked at startup so that an unmigrated database produces one clear message
        instead of an undefined-table error from whichever query happens to run first.
        """
        rows = await self.fetch(
            "SELECT name FROM unnest($1::text[]) AS name WHERE to_regclass(name) IS NULL",
            list(required),
        )
        absent = {str(row["name"]) for row in rows}
        return tuple(name for name in required if name in absent)

    def pool_stats(self) -> dict[str, int]:
        """Current pool occupancy, for metrics."""
        if self._pool is None:
            return {"size": 0, "idle": 0, "in_use": 0, "max_size": self._settings.max_pool_size}
        size = self._pool.get_size()
        idle = self._pool.get_idle_size()
        return {
            "size": size,
            "idle": idle,
            "in_use": size - idle,
            "max_size": self._settings.max_pool_size,
        }


async def _configure_connection(connection: asyncpg.Connection[asyncpg.Record]) -> None:
    """Register codecs on every pooled connection.

    JSONB is encoded and decoded with sorted keys so that the audit log's hash chain
    is computed over a byte-for-byte stable representation. NUMERIC is pinned to
    Decimal so that no driver default can turn a price into a float.
    """
    await connection.set_type_codec(
        "jsonb",
        encoder=_dump_json,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
    await connection.set_type_codec(
        "json",
        encoder=_dump_json,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
    await connection.set_type_codec(
        "numeric",
        encoder=_encode_numeric,
        decoder=Decimal,
        schema="pg_catalog",
        format="text",
    )


def _dump_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _encode_numeric(value: object) -> str:
    """Render a numeric for the wire, refusing floats.

    A float reaching this point would already have lost precision, so it is an error
    rather than something to round.
    """
    if isinstance(value, float):
        raise PersistenceError(
            f"refusing to store the float {value!r} in a NUMERIC column; use Decimal"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PersistenceError(f"refusing to store the non-finite value {value} in NUMERIC")
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return str(Decimal(value))
    raise PersistenceError(f"cannot store {type(value).__name__} in a NUMERIC column")
