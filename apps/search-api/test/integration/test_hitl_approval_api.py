"""integration 层：REST 裁决端点 POST /turns/{id}/approval（B22 批次 2 第五段，真服务）。

契约锚点（api.md §2.13 v1.37 逐字；真服务 hitl enabled——假 spec 缝沿 b2_runtime
先例：真 fast 拓扑 + LLM 节点桩 + 假体检索面）：
- 202 {code:"0", data:{turn_id, status:"running", choice}}；幂等同 choice 重复提交
  200 与已记录裁决；非 awaiting/异 choice 冲突 **409008**（api.md §0 v1.37 定号注：
  计划草案 409007 已被 kb_import_jobs 占用顺延）；未知/越权 404001；choice 缺失或
  非法枚举 → 400001（pydantic 边界）；note 超限 → 400001（服务层 note_max_chars
  键权威判定〔缺省 500〕——pydantic max_length=1000 为边界哨兵，批次 5 清偿④）；
- note sanitize（R35）：note 过 query 同款 sanitize 管道（load_safety_config 门控 +
  detect_pii/sanitize 占位词），落库为脱敏形态；
- 阶段 2 恢复=BackgroundTask resume_turn（POST 响应后执行——ASGITransport 在响应
  发送后跑 BackgroundTasks，conftest :584 先例）；
- 取消端点 awaiting 分支（§2.7 v1.37）：canceled(user)+审批行 canceled+两帧齐发。
"""

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest

from app.agent.graphs import fast as fast_module
from app.agent.graphs.fast import build_fast_graph
from app.config_center.defaults import BUILTIN_DEFAULTS
from app.core.config import common_settings
from app.crons.approval_timeout_sweep import sweep_expired_approvals
from app.db.session import get_session_factory

pytestmark = pytest.mark.requires_db

_USER_ID = common_settings.default_user_id
_SPEC_DATA_ID = "ai_search.agent.spec.json"
_MAPPING_DATA_ID = "ai_search.model.mapping.json"
_MCP_DATA_ID = "ai_search.agent.mcp.json"
_THREAD_ID = uuid.uuid4()
_TURN_ID = uuid.uuid4()


# ---- 记录器/桩图/假体（与 test_hitl_two_phase 同构——本地形态） ----


class _Recorder:
    def __init__(self) -> None:
        self.web_calls = 0
        self.kb_calls = 0
        self.merged_candidates: list[dict[str, Any]] = []


def _build_patched_graph(recorder: _Recorder, queries: list[str]):
    async def _stub_rewrite(state: Any, config: Any = None) -> dict[str, Any]:
        from app.agent.graphs.signals import emit_stage

        # 生产同款自身 done 帧（fast_rewrite.py:712 形态——评审 R2 验证分叉对齐）
        emit_stage("rewrite", "done", extra={"strategy": "web"})
        return {
            "rewritten_queries": list(queries),
            "query_type": "factoid",
            "search_strategy": "web",
        }

    async def _stub_web(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.web_calls += 1
        return {"candidates": [], "errors": []}

    async def _stub_kb(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.kb_calls += 1
        return {"candidates": [], "errors": []}

    async def _stub_merge(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.merged_candidates.extend(state.get("candidates") or [])
        return {"fused": [], "images_raw": []}

    async def _stub_fuse(state: Any, config: Any = None) -> dict[str, Any]:
        return {"fused": list(state.get("fused") or [])}

    async def _stub_synthesis(state: Any, config: Any = None) -> dict[str, Any]:
        return {"answer_md": "# REST 两阶段答案"}

    original = {
        name: getattr(fast_module, name)
        for name in (
            "rewrite",
            "web_search",
            "kb_search",
            "merge_dedupe",
            "fuse_rerank",
            "synthesis",
        )
    }
    fast_module.rewrite = _stub_rewrite
    fast_module.web_search = _stub_web
    fast_module.kb_search = _stub_kb
    fast_module.merge_dedupe = _stub_merge
    fast_module.fuse_rerank = _stub_fuse
    fast_module.synthesis = _stub_synthesis
    try:
        graph = build_fast_graph()
    finally:
        for name, fn in original.items():
            setattr(fast_module, name, fn)
    return graph


class _FakeRetriever:
    def __init__(self) -> None:
        self.calls = 0

    def _rows(self) -> list[dict[str, Any]]:
        channel = "bm25" if self.calls % 2 == 1 else "vector"
        return [
            {
                "chunk_id": "x",
                "kb_doc_id": "d1",
                "content": "KB 命中内容",
                "score": 0.8,
                "heading_path": ["文档x", "命中X"],
                "node_id": None,
                "channel": channel,
            }
        ]

    async def bm25_search(self, query: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._rows())

    async def vector_search(self, query_text: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._rows())


class _FakeRerank:
    async def __call__(self, query: str, documents: list[str], *, model_alias: str, timeout_s: float) -> list[float | None]:
        return [0.9] * len(documents)


def _arm_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
    recorder: _Recorder,
    queries: list[str],
    hitl_overrides: dict[str, Any] | None = None,
) -> None:
    """真服务调度器接假 spec 缝（hitl enabled）+ 桩图 + 假体检索面。

    hitl_overrides（test_hitl_two_phase._make_scheduler 同构）：在 setattr 前
    一次性并入 spec["hitl"]——单 patch 单 spec 对象，避免二次覆写 _capture_spec_payload。
    """
    scheduler = runtime.scheduler
    spec = copy.deepcopy(BUILTIN_DEFAULTS[_SPEC_DATA_ID])
    spec["hitl"]["enabled"] = True
    spec["hitl"]["shadow"] = False
    if hitl_overrides:
        spec["hitl"].update(hitl_overrides)
    spec["kb"]["es_enabled"] = True
    spec["kb"]["embedding_enabled"] = True
    scheduler._graph = _build_patched_graph(recorder, queries)
    monkeypatch.setattr(scheduler, "_capture_spec_payload", lambda: spec)
    monkeypatch.setattr(
        scheduler, "_capture_mapping_payload", lambda: BUILTIN_DEFAULTS[_MAPPING_DATA_ID]
    )
    monkeypatch.setattr(
        scheduler, "_capture_mcp_payload", lambda: BUILTIN_DEFAULTS[_MCP_DATA_ID]
    )
    monkeypatch.setattr(scheduler, "_kb_retriever", lambda: _FakeRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())


def _connect(info: Any):
    return psycopg2.connect(
        host=info.host,
        port=int(info.port),
        user=info.user,
        password=info.password,
        dbname=info.name,
        connect_timeout=5,
    )


def _seed(info: Any) -> None:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, mode_summary, turn_count) "
                "VALUES (%s, %s, %s, %s, 1)",
                (str(_THREAD_ID), _USER_ID, "REST 裁决测试线程", "fast"),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name, timing, usage, "
                "query_lang, started_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, "
                "%s::jsonb, %s, %s)",
                (
                    str(_TURN_ID),
                    str(_THREAD_ID),
                    _USER_ID,
                    "fast",
                    "running",
                    "知识库里有审批门的资料吗？",
                    '["知识库 审批门"]',
                    "qwen",
                    "chat-standard",
                    "{}",
                    "{}",
                    "zh",
                    datetime.now(UTC),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _turn_status(info: Any) -> str | None:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT status FROM turns WHERE id = %s", (str(_TURN_ID),))
            row = cursor.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _events(info: Any) -> list[tuple[str, dict[str, Any]]]:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, payload FROM turn_events WHERE turn_id = %s "
                "ORDER BY seq ASC",
                (str(_TURN_ID),),
            )
            return [(r[0], r[1]) for r in cursor.fetchall()]
    finally:
        conn.close()


async def _run_phase1(runtime: Any) -> None:
    async with get_session_factory()() as session:
        from app.db.models import Turn

        row = await session.get(Turn, _TURN_ID)
        assert row is not None
        await runtime.scheduler._execute_turn(row)


@pytest.fixture
def approval_api_env(
    monkeypatch: pytest.MonkeyPatch, b2_runtime: Any, migrated_db: Any
) -> dict[str, Any]:
    """真服务运行时 + 假 spec 缝（hitl enabled）+ 播种 running 轮。"""
    _seed(migrated_db)
    recorder = _Recorder()
    _arm_runtime(monkeypatch, b2_runtime, recorder, queries=["知识库 审批门"])
    return {"info": migrated_db, "runtime": b2_runtime, "recorder": recorder}


# ---- 全流程：POST 202 → 阶段 2 → 幂等 200 → 异 choice 409008 ----


async def test_approval_endpoint_full_flow(
    approval_api_env: dict[str, Any], async_client: Any
) -> None:
    """§2.13 逐字：POST 202 信封 {code:0,data:{turn_id,status:running,choice}}；
    幂等同 choice 重复提交 200；异 choice 409008；阶段 2 BackgroundTask 恢复完成。"""
    from test.integration.conftest import await_until

    info = approval_api_env["info"]
    await _run_phase1(approval_api_env["runtime"])
    assert _turn_status(info) == "awaiting_approval"

    response = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/approval",
        json={"choice": "kb_only"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["code"] == "0"
    assert body["data"] == {
        "turn_id": str(_TURN_ID),
        "status": "running",
        "choice": "kb_only",
    }

    await await_until(
        lambda: _turn_status(info) == "completed", description="阶段 2 完成"
    )
    events = _events(info)
    assert [p for t, p in events if t == "approval_requested"]
    resolved = [p for t, p in events if t == "approval_resolved"]
    assert resolved and resolved[0]["resolved_by"] == "submit"
    done = [p for t, p in events if t == "done"]
    assert done and done[0]["finish"] == "completed"

    # 幂等：同 choice 重复提交 200 与已记录裁决
    repeat = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/approval",
        json={"choice": "kb_only"},
    )
    assert repeat.status_code == 200
    assert repeat.json()["data"]["choice"] == "kb_only"
    # 异 choice → 409008
    conflict = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/approval",
        json={"choice": "web_only"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "409008"


async def test_approval_endpoint_error_faces(
    approval_api_env: dict[str, Any], async_client: Any
) -> None:
    """错误面（api.md §2.13）：未知/越权 404001；choice 非法枚举 400001（pydantic
    边界）；note 超限（501>缺省 500）→ 400001（服务层 note_max_chars 键权威判定
    ——pydantic 边界哨兵 1000 不拦 501，批次 5 清偿④双层形态）；非 awaiting 轮
    （无审批行 running）→ 409008。"""
    info = approval_api_env["info"]

    unknown = await async_client.post(
        f"/api/v1/turns/{uuid.uuid4()}/approval", json={"choice": "kb_only"}
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "404001"

    bad_choice = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/approval", json={"choice": "hybrid"}
    )
    assert bad_choice.status_code == 400
    assert bad_choice.json()["code"] == "400001"

    long_note = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/approval",
        json={"choice": "kb_only", "note": "长" * 501},
    )
    assert long_note.status_code == 400

    conflict = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/approval", json={"choice": "both"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "409008"
    assert _turn_status(info) == "running"  # 非 awaiting（无审批行 running 轮未被动）


async def test_approval_note_sanitized_r35(
    approval_api_env: dict[str, Any], async_client: Any
) -> None:
    """R35：note 过 query 同款 sanitize 管道（PII 占位词）——落库为脱敏形态，
    原文不入审批行。"""
    info = approval_api_env["info"]
    await _run_phase1(approval_api_env["runtime"])

    response = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/approval",
        json={"choice": "custom", "note": "补充：联系电话 13800138000 找我"},
    )
    assert response.status_code == 202

    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT note FROM turn_approvals WHERE turn_id = %s",
                (str(_TURN_ID),),
            )
            stored = cursor.fetchone()[0]
    finally:
        conn.close()
    assert stored is not None
    assert "13800138000" not in stored  # 原文不落库（占位词替换）


async def test_approval_timeout_hybrid_via_sweep(
    approval_api_env: dict[str, Any], async_client: Any
) -> None:
    """超时 hybrid（on_timeout 缺省）：expires_at 过期 → 扫描器收口+恢复调度 →
    both 形态完成（web+kb 双 Send）；超时后人为裁决 → 409008。"""
    from test.integration.conftest import await_until

    info = approval_api_env["info"]
    runtime = approval_api_env["runtime"]
    await _run_phase1(runtime)

    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turn_approvals SET expires_at = %s WHERE turn_id = %s",
                (datetime.now(UTC) - timedelta(seconds=1), str(_TURN_ID)),
            )
        conn.commit()
    finally:
        conn.close()

    await sweep_expired_approvals(scheduler=runtime.scheduler)
    await await_until(
        lambda: _turn_status(info) == "completed", description="超时收口阶段 2 完成"
    )
    recorder = approval_api_env["recorder"]
    assert recorder.web_calls == 1 and recorder.kb_calls == 1  # both=现状 hybrid 形态

    late = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/approval",
        json={"choice": "both"},
    )
    assert late.status_code == 409
    assert late.json()["code"] == "409008"  # 超时后再到的人为裁决


async def test_cancel_endpoint_awaiting_branch(
    approval_api_env: dict[str, Any], async_client: Any
) -> None:
    """§2.7 v1.37：awaiting 取消=直接终态 canceled(user)+审批行 canceled+
    approval_resolved(resolved_by=cancel) → done(canceled)。"""
    info = approval_api_env["info"]
    await _run_phase1(approval_api_env["runtime"])
    assert _turn_status(info) == "awaiting_approval"

    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/cancel")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "canceling"

    assert _turn_status(info) == "canceled"
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, decided_by FROM turn_approvals WHERE turn_id = %s",
                (str(_TURN_ID),),
            )
            approval = cursor.fetchone()
    finally:
        conn.close()
    assert approval[0] == "canceled" and approval[1] == "system"
    events = _events(info)
    resolved = [p for t, p in events if t == "approval_resolved"]
    assert resolved and resolved[0]["resolved_by"] == "cancel"
    done = [p for t, p in events if t == "done"]
    assert done and done[0]["finish"] == "canceled" and done[0]["cancel_reason"] == "user"


@pytest.fixture
def approval_cancel_api_env(
    monkeypatch: pytest.MonkeyPatch, b2_runtime: Any, migrated_db: Any
) -> dict[str, Any]:
    """on_timeout=cancel spec 缝变体（_arm_runtime hitl_overrides 单 patch 单
    spec 对象——勿在用例内二次覆写 _capture_spec_payload 重建 spec，会丢 kb
    es/embedding 通道开启导致门未 armed，test_timeout_cancel_rest_canceled_key_omission
    首版踩坑留注）。"""
    _seed(migrated_db)
    recorder = _Recorder()
    _arm_runtime(
        monkeypatch,
        b2_runtime,
        recorder,
        queries=["知识库 审批门"],
        hitl_overrides={"on_timeout": "cancel"},
    )
    return {"info": migrated_db, "runtime": b2_runtime, "recorder": recorder}


async def test_timeout_cancel_rest_canceled_key_omission(
    approval_cancel_api_env: dict[str, Any], async_client: Any
) -> None:
    """超时 cancel 收口的 REST 2.5 消费面（api.md §2.5 v1.38 契约钉正批字面）：

    - `hitl.on_timeout=cancel` → 轮 canceled 且 `cancel_reason=timeout`
      （§2.5 cancel_reason 第 4 值——审批等待超时静默收口，nacos §3 v1.50
      `on_timeout=cancel`「静默收尾 canceled(timeout)」的执行面）；
    - **`status=canceled` 的审批行 `approval` 键整体省略**（§2.5 v1.38 钉句——
      回看面审计留痕在 turn_approvals 表，data-model.md §1.20；前端回看面无
      审批折叠条，frontend-api.md §4 turnStore v1.41 同批句）。
    """
    from test.integration.conftest import await_until

    info = approval_cancel_api_env["info"]
    runtime = approval_cancel_api_env["runtime"]
    await _run_phase1(runtime)
    assert _turn_status(info) == "awaiting_approval"

    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turn_approvals SET expires_at = %s WHERE turn_id = %s",
                (datetime.now(UTC) - timedelta(seconds=1), str(_TURN_ID)),
            )
        conn.commit()
    finally:
        conn.close()

    await sweep_expired_approvals(scheduler=runtime.scheduler)
    await await_until(
        lambda: _turn_status(info) == "canceled", description="超时 cancel 收口"
    )

    response = await async_client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "canceled"
    assert data["cancel_reason"] == "timeout"  # §2.5 v1.38 cancel_reason 第 4 值
    assert "approval" not in data  # §2.5 v1.38 钉句：canceled 审批行=键整体省略


async def test_timeout_hybrid_2p5_expired_projected_approved(
    approval_api_env: dict[str, Any], async_client: Any
) -> None:
    """双评审前收口自检②：超时 hybrid 收口的审批行 status='expired' → REST 2.5
    投影 approval={"status":"approved","choice":"both","decided_by":"timeout"}
    （get_turn_detail approved/expired 统一投影 + decided_by 区分——存储/投影命名
    分歧为契约自身定义：data-model.md §1.20 status 注 vs api.md §2.5 decided 形态）；
    阶段 2 完成后审批行不复写（保持 expired）。"""
    from test.integration.conftest import await_until

    info = approval_api_env["info"]
    runtime = approval_api_env["runtime"]
    await _run_phase1(runtime)

    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turn_approvals SET expires_at = %s WHERE turn_id = %s",
                (datetime.now(UTC) - timedelta(seconds=1), str(_TURN_ID)),
            )
        conn.commit()
    finally:
        conn.close()

    await sweep_expired_approvals(scheduler=runtime.scheduler)
    await await_until(
        lambda: _turn_status(info) == "completed", description="超时 hybrid 阶段 2 完成"
    )

    # DB 行保持 expired + decided_by=timeout + choice=both（阶段 2 完成不复写）
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, decided_by, choice FROM turn_approvals WHERE turn_id = %s",
                (str(_TURN_ID),),
            )
            row = cursor.fetchone()
    finally:
        conn.close()
    assert row[0] == "expired" and row[1] == "timeout" and row[2] == "both"

    response = await async_client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 200
    approval = response.json()["data"]["approval"]
    assert approval["status"] == "approved"  # expired → 投影 approved（decided_by 区分）
    assert approval["decided_by"] == "timeout"
    assert approval["choice"] == "both"
