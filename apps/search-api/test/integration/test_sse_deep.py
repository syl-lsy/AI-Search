"""integration：deep SSE 全链路联调闭环（M2 批次 5——对标 test_sse_s2.py 七项骨架）。

真库 + 真 EventBus（b2_runtime）+ 进程内 ASGI 真流（同 test_sse 口径）；deep 分流
走脚本化假图（stub_flow.DeepFakeScriptedGraph——按 api.md §7.4 深度主干产事件，
零 LLM/零真源/零网络；fast 分流回落既有 FakeScriptedGraph 保形）。

七项闭环对应（用例名「闭环①」~「闭环⑦」）：
- ① stage 序列（api.md §7.4 深度主干：plan → search → sources → read →
  reflect⇄search → write → done；plan running 归 TurnRunner 图前发布）；
- ② sources 分批 ref_no 首现冻结（批次只增不改；snapshot_available false→true
  以终态 §2.5 为准——快照回填链）；
- ③ extra.subagents（§7.2 键面 {subtopic, status}；枚举 running|done|failed；
  同 key 重复发射单调推进）；
- ④ context_compact 重放边界（§7.2/§7.3 v1.3 墹注：Last-Event-ID 早于最近一次
  compact → 服务端自该事件起重放；compact publish-only 不入 turn_events）；
- ⑤ 断线重连补帧（Last-Event-ID=断点 seq → 补帧无缺无重 + 跟随至 done；
  ASGITransport 全量缓冲下以「首连流前缀游标」模拟断点——深度刷新重连续传形态）；
- ⑥ done↔REST 对账（usage / degraded_reason 双形态：completed 收束 +
  guards 熔断 degraded 收束〔零 error〕）；
- ⑦ 取消链路（done finish=canceled, cancel_reason=user 零 error、不进降级）。

文件名注记：任务稿命名 test_deep_s2.py，上轮门禁以 apps/search-api/test/integration/
test_sse_deep.py 路径收集（test_sse_s2.py 同族命名）——本文件即该套件（内容逐字
平移，含门禁修复），test_deep_s2.py 已删除不再存在。
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from test.integration import stub_flow
from test.integration.conftest import TURN_COMPLETION_TIMEOUT_S
from test.integration.test_sse import (
    _assert_sse_envelope,
    _assert_stream_headers,
    parse_sse_frames,
)
from app.core.config import common_settings
from app.db.models import Snapshot, TurnEvent
from app.db.session import get_session_factory

_DEEP_QUERY = "deep SSE 联调：LangGraph 子图边界与事件冒泡的关系？"

#: §7.2 extra.subagents[].status 枚举（api.md §7.2 钉死三值）
_SUBAGENT_STATUS_ENUM = {"running", "done", "failed"}


async def _db_turn_events_rows(turn_id: str) -> list[TurnEvent]:
    """该轮 turn_events 落库行（seq 升序）——ADR-003 第 4 条子集断言的数据源。"""
    async with get_session_factory()() as session:
        rows = (
            await session.scalars(
                select(TurnEvent)
                .where(TurnEvent.turn_id == uuid.UUID(turn_id))
                .order_by(TurnEvent.seq.asc())
            )
        ).all()
        return list(rows)


@pytest.fixture
def deep_fake_graph(b2_runtime, monkeypatch) -> stub_flow.DeepFakeScriptedGraph:
    """deep 分流换脚本化假图：调度器构造期已捕获 main_graph 引用（ThreadScheduler
    __init__ `graph if graph is not None else main_graph`）——直换实例属性
    `_graph`（monkeypatch 回滚恢复 conftest 假体包装）。"""
    fake = stub_flow.DeepFakeScriptedGraph()
    monkeypatch.setattr(b2_runtime.scheduler, "_graph", fake)
    return fake


def _post_deep(async_client, query: str = _DEEP_QUERY) -> Any:
    """POST 建深度轮（BackgroundTask 在 ASGITransport 下全程驱动——调用方 create_task）。"""
    return async_client.post("/api/v1/threads", json={"query": query, "mode": "deep"})


def _stage_status_list(frames: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """stage/sources 业务帧的 (stage_key|event, status) 相对序提取（compact/token 剔除）。"""
    out: list[tuple[str, str]] = []
    for frame in frames:
        if frame["event"] == "stage":
            out.append((str(frame["data"].get("stage_key")), str(frame["data"].get("status"))))
        elif frame["event"] == "sources":
            out.append(("sources", ""))
    return out


async def _seed_snapshot_row(turn_id: str, thread_id: str, url: str, snapshot_id: uuid.UUID) -> None:
    """播种 snapshots 宿主行（Source.snapshot_id FK 指向 snapshots.id——回填约束面）。"""
    async with get_session_factory()() as session:
        session.add(
            Snapshot(
                id=snapshot_id,
                user_id=uuid.UUID(common_settings.default_user_id),
                thread_id=uuid.UUID(thread_id),
                turn_id=uuid.UUID(turn_id),
                url=url,
                url_hash="h" * 64,
                object_key=f"snapshots/it/{url}",
                content_type="text/markdown",
                size_bytes=2048,
                fetched_at=datetime.now(UTC),
            )
        )
        await session.commit()


# ---- 闭环①：stage 序列（api §7.4 深度主干）----


@pytest.mark.requires_db
async def test_deep_stage_sequence_s74(async_client, b2_rest, b2_runtime, deep_fake_graph):
    """§7.4 深度主干相对序：plan(running 图前) → plan(done,extra.subtopics) →
    search(running,extra.subagents) → sources×2 → read(running) → [context_compact] →
    read(done,progress) → reflect(running) → search(running,supplement) → sources →
    reflect(done) → write(running) → token…token → write(done,extra.sections) →
    done(completed)；零 error；compact 落 research 段且 publish-only 不入 turn_events。"""
    resp = await _post_deep(async_client)
    assert resp.status_code == 200
    turn_id = resp.json()["data"]["turn_id"]
    detail = await b2_rest.wait_turn_status(turn_id, {"completed"}, timeout_s=TURN_COMPLETION_TIMEOUT_S)

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    _assert_stream_headers(stream)
    frames, _ = parse_sse_frames(stream.text)
    assert frames, "deep 桩轮应产出业务帧"
    for frame in frames:
        _assert_sse_envelope(frame, turn_id)
    ids = [f["id"] for f in frames]
    assert ids == list(range(ids[0], ids[0] + len(ids)))  # seq 线程级单调连续
    assert "error" not in [f["event"] for f in frames]

    # 首事件=stage(plan, running)——TurnRunner 图前发布（§4.3 首个 stage ≤1s）
    assert frames[0]["event"] == "stage"
    assert frames[0]["data"]["stage_key"] == "plan" and frames[0]["data"]["status"] == "running"

    # §7.4 深度主干（stage/sources 相对序逐帧核对；token/thinking/context_compact 剔除）：
    # plan running(图前) → plan done → search running(入口) → sources×3 → search
    # running×2（子任务状态推进——§7.2 同 key 重复发射）→ read → compact → read done →
    # reflect running → search running(supplement 段) → sources(补搜批次) → reflect done →
    # write running → write done
    sequence = _stage_status_list(frames)
    expected = [
        ("plan", "running"),
        ("plan", "done"),
        ("search", "running"),
        ("sources", ""),
        ("sources", ""),
        ("sources", ""),
        ("search", "running"),  # 状态推进（首项 done）
        ("search", "running"),  # 状态推进（终帧全 done）
        ("read", "running"),
        ("read", "done"),
        ("reflect", "running"),
        ("search", "running"),  # supplement 段（§7.4:875 相对序）
        ("sources", ""),  # 补搜增量批次
        ("reflect", "done"),
        ("write", "running"),
        ("write", "done"),
    ]
    assert sequence == expected, f"§7.4 深度主干相对序不符：{sequence}"

    # plan done 携带 extra.subtopics；write done 携带 extra.sections（finalize 收束帧）
    plan_done = frames[1]
    assert isinstance(plan_done["data"].get("extra", {}).get("subtopics"), int)
    write_done = next(
        f for f in frames
        if f["event"] == "stage" and f["data"].get("stage_key") == "write" and f["data"].get("status") == "done"
    )
    assert write_done["data"]["extra"]["sections"] >= 1
    write_running = next(
        f for f in frames
        if f["event"] == "stage" and f["data"].get("stage_key") == "write" and f["data"].get("status") == "running"
    )
    assert write_running["data"]["label"] == "撰写第 1 节"  # §7.4:876 契约 label

    # token 全部落 write running→done 之间（writer messages 流唯一 token 面）；
    # 拼接 == §2.5 answer_md（假图 DEEP_FAKE_TOKEN_DELTAS 基准）
    write_run_seq = write_running["id"]
    write_done_seq = write_done["id"]
    tokens = [f for f in frames if f["event"] == "token"]
    assert tokens, "deep writer token 事件（messages 流域）"
    assert all(write_run_seq < f["id"] < write_done_seq for f in tokens)
    assert "".join(f["data"]["delta"] for f in tokens) == stub_flow.DEEP_FAKE_ANSWER_MD
    assert detail["answer_md"] == stub_flow.DEEP_FAKE_ANSWER_MD

    # context_compact：恰一次、落 research/reflect 段（search running 与 write running 间）、
    # §7.2 四键钉面、publish-only 不入 turn_events
    compacts = [f for f in frames if f["event"] == "context_compact"]
    assert len(compacts) == 1
    compact = compacts[0]
    search_run_seq = next(f["id"] for f in frames if f["event"] == "stage" and f["data"].get("stage_key") == "search")
    assert search_run_seq < compact["id"] < write_run_seq
    assert set(stub_flow.DEEP_FAKE_CONTEXT_COMPACT) <= set(compact["data"])
    assert isinstance(compact["data"]["offloaded_files"], list)
    rows = await _db_turn_events_rows(turn_id)
    row_types = {r.event_type for r in rows}
    assert "context_compact" not in row_types and "token" not in row_types  # ADR-003 第 4 条子集
    assert rows[-1].event_type == "done" and rows[-1].payload["finish"] == "completed"
    # 流 ⊇ 落库子集（逐字段核对于闭环⑥；此处锁子集关系）
    by_seq = {f["id"]: f for f in frames}
    for row in rows:
        assert row.seq in by_seq

    # done 帧 ↔ REST 终态（对账详表在闭环⑥）
    assert frames[-1]["event"] == "done" and frames[-1]["data"]["finish"] == "completed"


# ---- 闭环②：sources 分批 ref_no 首现冻结 + snapshot_available false→true ----


@pytest.mark.requires_db
async def test_deep_sources_batches_refno_frozen(async_client, b2_rest, b2_runtime, deep_fake_graph, monkeypatch):
    """§7.2 sources：批次只增不改——URL 首现即冻结 ref_no（1..N 连续递增、无 URL
    换号）；snapshot_available 在事件面 false、快照回填后终态 §2.5 翻真（「以终态
    2.5 为准」）；done.source_count == 批次累计 == §2.5 sources 行数。"""
    monkeypatch.setattr(stub_flow, "FAKE_DEEP_DELAY_S", 2.0)  # 回填窗口（种子须先于完成）
    monkeypatch.setattr(stub_flow, "FAKE_DEEP_SNAPSHOTS", [
        {
            "url": stub_flow.DEEP_FAKE_SOURCE_URLS[0],
            "object_key": f"snapshots/it/{stub_flow.DEEP_FAKE_SOURCE_URLS[0]}",
            "size_bytes": 2048,
            "snapshot_id": str(uuid.uuid5(uuid.NAMESPACE_URL, stub_flow.DEEP_FAKE_SOURCE_URLS[0])),
        }
    ])
    post_task = asyncio.create_task(_post_deep(async_client))
    try:
        thread_id, turn_id = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn_id, {"running"}, timeout_s=10)
        # 快照宿主行（FK 宿主——须先于终态回填；运行窗口 ≥12s 充裕）
        await _seed_snapshot_row(
            turn_id,
            thread_id,
            stub_flow.DEEP_FAKE_SOURCE_URLS[0],
            uuid.uuid5(uuid.NAMESPACE_URL, stub_flow.DEEP_FAKE_SOURCE_URLS[0]),
        )
        detail = await b2_rest.wait_turn_status(turn_id, {"completed"}, timeout_s=TURN_COMPLETION_TIMEOUT_S)
    finally:
        await asyncio.gather(post_task, return_exceptions=True)

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _ = parse_sse_frames(stream.text)
    source_frames = [f for f in frames if f["event"] == "sources"]
    assert len(source_frames) == 4  # search 段 batch1-3 + supplement batch4

    # ref_no 首现冻结：URL→ref_no 双射、首现序 1..4 连续、批次只增不改（无换号）
    url_to_ref: dict[str, int] = {}
    first_refs: list[int] = []
    for frame in source_frames:
        for item in frame["data"]["batch"]:
            url = item["url"]
            ref = item["ref_no"]
            if url in url_to_ref:
                assert url_to_ref[url] == ref  # 复现不改号
            else:
                url_to_ref[url] = ref
                first_refs.append(ref)
    assert first_refs == [1, 2, 3, 4]
    assert sorted(url_to_ref.values()) == [1, 2, 3, 4]
    assert set(url_to_ref) == set(stub_flow.DEEP_FAKE_SOURCE_URLS)

    # done.source_count == 批次条数累计 == §2.5 sources 行数
    done = frames[-1]
    assert done["event"] == "done"
    assert done["data"]["source_count"] == 4 == len(detail["sources"])
    # §2.5 与事件面 ref_no↔url 同映射；事件面 snapshot_available=false、终态翻真
    rest_by_url = {s["url"]: s for s in detail["sources"]}
    assert {url: s["ref_no"] for url, s in rest_by_url.items()} == url_to_ref
    assert all(
        item["snapshot_available"] is False
        for frame in source_frames
        for item in frame["data"]["batch"]
    )
    assert rest_by_url[stub_flow.DEEP_FAKE_SOURCE_URLS[0]]["snapshot_available"] is True
    assert rest_by_url[stub_flow.DEEP_FAKE_SOURCE_URLS[1]]["snapshot_available"] is False


# ---- 闭环③：extra.subagents 键面与枚举（§7.2）----


@pytest.mark.requires_db
async def test_deep_stage_extra_subagents(async_client, b2_rest, b2_runtime, deep_fake_graph):
    """§7.2 extra.subagents[]：search running 帧专用——键面恰 {subtopic, status}、
    status ∈ running|done|failed、同 key 重复发射下单 subtopic 状态单调推进
    （running→done 无回退）、终帧全 done。"""
    resp = await _post_deep(async_client)
    turn_id = resp.json()["data"]["turn_id"]
    await b2_rest.wait_turn_status(turn_id, {"completed"}, timeout_s=TURN_COMPLETION_TIMEOUT_S)

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _ = parse_sse_frames(stream.text)
    search_frames = [
        f for f in frames
        if f["event"] == "stage" and f["data"].get("stage_key") == "search"
    ]
    assert search_frames, "deep 桩轮应有 search 段帧"
    running_frames = [f for f in search_frames if f["data"]["status"] == "running"]
    assert running_frames, "search(running) 帧在场（§7.4 入口帧）"
    with_extra = [f for f in running_frames if "subagents" in f["data"].get("extra", {})]
    assert with_extra, "search(running) 携带 extra.subagents（入口帧）"

    seen: dict[str, list[str]] = {}
    for frame in with_extra:
        subagents = frame["data"]["extra"]["subagents"]
        assert isinstance(subagents, list) and subagents
        for item in subagents:
            assert set(item) == {"subtopic", "status"}  # §7.2 钉死键面
            assert item["status"] in _SUBAGENT_STATUS_ENUM  # 枚举 running|done|failed
            seen.setdefault(item["subtopic"], []).append(item["status"])

    assert set(seen) == set(stub_flow.DEEP_FAKE_SUBTOPICS)
    for statuses in seen.values():
        # 单调推进：running…→done（无回退、无未知值）
        assert statuses == sorted(statuses, key=lambda s: {"running": 0, "done": 1, "failed": 2}[s])
        assert statuses[-1] == "done"
    # 终帧全 done（research 收口面）
    final = with_extra[-1]["data"]["extra"]["subagents"]
    assert all(item["status"] == "done" for item in final)


# ---- 闭环④：context_compact 重放边界（§7.2/§7.3 v1.3 增注）----


@pytest.mark.requires_db
async def test_context_compact_replay_boundary(async_client, b2_rest, b2_runtime, deep_fake_graph):
    """Last-Event-ID 早于最近一次 context_compact → 旧游标失效：服务端**自该事件起**
    重放（压缩前内容不再重放——以 §2.3/§2.5 对账）；游标 ≥ compact seq → 既有语义
    （自游标后正常补帧）；首连（无 header）恒全量重放（§7.1——边界只作用于显式
    数字游标）。"""
    resp = await _post_deep(async_client)
    turn_id = resp.json()["data"]["turn_id"]
    await b2_rest.wait_turn_status(turn_id, {"completed"}, timeout_s=TURN_COMPLETION_TIMEOUT_S)

    # 首连（无 header）：全量重放——压缩前帧在场（边界不作用于首连语义）
    baseline = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _ = parse_sse_frames(baseline.text)
    compact_idx = next(i for i, f in enumerate(frames) if f["event"] == "context_compact")
    compact = frames[compact_idx]
    compact_seq = compact["id"]
    assert compact_idx >= 3  # 压缩前帧（plan/search/sources 段）在首连流中
    assert frames[0]["event"] == "stage" and frames[0]["id"] < compact_seq
    pre_seq = frames[0]["id"]  # 游标早于 compact（首帧）

    # 重连携带早于 compact 的数字游标：旧游标失效——自 compact 事件起重放
    replayed = await async_client.get(
        f"/api/v1/turns/{turn_id}/events", headers={"Last-Event-ID": str(pre_seq)}
    )
    rframes, _ = parse_sse_frames(replayed.text)
    assert rframes, "早于 compact 的游标仍须补帧"
    assert rframes[0]["event"] == "context_compact" and rframes[0]["id"] == compact_seq
    rids = [f["id"] for f in rframes]
    assert rids == list(range(compact_seq, compact_seq + len(rids)))
    assert rframes[-1]["event"] == "done"

    # 游标 == compact seq：不做边界钳制（正常自 compact+1 补帧——既有语义）
    after = await async_client.get(
        f"/api/v1/turns/{turn_id}/events", headers={"Last-Event-ID": str(compact_seq)}
    )
    aframes, _ = parse_sse_frames(after.text)
    assert aframes[0]["id"] == compact_seq + 1

    # 非法游标（非数字）：等同首次连接（§7.1 513 行宽路径）——零钳制全量重放
    invalid = await async_client.get(
        f"/api/v1/turns/{turn_id}/events", headers={"Last-Event-ID": "not-a-number"}
    )
    assert invalid.text == baseline.text  # 逐字节一致：全量重放


# ---- 闭环⑤：断线重连补帧（A13 §7.3 第 2 条）----


@pytest.mark.requires_db
async def test_reconnect_replays_missing_frames_then_follows(
    async_client, b2_rest, b2_runtime, deep_fake_graph, monkeypatch
):
    """断线重连补帧（§7.3 第 2/3 条）：客户端持前缀游标（首连流 compact 后第 2 帧
    ——write 段中断线的常态位置）→ 以 Last-Event-ID=断点 seq 重连 → 服务端补帧自
    断点+1 起、无缺无重、跟随至 done(completed)；补帧与首连流逐帧 event/payload
    对齐。早于 compact 的游标失效形态归闭环④（重放边界）。

    实现注记（如实）：httpx ASGITransport 0.28 全量缓冲（app 跑完才返回 body），
    进程内传输不支持真中途断连——断点以「首连流截取前缀游标」模拟；服务端补帧
    语义（EventBus.subscribe 重放 seq > cursor + SSE 端点跟随至终态）为真管道面。
    真浏览器 EventSource 自动重连带 Last-Event-ID 的端到端行为归批次 5 live 探针
    （fast 面已实证，A13）。"""
    resp = await _post_deep(async_client)
    turn_id = resp.json()["data"]["turn_id"]
    detail = await b2_rest.wait_turn_status(turn_id, {"completed"}, timeout_s=TURN_COMPLETION_TIMEOUT_S)
    assert detail["status"] == "completed"

    # 首连流（全量）——断点取 compact 之后第 2 帧（write 段前：reflect running），
    # 模拟客户端断线前已处理至该 seq（重连游标晚于 compact——补帧=断点+1 起既有语义）
    stream_a = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    _assert_stream_headers(stream_a)
    frames_a, _ = parse_sse_frames(stream_a.text)
    compact_idx = next(i for i, f in enumerate(frames_a) if f["event"] == "context_compact")
    assert compact_idx + 2 < len(frames_a) - 1
    last_seq = frames_a[compact_idx + 2]["id"]

    # 重连（Last-Event-ID=断点）：补帧自断点+1 起、无缺无重、终帧 done
    stream_b = await async_client.get(
        f"/api/v1/turns/{turn_id}/events", headers={"Last-Event-ID": str(last_seq)}
    )
    _assert_stream_headers(stream_b)
    frames_b, _ = parse_sse_frames(stream_b.text)
    assert frames_b, "重连应补帧"
    ids_b = [f["id"] for f in frames_b]
    assert ids_b[0] == last_seq + 1  # 自断点+1 起
    assert ids_b == list(range(ids_b[0], ids_b[0] + len(ids_b)))  # 补帧无缺无重
    assert frames_b[-1]["event"] == "done" and frames_b[-1]["data"]["finish"] == "completed"
    # 前缀（≤断点）∪ 补帧 = 首连流全域（断线不丢帧、重连不重复）
    prefix = frames_a[: compact_idx + 3]  # 断点帧及其之前
    ids_prefix = [f["id"] for f in prefix]
    done_seq = frames_a[-1]["id"]
    assert ids_prefix == list(range(ids_prefix[0], last_seq + 1))
    assert ids_b == list(range(last_seq + 1, done_seq + 1))
    # 补帧内容与首连流对应帧逐字段对齐（event / 去信封 payload）
    tail_a = {f["id"]: f for f in frames_a}
    envelope_keys = {"seq", "turn_id", "ts"}
    for frame in frames_b:
        source = tail_a[frame["id"]]
        assert frame["event"] == source["event"]
        assert {k: v for k, v in frame["data"].items() if k not in envelope_keys} == {
            k: v for k, v in source["data"].items() if k not in envelope_keys
        }
    # 落库子集 ⊆ 前缀 ∪ 补帧（断线不丢事件）
    rows = await _db_turn_events_rows(turn_id)
    streamed = set(ids_prefix) | set(ids_b)
    for row in rows:
        assert row.seq in streamed


# ---- 闭环⑥：done↔REST 对账（usage / degraded_reason 双形态）----


@pytest.mark.requires_db
async def test_done_rest_reconciliation_usage_completed(async_client, b2_rest, b2_runtime, deep_fake_graph):
    """completed 收束：done 帧 ↔ GET /turns 逐项对账——usage 三键（假图经
    collector.on_llm_end 注入——M2 批次 1 方案 A 消费面）、degraded_reason=None、
    duration_ms/source_count；turn_events 落库子集逐字段相等（不抽样）。"""
    resp = await _post_deep(async_client)
    turn_id = resp.json()["data"]["turn_id"]
    detail = await b2_rest.wait_turn_status(turn_id, {"completed"}, timeout_s=TURN_COMPLETION_TIMEOUT_S)

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _ = parse_sse_frames(stream.text)
    rows = await _db_turn_events_rows(turn_id)
    by_seq = {f["id"]: f for f in frames}
    for row in rows:
        frame = by_seq[row.seq]
        assert frame["event"] == row.event_type
        envelope_keys = {"seq", "turn_id", "ts"}
        assert {k: v for k, v in frame["data"].items() if k not in envelope_keys} == row.payload

    done = by_seq[rows[-1].seq]
    assert done["event"] == "done" and done["data"]["finish"] == "completed"
    assert detail["status"] == "completed"
    assert done["data"]["degraded"] is False and detail["degraded"] is False
    assert done["data"]["degraded_reason"] is None and detail["degraded_reason"] is None
    assert done["data"]["duration_ms"] == detail["duration_ms"]
    assert done["data"]["source_count"] == len(detail["sources"])
    # usage 对账：done.usage == collector 累计（假图注入值）== §2.5 usage 非空键
    expected_usage = {
        "prompt_tokens": stub_flow.FAKE_DEEP_USAGE["input_tokens"],
        "completion_tokens": stub_flow.FAKE_DEEP_USAGE["output_tokens"],
        "total_tokens": stub_flow.FAKE_DEEP_USAGE["total_tokens"],
    }
    assert done["data"]["usage"] == expected_usage
    detail_usage = {k: v for k, v in detail["usage"].items() if v is not None}
    assert detail_usage == expected_usage


@pytest.mark.requires_db
@pytest.mark.parametrize(("reason",), [("budget",), ("timeout",)])
async def test_done_rest_reconciliation_guard_degraded(
    async_client, b2_rest, b2_runtime, deep_fake_graph, monkeypatch, reason
):
    """guards 熔断降级收束（§7.2:845 v1.21 注）：done(finish=degraded,
    degraded=True, degraded_reason=timeout|budget) 零 error——降级报告路径语义
    （degraded_writer 同构：fallback_token 整块 + write done 裸帧）；REST
    status=degraded / degraded_reason 同值 / answer_md=降级全文。"""
    monkeypatch.setattr(stub_flow, "FAKE_DEEP_DEGRADED_REASON", reason)
    resp = await _post_deep(async_client)
    turn_id = resp.json()["data"]["turn_id"]
    detail = await b2_rest.wait_turn_status(turn_id, {"degraded"}, timeout_s=TURN_COMPLETION_TIMEOUT_S)
    assert detail["status"] == "degraded" and detail["degraded_reason"] == reason
    assert detail["answer_md"] == stub_flow.DEEP_FAKE_DEGRADED_DIGEST  # 降级产出非空持久化

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _ = parse_sse_frames(stream.text)
    events = [f["event"] for f in frames]
    assert "error" not in events  # 图内熔断零 error（降级报告路径）
    done = frames[-1]
    assert done["event"] == "done"
    assert done["data"]["finish"] == "degraded"
    assert done["data"]["degraded"] is True
    assert done["data"]["degraded_reason"] == reason
    assert done["data"]["source_count"] == len(detail["sources"]) == 4
    assert done["data"]["duration_ms"] == detail["duration_ms"]
    # 降级 token=单事件整块（fallback_token → token publish-only）+ write done 裸帧
    write_done = next(
        f for f in frames
        if f["event"] == "stage" and f["data"].get("stage_key") == "write" and f["data"].get("status") == "done"
    )
    assert "sections" not in (write_done["data"].get("extra") or {})  # 裸帧——两路径判别面
    tokens = [f for f in frames if f["event"] == "token"]
    assert [f["data"]["delta"] for f in tokens] == [stub_flow.DEEP_FAKE_DEGRADED_DIGEST]


# ---- 闭环⑦：取消链路（done finish=canceled 零 error、不进降级）----


@pytest.mark.requires_db
async def test_cancel_chain_deep_done_canceled_zero_error(
    async_client, b2_rest, b2_runtime, deep_fake_graph, monkeypatch
):
    """运行中显式取消（§2.7）：done(finish=canceled, cancel_reason=user) 零 error、
    degraded=False（取消非故障不进降级）；已产事件保留、seq 连续；REST status=
    canceled / cancel_reason=user 同源对账。"""
    monkeypatch.setattr(stub_flow, "FAKE_DEEP_DELAY_S", 1.5)
    post_task = asyncio.create_task(_post_deep(async_client))
    try:
        _, turn_id = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn_id, {"running"}, timeout_s=10)
        cancel_resp = await async_client.post(f"/api/v1/turns/{turn_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["data"]["status"] == "canceling"
        detail = await b2_rest.wait_turn_status(turn_id, {"canceled"}, timeout_s=30)
    finally:
        await asyncio.gather(post_task, return_exceptions=True)

    assert detail["status"] == "canceled" and detail["cancel_reason"] == "user"
    assert detail["degraded"] is False

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _ = parse_sse_frames(stream.text)
    events = [f["event"] for f in frames]
    assert "error" not in events  # 打断非故障（§7.2/§7.4 v1.4 注②）
    assert frames[0]["event"] == "stage"  # 已产事件保留（图前 plan running 在前）
    ids = [f["id"] for f in frames]
    assert ids == list(range(ids[0], ids[0] + len(ids)))
    done = frames[-1]
    assert done["event"] == "done"
    assert done["data"]["finish"] == "canceled"
    assert done["data"]["cancel_reason"] == "user"
    assert done["data"]["degraded"] is False and done["data"]["degraded_reason"] is None
    assert done["data"]["duration_ms"] == detail["duration_ms"]
