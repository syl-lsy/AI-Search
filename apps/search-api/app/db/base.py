"""ORM 声明基类：全部表模型的公共 DeclarativeBase。

Alembic autogenerate 以 `Base.metadata` 为 target_metadata（见 migrations/env.py）；
ORM 模型在 db/models/ 下逐表落地（B1 起，按 data-model.md 的 DDL 契约），
落地后在本包内统一 import 以确保进 metadata。
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """ai-search 全部 ORM 模型的声明基类。"""
