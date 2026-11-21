"""integration：regenerate 端点真库联装（B11 P2 / api.md §2.12 v1.19 / REQ-063 / A47）。

真依赖口径（.env.test 测试库；`migrated_db` upgrade head + 默认用户补种 + 用后截断；
`b2_runtime` 真 EventBus + ThreadScheduler，regenerate 微图走 stub_flow 假体——零 LLM）：

- 状态机：degraded → running → completed；再失败 → degraded（可再次 regenerate）；
  failed-with-sources（崩溃孤儿形态）同可重生成；
- 前置 409006 全前置面：completed / canceled / 零源 degraded / 零源 deep / 进行中
  重复点击（v1.28：deep 有源 degraded/failed 放开——deep 正形态专项用例）；
  归属 404001：不存在 / 越权 / 非法 UUID 同码；
- SSE 同通道续推（决策 11/15）：reopen 后旧事件重放 → stage(write,running) → token…
  → 第二个 done（seq 更大、finish=completed、source_count 沿该轮累计不变）；无
  stage(rewrite)/stage(search)/sources/images（不重检索）；
- 收尾口径（决策 14）：answer_md 原地更新、timing.stages 追加 write 段（不覆写）、
  duration_ms 不覆写、degraded_reason/error 清空；
- 取消联动：regenerate running 轮 cancel → canceled 收尾（done.cancel_reason=user）。

行种子走管理面 psycopg2 固定 SQL（标识符写死、值全参数绑定），不依赖主图运行。
"""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest

from app.agent.nodes.synthesis import SynthesisFallback
from app.core.config import common_settings
from app.services.event_bus import SseEvent
from app.services.turn_service import seed_seq_from_db
from test.integration import stub_flow
from test.integration.conftest import TURN_COMPLETION_TIMEOUT_S
from test.integration.test_sse import _assert_sse_envelope, parse_sse_frames

pytestmark = pytest.mark.requires_db

_THREAD_ID = uuid.UUID("7d2f0a1e-0000-4000-8000-0000000000c1")
_TURN_ID = uuid.UUID("c31b9d74-0000-4000-8000-0000000000c2")
_OTHER_USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000c3")
_OTHER_THREAD_ID = uuid.UUID("7d2f0a1e-0000-4000-8000-0000000000c4")
_OTHER_TURN_ID = uuid.UUID("c31b9d74-0000-4000-8000-0000000000c5")

_OLD_ANSWER = "旧答案（降级收束前的部分内容）[1]。"
_DURATION_MS = 5000
_OLD_STAGES: list[dict[str, Any]] = [
    {"stage_key": "rewrite", "status": "done", "label": "", "duration_ms": 500},
    {"stage_key": "search", "status": "done", "label": "", "duration_ms": 3000},
    {"stage_key": "write", "status": "done", "label": "综合中", "duration_ms": 1000},
]


def _connect(info: Any):
    return psycopg2.connect(
        host=info.host,
        port=int(info.port),
        user=info.user,
        password=info.password,
        dbname=info.name,
        connect_timeout=5,
    )


def _seed(
    info: Any,
    *,
    status: str = "degraded",
    mode: str = "fast",
    with_sources: bool = True,
    thread_id: uuid.UUID = _THREAD_ID,
    turn_id: uuid.UUID = _TURN_ID,
    user_id: str | None = None,
) -> None:
    """插入一条「上一轮已收束」的轮次（+ sources/turn_events）；值全参数绑定。"""
    owner = user_id or common_settings.default_user_id
    import json

    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, mode_summary, turn_count) "
                "VALUES (%s, %s, %s, %s, 1)",
                (str(thread_id), owner, "重生成测试线程", mode),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name, answer_md, timing, "
                "duration_ms, error, query_lang) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'qwen', 'chat-standard', "
                "%s, %s::jsonb, %s, %s::jsonb, 'zh')",
                (
                    str(turn_id),
                    str(thread_id),
                    owner,
                    mode,
                    status,
                    "原问题：pgvector 与 Milvus 怎么选？",
                    json.dumps(["改写检索 query"]),
                    _OLD_ANSWER,
                    json.dumps({"stages": _OLD_STAGES}),
                    _DURATION_MS,
                    json.dumps(
                        {
                            "code": "500101",
                            "message": "综合生成失败，已保留检索来源",
                            "stage_key": None,
                        }
                    )
                    if status in ("degraded", "failed")
                    else None,
                ),
            )
            if with_sources:
                for ref_no, url in (
                    (1, "https://example.com/a"),
                    (2, "https://example.com/b"),
                ):
                    cursor.execute(
                        "INSERT INTO sources (id, turn_id, user_id, ref_no, source_type, "
                        "title, url, snippet, score) "
                        "VALUES (%s, %s, %s, %s, 'web', %s, %s, %s, %s)",
                        (
                            str(uuid.uuid4()),
                            str(turn_id),
                            owner,
                            ref_no,
                            f"来源 {ref_no}",
                            url,
                            "片段",
                            0.9,
                        ),
                    )
            # 事件种子：seq 1..4（seed_seq_from_db 据此把线程计数器推进到 4）
            for seq, event_type, payload in (
                (1, "stage", {"stage_key": "rewrite", "status": "done", "extra": {"strategy": "web"}}),
                (2, "stage", {"stage_key": "search", "status": "done"}),
                (3, "stage", {"stage_key": "write", "status": "done", "label": "综合中"}),
                (
                    4,
                    "done",
                    {"finish": "degraded", "degraded": True, "source_count": 2},
                ),
            ):
                cursor.execute(
                    "INSERT INTO turn_events (thread_id, turn_id, seq, event_type, payload) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb)",
                    (str(thread_id), str(turn_id), seq, event_type, json.dumps(payload)),
                )
        conn.commit()
    finally:
        conn.close()


async def _seed_and_sync(info: Any, **kwargs: Any) -> None:
    """运行中补种行 + 同步 EventBus 线程 seq 基线（避免与库内历史撞 uq(thread,seq)）。"""
    _seed(info, **kwargs)
    await seed_seq_from_db()


@pytest.fixture
def degraded_turn(migrated_db) -> Iterator[Any]:
    """degraded 快速轮（2 来源 + 事件种子）；用后随 truncate 清理。"""
    _seed(migrated_db)
    yield migrated_db


@pytest.fixture
def completed_turn(migrated_db) -> Iterator[Any]:
    _seed(migrated_db, status="completed")
    yield migrated_db


# ---- 状态机：degraded → running → completed ----


async def test_regenerate_degraded_turn_completes_and_reconciles(
    degraded_turn: Any, async_client: Any, b2_runtime: Any, b2_rest: Any
) -> None:
    """§2.12/A47：POST 200 {turn_id,running}；成功收尾 answer_md 原地更新、
    stages 追加 write 段（不覆写）、duration_ms 不覆写、error/degraded_reason 清空。"""
    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0"
    assert body["data"] == {"turn_id": str(_TURN_ID), "status": "running"}

    detail = await b2_rest.wait_turn_status(str(_TURN_ID), {"completed"})

    assert detail["status"] == "completed"
    assert detail["answer_md"] == stub_flow.FAKE_REGENERATE_ANSWER_MD
    assert detail["degraded"] is False and detail["degraded_reason"] is None
    assert detail["error"] is None  # 旧失败留痕清除
    assert detail["duration_ms"] == _DURATION_MS  # 提交→首次 done 口径不覆写
    assert len(detail["sources"]) == 2  # 不重检索：来源与 [n] 映射不变
    assert detail["rewritten_queries"] == ["改写检索 query"]  # 不改写
    stages = detail["stages"]
    assert [entry["stage_key"] for entry in stages[:3]] == ["rewrite", "search", "write"]
    assert stages[-1]["stage_key"] == "write"  # 追加新 write 段
    assert len(stages) == len(_OLD_STAGES) + 1


async def test_regenerate_emits_second_done_on_same_channel(
    degraded_turn: Any, async_client: Any, b2_runtime: Any, b2_rest: Any
) -> None:
    """§7.1/§7.4：同通道 seq 续推——重放旧 done 后跟随新流，出现第二个 done
    （seq 更大、finish=completed、source_count 沿该轮累计不变）；无 rewrite/search/
    sources/images 事件（不重检索）。"""
    bus = b2_runtime.bus
    # 模拟「降级收束后连接已关」的旧尾部（内存缓冲；DB 行仅用于 seed seq）
    await bus.publish(
        str(_TURN_ID),
        SseEvent(
            thread_id=str(_THREAD_ID),
            event_type="stage",
            data={"stage_key": "write", "status": "done"},
        ),
    )
    await bus.publish(
        str(_TURN_ID),
        SseEvent(
            thread_id=str(_THREAD_ID),
            event_type="done",
            data={"finish": "degraded", "degraded": True, "source_count": 2},
        ),
    )

    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 200
    await b2_rest.wait_turn_status(str(_TURN_ID), {"completed"})

    stream = await async_client.get(f"/api/v1/turns/{_TURN_ID}/events")
    assert stream.status_code == 200
    frames, _ = parse_sse_frames(stream.text)

    for frame in frames:
        _assert_sse_envelope(frame, str(_TURN_ID))
    ids = [frame["id"] for frame in frames]
    assert ids == sorted(set(ids))  # seq 严格递增无重
    names = [frame["event"] for frame in frames]
    assert names.count("done") == 2  # 旧 done 重放 + 第二个 done
    assert names[-1] == "done"
    dones = [frame["data"] for frame in frames if frame["event"] == "done"]
    assert dones[0]["finish"] == "degraded"
    assert dones[-1]["finish"] == "completed"
    assert dones[-1]["source_count"] == 2  # 沿该轮累计不变
    assert "token" in names  # token 流经 messages 翻译器
    assert "sources" not in names and "images" not in names  # 不重检索
    stage_keys = {
        frame["data"].get("stage_key") for frame in frames if frame["event"] == "stage"
    }
    assert stage_keys == {"write"}  # 新段仅 write（旧尾部 stage 同为 write done）


async def test_regenerate_failed_with_sources_allowed(
    migrated_db: Any, async_client: Any, b2_runtime: Any, b2_rest: Any
) -> None:
    """崩溃孤儿形态（failed-with-sources）可重生成 → completed（决策 12/决策 3）。"""
    await _seed_and_sync(migrated_db, status="failed")
    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 200
    detail = await b2_rest.wait_turn_status(str(_TURN_ID), {"completed"})
    assert detail["answer_md"] == stub_flow.FAKE_REGENERATE_ANSWER_MD


async def test_regenerate_failure_degrades_and_can_retry(
    degraded_turn: Any,
    async_client: Any,
    b2_runtime: Any,
    b2_rest: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1.59 增量 A：再失败 → 兜底产出收束（零 error、answer_md 落库、status=degraded）；
    随后可再次 regenerate 成功。"""
    monkeypatch.setattr(
        stub_flow,
        "FAKE_REGENERATE_ERROR",
        SynthesisFallback(
            answer_md="【来源速览】综合回答未能完成（生成超时或失败）……",
            kind="synthesis_digest",
            failure="timeout",
            attempts=2,
            source_count=2,
        ),
    )
    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 200
    detail = await b2_rest.wait_turn_status(str(_TURN_ID), {"degraded"})
    assert detail["error"] is None  # 兜底轮无错误卡（零 error 事件）
    assert detail["degraded_reason"] is None
    assert detail["answer_md"].startswith("【来源速览】")  # 兜底全文持久化
    assert len(detail["sources"]) == 2
    assert detail["duration_ms"] == _DURATION_MS

    # 迭代修复：再点一次重新生成 → completed
    monkeypatch.setattr(stub_flow, "FAKE_REGENERATE_ERROR", None)
    response2 = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response2.status_code == 200
    detail2 = await b2_rest.wait_turn_status(str(_TURN_ID), {"completed"})
    assert detail2["answer_md"] == stub_flow.FAKE_REGENERATE_ANSWER_MD


# ---- 前置 409006 / 归属 404001 ----


async def test_regenerate_completed_turn_409006(
    completed_turn: Any, async_client: Any, b2_runtime: Any
) -> None:
    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 409
    assert response.json()["code"] == "409006"


async def test_regenerate_zero_source_degraded_409006(
    migrated_db: Any, async_client: Any, b2_runtime: Any
) -> None:
    await _seed_and_sync(migrated_db, status="degraded", with_sources=False)
    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 409
    assert response.json()["code"] == "409006"


async def test_regenerate_deep_zero_source_409006(
    migrated_db: Any, async_client: Any, b2_runtime: Any
) -> None:
    """§2.12 v1.28：deep 零源 degraded → 409006（前置第 2 条同判据单码覆盖；
    deep 有源 degraded/failed 已放开——正形态见 test_regenerate_deep_turn_completes）。"""
    await _seed_and_sync(migrated_db, status="degraded", mode="deep", with_sources=False)
    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 409
    assert response.json()["code"] == "409006"


async def test_regenerate_deep_turn_completes_and_reconciles(
    migrated_db: Any, async_client: Any, b2_runtime: Any, b2_rest: Any
) -> None:
    """§2.12 v1.28 + §7.4 深度 regenerate 形态：deep 有源 degraded 轮 → 200 running →
    completed；事件面无 plan/search/read/reflect 段（stage_key 仅 write）、token 流经
    messages（writer 帧）、第二 done（finish=completed、source_count 沿轮累计不变）；
    收尾 answer_md 原地更新、stages 追加 write 段、duration_ms 不覆写、
    error/degraded_reason 清空。"""
    await _seed_and_sync(migrated_db, status="degraded", mode="deep")
    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "running"

    detail = await b2_rest.wait_turn_status(str(_TURN_ID), {"completed"})
    assert detail["answer_md"] == stub_flow.FAKE_DEEP_REGENERATE_ANSWER_MD
    assert detail["degraded"] is False and detail["degraded_reason"] is None
    assert detail["error"] is None
    assert detail["duration_ms"] == _DURATION_MS  # 不覆写
    assert len(detail["sources"]) == 2  # 不重检索
    stages = detail["stages"]
    assert len(stages) == len(_OLD_STAGES) + 1  # 追加不覆写
    assert stages[-1]["stage_key"] == "write"

    stream = await async_client.get(f"/api/v1/turns/{_TURN_ID}/events")
    assert stream.status_code == 200
    frames, _ = parse_sse_frames(stream.text)
    for frame in frames:
        _assert_sse_envelope(frame, str(_TURN_ID))
    names = [frame["event"] for frame in frames]
    assert names.count("done") >= 1 and names[-1] == "done"
    dones = [frame["data"] for frame in frames if frame["event"] == "done"]
    assert dones[-1]["finish"] == "completed"
    assert dones[-1]["source_count"] == 2
    assert "token" in names
    assert "sources" not in names and "images" not in names  # 不重检索
    stage_keys = {
        frame["data"].get("stage_key") for frame in frames if frame["event"] == "stage"
    }
    assert stage_keys == {"write"}  # 无 plan/search/read/reflect 段
    write_running = next(
        frame["data"]
        for frame in frames
        if frame["event"] == "stage" and frame["data"].get("status") == "running"
    )
    assert write_running["label"] == "撰写报告"  # 无大纲回落 label（§7.4:944）


async def test_regenerate_deep_turn_fallback_degrades_without_error(
    migrated_db: Any, async_client: Any, b2_runtime: Any, b2_rest: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7.4:946 deep 再失败形态：writer 兜底标记 → 兜底产出收束（零 error 事件、
    status=degraded、degraded_reason=None、answer_md=兜底全文、duration 不覆写）。"""
    digest = "报告撰写未能完成（生成超时或失败），以下是本次研究收集到的来源速览。"
    monkeypatch.setattr(stub_flow, "FAKE_DEEP_REGENERATE_FALLBACK", digest)
    await _seed_and_sync(migrated_db, status="degraded", mode="deep")
    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 200

    detail = await b2_rest.wait_turn_status(str(_TURN_ID), {"degraded"})
    assert detail["error"] is None  # 兜底轮无错误卡
    assert detail["degraded_reason"] is None  # 枚举零扩容
    assert detail["answer_md"] == digest
    assert detail["duration_ms"] == _DURATION_MS
    assert len(detail["sources"]) == 2


async def test_regenerate_not_found_and_cross_user_404001(
    migrated_db: Any, async_client: Any, b2_runtime: Any
) -> None:
    # 不存在 / 非法 UUID
    for turn_id in (str(uuid.uuid4()), "not-a-uuid"):
        response = await async_client.post(f"/api/v1/turns/{turn_id}/regenerate")
        assert response.status_code == 404
        assert response.json()["code"] == "404001"

    # 他人轮（轮存在但归属他人）→ 同码不泄漏存在性
    conn = _connect(migrated_db)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (str(_OTHER_USER_ID), "他人用户"),
            )
        conn.commit()
    finally:
        conn.close()
    await _seed_and_sync(
        migrated_db,
        thread_id=_OTHER_THREAD_ID,
        turn_id=_OTHER_TURN_ID,
        user_id=str(_OTHER_USER_ID),
    )
    response = await async_client.post(f"/api/v1/turns/{_OTHER_TURN_ID}/regenerate")
    assert response.status_code == 404
    assert response.json()["code"] == "404001"


async def test_regenerate_double_click_while_running_409006(
    degraded_turn: Any,
    async_client: Any,
    b2_runtime: Any,
    b2_rest: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进行中重复点击：第一次 200（turn 置 running），第二次 409006（条件推进/前置落败）。"""
    monkeypatch.setattr(stub_flow, "FAKE_REGENERATE_DELAY_S", 1.0)
    # ASGITransport 下 BackgroundTask 在响应前执行——先并发提交，再在 running 窗口内二次点击
    first_task = asyncio.create_task(
        async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    )
    try:
        await b2_rest.wait_turn_status(str(_TURN_ID), {"running"}, timeout_s=10)
        second = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
        assert second.status_code == 409
        assert second.json()["code"] == "409006"
        first = await asyncio.wait_for(first_task, timeout=30)
        assert first.status_code == 200
    finally:
        if not first_task.done():
            first_task.cancel()
    await b2_rest.wait_turn_status(
        str(_TURN_ID), {"completed"}, timeout_s=TURN_COMPLETION_TIMEOUT_S
    )


# ---- 取消联动 ----


async def test_regenerate_canceled_by_user(
    degraded_turn: Any,
    async_client: Any,
    b2_runtime: Any,
    b2_rest: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """regenerate running 轮显式取消（§2.7 复用既有协作取消语义）→ canceled 收尾。"""
    monkeypatch.setattr(stub_flow, "FAKE_REGENERATE_DELAY_S", 1.0)
    post_task = asyncio.create_task(
        async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    )
    try:
        await b2_rest.wait_turn_status(str(_TURN_ID), {"running"}, timeout_s=10)
        cancel = await async_client.post(f"/api/v1/turns/{_TURN_ID}/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["data"]["status"] == "canceling"
        response = await asyncio.wait_for(post_task, timeout=30)
        assert response.status_code == 200
    finally:
        if not post_task.done():
            post_task.cancel()

    detail = await b2_rest.wait_turn_status(str(_TURN_ID), {"canceled"})
    assert detail["cancel_reason"] == "user"
    assert detail["duration_ms"] == _DURATION_MS  # canceled 亦不覆写（merge 口径）


async def test_regenerate_thread_busy_409006(
    degraded_turn: Any, async_client: Any, b2_runtime: Any
) -> None:
    """线程级单轮不变式：同线程另有进行中轮 → 409006（不产生两轮并行）。"""
    busy_turn_id = uuid.UUID("c31b9d74-0000-4000-8000-0000000000c6")
    conn = _connect(degraded_turn)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name) "
                "VALUES (%s, %s, %s, 'fast', 'running', '进行中的另一轮', '[]'::jsonb, "
                "'qwen', 'chat-standard')",
                (str(busy_turn_id), str(_THREAD_ID), common_settings.default_user_id),
            )
        conn.commit()
    finally:
        conn.close()

    response = await async_client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 409
    assert response.json()["code"] == "409006"
