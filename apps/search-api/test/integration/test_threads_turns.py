"""integration：threads/turns REST 契约测试（api.md §2.1~§2.5/§2.7 v1.10）与 B2 闭环①~④取证。

真库 + 桩主图 + 真 EventBus（conftest.b2_runtime：lifespan 等价启停）+ 进程内 ASGI。

契约断言逐字段对照（只读冻结契约，禁止私改）：
- §0 通用约定：成功信封 {"code":"0","message":"ok","data"}、错误信封 {"code","message"}、
  时间戳 ISO-8601 UTC 带时区、ID UUID v4、分页 limit 默认 50 上限 200；
- §2.1 POST /threads：data={thread_id,turn_id,status}（status∈{running,queued}）、
  query 1~2000/mode·model·on_conflict·incognito 校验失败 400001；
- §2.2 GET /threads：data={total,items[{thread_id,title,mode_summary,turn_count,
  created_at,updated_at}]}、updated_at DESC、mode 过滤；
- §2.3 GET /threads/{id}：骨架字段集与 turns[]（has_report/degraded 推导）；
- §2.4 POST /threads/{id}/turns：请求体同 §2.1、404001/409001；
- §2.5 GET /turns/{id}：全字段集 + sources[]/stages[] 形状、进行中可读、分期口径
  （incognito=false / followups=null / images=[]；memory_hits 无此字段——M5 前契约口径）；
- §2.7 POST /turns/{id}/cancel：data={turn_id,status:"canceling"}、409002 终态；
- §7.4 主干序列对账（turn_events 落库与 GET /turns 一致，token/thinking 不落库）。

闭环①②：test_post_threads_success_and_turn_events_reconciliation；
闭环③：test_post_turn_followup / test_cancel_running_turn / test_cancel_queued_turn；
闭环④：test_reject_strategy / test_enqueue_strategy / test_interrupt_strategy /
test_rollback_strategy。

并发交互口径：BackgroundTask（schedule_turn→图运行）在 POST 响应返回前由 ASGI 链路
驱动完成——需要运行窗口的用例以 asyncio.create_task 提交 POST，再轮询已提交可见的
行（get_db teardown commit 先于 BackgroundTask，FastAPI 0.106+ 时序）拿 id 后并发
操作；桩图运行窗口经 monkeypatch fast_graph.SEARCH_DELAY_S 制造（ask 指定口径）。

source_id 三面同源（评审 P2 修复后）：stub 的固定测试 uuid（fast.py）仅是图侧占位
——翻译层先建 sources 行定 id、以行 id 回写事件 batch 后再发布，§7.2 sources 事件 /
turn_events payload / sources 表 / §2.5 sources[] 四面同 id（data-model.md §1.5），
对账断言见 test_post_threads_success_and_turn_events_reconciliation。
"""

import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.agent.graphs import deep as deep_graph
from app.agent.graphs import fast as fast_graph
from app.db.models import Source, Turn, TurnEvent
from app.db.session import get_session_factory

# ---- 契约字段集（api.md 逐字段冻结；DTO 新增/删减字段在此显式失败）----

_THREAD_CREATE_DATA_KEYS = {"thread_id", "turn_id", "status"}
_THREAD_LIST_ITEM_KEYS = {
    "thread_id",
    "title",
    "mode_summary",
    "turn_count",
    "created_at",
    "updated_at",
}
_THREAD_DETAIL_KEYS = {
    "thread_id",
    "title",
    "mode_summary",
    "turn_count",
    "has_artifacts",
    "created_at",
    "updated_at",
    "turns",
}
_TURN_SKELETON_KEYS = {
    "turn_id",
    "mode",
    "status",
    "query",
    "query_lang",
    "has_report",
    "degraded",
    "duration_ms",
    "created_at",
}
# §2.5 全字段（含 v1.2 incognito / v1.4 cancel_reason / v1.7 followups；无 memory_hits）
_TURN_DETAIL_KEYS = {
    "turn_id",
    "thread_id",
    "mode",
    "status",
    "query",
    "query_lang",
    "rewritten_queries",
    "parent_turn_id",
    "answer_md",
    "sources",
    "images",
    "stages",
    "degraded",
    "degraded_reason",
    "error",
    "usage",
    "duration_ms",
    "export_available",
    "incognito",
    "followups",
    "cancel_reason",
    "created_at",
    "finished_at",
}
_SOURCE_KEYS = {
    "source_id",
    "ref_no",
    "source_type",
    "title",
    "url",
    "snippet",
    "score",
    "snapshot_available",
    "kb_doc_id",
}
_STAGE_KEYS = {
    "stage_key",
    "status",
    "label",
    "started_at",
    "ended_at",
    "duration_ms",
    "extra",
}
_USAGE_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"}

# §7.4 快速主干（桩期序列）：rewrite(r,d) → search(r,d) → sources → write(r,d) → done
_STUB_FAST_EVENT_SEQUENCE = [
    "stage",
    "stage",
    "stage",
    "stage",
    "sources",
    "stage",
    "stage",
    "done",
]

_FAST_QUERY = "LangGraph 的 Send API 和 Router 的区别？"


# ---- DB 直读辅助（闭环② turn_events 落库对账）----


async def _db_turn(turn_id: str) -> Turn:
    async with get_session_factory()() as session:
        return await session.get(Turn, uuid.UUID(turn_id))


async def _db_turn_events(turn_id: str) -> list[TurnEvent]:
    async with get_session_factory()() as session:
        rows = (
            await session.scalars(
                select(TurnEvent)
                .where(TurnEvent.turn_id == uuid.UUID(turn_id))
                .order_by(TurnEvent.seq.asc())
            )
        ).all()
        return list(rows)


async def _db_sources(turn_id: str) -> list[Source]:
    async with get_session_factory()() as session:
        rows = (
            await session.scalars(
                select(Source)
                .where(Source.turn_id == uuid.UUID(turn_id))
                .order_by(Source.ref_no.asc())
            )
        ).all()
        return list(rows)


# ---- 断言辅助 ----


def _assert_ok_envelope(resp, *, status_code: int = 200) -> dict:
    """§0 成功信封：{"code":"0","message":"ok","data":{...}}。"""
    assert resp.status_code == status_code
    body = resp.json()
    assert body["code"] == "0", body
    assert body["message"] == "ok"
    assert "data" in body
    return body["data"]


def _assert_error(resp, *, status_code: int, code: str) -> dict:
    """§0 错误信封：{"code","message"}（message 用户可读非空；无 data 键）。"""
    assert resp.status_code == status_code, resp.text
    body = resp.json()
    assert body["code"] == code, body
    assert isinstance(body["message"], str) and body["message"]
    assert "data" not in body
    return body


def _assert_utc_iso(value: str) -> datetime:
    """§0 时间戳：ISO-8601 UTC 带时区（Z 或 +00:00）。"""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    return parsed


def _assert_usage_all_null(usage: dict) -> None:
    """M1 简化口径：usage={} → UsageBrief 全 null（§2.5 usage 形状锁定）。"""
    assert set(usage) == _USAGE_KEYS
    assert all(usage[k] is None for k in _USAGE_KEYS)


# ---- §2.1 POST /threads + 闭环①②（终态对账 turn_events）----


@pytest.mark.requires_db
async def test_post_threads_success_and_turn_events_reconciliation(
    async_client, b2_rest, b2_runtime
):
    """闭环①②：POST /threads 建线程跑完桩主图 pending→running→completed，
    终态字段与 turn_events 落库逐条对账。"""
    resp = await async_client.post(
        "/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"}
    )
    data = _assert_ok_envelope(resp)
    # §2.1 data 三键：thread_id/turn_id/status（status∈{running,queued}）
    assert set(data) == _THREAD_CREATE_DATA_KEYS
    assert data["status"] in {"running", "queued"}
    assert data["status"] == "running"  # 空闲线程 PROCEED → 立即执行
    uuid.UUID(data["thread_id"])
    turn_id = data["turn_id"]
    uuid.UUID(turn_id)

    # 闭环①：轮询至 completed（超时 30s 判 fail）
    detail = await b2_rest.wait_turn_status(turn_id, {"completed"})

    # ---- §2.5 全字段逐一对照 ----
    assert set(detail) == _TURN_DETAIL_KEYS
    assert detail["turn_id"] == turn_id
    assert detail["thread_id"] == data["thread_id"]
    assert detail["mode"] == "fast"
    assert detail["status"] == "completed"
    assert detail["query"] == _FAST_QUERY
    assert detail["query_lang"] is None  # B2 桩期不判定（M1 简化记录）
    assert detail["rewritten_queries"] == []
    assert detail["parent_turn_id"] is None
    # 桩答案经 token 流→updates 合并→终态落库→REST 读回全链一致
    assert detail["answer_md"] == fast_graph.STUB_FAST_ANSWER_MD
    # sources[]：§2.5 形状 + 桩值；分期口径 snapshot_available=false / kb_doc_id=null
    assert len(detail["sources"]) == 2
    assert [s["ref_no"] for s in detail["sources"]] == [1, 2]
    for src in detail["sources"]:
        assert set(src) == _SOURCE_KEYS
        assert src["source_type"] == "web"
        assert src["url"] is not None and src["url"].startswith("https://")
        assert src["title"] and src["snippet"]
        assert src["snapshot_available"] is False  # M2（snapshot_id 列）起真实值
        assert src["kb_doc_id"] is None  # M3 起真实值
    assert [s["score"] for s in detail["sources"]] == [0.91, 0.87]
    assert detail["images"] == []  # B2 桩不发 images 事件（M4 定回放口径）
    # stages[]：§2.5 形状 + running/done 配对聚合结果
    assert [s["stage_key"] for s in detail["stages"]] == ["rewrite", "search", "write"]
    for stage in detail["stages"]:
        assert set(stage) == _STAGE_KEYS
        assert stage["status"] == "done"
        assert stage["started_at"] is not None and stage["ended_at"] is not None
        assert stage["duration_ms"] is not None
        assert _assert_utc_iso(stage["started_at"])
    assert detail["stages"][2]["label"] == "综合中"  # write 节点 label 透传
    assert detail["degraded"] is False
    assert detail["degraded_reason"] is None
    assert detail["error"] is None
    _assert_usage_all_null(detail["usage"])
    assert isinstance(detail["duration_ms"], int) and detail["duration_ms"] >= 0
    assert detail["export_available"] is False  # fast 轮恒 false
    # 分期口径（用户裁决 2026-11-08）：incognito=false（M5）/ followups=null（0009-B7）；
    # memory_hits 无此字段（api.md §2.5「M5 及之前无此字段」，评审 P2 勘误移除）
    assert detail["incognito"] is False
    assert detail["followups"] is None
    assert "memory_hits" not in detail
    assert detail["cancel_reason"] is None
    created_at = _assert_utc_iso(detail["created_at"])
    finished_at = _assert_utc_iso(detail["finished_at"])
    assert finished_at >= created_at
    # duration 口径=「提交→done」（data-model.md §1.3）：finished_at − created_at
    delta_ms = (finished_at - created_at).total_seconds() * 1000
    assert abs(detail["duration_ms"] - delta_ms) < 1.0

    # ---- turns 表终态行直读对账 ----
    turn_row = await _db_turn(turn_id)
    assert turn_row is not None
    assert turn_row.status == "completed"
    assert turn_row.started_at is not None  # PROCEED 裁决同事务盖章（图运行开始）
    assert turn_row.finished_at is not None
    assert turn_row.started_at < turn_row.finished_at  # 非终态防御分支补盖（评审 P2）
    assert turn_row.answer_md == fast_graph.STUB_FAST_ANSWER_MD
    assert turn_row.duration_ms == detail["duration_ms"]
    assert turn_row.usage == {}
    assert [s["stage_key"] for s in turn_row.timing["stages"]] == [
        "rewrite",
        "search",
        "write",
    ]
    assert turn_row.chat_model_key == "qwen"  # 缺省 chat.default（映射可证 A10）
    assert turn_row.model_name == "chat-standard"

    # ---- 闭环②：turn_events 落库逐条对账 ----
    events = await _db_turn_events(turn_id)
    types = [e.event_type for e in events]
    # §7.4 快速主干 + token/thinking 不落库（ADR-003 第 4 条）
    assert types == _STUB_FAST_EVENT_SEQUENCE
    assert "token" not in types and "thinking" not in types
    # seq 线程内单调：新线程自 1 起严格递增（EventBus 盖章与落库同 seq）——
    # 但不连续：token/thinking 只发布不落库、照常占号（ADR-003 第 4 条）
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs[0] == 1  # 新线程（db_truncate 后）首事件 seq=1
    # stage 事件 payload 与 GET /turns stages[] 一致（running/done 配对）
    stage_done = [
        e.payload["stage_key"]
        for e in events
        if e.event_type == "stage" and e.payload["status"] == "done"
    ]
    assert stage_done == ["rewrite", "search", "write"]
    write_running = next(
        e for e in events if e.payload.get("stage_key") == "write" and e.payload["status"] == "running"
    )
    assert write_running.payload["label"] == "综合中"
    # sources 事件 payload 与 REST sources / sources 表三方一致
    sources_event = next(e for e in events if e.event_type == "sources")
    assert sources_event.payload["total"] == 2
    batch = sources_event.payload["batch"]
    assert [b["ref_no"] for b in batch] == [1, 2]
    for brief, item in zip(detail["sources"], batch):
        assert (item["ref_no"], item["source_type"], item["title"], item["url"]) == (
            brief["ref_no"],
            brief["source_type"],
            brief["title"],
            brief["url"],
        )
        assert abs(item["score"] - brief["score"]) < 1e-9
    db_rows = await _db_sources(turn_id)
    assert [(r.ref_no, r.source_type, r.title) for r in db_rows] == [
        (1, "web", batch[0]["title"]),
        (2, "web", batch[1]["title"]),
    ]
    assert float(db_rows[0].score) == 0.91 and float(db_rows[1].score) == 0.87
    # source_id 四面同源（评审 P2 修复）：§7.2 事件 batch / turn_events payload /
    # sources 表行 id / §2.5 sources[] 一致（data-model.md §1.5「id 即 api.md 的
    # source_id」）；stub 固定测试 uuid 仅图侧占位，已被行 id 回写覆写
    for brief, item, row in zip(detail["sources"], batch, db_rows):
        assert item["source_id"] == brief["source_id"] == str(row.id)
        uuid.UUID(brief["source_id"])  # 合法 UUID v4（api.md §0）
        assert brief["source_id"] != fast_graph.STUB_FAST_SOURCES[0]["source_id"]
        assert brief["source_id"] != fast_graph.STUB_FAST_SOURCES[1]["source_id"]
    # done 事件 payload 与 REST 终态一致（先落库后发布——「事件到达时 2.5 已可对账」）
    done = events[-1]
    assert done.payload["finish"] == "completed"
    assert done.payload["degraded"] is False
    assert done.payload["degraded_reason"] is None
    assert done.payload["duration_ms"] == detail["duration_ms"]
    assert done.payload["source_count"] == 2
    assert done.payload["usage"] == {}


# ---- §2.1 校验失败 400001（含 model 非法键零写库）----


@pytest.mark.requires_db
async def test_post_threads_validation_errors(async_client, b2_rest, b2_runtime):
    cases = [
        ({"query": "", "mode": "fast"}, "query 空"),
        ({"query": "x" * 2001, "mode": "fast"}, "query 超 2000 字符"),
        ({"query": "合法问题", "mode": "hybrid"}, "mode 非法枚举"),
        ({"query": "合法问题", "mode": "fast", "model": "gpt5"}, "model 非法键"),
        ({"query": "合法问题", "mode": "fast", "on_conflict": "pause"}, "on_conflict 非法枚举"),
        ({"query": "合法问题", "mode": "fast", "incognito": "yes"}, "incognito 非布尔"),
    ]
    for payload, label in cases:
        resp = await async_client.post("/api/v1/threads", json=payload)
        _assert_error(resp, status_code=400, code="400001")
    # 全部校验前置（model 解析先于建轮）：零写库
    listing = await async_client.get("/api/v1/threads")
    assert listing.json()["data"]["total"] == 0


# ---- §2.2 GET /threads：分页 / mode 过滤 / updated_at DESC ----


@pytest.mark.requires_db
async def test_get_threads_list_pagination_and_mode_filter(
    async_client, b2_rest, b2_runtime
):
    created: list[str] = []
    for query, mode in [
        ("fast 问题一", "fast"),
        ("fast 问题二", "fast"),
        ("deep 问题三", "deep"),
    ]:
        resp = await async_client.post(
            "/api/v1/threads", json={"query": query, "mode": mode}
        )
        assert resp.status_code == 200
        created.append(resp.json()["data"]["thread_id"])

    # 全量：total + items；updated_at DESC（后建在前）
    data = _assert_ok_envelope(await async_client.get("/api/v1/threads"))
    assert set(data) == {"total", "items"}
    assert data["total"] == 3
    assert [item["thread_id"] for item in data["items"]] == [
        created[2],
        created[1],
        created[0],
    ]
    for item in data["items"]:
        assert set(item) == _THREAD_LIST_ITEM_KEYS
    assert data["items"][2]["mode_summary"] == "fast"

    # 分页 limit/offset（total 不受分页影响）
    data = _assert_ok_envelope(await async_client.get("/api/v1/threads?limit=2&offset=1"))
    assert data["total"] == 3
    assert [item["thread_id"] for item in data["items"]] == [created[1], created[0]]

    # mode 过滤（fast/deep 命中；mixed 无混合线程 → 0）
    data = _assert_ok_envelope(await async_client.get("/api/v1/threads?mode=deep"))
    assert data["total"] == 1
    assert data["items"][0]["thread_id"] == created[2]
    assert data["items"][0]["mode_summary"] == "deep"
    data = _assert_ok_envelope(await async_client.get("/api/v1/threads?mode=fast"))
    assert data["total"] == 2
    data = _assert_ok_envelope(await async_client.get("/api/v1/threads?mode=mixed"))
    assert data["total"] == 0 and data["items"] == []

    # 分页/过滤参数校验 → 400001
    _assert_error(
        await async_client.get("/api/v1/threads?limit=201"), status_code=400, code="400001"
    )
    _assert_error(
        await async_client.get("/api/v1/threads?limit=0"), status_code=400, code="400001"
    )
    _assert_error(
        await async_client.get("/api/v1/threads?mode=archived"),
        status_code=400,
        code="400001",
    )


# ---- §2.3 GET /threads/{id}：骨架契约 + 404001 ----


@pytest.mark.requires_db
async def test_get_thread_detail_contract_and_404(async_client, b2_rest, b2_runtime):
    resp = await async_client.post(
        "/api/v1/threads", json={"query": "首问标题会截断为五十个字符以内" * 5, "mode": "fast"}
    )
    data = _assert_ok_envelope(resp)
    thread_id, turn_id = data["thread_id"], data["turn_id"]
    await b2_rest.wait_turn_status(turn_id, {"completed"})

    detail = _assert_ok_envelope(
        await async_client.get(f"/api/v1/threads/{thread_id}")
    )
    assert set(detail) == _THREAD_DETAIL_KEYS
    assert detail["thread_id"] == thread_id
    assert detail["title"] == ("首问标题会截断为五十个字符以内" * 5)[:50]  # REQ-006
    assert detail["mode_summary"] == "fast"
    assert detail["turn_count"] == 1
    assert detail["has_artifacts"] is False  # 分期口径：0002-M2 起真实值
    _assert_utc_iso(detail["created_at"])
    _assert_utc_iso(detail["updated_at"])
    assert len(detail["turns"]) == 1
    skeleton = detail["turns"][0]
    assert set(skeleton) == _TURN_SKELETON_KEYS
    assert skeleton["turn_id"] == turn_id
    assert skeleton["status"] == "completed"
    # api.md v1.10 §2.3：has_report = mode=deep 且 answer_md 非空——fast 轮的综合回答
    # 不计入 report（即使 answer_md 非空）
    assert skeleton["mode"] == "fast"
    assert skeleton["has_report"] is False
    assert skeleton["degraded"] is False
    assert skeleton["query_lang"] is None  # B2 桩期
    assert isinstance(skeleton["duration_ms"], int)

    # 404001：不存在与非法 UUID 同码不泄漏存在性
    body = _assert_error(
        await async_client.get(f"/api/v1/threads/{uuid.uuid4()}"),
        status_code=404,
        code="404001",
    )
    assert body["message"] == "线程不存在"  # api.md §0 错误信封示例措辞
    _assert_error(
        await async_client.get("/api/v1/threads/not-a-uuid"),
        status_code=404,
        code="404001",
    )


@pytest.mark.requires_db
async def test_get_thread_detail_has_report_deep_positive(
    async_client, b2_rest, b2_runtime
):
    """api.md v1.10 §2.3 判据正例：mode=deep 且 answer_md 非空 → has_report=true。

    与 fast 负例（test_get_thread_detail_contract_and_404 中 fast 轮 has_report=false）
    成对——判据两条件缺一即 false（fast+有产出、deep+无产出两类负例）。
    """
    data = _assert_ok_envelope(
        await async_client.post(
            "/api/v1/threads", json={"query": "深度问题：LangGraph 子图边界", "mode": "deep"}
        )
    )
    thread_id, turn_id = data["thread_id"], data["turn_id"]
    turn = await b2_rest.wait_turn_status(turn_id, {"completed"})
    assert turn["mode"] == "deep"
    assert turn["answer_md"] == deep_graph.STUB_DEEP_ANSWER_MD  # 桩期固定报告（非空）

    detail = _assert_ok_envelope(
        await async_client.get(f"/api/v1/threads/{thread_id}")
    )
    assert detail["mode_summary"] == "deep"
    skeleton = detail["turns"][0]
    assert set(skeleton) == _TURN_SKELETON_KEYS
    assert skeleton["mode"] == "deep"
    assert skeleton["status"] == "completed"
    assert skeleton["has_report"] is True  # mode=deep 且 answer_md 非空
    assert skeleton["degraded"] is False


@pytest.mark.requires_db
async def test_get_turn_detail_deep_completed_export_available(
    async_client, b2_rest, b2_runtime
):
    """api.md v1.10 §2.5：export_available 与 §2.3 has_report 同源——deep 轮
    completed 且 answer_md 非空才 true（桩 deep 轮拿到非空报告）。"""
    data = _assert_ok_envelope(
        await async_client.post(
            "/api/v1/threads", json={"query": "深度问题：子图状态投影", "mode": "deep"}
        )
    )
    turn = await b2_rest.wait_turn_status(data["turn_id"], {"completed"})
    assert turn["answer_md"] == deep_graph.STUB_DEEP_ANSWER_MD  # 非空
    assert turn["export_available"] is True


# ---- §2.4 POST /threads/{id}/turns：追问 + 404001（闭环③追问真实走通）----


@pytest.mark.requires_db
async def test_post_turn_followup_contract(async_client, b2_rest, b2_runtime):
    data = _assert_ok_envelope(
        await async_client.post(
            "/api/v1/threads", json={"query": "首问：什么是条件边？", "mode": "fast"}
        )
    )
    thread_id, _ = data["thread_id"], data["turn_id"]

    resp = await async_client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"query": "追问：Send API 怎么扇出？", "mode": "fast"},
    )
    followup = _assert_ok_envelope(resp)
    assert set(followup) == _THREAD_CREATE_DATA_KEYS
    assert followup["thread_id"] == thread_id
    assert followup["status"] == "running"  # 空闲线程追问 PROCEED
    detail = await b2_rest.wait_turn_status(followup["turn_id"], {"completed"})
    assert detail["answer_md"] == fast_graph.STUB_FAST_ANSWER_MD
    assert detail["thread_id"] == thread_id
    assert detail["parent_turn_id"] is None  # 无深度轮可挂（REQ-017 仅 deep completed/degraded）

    # 聚合推进：turn_count=2、骨架两行
    thread_detail = _assert_ok_envelope(
        await async_client.get(f"/api/v1/threads/{thread_id}")
    )
    assert thread_detail["turn_count"] == 2
    assert len(thread_detail["turns"]) == 2
    assert thread_detail["turns"][1]["query"] == "追问：Send API 怎么扇出？"

    # 404001：线程不存在（不存在与非法 UUID 同码）
    body = _assert_error(
        await async_client.post(
            f"/api/v1/threads/{uuid.uuid4()}/turns",
            json={"query": "追问", "mode": "fast"},
        ),
        status_code=404,
        code="404001",
    )
    assert body["message"] == "线程不存在"


@pytest.mark.requires_db
async def test_get_turn_detail_404(async_client, migrated_db):
    """§2.5/§0：不存在与非法 UUID 同码 404001（无需基建——纯读路径）。"""
    _assert_error(
        await async_client.get(f"/api/v1/turns/{uuid.uuid4()}"),
        status_code=404,
        code="404001",
    )
    _assert_error(
        await async_client.get("/api/v1/turns/not-a-uuid"),
        status_code=404,
        code="404001",
    )


# ---- §2.5 进行中可读（running 轮返回已持久化部分）----


@pytest.mark.requires_db
async def test_get_turn_detail_while_running(async_client, b2_rest, b2_runtime, monkeypatch):
    monkeypatch.setattr(fast_graph, "SEARCH_DELAY_S", 1.5)
    post_task = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"})
    )
    try:
        _, turn_id = await b2_rest.wait_first_thread()
        payload = await b2_rest.wait_turn_status(turn_id, {"running"})
        assert payload["status"] == "running"
        assert payload["answer_md"] is None  # 未完成无终态产出
        assert payload["finished_at"] is None
        assert payload["duration_ms"] is None
        assert payload["error"] is None
    finally:
        post = await asyncio.wait_for(post_task, timeout=30)
    assert post.json()["data"]["status"] == "running"  # §2.1 响应映射 PROCEED→running
    await b2_rest.wait_turn_status(turn_id, {"completed"})


# ---- §2.7 cancel：running 态（闭环③之一）----


@pytest.mark.requires_db
async def test_cancel_running_turn(async_client, b2_rest, b2_runtime, monkeypatch):
    monkeypatch.setattr(fast_graph, "SEARCH_DELAY_S", 2.0)
    post_task = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "会被取消的问题", "mode": "fast"})
    )
    try:
        _, turn_id = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn_id, {"running"})
        resp = await async_client.post(f"/api/v1/turns/{turn_id}/cancel")
        data = _assert_ok_envelope(resp)
        # §2.7 响应契约：data={turn_id, status:"canceling"}
        assert data == {"turn_id": turn_id, "status": "canceling"}
        detail = await b2_rest.wait_turn_status(turn_id, {"canceled"})
        assert detail["cancel_reason"] == "user"
        assert detail["status"] == "canceled"
        assert detail["degraded"] is False
        assert detail["error"] is None  # 取消非故障
    finally:
        post = await asyncio.wait_for(post_task, timeout=30)
    assert post.json()["data"]["status"] == "running"
    # turn_events：无 error 事件；done(finish=canceled, cancel_reason=user)
    events = await _db_turn_events(turn_id)
    types = [e.event_type for e in events]
    assert "error" not in types
    done = next(e for e in events if e.event_type == "done")
    assert done.payload["finish"] == "canceled"
    assert done.payload["cancel_reason"] == "user"


# ---- §2.7 cancel：queued 态（闭环③之二；§0 排队轮可取消立即出队）----


@pytest.mark.requires_db
async def test_cancel_queued_turn(async_client, b2_rest, b2_runtime, wait_for, monkeypatch):
    monkeypatch.setattr(fast_graph, "SEARCH_DELAY_S", 2.0)
    post1 = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "占住运行位", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})
        post2 = asyncio.create_task(
            async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "排队中被取消", "mode": "fast", "on_conflict": "enqueue"},
            )
        )
        try:
            # 等第二行出现并处于 queued
            turn2: str | None = None

            async def _second_turn_visible() -> bool:
                nonlocal turn2
                detail = await async_client.get(f"/api/v1/threads/{thread_id}")
                if detail.status_code != 200:
                    return False
                turns = detail.json()["data"]["turns"]
                if len(turns) < 2:
                    return False
                turn2 = turns[1]["turn_id"]
                return True

            await wait_for(_second_turn_visible, description="第二轮次行提交可见")
            assert turn2 is not None
            await b2_rest.wait_turn_status(turn2, {"queued"})
            resp = await async_client.post(f"/api/v1/turns/{turn2}/cancel")
            assert _assert_ok_envelope(resp) == {
                "turn_id": turn2,
                "status": "canceling",
            }
            detail2 = await b2_rest.wait_turn_status(turn2, {"canceled"})
            assert detail2["cancel_reason"] == "user"
            # 排队期取消：无业务事件，恰一个 done
            events2 = await _db_turn_events(turn2)
            assert [e.event_type for e in events2] == ["done"]
            assert events2[0].payload["finish"] == "canceled"
            assert events2[0].payload["cancel_reason"] == "user"
            # 占位轮不受影响
            detail1 = await b2_rest.wait_turn_status(turn1, {"completed"})
            assert detail1["status"] == "completed"
        finally:
            post2_resp = await asyncio.wait_for(post2, timeout=30)
        assert post2_resp.json()["data"]["status"] == "queued"  # §2.1 ENQUEUE→queued
    finally:
        post1_resp = await asyncio.wait_for(post1, timeout=30)
    assert post1_resp.json()["data"]["status"] == "running"


# ---- §0 四策略：reject=409001（闭环④之一；零残留）----


@pytest.mark.requires_db
async def test_reject_strategy_409001(async_client, b2_rest, b2_runtime, monkeypatch):
    monkeypatch.setattr(fast_graph, "SEARCH_DELAY_S", 2.0)
    post1 = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "进行中", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})

        # 缺省策略：快照 turn.default_on_conflict=reject → 409001
        body = _assert_error(
            await async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "并发提问缺省策略", "mode": "fast"},
            ),
            status_code=409,
            code="409001",
        )
        assert body["message"] == "线程已有进行中轮次"
        # 显式 reject 同码
        _assert_error(
            await async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "并发提问显式 reject", "mode": "fast", "on_conflict": "reject"},
            ),
            status_code=409,
            code="409001",
        )
        # 零残留：REJECT 的 pending 轮随 get_db 回滚，线程仅剩原轮
        detail = _assert_ok_envelope(await async_client.get(f"/api/v1/threads/{thread_id}"))
        assert detail["turn_count"] == 1
        assert [t["turn_id"] for t in detail["turns"]] == [turn1]
    finally:
        post1_resp = await asyncio.wait_for(post1, timeout=30)
    assert post1_resp.json()["data"]["status"] == "running"
    await b2_rest.wait_turn_status(turn1, {"completed"})


# ---- §0 四策略：enqueue 按提交序执行（闭环④之二）----


@pytest.mark.requires_db
async def test_enqueue_strategy_fifo(async_client, b2_rest, b2_runtime, wait_for, monkeypatch):
    monkeypatch.setattr(fast_graph, "SEARCH_DELAY_S", 1.0)
    post1 = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "第一轮", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})
        post2 = asyncio.create_task(
            async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "第二轮排队", "mode": "fast", "on_conflict": "enqueue"},
            )
        )
        try:
            turn2: str | None = None

            async def _second_visible() -> bool:
                nonlocal turn2
                detail = await async_client.get(f"/api/v1/threads/{thread_id}")
                if detail.status_code != 200:
                    return False
                turns = detail.json()["data"]["turns"]
                if len(turns) < 2:
                    return False
                turn2 = turns[1]["turn_id"]
                return True

            await wait_for(_second_visible, description="第二轮次行提交可见")
            assert turn2 is not None
            await b2_rest.wait_turn_status(turn2, {"queued"})
        finally:
            post2_resp = await asyncio.wait_for(post2, timeout=30)
        assert post2_resp.json()["data"]["status"] == "queued"
    finally:
        post1_resp = await asyncio.wait_for(post1, timeout=30)
    assert post1_resp.json()["data"]["status"] == "running"

    detail1 = await b2_rest.wait_turn_status(turn1, {"completed"})
    detail2 = await b2_rest.wait_turn_status(turn2, {"completed"})
    # 按提交序先后执行完：done1.seq < turn2 首事件 seq（线程级单调 seq 即全局序）
    events1 = await _db_turn_events(turn1)
    events2 = await _db_turn_events(turn2)
    done1_seq = events1[-1].seq
    assert done1_seq < min(e.seq for e in events2)
    # 排队轮真实执行过（有 stage 事件）且 finishing 晚于第一轮
    assert any(e.event_type == "stage" for e in events2)
    assert detail1["finished_at"] <= detail2["finished_at"]


# ---- §0 四策略：interrupt（闭环④之三：canceled+superseded、无 error 事件）----


@pytest.mark.requires_db
async def test_interrupt_strategy(async_client, b2_rest, b2_runtime, wait_for, monkeypatch):
    await _assert_break_strategy(
        async_client,
        b2_rest,
        monkeypatch,
        wait_for,
        on_conflict="interrupt",
        expected_reason="superseded",
    )


# ---- §0 四策略：rollback（闭环④之四：canceled+rolled_back）----


@pytest.mark.requires_db
async def test_rollback_strategy(async_client, b2_rest, b2_runtime, wait_for, monkeypatch):
    await _assert_break_strategy(
        async_client,
        b2_rest,
        monkeypatch,
        wait_for,
        on_conflict="rollback",
        expected_reason="rolled_back",
    )


async def _assert_break_strategy(
    async_client, b2_rest, monkeypatch, wait_for, *, on_conflict: str, expected_reason: str
) -> None:
    """interrupt/rollback 共用断言：旧轮 canceled+cancel_reason 落库、无 error 事件、
    新轮在旧轮终态后启动并完成。"""
    monkeypatch.setattr(fast_graph, "SEARCH_DELAY_S", 2.0)
    post1 = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "被打断的问题", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})
        post2 = asyncio.create_task(
            async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "替换的新问题", "mode": "fast", "on_conflict": on_conflict},
            )
        )
        try:
            turn2: str | None = None

            async def _second_visible() -> bool:
                nonlocal turn2
                detail = await async_client.get(f"/api/v1/threads/{thread_id}")
                if detail.status_code != 200:
                    return False
                turns = detail.json()["data"]["turns"]
                if len(turns) < 2:
                    return False
                turn2 = turns[1]["turn_id"]
                return True

            await wait_for(_second_visible, description="第二轮次行提交可见")
            assert turn2 is not None
            await b2_rest.wait_turn_status(turn2, {"queued"})
        finally:
            post2_resp = await asyncio.wait_for(post2, timeout=30)
        # INTERRUPT/ROLLBACK → 新轮先 queued（api.md §0 打断时序：旧轮终态后启动）
        assert post2_resp.json()["data"]["status"] == "queued"
    finally:
        post1_resp = await asyncio.wait_for(post1, timeout=30)
    assert post1_resp.json()["data"]["status"] == "running"

    old_detail = await b2_rest.wait_turn_status(turn1, {"canceled"})
    assert old_detail["cancel_reason"] == expected_reason
    old_events = await _db_turn_events(turn1)
    old_types = [e.event_type for e in old_events]
    assert "error" not in old_types  # 打断非故障，不产生 error 事件
    done = next(e for e in old_events if e.event_type == "done")
    assert done.payload["finish"] == "canceled"
    assert done.payload["cancel_reason"] == expected_reason

    new_detail = await b2_rest.wait_turn_status(turn2, {"completed"})
    assert new_detail["query"] == "替换的新问题"
    new_events = await _db_turn_events(turn2)
    # 新轮在被打断轮到达终态后启动：旧轮 done.seq < 新轮首事件 seq
    assert done.seq < min(e.seq for e in new_events)


# ---- §0 四策略：interrupt 与既有 queued 并存（裁决 A：新轮插队）----


@pytest.mark.requires_db
async def test_interrupt_with_queued_new_turn_preempts_queue(
    async_client, b2_rest, b2_runtime, wait_for, monkeypatch
):
    """裁决 A（2026-11-08，评审 P2 次序澄清）：running+queued 时 interrupt——
    被打断轮 canceled 后执行槽归 interrupt 新轮（「不经排队」），既有 queued 轮
    顺延到抢占轮终态之后。started_at 实证次序：抢占轮先于既有 queued 启动。"""
    monkeypatch.setattr(fast_graph, "SEARCH_DELAY_S", 2.0)

    async def _turn_at(thread_id: str, index: int) -> str | None:
        detail = await async_client.get(f"/api/v1/threads/{thread_id}")
        if detail.status_code != 200:
            return None
        turns = detail.json()["data"]["turns"]
        return turns[index]["turn_id"] if len(turns) > index else None

    post1 = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "运行中的问题", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})
        # 既有 queued 轮（enqueue 占位；created_at 早于抢占轮）。不在此 await
        # post2——其 BackgroundTask 要等抢占轮跑完才轮到它（裁决 A 次序），
        # 先 await 会把整个时序推到线程空闲后再提交 interrupt。
        post2 = asyncio.create_task(
            async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "先排队的问题", "mode": "fast", "on_conflict": "enqueue"},
            )
        )
        try:
            await wait_for(
                lambda: _turn_at(thread_id, 1), description="既有 queued 轮行提交可见"
            )
            turn2 = await _turn_at(thread_id, 1)
            assert turn2 is not None
            await b2_rest.wait_turn_status(turn2, {"queued"})
            # interrupt 新轮（抢占轮；打断 turn1，插队启动）——须在 turn1 运行
            # 窗口内提交（SEARCH_DELAY_S=2.0 的运行窗口足够轮询完成）
            post3 = asyncio.create_task(
                async_client.post(
                    f"/api/v1/threads/{thread_id}/turns",
                    json={"query": "插队的新问题", "mode": "fast", "on_conflict": "interrupt"},
                )
            )
            try:
                await wait_for(
                    lambda: _turn_at(thread_id, 2), description="抢占轮行提交可见"
                )
                turn3 = await _turn_at(thread_id, 2)
                assert turn3 is not None
                await b2_rest.wait_turn_status(turn3, {"queued"})
            finally:
                resp3 = await asyncio.wait_for(post3, timeout=30)
            assert resp3.json()["data"]["status"] == "queued"
        finally:
            resp2 = await asyncio.wait_for(post2, timeout=30)
        assert resp2.json()["data"]["status"] == "queued"
    finally:
        resp1 = await asyncio.wait_for(post1, timeout=30)
    assert resp1.json()["data"]["status"] == "running"

    # 终态：被打断轮 canceled(superseded)；抢占轮与既有 queued 轮先后跑完
    old_detail = await b2_rest.wait_turn_status(turn1, {"canceled"})
    assert old_detail["cancel_reason"] == "superseded"
    preempt_detail = await b2_rest.wait_turn_status(turn3, {"completed"})
    queued_detail = await b2_rest.wait_turn_status(turn2, {"completed"})
    # 次序实证（裁决 A）：抢占轮提交更晚（created_at 更大）却先完成
    # （finished_at 更小）——旧口径（新轮按提交序排在既有 queued 之后）下
    # finished_at 断言反向失败
    assert preempt_detail["created_at"] > queued_detail["created_at"]  # 提交序自证
    assert preempt_detail["finished_at"] < queued_detail["finished_at"]


# ---- §2.7：终态轮取消 → 409002 ----


@pytest.mark.requires_db
async def test_cancel_terminal_turn_409002(async_client, b2_rest, b2_runtime):
    data = _assert_ok_envelope(
        await async_client.post(
            "/api/v1/threads", json={"query": "先跑完再取消", "mode": "fast"}
        )
    )
    await b2_rest.wait_turn_status(data["turn_id"], {"completed"})
    body = _assert_error(
        await async_client.post(f"/api/v1/turns/{data['turn_id']}/cancel"),
        status_code=409,
        code="409002",
    )
    assert body["message"] == "轮次已处于终态，不可取消"
