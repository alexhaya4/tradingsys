#!/usr/bin/env python
"""Measure bytes per tick row and what compression achieves on rows of that shape.

The two constants in `deploy/provision/healthcheck.sh`, 243.81 bytes uncompressed and
21.90 compressed, came from a one off script in a session scratchpad that was never
committed. The comment beside them cited `PROGRESS.md` for provenance and `PROGRESS.md`
did not carry it, which is a number asserted with no source: the same defect as the uid
comment that broke the host. This is that script, in the repository, so the figures are
reproducible rather than remembered.

**Why a short sample is legitimate here and is not legitimate for rate estimation.**
These are properties of the row shape rather than of the arrival rate. How wide a tick
row is on disk, and how well a column of near-identical numerics compresses, do not
change between a quiet hour and a busy one. The number of rows per day does, which is why
the rate is measured over a day and this is measured over minutes.

**What it deliberately does not tell you.** The authoritative compression ratio is the one
the running host reports once its first chunks compress, because that is the real table,
the real chunk size and the real write pattern. This measures a chunk built in one pass
from a few minutes of data. Larger chunks generally compress better, since the encodings
work over longer runs, so this figure is closer to a floor than to a ceiling. Against that,
`inserted_at` here is a single instant for the whole load and in production it varies per
batch, which pushes the other way.

Usage::

    set -a; . ./.env; set +a
    TRADINGSYS_DATABASE__HOST=localhost scripts/measure_storage_footprint.py --seconds 300

The host override is needed because the development configuration names the database `db`,
which resolves inside the compose network and not from a shell beside it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg
import websockets

from tradingsys.config import load_settings

DEFAULT_URL = "wss://stream.bybit.com/v5/public/linear"

# The measurement table mirrors the shipped schema rather than approximating it. Column
# order, types, primary key, and both compression settings are what migration 0002
# creates, because every one of them moves the answer.
DDL = """
CREATE TABLE {name} (
    instrument_id BIGINT      NOT NULL,
    source        TEXT        NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    bid           NUMERIC     NOT NULL,
    ask           NUMERIC     NOT NULL,
    bid_size      NUMERIC,
    ask_size      NUMERIC,
    tradeable     BOOLEAN     NOT NULL DEFAULT true,
    inserted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT {name}_pkey PRIMARY KEY (instrument_id, source, ts)
)
"""


def default_symbols() -> tuple[str, ...]:
    """The crypto universe, read from configuration rather than written here."""
    universe = load_settings().universe
    return universe.venue_symbols("bybit")


async def collect(url: str, symbols: tuple[str, ...], seconds: float) -> list[tuple[Any, ...]]:
    """Real top of book from the venue, for the configured symbols.

    Duplicate instants are dropped, because the primary key would reject them and a
    measurement that counted rows the table cannot hold would overstate the row count and
    understate bytes per row.
    """
    row_ids = {symbol: index + 1 for index, symbol in enumerate(symbols)}
    rows: list[tuple[Any, ...]] = []
    seen: set[tuple[int, datetime]] = set()
    deadline = datetime.now(tz=UTC).timestamp() + seconds
    async with websockets.connect(url, ping_interval=20) as socket:
        await socket.send(
            json.dumps({"op": "subscribe", "args": [f"orderbook.1.{s}" for s in symbols]})
        )
        while datetime.now(tz=UTC).timestamp() < deadline:
            message = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
            data = message.get("data")
            if not data or not data.get("b") or not data.get("a"):
                continue
            symbol = data["s"]
            if symbol not in row_ids:
                continue
            ts = datetime.fromtimestamp(message["ts"] / 1000, tz=UTC)
            key = (row_ids[symbol], ts)
            if key in seen:
                continue
            seen.add(key)
            bid, ask = data["b"][0], data["a"][0]
            rows.append(
                (
                    row_ids[symbol],
                    "bybit",
                    ts,
                    Decimal(bid[0]),
                    Decimal(ask[0]),
                    Decimal(bid[1]),
                    Decimal(ask[1]),
                    True,
                    datetime.now(tz=UTC),
                )
            )
    return rows


async def measure(
    connection: asyncpg.Connection, name: str, rows: list[tuple[Any, ...]]
) -> dict[str, Any]:
    """Load, size, compress, size again."""
    await connection.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
    await connection.execute(DDL.format(name=name))
    await connection.execute(
        f"SELECT create_hypertable('{name}', 'ts', chunk_time_interval => INTERVAL '1 day')"
    )
    await connection.execute(
        f"""ALTER TABLE {name} SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'instrument_id, source',
            timescaledb.compress_orderby = 'ts DESC'
        )"""
    )
    await connection.copy_records_to_table(
        name,
        records=rows,
        columns=[
            "instrument_id",
            "source",
            "ts",
            "bid",
            "ask",
            "bid_size",
            "ask_size",
            "tradeable",
            "inserted_at",
        ],
    )
    await connection.execute(f"ANALYZE {name}")

    count: int = await connection.fetchval(f"SELECT count(*) FROM {name}")
    if not count:
        return {"name": name, "rows": 0}
    before: int = await connection.fetchval(f"SELECT hypertable_size('{name}')")
    detail = await connection.fetchrow(
        f"""SELECT sum(table_bytes) AS heap, sum(index_bytes) AS indexes,
                   sum(toast_bytes) AS toast
            FROM hypertable_detailed_size('{name}')"""
    )
    for record in await connection.fetch(f"SELECT show_chunks('{name}') AS chunk"):
        await connection.execute(f"SELECT compress_chunk('{record['chunk']}')")
    await connection.execute(f"ANALYZE {name}")
    after: int = await connection.fetchval(f"SELECT hypertable_size('{name}')")

    return {
        "name": name,
        "rows": count,
        "span_seconds": round(
            (max(r[2] for r in rows) - min(r[2] for r in rows)).total_seconds(), 1
        ),
        "uncompressed_bytes": before,
        "uncompressed_heap": int(detail["heap"] or 0),
        "uncompressed_indexes": int(detail["indexes"] or 0),
        "uncompressed_toast": int(detail["toast"] or 0),
        "compressed_bytes": after,
        "bytes_per_row_uncompressed": round(before / count, 2),
        "bytes_per_row_compressed": round(after / count, 2),
        "ratio": round(before / after, 2) if after else None,
    }


async def run(seconds: float, url: str) -> int:
    symbols = default_symbols()
    settings = load_settings()
    print(f"collecting real top of book for {symbols} over {seconds:.0f}s", flush=True)
    rows = await collect(url, symbols, seconds)
    print(f"collected {len(rows)} rows", flush=True)
    if not rows:
        print("no rows collected, so nothing can be measured", file=sys.stderr)
        return 1

    connection = await asyncpg.connect(settings.database.dsn(reveal_password=True))
    try:
        result = await measure(connection, "sizing_ticks", rows)
        await connection.execute("DROP TABLE IF EXISTS sizing_ticks CASCADE")
    finally:
        await connection.close()

    print(json.dumps(result, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    return asyncio.run(run(args.seconds, args.url))


if __name__ == "__main__":
    raise SystemExit(main())
