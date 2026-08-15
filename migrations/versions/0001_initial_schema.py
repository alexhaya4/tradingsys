"""Initial schema: instruments, bars, ticks, and the append-only audit log.

Design notes that the DDL alone does not explain:

*Every price and size is NUMERIC.* Storing a price as double precision loses the exact
decimal value the venue sent, and the loss is not recoverable. NUMERIC costs more space
and is the only correct choice here.

*Bars and ticks are hypertables.* They are the two tables that grow without bound, so
they are partitioned by time. The audit log is not: it is low volume, and it carries a
strictly increasing sequence with a uniqueness constraint that partitioning would
complicate for no benefit.

*The audit log is append only in the database.* A trigger raises on UPDATE and DELETE
rather than relying on the application to behave. Privileges are revoked as well, so
neither a bug nor a direct psql session can rewrite history.

Revision ID: 0001
Revises:
Created: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    _create_instruments()
    _create_bars()
    _create_ticks()
    _create_views()
    _create_audit_log()


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS ticks_with_instrument")
    op.execute("DROP VIEW IF EXISTS ohlcv_bars_with_instrument")
    op.execute("DROP TABLE IF EXISTS ticks")
    op.execute("DROP TABLE IF EXISTS ohlcv_bars")
    op.execute("DROP TRIGGER IF EXISTS audit_log_is_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_log_mutation()")
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TABLE IF EXISTS instruments")


def _create_instruments() -> None:
    op.execute(
        """
        CREATE TABLE instruments (
            id                  BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            venue               TEXT        NOT NULL,
            symbol              TEXT        NOT NULL,
            venue_symbol        TEXT        NOT NULL,
            asset_class         TEXT        NOT NULL,
            base_currency       TEXT        NOT NULL,
            quote_currency      TEXT        NOT NULL,
            settlement_currency TEXT        NOT NULL,
            price_increment     NUMERIC     NOT NULL CHECK (price_increment > 0),
            price_precision     INTEGER     NOT NULL CHECK (price_precision >= 0),
            pip_size            NUMERIC     CHECK (pip_size IS NULL OR pip_size > 0),
            quantity_unit       TEXT        NOT NULL,
            contract_size       NUMERIC     NOT NULL CHECK (contract_size > 0),
            quantity_increment  NUMERIC     NOT NULL CHECK (quantity_increment > 0),
            min_quantity        NUMERIC     NOT NULL CHECK (min_quantity > 0),
            max_quantity        NUMERIC     CHECK (max_quantity IS NULL OR max_quantity > 0),
            min_notional        NUMERIC     CHECK (min_notional IS NULL OR min_notional > 0),
            max_leverage        NUMERIC     CHECK (max_leverage IS NULL OR max_leverage > 0),
            financing           JSONB       NOT NULL,
            schedule            JSONB       NOT NULL,
            status              TEXT        NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT instruments_venue_symbol_key UNIQUE (venue, symbol),
            CONSTRAINT instruments_base_is_not_quote CHECK (base_currency <> quote_currency),
            CONSTRAINT instruments_max_above_min
                CHECK (max_quantity IS NULL OR max_quantity >= min_quantity)
        )
        """
    )
    op.execute("CREATE INDEX instruments_venue_idx ON instruments (venue)")
    op.execute("CREATE INDEX instruments_status_idx ON instruments (status)")
    op.execute(
        """
        COMMENT ON TABLE instruments IS
        'Venue scoped instrument definitions. The same symbol at two venues is two rows, '
        'because tick size, step size, and leverage differ between them. symbol is the '
        'canonical name; venue_symbol is the venue own spelling used on the wire.'
        """
    )


def _create_bars() -> None:
    op.execute(
        """
        CREATE TABLE ohlcv_bars (
            instrument_id   BIGINT      NOT NULL REFERENCES instruments (id) ON DELETE CASCADE,
            granularity     TEXT        NOT NULL,
            price_component TEXT        NOT NULL,
            ts              TIMESTAMPTZ NOT NULL,
            open            NUMERIC     NOT NULL CHECK (open > 0),
            high            NUMERIC     NOT NULL CHECK (high > 0),
            low             NUMERIC     NOT NULL CHECK (low > 0),
            close           NUMERIC     NOT NULL CHECK (close > 0),
            volume          NUMERIC     NOT NULL DEFAULT 0 CHECK (volume >= 0),
            trade_count     INTEGER     NOT NULL DEFAULT 0 CHECK (trade_count >= 0),
            complete        BOOLEAN     NOT NULL DEFAULT true,
            inserted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ohlcv_bars_pkey
                PRIMARY KEY (instrument_id, granularity, price_component, ts),
            CONSTRAINT ohlcv_bars_high_is_highest CHECK (high >= low AND high >= open
                                                         AND high >= close),
            CONSTRAINT ohlcv_bars_low_is_lowest CHECK (low <= open AND low <= close)
        )
        """
    )
    # ts is the partition column. A week per chunk keeps a typical query inside one or
    # two chunks while keeping the chunk count manageable over years of history.
    op.execute(
        "SELECT create_hypertable('ohlcv_bars', 'ts', chunk_time_interval => INTERVAL '7 days')"
    )
    op.execute(
        """
        ALTER TABLE ohlcv_bars SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'instrument_id, granularity, price_component',
            timescaledb.compress_orderby = 'ts DESC'
        )
        """
    )
    # Bars older than 30 days are read for backtests and never updated, so compressing
    # them trades a little decompression time for a large reduction in storage.
    op.execute("SELECT add_compression_policy('ohlcv_bars', INTERVAL '30 days')")
    op.execute("CREATE INDEX ohlcv_bars_instrument_time_idx ON ohlcv_bars (instrument_id, ts DESC)")
    op.execute(
        """
        COMMENT ON COLUMN ohlcv_bars.ts IS
        'Inclusive left edge of the bar interval. The bar covers [ts, ts + granularity).'
        """
    )


def _create_ticks() -> None:
    op.execute(
        """
        CREATE TABLE ticks (
            instrument_id BIGINT      NOT NULL REFERENCES instruments (id) ON DELETE CASCADE,
            ts            TIMESTAMPTZ NOT NULL,
            bid           NUMERIC     NOT NULL CHECK (bid > 0),
            ask           NUMERIC     NOT NULL CHECK (ask > 0),
            bid_size      NUMERIC     CHECK (bid_size IS NULL OR bid_size >= 0),
            ask_size      NUMERIC     CHECK (ask_size IS NULL OR ask_size >= 0),
            tradeable     BOOLEAN     NOT NULL DEFAULT true,
            inserted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ticks_pkey PRIMARY KEY (instrument_id, ts),
            CONSTRAINT ticks_not_crossed CHECK (ask >= bid)
        )
        """
    )
    # Ticks arrive far faster than bars, so chunks cover a single day.
    op.execute("SELECT create_hypertable('ticks', 'ts', chunk_time_interval => INTERVAL '1 day')")
    op.execute(
        """
        ALTER TABLE ticks SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'instrument_id',
            timescaledb.compress_orderby = 'ts DESC'
        )
        """
    )
    op.execute("SELECT add_compression_policy('ticks', INTERVAL '7 days')")


def _create_views() -> None:
    # Bars and ticks are keyed by the surrogate instrument id for storage efficiency.
    # These views join the venue and symbol back on so that a row can be mapped
    # straight to a domain object without a second query.
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


def _create_audit_log() -> None:
    op.execute(
        """
        CREATE TABLE audit_log (
            sequence       BIGINT      NOT NULL PRIMARY KEY,
            ts             TIMESTAMPTZ NOT NULL,
            correlation_id TEXT        NOT NULL,
            category       TEXT        NOT NULL,
            actor          TEXT        NOT NULL,
            action         TEXT        NOT NULL,
            summary        TEXT        NOT NULL,
            instrument_id  TEXT,
            payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,
            previous_hash  CHAR(64)    NOT NULL,
            entry_hash     CHAR(64)    NOT NULL,
            recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT audit_log_sequence_is_positive CHECK (sequence >= 1),
            CONSTRAINT audit_log_entry_hash_key UNIQUE (entry_hash),
            CONSTRAINT audit_log_previous_hash_key UNIQUE (previous_hash)
        )
        """
    )
    op.execute("CREATE INDEX audit_log_ts_idx ON audit_log (ts DESC)")
    op.execute("CREATE INDEX audit_log_correlation_idx ON audit_log (correlation_id)")
    op.execute("CREATE INDEX audit_log_category_ts_idx ON audit_log (category, ts DESC)")
    op.execute("CREATE INDEX audit_log_actor_idx ON audit_log (actor)")

    # The uniqueness of previous_hash is what prevents a fork: two entries can never
    # both claim the same predecessor, even if the advisory lock were bypassed.
    op.execute(
        """
        CREATE FUNCTION reject_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is append only: % on sequence % was rejected',
                TG_OP, COALESCE(OLD.sequence, NEW.sequence)
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_is_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()
        """
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM PUBLIC")
    op.execute(
        """
        COMMENT ON TABLE audit_log IS
        'Append only, hash chained record of every decision the system makes. '
        'UPDATE and DELETE are rejected by trigger. Verify with AuditLog.verify().'
        """
    )
