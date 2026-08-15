"""Alembic environment.

The connection URL comes from the application's own settings rather than from
alembic.ini, so a migration can only ever run against the database the application is
configured for, and no password is committed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from tradingsys.config import load_settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config

target_metadata = None
"""Migrations are written by hand.

Autogenerate is deliberately unavailable: the schema uses TimescaleDB hypertables,
compression policies, and triggers that SQLAlchemy's metadata cannot express, and a
generated migration would quietly drop them.
"""


def _database_url() -> str:
    settings = load_settings()
    dsn = settings.database.dsn(reveal_password=True)
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it, for review before a production apply."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
