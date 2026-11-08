"""unit 层：默认用户 seed（app/db/seed.py；data-model.md §1.1 users 行 + §4 迁移表 0001 行）。

全 mock 零网络（mock Session）断言——
① 幂等分支：users 无该 id → ORM 参数化插入（add User(id/name=配置值) + commit）
  返回 True；已有该 id → 不插入返回 False（等价「已存在跳过」的幂等 upsert 语义）；
② Session 以 create_savepoint 接入迁移事务（commit 只释放保存点，INSERT 落在
  迁移事务内，不破坏迁移原子性）；
③ 配置值映射：id 经 uuid.UUID 显式转换（迁移在线模式走 asyncpg、严格类型），
  name 原样透传。

参数化红线（值不进 SQL 文本）由 ORM 语句构造保证——本路径零 SQL 字符串、零
execute 调用（与 main.py _cleanup_orphan_turns 的已接受 ORM 形态同构）；真库端
到端复核在 integration 层（test_migrations.py）。
"""
import uuid

from app.core.config import common_settings
from app.db.models import User


class _FakeSession:
    """捕获 Session 交互的最小假体（get/add/commit/生命周期）。"""

    def __init__(self, existing: User | None = None) -> None:
        self.existing = existing
        self.init_kwargs: dict[str, object] = {}
        self.gets: list[tuple[type, uuid.UUID]] = []
        self.added: list[User] = []
        self.committed = False
        self.closed = False

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.closed = True
        return False

    def get(self, model: type, pk: object) -> User | None:
        self.gets.append((model, pk))
        return self.existing

    def add(self, obj: User) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True


def _install_fake_session(monkeypatch, existing: User | None) -> list[_FakeSession]:
    """把 seed 模块的 Session 换成假体工厂；返回创建的会话列表（恰一个）。"""
    from app.db import seed as seed_module

    created: list[_FakeSession] = []

    def _factory(bind=None, join_transaction_mode=None):
        session = _FakeSession(existing=existing)
        session.init_kwargs = {
            "bind": bind,
            "join_transaction_mode": join_transaction_mode,
        }
        created.append(session)
        return session

    monkeypatch.setattr(seed_module, "Session", _factory)
    return created


def test_seed_inserts_default_user_when_absent(monkeypatch):
    """users 无该 id：ORM 插入 User(id/name=配置值) 并提交，返回 True。"""
    from app.db import seed as seed_module

    pinned_id = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(common_settings, "default_user_id", pinned_id)
    monkeypatch.setattr(common_settings, "default_user_name", "Pinned-Name")

    conn = object()
    created = _install_fake_session(monkeypatch, existing=None)

    assert seed_module.seed_default_user(conn) is True
    assert len(created) == 1
    session = created[0]
    assert session.init_kwargs == {
        "bind": conn,
        "join_transaction_mode": "create_savepoint",
    }
    assert session.gets == [(User, uuid.UUID(pinned_id))]
    assert len(session.added) == 1
    assert session.added[0].id == uuid.UUID(pinned_id)
    assert session.added[0].name == "Pinned-Name"
    assert session.committed is True


def test_seed_skips_when_default_user_exists(monkeypatch):
    """users 已有该 id：不插入、不提交（零副作用），返回 False。"""
    from app.db import seed as seed_module

    pinned_id = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(common_settings, "default_user_id", pinned_id)
    monkeypatch.setattr(common_settings, "default_user_name", "Pinned-Name")

    created = _install_fake_session(
        monkeypatch, existing=User(id=uuid.UUID(pinned_id), name="Keep-Name")
    )

    assert seed_module.seed_default_user(object()) is False
    assert len(created) == 1
    session = created[0]
    assert session.gets == [(User, uuid.UUID(pinned_id))]
    assert session.added == []
    assert session.committed is False
    assert session.closed is True  # with 块正常退出（Session 关闭）
