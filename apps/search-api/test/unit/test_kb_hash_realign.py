"""unit 层：存量 content_hash 基线重算脚本（scripts/kb_es_backfill.py --mode
hash-realign，plan-m3-b19 §2.1 A4 / §3.4 必答项①建议形态 + P3#4 连接异常隔离）。

契约锚点：
- plan §3.4①：1022 ready 文档均有 ir.json（B16 起落桶）——重算走 `chunk_forest`
  同一代码路径（零算法复制），UPDATE kb_documents.content_hash 到方案 A 新口径
  基线；零重导零重嵌（纯 MinIO GET + hash）；不迁移的后果=飞书定时 diff 判定序④
  对存量恒判变更 → 首轮全量误重导 + 重嵌；
- plan §7 T1 `test_kb_hash_realign.py`：ready 文档重算 → UPDATE 调用参数断言；
  非 ready 跳过；ir 缺失记账续跑；
- P3#4（b4 §三 #5）：回填脚本连接类异常单文档隔离（不中断整批）——hash-realign
  与既有 backfill/audit 逐文档循环同款纪律。

TDD 红绿：unit 全 mock 零网络（脚本经 importlib 从仓库根加载——
test_canonical_serialization.py 先例；session/MinIO/IR 全假体）。
"""

import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from app.rag.ir_schema import IrNode, IrNodeType
from app.rag.chunker import ChunkingConfig, chunk_forest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[4] / "scripts" / "kb_es_backfill.py"
)

_USER_ID = "00000000-0000-4000-8000-000000000001"
_DOC_ID = "7d2f0a1e-0000-4000-8000-0000000000b1"


@pytest.fixture
def script():
    """从仓库根加载 scripts/kb_es_backfill.py（__main__ 守卫，导入不执行）。"""
    spec = importlib.util.spec_from_file_location("kb_es_backfill_unit", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _forest_root() -> IrNode:
    """合成 Forest 夹具：带 heading 的单文档（新口径 hash ≠ 旧口径纯正文 hash）。"""
    para = lambda nid, text: IrNode(  # noqa: E731
        id=f"loc:blk:{nid}", type=IrNodeType.PARAGRAPH, text=text, meta={"raw_type": 2}
    )
    heading = IrNode(
        id="loc:blk:h1", type=IrNodeType.HEADING, level=1, title="第一章",
        meta={"raw_type": 1},
    )
    doc = IrNode(
        id=f"loc:doc:{_DOC_ID}",
        type=IrNodeType.DOCUMENT,
        title="示例文档",
        catalog_path=[],
        children=[heading, para("p1", "正文一。"), para("p2", "正文二。")],
        meta={"raw_type": "page"},
    )
    return IrNode(
        id="virtual:root",
        type=IrNodeType.DOCUMENT,
        meta={"raw_type": "virtual_root"},
        children=[doc],
    )


def _ready_doc(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.UUID(_DOC_ID),
        "user_id": uuid.UUID(_USER_ID),
        "chunk_count": 2,
        "catalog_path": None,
        "title": "示例文档",
        "status": "ready",
        "content_hash": "0" * 64,  # 存量旧口径占位（必与重算值异——新口径含结构清单）
    }
    row.update(overrides)
    return row


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, rowcount: int = 1) -> None:
        self.executed: list[Any] = []
        self.commits = 0
        self._rowcount = rowcount

    async def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        return _FakeResult(self._rowcount)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


# ---------------------------------------------------------------------------
# realign_doc：单文档重算（chunk_forest 同一代码路径 → UPDATE 参数断言）
# ---------------------------------------------------------------------------


async def test_realign_ready_doc_updates_content_hash(script, monkeypatch):
    """ready 文档重算：UPDATE 调用参数断言——content_hash=chunk_forest 新口径值，
    WHERE 含 id + status='ready' 乐观卫；chunk 级面零触碰（零重导零重嵌）。"""
    root = _forest_root()

    async def fake_get_ir_json(user_id, doc_id, *, client=None):
        assert user_id == _USER_ID and doc_id == _DOC_ID
        return root

    monkeypatch.setattr(script, "get_ir_json", fake_get_ir_json)
    session = _FakeSession()
    doc = _ready_doc()
    action, new_hash = await script.realign_doc(
        doc, session_factory=_FakeSessionFactory(session), minio_client=object()
    )
    expected = chunk_forest(root, config=ChunkingConfig())[0].document_content_hash
    assert action == "ok"
    assert new_hash == expected
    assert new_hash != "0" * 64  # 带 heading 文档：方案 A ≠ 旧口径占位
    assert len(session.executed) == 1
    params = session.executed[0].compile().params
    assert any(v == expected for v in params.values())
    assert any(v == uuid.UUID(_DOC_ID) for v in params.values())
    assert "ready" in [str(v) for v in params.values()]


async def test_realign_skip_when_hash_already_consistent(script, monkeypatch):
    """幂等：行值已等于新口径重算值 → skip 零写（二次执行近零成本）。"""
    root = _forest_root()
    expected = chunk_forest(root, config=ChunkingConfig())[0].document_content_hash

    async def fake_get_ir_json(user_id, doc_id, *, client=None):
        return root

    monkeypatch.setattr(script, "get_ir_json", fake_get_ir_json)
    session = _FakeSession()
    action, new_hash = await script.realign_doc(
        _ready_doc(content_hash=expected),
        session_factory=_FakeSessionFactory(session),
        minio_client=object(),
    )
    assert action == "skip"
    assert new_hash == expected
    assert session.executed == []


async def test_realign_rowcount_zero_is_gone(script, monkeypatch):
    """乐观卫 rowcount 0（行被并发改写/删除，不在 ready 态）→ gone 不覆盖他人。"""

    async def fake_get_ir_json(user_id, doc_id, *, client=None):
        return _forest_root()

    monkeypatch.setattr(script, "get_ir_json", fake_get_ir_json)
    session = _FakeSession(rowcount=0)
    action, _hash = await script.realign_doc(
        _ready_doc(), session_factory=_FakeSessionFactory(session), minio_client=object()
    )
    assert action == "gone"
    assert len(session.executed) == 1


async def test_ir_missing_accounted_and_continues(script, monkeypatch):
    """ir.json 不可达 → ir_missing 记账（返回空 hash、零写），循环续跑。"""

    async def fake_get_ir_json(user_id, doc_id, *, client=None):
        raise RuntimeError("minio unreachable")

    monkeypatch.setattr(script, "get_ir_json", fake_get_ir_json)
    session = _FakeSession()
    action, new_hash = await script.realign_doc(
        _ready_doc(), session_factory=_FakeSessionFactory(session), minio_client=object()
    )
    assert action == "ir_missing"
    assert new_hash == ""
    assert session.executed == []


# ---------------------------------------------------------------------------
# _load_ready_docs：非 ready 跳过（查询级 WHERE status='ready'）
# ---------------------------------------------------------------------------


async def test_load_ready_docs_filters_status_ready(script, monkeypatch):
    """非 ready 跳过 = 查询级过滤：SELECT 语句 WHERE status='ready' 断言。"""
    captured: list[Any] = []

    class _Result:
        def all(self):
            return []

    class _Session:
        async def execute(self, stmt):
            captured.append(stmt)
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Factory:
        def __call__(self):
            return _Session()

    monkeypatch.setattr(script, "get_session_factory", _Factory)
    rows = await script._load_ready_docs()
    assert rows == []
    assert len(captured) == 1
    sql = str(captured[0])
    assert "status" in sql and "content_hash" in sql  # 过滤列 + 幂等判定投影
    # WHERE status = :status_1 绑定参数形态（非拼接——编译 params 携带 'ready'）
    assert "ready" in captured[0].compile().params.values()


# ---------------------------------------------------------------------------
# cmd_hash_realign / cmd_backfill：整批循环（连接类异常单文档隔离——P3#4）
# ---------------------------------------------------------------------------


def _patch_load_docs(script, monkeypatch, docs):
    async def fake_load():
        return docs

    monkeypatch.setattr(script, "_load_ready_docs", fake_load)


async def test_cmd_hash_realign_reports_and_isolates(script, monkeypatch):
    """cmd hash-realign：ir 缺失记账续跑 + 连接类异常（DBAPIError）单文档隔离
    不中断整批；返回码=有失败即 1。"""
    doc_a = _ready_doc(id=uuid.UUID(_DOC_ID))
    doc_b = _ready_doc(id=uuid.UUID("7d2f0a1e-0000-4000-8000-0000000000b2"))
    calls: list[str] = []

    async def fake_realign_doc(doc, *, session_factory, minio_client):
        calls.append(str(doc["id"]))
        if doc["id"] == uuid.UUID(_DOC_ID):
            raise DBAPIError("stmt", {}, OSError("conn refused"))
        return ("ok", "a" * 64)

    monkeypatch.setattr(script, "realign_doc", fake_realign_doc)
    _patch_load_docs(script, monkeypatch, [doc_a, doc_b])

    class _NullSession(_FakeSession):
        pass

    monkeypatch.setattr(script, "get_session_factory", _FakeSessionFactory(_NullSession()))
    monkeypatch.setattr(script, "get_kb_minio_client", lambda: object())

    code = await script.cmd_hash_realign()
    assert calls == [_DOC_ID, "7d2f0a1e-0000-4000-8000-0000000000b2"]  # 续跑不中断
    assert code == 1


async def test_cmd_backfill_connection_error_isolated_per_doc(script, monkeypatch):
    """P3#4 主诉：cmd_backfill 连接类异常（EsConnectionError）单文档隔离——
    修复前该异常中断整批（仅捕 EsIndexError）；修复后两文档都被处理。"""
    from elastic_transport import ConnectionError as EsConnectionError

    doc_a = _ready_doc()
    doc_b = _ready_doc(id=uuid.UUID("7d2f0a1e-0000-4000-8000-0000000000b2"))
    calls: list[str] = []

    async def fake_backfill_doc(indexer, repo, doc):
        calls.append(str(doc["id"]))
        if doc["id"] == uuid.UUID(_DOC_ID):
            raise EsConnectionError("connection refused")
        return "ok"

    monkeypatch.setattr(script, "backfill_doc", fake_backfill_doc)
    _patch_load_docs(script, monkeypatch, [doc_a, doc_b])

    class _FakeIndices:
        async def exists(self, index: str) -> bool:
            return True

        async def refresh(self, index: str) -> dict:
            return {}

    class _FakeCmdClient:
        def __init__(self) -> None:
            self.indices = _FakeIndices()
            self.closed = 0

        async def count(self, index: str) -> dict:
            return {"count": 0}

        async def close(self) -> None:
            self.closed += 1

    fake_client = _FakeCmdClient()
    monkeypatch.setattr(script, "get_es_client", lambda: fake_client)

    code = await script.cmd_backfill(rebuild=False)
    assert calls == [_DOC_ID, "7d2f0a1e-0000-4000-8000-0000000000b2"]  # 两文档都被处理
    assert code == 1  # failed=1 → 非零退出（如实呈现）
    assert fake_client.closed == 1
