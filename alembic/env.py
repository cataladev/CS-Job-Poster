from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

load_dotenv()
database_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")


def _sqlalchemy_database_url(url: str) -> str:
    # The runtime stack uses psycopg v3 directly, while Alembic/SQLAlchemy need
    # an explicit driver name to avoid falling back to psycopg2.
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


sqlalchemy_database_url = _sqlalchemy_database_url(database_url)
config.set_main_option("sqlalchemy.url", sqlalchemy_database_url)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=sqlalchemy_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(sqlalchemy_database_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
