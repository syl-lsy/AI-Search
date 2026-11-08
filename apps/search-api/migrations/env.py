"""Alembic 迁移环境（异步骨架）。

- DB 连接串从 core/config 的 db_settings 组装（密钥只进 .env，不落盘、不写死）；
- 写入 alembic 主配置项前经 db/session.render_main_option_url 做 `%` → `%%` 转义
  （configparser 基本插值：密码含 @ / : % 时 URL 含 %XX 编码，未转义 set 即抛
  ValueError 并把可逆还原的连接串带进 traceback）；
- target_metadata = app.db.base.Base.metadata（ORM 模型在 db/models/ 落地后自动进
  metadata，autogenerate 即可用；骨架阶段 models/ 为空，metadata 为空是预期）；
- 版本文件模板见 migrations/script.py.mako；versions/ 里程碑分版（data-model.md §4）。
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import URL
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import db_settings
from app.db.base import Base
from app.db.session import render_main_option_url
import app.db.models  # noqa: F401 —— 显式 import 全部模型填充 Base.metadata
# （python 无自动 import：不导入 models/ 则 metadata 为空，autogenerate 恒出空迁移）

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> URL:
    """与 db/session.py 同口径组装连接串（改池化/驱动时两处一起动）。

    URL.create 由 SQLAlchemy 负责转义特殊字符（f-string 裸拼在密码含 @ / 时碎裂）；
    SecretStr.get_secret_value() 是密钥明文的唯一出口——f-string 直接插值 SecretStr
    会把打码掩码 ********** 拼进连接串（真实连库时以 InvalidPasswordError 暴露）。
    """
    return URL.create(
        "postgresql+asyncpg",
        username=db_settings.user,
        password=db_settings.password.get_secret_value(),
        host=db_settings.host,
        port=db_settings.port,
        database=db_settings.name,
    )


config.set_main_option(
    "sqlalchemy.url", render_main_option_url(_database_url())
)


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
