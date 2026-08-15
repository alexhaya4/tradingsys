"""Tick provenance, split volume semantics, derived per-side bars, resumable backfill.

Four changes, each forced by something learned after phase 1 closed.

*Ticks carry a source.* Deep forex history comes from Dukascopy, which is a different
liquidity pool from the execution venue. Its spreads are not the spreads we will pay,
so a cost model calibrated on them would be wrong in a way nobody would notice. Every
tick therefore records where it came from, the primary key includes it so two sources
can price the same instant without colliding, and the ``execution_venue_ticks`` view
below is the only thing the calibration path reads.

*Volume becomes two columns.* A cTrader trendbar's volume is a count of ticks; a Bybit
kline's is real traded size. One column holding both would hand every consumer a number
that looks plausible and means something different depending on the venue. Separate
nullable columns make the absence explicit: code reading ``traded_volume`` on a forex
bar gets NULL and fails, rather than quietly averaging tick counts.

*Per-side bars are derived, not stored.* Neither venue publishes bid or ask candles.
cTrader trendbars have no quote-type parameter at all and Bybit klines are trade prices,
so the only honest source of a per-side bar is the per-side ticks. They become
continuous aggregates over ``ticks``. ``ohlcv_bars`` stays for venue-supplied OHLCV,
which is still the only historical source for crypto.

*Backfill is resumable.* Dukascopy returns a 503 HTML body under parallel load, and a
downloader that trusted the response would skip an hour silently. Per-hour state is
persisted so an interrupted backfill resumes without refetching what landed and without
leaving holes where it stopped. A failed hour is recorded as failed, never as complete.

**Compression is suspended around the alterations.** TimescaleDB refuses ALTER COLUMN
and ADD CONSTRAINT while compression is enabled, and removing the policy and
decompressing the chunks is not enough: the setting itself is the block. All three come
off and all three go back on. This works because migrations run with statement level
autocommit, for the reasons set out in migrations/env.py.

Revision ID: 0002
Revises: 0001
Created: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BID_AGGREGATE = "ohlcv_1m_bid"
ASK_AGGREGATE = "ohlcv_1m_ask"

EXECUTION_VENUE_SOURCES = ("ctrader", "bybit")
"""Sources whose ticks may be calibrated on.

Mirrors ``TickSource.execution_venues()``. A test asserts the two agree, so adding a
venue in one place and not the other fails the suite rather than silently excluding
real data or admitting research data into the cost model.
"""


def upgrade() -> None:
    _add_tick_source()
    _split_bar_volume()
    _create_views()
    _create_per_side_aggregates()
    _create_backfill_state()


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS backfill_hours")
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {ASK_AGGREGATE}")
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {BID_AGGREGATE}")
    op.execute("DROP VIEW IF EXISTS execution_venue_ticks")
    op.execute("DROP VIEW IF EXISTS ticks_with_instrument")
    op.execute("DROP VIEW IF EXISTS ohlcv_bars_with_instrument")

    _suspend_compression("ohlcv_bars")
    op.execute("ALTER TABLE ohlcv_bars ADD COLUMN volume NUMERIC NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE ohlcv_bars ADD COLUMN trade_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE ohlcv_bars DROP COLUMN traded_volume")
    op.execute("ALTER TABLE ohlcv_bars DROP COLUMN tick_count")
    _restore_compression("ohlcv_bars", "instrument_id, granularity, price_component", "30 days")

    _suspend_compression("ticks")
    op.execute("ALTER TABLE ticks DROP CONSTRAINT ticks_pkey")
    op.execute("DROP INDEX IF EXISTS ticks_source_time_idx")
    op.execute("ALTER TABLE ticks DROP COLUMN source")
    op.execute("ALTER TABLE ticks ADD CONSTRAINT ticks_pkey PRIMARY KEY (instrument_id, ts)")
    _restore_compression("ticks", "instrument_id", "7 days")

    op.execute(
        """
        CREATE VIEW ohlcv_bars_with_instrument AS
        SELECT b.instrument_id, i.venue, i.symbol, b.granularity, b.price_component, b.ts,
               b.open, b.high, b.low, b.close, b.volume, b.trade_count, b.complete
        FROM ohlcv_bars b
        JOIN instruments i ON i.id = b.instrument_id
        """
    )
    op.execute(
        """
        CREATE VIEW ticks_with_instrument AS
        SELECT t.instrument_id, i.venue, i.symbol, t.ts, t.bid, t.ask, t.bid_size,
               t.ask_size, t.tradeable
        FROM ticks t
        JOIN instruments i ON i.id = t.instrument_id
        """
    )


def _suspend_compression(table: str) -> None:
    """Take compression off a hypertable so its shape can be altered.

    All three of the policy, the compressed chunks, and the setting have to go. Leaving
    the setting in place blocks ALTER even when nothing is actually compressed.
    """
    op.execute(f"SELECT remove_compression_policy('{table}', if_exists => true)")
    op.execute(f"SELECT decompress_chunk(c, true) FROM show_chunks('{table}') c")
    op.execute(f"ALTER TABLE {table} SET (timescaledb.compress = false)")


def _restore_compression(table: str, segment_by: str, policy: str) -> None:
    """Put compression and its policy back after an alteration."""
    op.execute(
        f"""
        ALTER TABLE {table} SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = '{segment_by}',
            timescaledb.compress_orderby = 'ts DESC'
        )
        """
    )
    op.execute(f"SELECT add_compression_policy('{table}', INTERVAL '{policy}')")


def _add_tick_source() -> None:
    op.execute("DROP VIEW IF EXISTS ticks_with_instrument")
    _suspend_compression("ticks")

    op.execute("ALTER TABLE ticks ADD COLUMN source TEXT")
    # Anything already stored came from the execution venue, because Dukascopy
    # ingestion does not exist yet. Stated rather than defaulted, so the backfill of
    # this column is auditable.
    op.execute("UPDATE ticks SET source = 'ctrader' WHERE source IS NULL")
    op.execute("ALTER TABLE ticks ALTER COLUMN source SET NOT NULL")

    # The key gains source: two pools legitimately quote the same instrument at the
    # same millisecond and neither should overwrite the other.
    op.execute("ALTER TABLE ticks DROP CONSTRAINT ticks_pkey")
    op.execute(
        "ALTER TABLE ticks ADD CONSTRAINT ticks_pkey PRIMARY KEY (instrument_id, source, ts)"
    )
    op.execute("CREATE INDEX ticks_source_time_idx ON ticks (source, ts DESC)")

    _restore_compression("ticks", "instrument_id, source", "7 days")
    op.execute(
        """
        COMMENT ON COLUMN ticks.source IS
        'Which feed this tick came from. Determines what it may be used for: execution '
        'venue data may be calibrated on, research data may not. See '
        'tradingsys.core.provenance.'
        """
    )


def _split_bar_volume() -> None:
    op.execute("DROP VIEW IF EXISTS ohlcv_bars_with_instrument")
    _suspend_compression("ohlcv_bars")

    op.execute("ALTER TABLE ohlcv_bars ADD COLUMN tick_count INTEGER")
    op.execute("ALTER TABLE ohlcv_bars ADD COLUMN traded_volume NUMERIC")
    op.execute(
        "ALTER TABLE ohlcv_bars ADD CONSTRAINT ohlcv_bars_tick_count_not_negative "
        "CHECK (tick_count IS NULL OR tick_count >= 0)"
    )
    op.execute(
        "ALTER TABLE ohlcv_bars ADD CONSTRAINT ohlcv_bars_traded_volume_not_negative "
        "CHECK (traded_volume IS NULL OR traded_volume >= 0)"
    )
    # Nothing has been ingested, so there is no value to carry across and no need to
    # guess which of the two meanings the old column held.
    op.execute("ALTER TABLE ohlcv_bars DROP COLUMN volume")
    op.execute("ALTER TABLE ohlcv_bars DROP COLUMN trade_count")

    _restore_compression("ohlcv_bars", "instrument_id, granularity, price_component", "30 days")
    op.execute(
        """
        COMMENT ON COLUMN ohlcv_bars.tick_count IS
        'Number of ticks in the bar. Supplied by cTrader, which calls it volume. NULL '
        'where the venue does not report it.'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN ohlcv_bars.traded_volume IS
        'Base asset volume actually traded. Supplied by crypto venues. NULL for forex, '
        'where no such figure exists, so that code reading it fails rather than '
        'silently computing against a tick count.'
        """
    )


def _create_views() -> None:
    op.execute(
        """
        CREATE VIEW ohlcv_bars_with_instrument AS
        SELECT b.instrument_id, i.venue, i.symbol, b.granularity, b.price_component, b.ts,
               b.open, b.high, b.low, b.close, b.tick_count, b.traded_volume, b.complete
        FROM ohlcv_bars b
        JOIN instruments i ON i.id = b.instrument_id
        """
    )
    op.execute(
        """
        CREATE VIEW ticks_with_instrument AS
        SELECT t.instrument_id, i.venue, i.symbol, t.source, t.ts, t.bid, t.ask,
               t.bid_size, t.ask_size, t.tradeable
        FROM ticks t
        JOIN instruments i ON i.id = t.instrument_id
        """
    )
    sources = ", ".join(f"'{source}'" for source in EXECUTION_VENUE_SOURCES)
    op.execute(
        f"""
        CREATE VIEW execution_venue_ticks AS
        SELECT t.instrument_id, i.venue, i.symbol, t.source, t.ts, t.bid, t.ask,
               t.bid_size, t.ask_size, t.tradeable
        FROM ticks t
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.source IN ({sources})
        """
    )
    op.execute(
        """
        COMMENT ON VIEW execution_venue_ticks IS
        'Ticks from venues we actually trade on. The only permitted input to cost model '
        'calibration: research sources such as Dukascopy are a different liquidity pool '
        'and their spreads are not the spreads we will pay.'
        """
    )


def _create_per_side_aggregates() -> None:
    # One aggregate per side. The time_bucket left edge matches the convention already
    # in use for bars: a bar covers [ts, ts + granularity).
    for name, column in ((BID_AGGREGATE, "bid"), (ASK_AGGREGATE, "ask")):
        op.execute(
            f"""
            CREATE MATERIALIZED VIEW {name}
            WITH (timescaledb.continuous) AS
            SELECT
                instrument_id,
                source,
                time_bucket(INTERVAL '1 minute', ts) AS bucket,
                first({column}, ts) AS open,
                max({column})       AS high,
                min({column})       AS low,
                last({column}, ts)  AS close,
                count(*)            AS tick_count
            FROM ticks
            GROUP BY instrument_id, source, bucket
            WITH NO DATA
            """
        )
        # The refresh trails by two minutes so a bucket materialises only once the
        # minute it covers has closed and any late ticks have landed.
        op.execute(
            f"""
            SELECT add_continuous_aggregate_policy('{name}',
                start_offset => INTERVAL '3 days',
                end_offset   => INTERVAL '2 minutes',
                schedule_interval => INTERVAL '1 minute')
            """
        )
        # A continuous aggregate is created as a materialized view but registered in
        # the catalogue as a plain view, so COMMENT ON MATERIALIZED VIEW is rejected.
        op.execute(
            f"""
            COMMENT ON VIEW {name} IS
            'Per-side one minute bars derived from ticks. Neither venue publishes {column} '
            'candles, so these are computed rather than stored. source is carried through '
            'so research and execution venue bars never merge.'
            """
        )


def _create_backfill_state() -> None:
    op.execute(
        """
        CREATE TABLE backfill_hours (
            source        TEXT        NOT NULL,
            instrument_id BIGINT      NOT NULL REFERENCES instruments (id) ON DELETE CASCADE,
            hour_start    TIMESTAMPTZ NOT NULL,
            status        TEXT        NOT NULL,
            attempts      INTEGER     NOT NULL DEFAULT 0,
            rows_written  INTEGER,
            last_error    TEXT,
            claimed_at    TIMESTAMPTZ,
            completed_at  TIMESTAMPTZ,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT backfill_hours_pkey PRIMARY KEY (source, instrument_id, hour_start),
            CONSTRAINT backfill_hours_status_known
                CHECK (status IN ('pending', 'in_progress', 'complete', 'failed')),
            CONSTRAINT backfill_hours_attempts_not_negative CHECK (attempts >= 0),
            CONSTRAINT backfill_hours_complete_has_a_row_count
                CHECK (status <> 'complete' OR rows_written IS NOT NULL),
            CONSTRAINT backfill_hours_complete_has_a_completion_time
                CHECK (status <> 'complete' OR completed_at IS NOT NULL),
            CONSTRAINT backfill_hours_failure_has_a_reason
                CHECK (status <> 'failed' OR last_error IS NOT NULL),
            CONSTRAINT backfill_hours_hour_is_aligned
                CHECK (hour_start = date_trunc('hour', hour_start))
        )
        """
    )
    op.execute(
        "CREATE INDEX backfill_hours_outstanding_idx "
        "ON backfill_hours (source, status, hour_start) WHERE status <> 'complete'"
    )
    op.execute(
        """
        COMMENT ON TABLE backfill_hours IS
        'One row per source, instrument, and hour of history. An hour is complete only '
        'once its rows are committed, so an interrupted backfill resumes without '
        'refetching and without leaving holes. A failed hour stays failed and carries '
        'its reason; it is retried later and never silently treated as done.'
        """
    )
