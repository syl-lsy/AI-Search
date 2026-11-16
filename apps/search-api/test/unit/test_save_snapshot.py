"""unit 层：save_snapshot 快照工具（M2 批次 1 工具面——data-model.md §1.6、ARCH §6.3/§10 Q2、
REQ-015；TDD 红绿，unit 全 mock 零网络：MinIO 客户端与会话工厂均注入假体）。

钉住的语义（冻结契约，逐条对应测试）：
- **对象键契约**：`snapshots/{user_id}/{thread_id}/{turn_id}/{url_hash}.md`（§1.6
  object_key 行）+ url_hash = sha256(url) hex64（「唯一键用，URL 可能超长」）；
- **url_hash 幂等**：uq_snapshots_turn_url (turn_id, url_hash)——同轮同 URL 重复执行
  **零重复行、零重复对象**（首次后不再 put 对象），冲突（并发竞态唯一键 IntegrityError）
  走幂等复用（回查既有行返回）；
- **>2MB 口径（ARCH §10 Q2）**：「大文件 >2MB 截断入库、对象全量存 MinIO」——冻结 DDL
  （§1.6）快照行仅元数据无正文列，「对象全量存 MinIO」为可执行半边：**对象恒全量写入**，
  行 size_bytes 记全量字节数；>2MB 在工具返回面落 `oversize=True` 留痕（实现侧口径，
  供批次 2 research 节点/巡检裁决——见模块尾注）；
- **仅深度模式**：fast 搜索源白名单物理不含快照工具（SEARCH_SOURCE_NAMES ∩ 工具名 = ∅），
  fast 图面模块零导入（AST 哨兵）；
- **失败语义**：MinIO/DB 基建失败 → SnapshotError（带 original），零行登记（不产孤儿行）；
  行登记失败回滚（对象已写=同键覆盖不产生重复对象，幂等复用路径兜底）。

>2MB 实现口径注（非契约变更，供文档巡检引用）：Q2 为 §10「未决问题」建议默认值，
冻结契约（data-model.md §1.6）无 2MB 条款且快照行无正文列——本工具按「对象全量 +
行元数据全量 + oversize 留痕」落地两个半句的可执行交集；「截断入库」在当前 DDL 下
无行级承载（摘录面 2000 字符为 api.md §5.1 读取期口径，与本工具无关）。
"""

import ast
import asyncio
import hashlib
import io
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.agent.tools.save_snapshot import (
    SNAPSHOT_OVERSIZE_BYTES,
    SnapshotError,
    object_key_for,
    save_snapshot,
    url_hash_for,
)
from app.db.models.snapshot import Snapshot


# ---- 假体：MinIO 客户端 / AsyncSession ----


class _FakeMinio:
    """minio-py 假体：记录 put_object 物理调用（桶/键/字节/长度/类型）与建桶行为。"""

    def __init__(self, *, fail: bool = False, existing_buckets: set[str] | None = None) -> None:
        self.puts: list[tuple[str, str, bytes, int, str]] = []
        self.bucket_checks: list[str] = []
        self.buckets = set(existing_buckets or set())
        self.fail = fail

    def bucket_exists(self, name: str) -> bool:
        self.bucket_checks.append(name)
        return name in self.buckets

    def make_bucket(self, name: str) -> None:
        self.buckets.add(name)

    def put_object(self, bucket: str, key: str, data: io.IOBase, length: int, content_type: str) -> None:
        if self.fail:
            raise RuntimeError("minio 不可用")
        self.puts.append((bucket, key, bytes(data.read()), length, content_type))


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """AsyncSession 假体：execute 按脚本回放查询结果；记录 add/commit/rollback。"""

    def __init__(self, query_results: list[Any], *, fail_commit: bool = False) -> None:
        self.query_results = list(query_results)
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.fail_commit = fail_commit

    async def execute(self, stmt: Any) -> _FakeResult:
        rows = self.query_results.pop(0) if self.query_results else []
        return _FakeResult(rows)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_commit:
            raise IntegrityError("uq_snapshots_turn_url", None, Exception("duplicate key"))

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self.close()
        return False


def _ids() -> tuple[str, str, str]:
    return (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    )


def _save(session: _FakeSession, minio: _FakeMinio, *, url: str = "https://a.example/p", content: str = "# 正文") -> dict:
    user_id, thread_id, turn_id = _ids()
    return asyncio.run(
        save_snapshot(
            url=url,
            content=content,
            user_id=user_id,
            thread_id=thread_id,
            turn_id=turn_id,
            session_factory=lambda: session,
            minio_client_factory=lambda: minio,
        )
    )


# ---- 对象键与 url_hash 契约（golden）----


def test_url_hash_is_sha256_hex64() -> None:
    assert url_hash_for("https://a.example/p") == hashlib.sha256(b"https://a.example/p").hexdigest()
    assert len(url_hash_for("https://a.example/p")) == 64


def test_object_key_golden_matches_contract() -> None:
    """对象键逐字节 = data-model §1.6：snapshots/{user}/{thread}/{turn}/{url_hash}.md。"""
    user_id, thread_id, turn_id = _ids()
    url_hash = url_hash_for("https://a.example/p")
    assert object_key_for(user_id, thread_id, turn_id, url_hash) == (
        f"snapshots/{user_id}/{thread_id}/{turn_id}/{url_hash}.md"
    )


# ---- 首次执行：写对象 + 登记行 ----


def test_first_execution_puts_full_object_and_registers_row() -> None:
    session, minio = _FakeSession(query_results=[[]]), _FakeMinio()
    out = _save(session, minio, content="# 标题\n正文")

    assert out["reused"] is False
    assert out["oversize"] is False
    assert out["url_hash"] == url_hash_for("https://a.example/p")
    assert out["object_key"].startswith("snapshots/11111111-1111-4111-8111-111111111111/")
    assert len(minio.puts) == 1
    bucket, key, data, length, content_type = minio.puts[0]
    assert key == out["object_key"]
    assert data == "# 标题\n正文".encode("utf-8")  # 对象全量字节
    assert length == len(data)
    assert content_type == "text/markdown"

    assert len(session.added) == 1  # 零重复行：恰好一行
    row = session.added[0]
    assert isinstance(row, Snapshot)
    assert row.url == "https://a.example/p"
    assert row.url_hash == out["url_hash"]
    assert row.object_key == out["object_key"]
    assert row.size_bytes == len(data)  # 行级 size = 全量字节数
    assert row.content_type == "text/markdown"
    assert row.user_id == uuid.UUID(_ids()[0])
    assert row.thread_id == uuid.UUID(_ids()[1])
    assert row.turn_id == uuid.UUID(_ids()[2])
    assert isinstance(row.fetched_at, datetime) and row.fetched_at.tzinfo is not None
    assert session.commits == 1


def test_missing_bucket_is_created_then_object_written() -> None:
    """桶不存在 → 建桶后写入（compose 无建桶初始化容器——工具侧自兜）。"""
    from app.core.config import minio_settings

    session, minio = _FakeSession(query_results=[[]]), _FakeMinio()
    _save(session, minio)
    assert minio.bucket_checks == [minio_settings.bucket_snapshot]
    assert minio_settings.bucket_snapshot in minio.buckets  # 已建桶
    assert len(minio.puts) == 1


# ---- 幂等：重复执行 / 竞态冲突 ----


def test_idempotent_double_execution_zero_duplicate_row_and_object() -> None:
    """重复执行（同轮同 URL）→ 第二次幂等复用：不再 put 对象、不再插行（闭环条件④）。"""
    session1, minio = _FakeSession(query_results=[[]]), _FakeMinio()
    first = _save(session1, minio)

    # 第二次执行：查询命中既有行（行形态假体回放）；put 计数不变、insert 计数不变
    existing = SimpleNamespace(
        id=first["snapshot_id"],
        url=first["url"],
        url_hash=first["url_hash"],
        object_key=first["object_key"],
        size_bytes=first["size_bytes"],
    )
    session2 = _FakeSession(query_results=[[existing]])
    second = _save(session2, minio, content="# 标题\n正文")

    assert second["reused"] is True
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["object_key"] == first["object_key"]
    assert len(minio.puts) == 1  # 零重复对象
    assert len(session2.added) == 0  # 零重复行
    assert session2.commits == 0


def test_conflict_race_reuses_existing_row_idempotently() -> None:
    """并发竞态：commit 撞唯一键 IntegrityError → 回滚 + 回查既有行幂等复用（不抛）。"""
    existing_id = uuid.uuid4()
    existing = SimpleNamespace(
        id=existing_id,
        url="https://a.example/p",
        url_hash=url_hash_for("https://a.example/p"),
        object_key="snapshots/x/y/z/abc.md",
        size_bytes=10,
    )
    session = _FakeSession(
        query_results=[[], [existing]],  # 首查无 → 竞态后回查命中
        fail_commit=True,
    )
    minio = _FakeMinio()
    out = _save(session, minio)

    assert out["reused"] is True
    assert out["snapshot_id"] == existing_id
    assert session.rollbacks == 1
    assert len(session.added) == 1  # 曾尝试插入（被唯一键拒）——终态零重复行


# ---- >2MB 口径（Q2：对象全量存 MinIO；实现侧 oversize 留痕）----


def test_oversize_content_object_stored_full_and_flag_set() -> None:
    """>2MB：对象字节恒全量（「对象全量存 MinIO」）、行 size_bytes 全量、返回 oversize=True。"""
    big = "x" * (SNAPSHOT_OVERSIZE_BYTES + 1)  # ASCII 1 byte/char
    assert SNAPSHOT_OVERSIZE_BYTES == 2 * 1024 * 1024
    session, minio = _FakeSession(query_results=[[]]), _FakeMinio()
    out = _save(session, minio, content=big)

    assert out["oversize"] is True
    _, _, data, length, _ = minio.puts[0]
    assert len(data) == SNAPSHOT_OVERSIZE_BYTES + 1  # 全量，无截断
    assert length == len(data)
    assert session.added[0].size_bytes == len(data)


def test_boundary_size_exactly_2mb_is_not_oversize() -> None:
    """边界：恰 2MB 不越界（>2MB 才算大文件）。"""
    exact = "x" * SNAPSHOT_OVERSIZE_BYTES
    session, minio = _FakeSession(query_results=[[]]), _FakeMinio()
    out = _save(session, minio, content=exact)
    assert out["oversize"] is False


# ---- 失败语义 ----


def test_minio_failure_raises_snapshot_error_without_row() -> None:
    """MinIO 失败 → SnapshotError（original 链入），零行登记（不产孤儿行）。"""
    session, minio = _FakeSession(query_results=[[]]), _FakeMinio(fail=True)
    with pytest.raises(SnapshotError):
        _save(session, minio)
    assert session.added == []
    assert session.commits == 0


def test_malformed_uuid_is_programming_error() -> None:
    """user_id 非法 UUID → ValueError（编程错误，先于任何 IO）。"""
    minio = _FakeMinio()
    session = _FakeSession(query_results=[[]])
    with pytest.raises(ValueError):
        asyncio.run(
            save_snapshot(
                url="https://a.example/",
                content="正文",
                user_id="not-a-uuid",
                thread_id=_ids()[1],
                turn_id=_ids()[2],
                session_factory=lambda: session,
                minio_client_factory=lambda: minio,
            )
        )
    assert minio.puts == []


def test_blank_url_or_content_rejected() -> None:
    """url/content 空白 → ValueError（编程错误，零 IO）。"""
    session, minio = _FakeSession(query_results=[[]]), _FakeMinio()
    for kwargs in ({"url": "  ", "content": "正文"}, {"url": "https://a.example/", "content": ""}):
        with pytest.raises(ValueError):
            asyncio.run(
                save_snapshot(
                    user_id=_ids()[0],
                    thread_id=_ids()[1],
                    turn_id=_ids()[2],
                    session_factory=lambda: session,
                    minio_client_factory=lambda: minio,
                    **kwargs,
                )
            )
    assert minio.puts == []
    assert session.added == []


# ---- fast 工具面物理不含 save_snapshot（AST 哨兵，代码级保证）----


def test_search_whitelist_physically_excludes_snapshot_tool() -> None:
    """fast 搜索源白名单不含快照/读取链面（共识约束：快速模式不写任何原文快照，REQ-015）。"""
    from app.agent.tools.registry import SEARCH_SOURCE_NAMES

    assert "save_snapshot" not in SEARCH_SOURCE_NAMES
    assert set(SEARCH_SOURCE_NAMES) == {"tavily", "bocha", "searxng", "anysearch"}


#: 哨兵禁用面：fast 图面不得导入的快照/deep 工具模块（模块路径精确匹配）
_FORBIDDEN_FAST_MODULES: tuple[str, ...] = (
    "app.agent.tools.save_snapshot",
    "app.agent.tools.deep_tools",
)


def _forbidden_fast_module_imports(source: str) -> list[str]:
    """哨兵检查函数：解析源码，返回命中的禁用模块路径（导入形态双覆盖）。

    - `from app.agent.tools.save_snapshot import save_snapshot` → ast.ImportFrom
      （node.module 匹配）；
    - `import app.agent.tools.save_snapshot` / `import a, b` → ast.Import
      （alias.name 精确匹配；直导形态不产生 ImportFrom 节点）。
    alias.name 精确等值（`app.agent.tools.save_snapshot.helper` 非本哨兵面）。
    """
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module in _FORBIDDEN_FAST_MODULES:
            hits.append(node.module)
        elif isinstance(node, ast.Import):
            hits.extend(
                alias.name
                for alias in node.names
                if alias.name in _FORBIDDEN_FAST_MODULES
            )
    return hits


def test_sentinel_detects_direct_import_form() -> None:
    """哨兵负例（直导形态）：`import app.agent.tools.save_snapshot` 不产生 ImportFrom
    节点——检查函数必须同时覆盖 ast.Import（alias.name 精确匹配），否则该形态绕过哨兵。"""
    direct = (
        "import app.agent.tools.save_snapshot\n"
        "import app.agent.tools.deep_tools as dt\n"
    )
    assert _forbidden_fast_module_imports(direct) == [
        "app.agent.tools.save_snapshot",
        "app.agent.tools.deep_tools",
    ]
    # from-import 形态既有分支保持（零回退）
    assert _forbidden_fast_module_imports(
        "from app.agent.tools.save_snapshot import save_snapshot\n"
    ) == ["app.agent.tools.save_snapshot"]
    # 非目标导入零误报（模块路径精确匹配，前缀相近不命中）
    assert _forbidden_fast_module_imports(
        "import app.agent.tools.registry\nfrom app.agent.tools import registry\n"
    ) == []


def test_fast_graph_modules_do_not_import_save_snapshot() -> None:
    """AST 哨兵：fast 图壳与全部 fast 节点模块零 import save_snapshot/deep_tools
    （ImportFrom 与直导 ast.Import 两形态同面）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "agent"
    fast_files = sorted((root / "nodes").glob("*.py")) + sorted(
        p for p in (root / "graphs").glob("*.py") if p.name != "deep.py"
    )
    assert fast_files, "fast 图面文件缺失（哨兵扫描面为空）"
    offenders: list[str] = []
    for path in fast_files:
        for module in _forbidden_fast_module_imports(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}: {module}")
    assert offenders == [], f"fast 面出现快照工具导入: {offenders}"

