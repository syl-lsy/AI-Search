"""默认用户 seed（data-model.md §1.1 users 行 + §4 迁移表 0001 行）。

落位裁决（2026-11-07 评审解锁）：由迁移 0001 幂等 seed 承担——`make db-upgrade`
后 users 即非空，B2 起 threads/turns（user_id FK→users.id）任何写入不踩缺行；
启动钩子不重复承担（main.py lifespan docstring）。本模块供迁移 import 调用，
亦可用于人工补种。

参数化全形态（安全口径）：**ORM 单元工作**（Session.get → Session.add）——查询
与插入语句全部由 ORM 生成并绑定参数，本文件零 SQL 字符串、零 execute 调用
（与 main.py `_cleanup_orphan_turns` 的已接受 ORM 形态同构）。此前 sa.text
命名占位符与 Core 表达式两形态被 Mimosa 静态门禁三度误拦（占位符风格 ：name
与 .values() 链不在其参数化白名单），按「参数化最强形态 = ORM」落位，注入面
最小。幂等语义：以 id 为锚先查后插——已存在跳过（返回 False）、不存在插入
（返回 True），迁移事务内串行执行，等价幂等 upsert；name 冲突保留现值（契约
锚定的是 id）。

事务边界：Session 以 join_transaction_mode="create_savepoint" 接入迁移事务，
commit 只释放保存点——seed 行随迁移事务一起生效/回滚，不破坏迁移原子性。id
显式转 uuid.UUID：迁移在线模式走 asyncpg（migrations/env.py
run_async_migrations），严格类型不隐式转换；配置值非法 UUID 时迁移 fail-fast。
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import common_settings
from app.db.models import User


def seed_default_user(conn: sa.Connection) -> bool:
    """幂等 seed 默认用户：恰一行，id/name 取 DEFAULT_USER_ID/DEFAULT_USER_NAME。

    以 id 为锚先查后插：已存在跳过（返回 False），不存在则 ORM 参数化插入并
    提交（返回 True）。重复执行零副作用。
    """
    user_id = uuid.UUID(common_settings.default_user_id)
    with Session(bind=conn, join_transaction_mode="create_savepoint") as session:
        if session.get(User, user_id) is not None:
            return False
        session.add(User(id=user_id, name=common_settings.default_user_name))
        session.commit()
        return True
