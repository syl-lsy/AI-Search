"""Alembic 迁移环境（异步骨架）。

- DB 连接串从 core/config 的 db_settings 组装（密钥只进 .env，不落盘、不写死）；
- target_metadata = app.db.base.Base.metadata（ORM 模型在 db/models/ 落地后自动进
  metadata，autogenerate 即可用；骨架阶段 models/ 为空，metadata 为空是预期）；
- 版本文件模板见 migrations/script.py.mako；versions/ 里程碑分版（data-model.md §4）。
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import db_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """与 db/session.py 同口径组装连接串（改池化/驱动时两处一起动）。"""
    return (
        f"postgresql+asyncpg://{db_settings.user}:{db_settings.password}"
        f"@{db_settings.host}:{db_settings.port}/{db_settings.name}"
    )


config.set_main_option("sqlalchemy.url", _database_url())


def run_migrations_offline() -> None:
    """离线模式（--sql）：只生成 SQL 不连库。"""
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
    """在线模式（异步引擎）：NullPool 一次性连接，跑完即释放。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
