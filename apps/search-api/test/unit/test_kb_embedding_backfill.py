"""unit 层：存量向量真实化回填脚本（scripts/kb_embedding_backfill.py，plan-m3-b19
§2.1 A5 / §3.4② 必答项②建议形态 + P3#5 同款隔离纪律）。

契约锚点：
- plan §3.4②：独立回填脚本（语义与 ES backfill 不同——外部 API 计费、失败面不同）；
  行级判据=零向量（本会话 SQL 实测 25979/25979 全零向量占位）；幂等=非零跳过；
  **回填输入=新口径（heading_path 前置轨迹，同一 build_embedding_input）**——混用
  两口径向量会撕裂向量空间；连接异常 per-文档隔离记账续跑（P3#5 同款教训）；
- plan §3.3：回填完成后与 es_enabled 同窗翻 `kb.embedding_enabled=true`（用户收官
  动作）；回填完成前向量腿短路——零向量行不得进 cosine 查询（通道级判定，归
  检索面 B1，非本脚本职责）；
- audit 模式：零向量统计对账出口（plan §10 Risk 5——回填进度对账）。

TDD 红绿：unit 全 mock 零网络零 DB（importlib 加载脚本——test_kb_hash_realign
先例；session/embed 函数全假体；模型映射快照缝 monkeypatch）。
"""

import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from app.rag.embedding import EmbeddingError, build_embedding_input

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[4] / "scripts" / "kb_embedding_backfill.py"
)

_USER_ID = "00000000-0000-4000-8000-000000000001"
_DOC_ID = "7d2f0a1e-0000-4000-8000-0000000000b1"
_DOC_ID_B = "7d2f0a1e-0000-4000-8000-0000000000b2"
_DIM = 1024


@pytest.fixture
def script():
    """从仓库根加载 scripts/kb_embedding_backfill.py（__main__ 守卫，导入不执行）。"""
    spec = importlib.util.spec_from_file_location(
        "kb_embedding_backfill_unit", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []
        self.rowcount = 1

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.commits = 0

    async def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        return _FakeResult()

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession()
        self.sessions.append(session)
        return session


def _chunk_row(index: int = 0, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.UUID(f"11111111-1111-4111-8111-{index:012d}"),
        "content": f"切片正文{index}",
        "meta": {"heading_path": ["示例文档", "第一章"]},
        "embedding": [0.0] * _DIM,  # 缺省=零向量占位（存量基线形态）
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# is_zero_vector：行级判据（零向量/缺失/空 = 待回填）
# ---------------------------------------------------------------------------


def test_zero_vector_detection_matrix(script):
    """None/空/全零 → 待回填；非零/部分零 → 已回填（幂等判据面）。"""
    assert script.is_zero_vector(None) is True
    assert script.is_zero_vector([]) is True
    assert script.is_zero_vector([0.0] * _DIM) is True
    assert script.is_zero_vector([0.1] * _DIM) is False
    assert script.is_zero_vector([0.0] * (_DIM - 1) + [0.5]) is False


# ---------------------------------------------------------------------------
# backfill_doc：行级零向量判定 → 新口径输入 → 行级 UPDATE
# ---------------------------------------------------------------------------


async def test_backfill_embeds_only_zero_rows_with_new_caliber_input(
    script, monkeypatch
):
    """零向量行 → 恰用 build_embedding_input 新口径输入（标题轨迹前置）embed；
    行级 UPDATE 参数断言（embedding 值 + 行 id 对位）；非零行零触碰。"""
    zero_row = _chunk_row(0)
    nonzero_row = _chunk_row(1, embedding=[0.3] * _DIM)
    rows = [zero_row, nonzero_row]

    async def fake_load_rows(session, doc_id):
        return rows

    monkeypatch.setattr(script, "_load_doc_chunk_rows", fake_load_rows)
    captured_inputs: list[list[str]] = []

    async def fake_embed(inputs, *, mapping=None):
        captured_inputs.append(list(inputs))
        return [[0.7] * _DIM for _ in inputs]

    factory = _FakeSessionFactory()
    counts = await script.backfill_doc(
        {"id": uuid.UUID(_DOC_ID)}, session_factory=factory, embed_fn=fake_embed
    )
    assert counts == {"embedded": 1, "skipped": 1, "total": 2}
    # 新口径输入（plan §3.2/§3.4②：标题轨迹前置，同一 build_embedding_input）
    assert captured_inputs == [
        [build_embedding_input("切片正文0", ["示例文档", "第一章"])]
    ]
    assert captured_inputs[0][0] == "标题轨迹：示例文档 > 第一章\n切片正文0"
    # 行级 UPDATE：恰 1 条、embedding=返回向量、行 id 对位
    update_sessions = [s for s in factory.sessions if s.executed]
    assert len(update_sessions) == 1
    stmt = update_sessions[0].executed[0]
    params = stmt.compile().params
    assert any(v == [0.7] * _DIM for v in params.values())
    assert any(v == zero_row["id"] for v in params.values())


async def test_backfill_idempotent_nonzero_rows_no_embed_call(script, monkeypatch):
    """全非零 → skip（零 embed 调用、零 UPDATE——幂等=非零跳过）。"""

    async def fake_load_rows(session, doc_id):
        return [_chunk_row(0, embedding=[0.2] * _DIM), _chunk_row(1, embedding=[0.4] * _DIM)]

    monkeypatch.setattr(script, "_load_doc_chunk_rows", fake_load_rows)

    async def fail_embed(inputs, *, mapping=None):
        raise AssertionError("全非零行不应有 embed 调用")

    factory = _FakeSessionFactory()
    counts = await script.backfill_doc(
        {"id": uuid.UUID(_DOC_ID)}, session_factory=factory, embed_fn=fail_embed
    )
    assert counts == {"embedded": 0, "skipped": 2, "total": 2}
    assert all(not s.executed for s in factory.sessions)


async def test_backfill_count_mismatch_raises(script, monkeypatch):
    """embed 返回数量与待回填行不符 → EmbeddingError（零静默部分写）。"""

    async def fake_load_rows(session, doc_id):
        return [_chunk_row(0), _chunk_row(1)]

    monkeypatch.setattr(script, "_load_doc_chunk_rows", fake_load_rows)

    async def short_embed(inputs, *, mapping=None):
        return [[0.7] * _DIM]  # 缺一条

    with pytest.raises(EmbeddingError):
        await script.backfill_doc(
            {"id": uuid.UUID(_DOC_ID)},
            session_factory=_FakeSessionFactory(),
            embed_fn=short_embed,
        )


async def test_backfill_missing_meta_heading_path_degrades(script, monkeypatch):
    """meta 缺 heading_path（异常态行）→ 输入仅 content（build_embedding_input
    零前缀分支——不炸不伪造轨迹）。"""

    async def fake_load_rows(session, doc_id):
        return [_chunk_row(0, meta=None)]

    monkeypatch.setattr(script, "_load_doc_chunk_rows", fake_load_rows)
    captured_inputs: list[list[str]] = []

    async def fake_embed(inputs, *, mapping=None):
        captured_inputs.append(list(inputs))
        return [[0.7] * _DIM for _ in inputs]

    await script.backfill_doc(
        {"id": uuid.UUID(_DOC_ID)},
        session_factory=_FakeSessionFactory(),
        embed_fn=fake_embed,
    )
    assert captured_inputs == [["切片正文0"]]


# ---------------------------------------------------------------------------
# cmd_backfill：连接/网关异常 per-文档隔离（P3#5 同款纪律——不中断整批）
# ---------------------------------------------------------------------------


async def test_cmd_backfill_isolates_per_doc(script, monkeypatch):
    """doc1 EmbeddingError（网关耗尽）→ 记账续跑 doc2 照常回填；退出码 1。"""
    doc_a = {"id": uuid.UUID(_DOC_ID), "title": "文档甲"}
    doc_b = {"id": uuid.UUID(_DOC_ID_B), "title": "文档乙"}
    processed: list[str] = []

    async def fake_load_docs():
        return [doc_a, doc_b]

    async def fake_backfill_doc(doc, *, session_factory, embed_fn=None):
        processed.append(str(doc["id"]))
        if doc["id"] == uuid.UUID(_DOC_ID):
            raise EmbeddingError("embedding 调用失败: HTTP 503 attempts=2")
        return {"embedded": 3, "skipped": 0, "total": 3}

    monkeypatch.setattr(script, "_load_ready_docs", fake_load_docs)
    monkeypatch.setattr(script, "backfill_doc", fake_backfill_doc)
    code = await script.cmd_backfill()
    assert processed == [_DOC_ID, _DOC_ID_B]  # 续跑不中断
    assert code == 1  # failed=1 → 非零退出（如实呈现）


async def test_cmd_backfill_dbapi_isolated_per_doc(script, monkeypatch):
    """doc1 DB 连接类异常（DBAPIError）→ 同款隔离续跑（P3#5 教训面）。"""
    doc_a = {"id": uuid.UUID(_DOC_ID), "title": "文档甲"}
    doc_b = {"id": uuid.UUID(_DOC_ID_B), "title": "文档乙"}
    processed: list[str] = []

    async def fake_load_docs():
        return [doc_a, doc_b]

    async def fake_backfill_doc(doc, *, session_factory, embed_fn=None):
        processed.append(str(doc["id"]))
        if doc["id"] == uuid.UUID(_DOC_ID):
            raise DBAPIError("stmt", {}, OSError("conn refused"))
        return {"embedded": 0, "skipped": 2, "total": 2}

    monkeypatch.setattr(script, "_load_ready_docs", fake_load_docs)
    monkeypatch.setattr(script, "backfill_doc", fake_backfill_doc)
    code = await script.cmd_backfill()
    assert processed == [_DOC_ID, _DOC_ID_B]
    assert code == 1


# ---------------------------------------------------------------------------
# cmd_audit：零向量统计对账（plan §10 Risk 5——回填进度对账出口）
# ---------------------------------------------------------------------------


async def test_audit_reports_zero_vector_stats(script, monkeypatch):
    """audit：总数 + 零向量数 + 逐文档分布打印，只读零写。"""
    doc_a = uuid.UUID(_DOC_ID)
    doc_b = uuid.UUID(_DOC_ID_B)
    captured: list[Any] = []

    class _RowsResult:
        def all(self):
            return [(doc_a,), (doc_a,), (doc_b,)]

    class _FakeAuditSession:
        async def scalar(self, stmt):
            captured.append(stmt)
            return 4

        async def execute(self, stmt):
            captured.append(stmt)
            return _RowsResult()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Factory:
        def __call__(self):
            return _FakeAuditSession()

    monkeypatch.setattr(script, "get_session_factory", lambda: _Factory())
    code = await script.cmd_audit()
    assert code == 0
    assert len(captured) == 2  # 总数 scalar + 零向量 select，零写语句
    sql_texts = [str(stmt) for stmt in captured]
    assert any("count" in t.lower() for t in sql_texts)
    assert any("kb_chunks" in t for t in sql_texts)
    # 零向量判定走绑定参数（零向量字面为绑定值，非拼接 SQL）
    zero_stmt = next(
        stmt for stmt in captured if "kb_chunks" in str(stmt) and "count" not in str(stmt).lower()
    )
    assert any(
        v == [0.0] * script.kb_settings.embedding_dim
        for v in zero_stmt.compile().params.values()
    )
