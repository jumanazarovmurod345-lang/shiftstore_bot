"""Alembic env: async-движок, URL из DATABASE_URL, advisory lock от параллельного запуска."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import normalize_database_url
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
MIGRATION_LOCK_ID = 727_001  # произвольный ключ pg_advisory_lock


def _url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL не задан")
    return normalize_database_url(url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    # Даже если миграции случайно запустят из нескольких мест — выполнятся по очереди.
    connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_ID})
    try:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_ID})


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
