"""integration：B10 线程管理全链真库验证（api.md v1.18 §2.2/§2.3/§2.4/§2.10/§2.11）。

覆盖 §3.1 后端清单的 integration 面（B-2~B-10）：
- B-2 q 过滤：中英文命中/大小写不敏感/包含匹配/trim 空串=不过滤/超长 400001/
  % 与 _ 通配转义/与 mode·archived·unread AND 叠加；
- B-3 archived 视图：缺省活跃集不含归档；archived=true 仅归档且排序 updated_at DESC
  （pinned 不参与）；
- B-4 unread 视图：read_at IS NULL 过滤、已读不出现、与 archived 正交（无组合特判）；
- B-5 排序：活跃/未读视图 pinned DESC, updated_at DESC；
- B-7 PATCH：部分更新语义（title trim / pinned / is_unread 双向 / archived 双向）、
  **updated_at 不随管理动作变化**（D-09 冻结语义回归）、404001、400001；
- B-8 DELETE：物理删除 + 级联（turns/turn_events/sources/turn_feedback 行归零）、
  total 减一、running 与 queued 轮 409005 且行不动；
- B-9 详情置已读：未读线程 GET 后 read_at 非 NULL、重复 GET 幂等零写、归档线程同样置位；
- B-10 追问自动取消归档：归档线程 POST turns 成功后回到活跃视图。

数据构造两路：纯列表/管理面用例经 ORM 直插（精确控制 pinned/read_at/archived_at/
updated_at）；涉轮次用例走真 REST + 桩流假图（b2_runtime，同 test_threads_turns.py 口径）。
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text

from app.core.config import common_settings
from app.db.models import Source, Thread, Turn, TurnEvent, TurnFeedback
from app.db.session import get_session_factory
from test.integration import stub_flow

pytestmark = pytest.mark.requires_db

#: §2.2 items[] 条目字段集（v1.18 含 pinned/is_unread/archived 三布尔投影）
_BRIEF_KEYS = {
    "thread_id",
    "title",
    "mode_summary",
    "turn_count",
    "pinned",
    "is_unread",
    "archived",
    "created_at",
    "updated_at",
}

_UNSET = object()  # 缺省哨兵：区分「未传」（已读）与显式 None（未读）


# ---- 断言/构造辅助 ----


def _ok(resp: Any, *, status_code: int = 200) -> dict:
    assert resp.status_code == status_code, resp.text
    body = resp.json()
    assert body["code"] == "0" and body["message"] == "ok", body
    return body["data"]


def _err(resp: Any, *, status_code: int, code: str) -> dict:
    assert resp.status_code == status_code, resp.text
    body = resp.json()
    assert body["code"] == code, body
    assert isinstance(body["message"], str) and body["message"]
    assert "data" not in body
    return body


async def _insert_thread(
    *,
    title: str,
    pinned: bool = False,
    read_at: object = _UNSET,
    archived_at: datetime | None = None,
    updated_at: datetime | None = None,
    mode_summary: str = "fast",
    turn_count: int = 0,
) -> uuid.UUID:
    """ORM 直插线程行（控制三列/updated_at 精确造数；零 REST 依赖）。"""
    now = datetime.now(UTC)
    thread_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            Thread(
                id=thread_id,
                user_id=uuid.UUID(common_settings.default_user_id),
                title=title,
                mode_summary=mode_summary,
                turn_count=turn_count,
                pinned=pinned,
                read_at=now if read_at is _UNSET else read_at,
                archived_at=archived_at,
                created_at=(updated_at or now) - timedelta(minutes=5),
                updated_at=updated_at or now,
            )
        )
        await session.commit()
    return thread_id


async def _read_at(thread_id: uuid.UUID) -> datetime | None:
    async with get_session_factory()() as session:
        thread = await session.get(Thread, thread_id)
        assert thread is not None
        return thread.read_at


async def _turn_ids(thread_id: uuid.UUID) -> list[uuid.UUID]:
    async with get_session_factory()() as session:
        rows = await session.scalars(select(Turn.id).where(Turn.thread_id == thread_id))
        return list(rows.all())


async def _parent_turn_id(turn_id: str) -> uuid.UUID | None:
    async with get_session_factory()() as session:
        turn = await session.get(Turn, uuid.UUID(turn_id))
        assert turn is not None
        return turn.parent_turn_id


async def _related_counts(thread_id: uuid.UUID) -> dict[str, int]:
    """线程从属行计数（turns/turn_events/sources/turn_feedback）。

    sources/turn_feedback 经**删除前捕获的 turn_id 集**过滤——不能用 turn join 反查，
    否则「turns 已删但 sources 残留」的孤儿行会被 join 掩盖成 0（假绿）。
    """
    turn_ids = await _turn_ids(thread_id)
    async with get_session_factory()() as session:
        turns = await session.scalar(
            select(func.count()).select_from(Turn).where(Turn.thread_id == thread_id)
        )
        events = await session.scalar(
            select(func.count())
            .select_from(TurnEvent)
            .where(TurnEvent.thread_id == thread_id)
        )
        sources = 0
        feedback = 0
        if turn_ids:
            sources = await session.scalar(
                select(func.count())
                .select_from(Source)
                .where(Source.turn_id.in_(turn_ids))
            )
            feedback = await session.scalar(
                select(func.count())
                .select_from(TurnFeedback)
                .where(TurnFeedback.turn_id.in_(turn_ids))
            )
    return {
        "turns": int(turns or 0),
        "turn_events": int(events or 0),
        "sources": int(sources or 0),
        "turn_feedback": int(feedback or 0),
    }


# ---- B-2：q 标题模糊搜索（§2.2 v1.18）----


async def test_list_q_title_ilike_and_wildcard_escape(async_client, migrated_db):
    now = datetime.now(UTC)
    zh = await _insert_thread(title="LangGraph 入门指南", updated_at=now - timedelta(minutes=1))
    en_lower = await _insert_thread(title="langgraph 进阶", updated_at=now - timedelta(minutes=2))
    await _insert_thread(title="CrewOS 对比", updated_at=now - timedelta(minutes=3))
    underscore = await _insert_thread(title="abc_d 下划线", updated_at=now - timedelta(minutes=4))
    await _insert_thread(title="abcXd 普通", updated_at=now - timedelta(minutes=5))
    percent = await _insert_thread(title="100% 完成", updated_at=now - timedelta(minutes=6))
    await _insert_thread(title="0 ABC 完", updated_at=now - timedelta(minutes=7))

    # 大小写不敏感 + 包含匹配（中英文均命中）
    data = _ok(await async_client.get("/api/v1/threads", params={"q": "LANGGRAPH"}))
    assert data["total"] == 2
    assert {i["thread_id"] for i in data["items"]} == {str(zh), str(en_lower)}

    # _ 通配转义：abc_d 只命中字面量下划线行（未转义会连带命中 abcXd）
    data = _ok(await async_client.get("/api/v1/threads", params={"q": "abc_d"}))
    assert data["total"] == 1 and data["items"][0]["thread_id"] == str(underscore)

    # % 通配转义：0% 完 只命中 100% 完成（未转义会连带命中 0 ABC 完）
    data = _ok(await async_client.get("/api/v1/threads", params={"q": "0% 完"}))
    assert data["total"] == 1 and data["items"][0]["thread_id"] == str(percent)

    # trim 后空串=不过滤（全量 7 条）
    data = _ok(await async_client.get("/api/v1/threads", params={"q": "   "}))
    assert data["total"] == 7

    # 未命中
    data = _ok(await async_client.get("/api/v1/threads", params={"q": "不存在的关键词"}))
    assert data["total"] == 0 and data["items"] == []

    # 超长（>200 字符）400001
    _err(
        await async_client.get("/api/v1/threads", params={"q": "长" * 201}),
        status_code=400,
        code="400001",
    )
    # 200 字符边界合法
    _ok(await async_client.get("/api/v1/threads", params={"q": "长" * 200}))


async def test_list_q_combines_with_mode_and_view(async_client, migrated_db):
    now = datetime.now(UTC)
    fast_hit = await _insert_thread(
        title="LangGraph 快速", mode_summary="fast", updated_at=now - timedelta(minutes=1)
    )
    await _insert_thread(
        title="LangGraph 深度", mode_summary="deep", updated_at=now - timedelta(minutes=2)
    )
    unread_hit = await _insert_thread(
        title="LangGraph 未读", read_at=None, updated_at=now - timedelta(minutes=3)
    )

    data = _ok(
        await async_client.get("/api/v1/threads", params={"q": "LangGraph", "mode": "fast"})
    )
    assert {i["thread_id"] for i in data["items"]} == {str(fast_hit), str(unread_hit)}
    assert all(i["mode_summary"] == "fast" for i in data["items"])

    data = _ok(
        await async_client.get(
            "/api/v1/threads", params={"q": "LangGraph", "unread": "true"}
        )
    )
    assert data["total"] == 1 and data["items"][0]["thread_id"] == str(unread_hit)


# ---- B-3/B-5：archived 视图与排序口径 ----


async def test_archived_view_and_sorting(async_client, migrated_db):
    now = datetime.now(UTC)
    active_old = await _insert_thread(title="活跃较早", updated_at=now - timedelta(minutes=10))
    active_pinned = await _insert_thread(
        title="活跃置顶", pinned=True, updated_at=now - timedelta(minutes=20)
    )
    archived_pinned_older = await _insert_thread(
        title="归档置顶较早",
        pinned=True,
        archived_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=30),
    )
    archived_newer = await _insert_thread(
        title="归档较新",
        archived_at=now - timedelta(minutes=40),
        updated_at=now - timedelta(minutes=5),
    )

    # 缺省=活跃集：pinned DESC, updated_at DESC
    data = _ok(await async_client.get("/api/v1/threads"))
    assert data["total"] == 2
    assert [i["thread_id"] for i in data["items"]] == [
        str(active_pinned),
        str(active_old),
    ]
    assert all(i["archived"] is False for i in data["items"])
    for item in data["items"]:
        assert set(item) == _BRIEF_KEYS

    # archived=true：仅归档 + updated_at DESC（pinned 不参与）
    data = _ok(await async_client.get("/api/v1/threads", params={"archived": "true"}))
    assert data["total"] == 2
    assert [i["thread_id"] for i in data["items"]] == [
        str(archived_newer),
        str(archived_pinned_older),
    ]
    assert all(i["archived"] is True for i in data["items"])
    assert data["items"][1]["pinned"] is True  # 存储保留但排序不消费（D-07）


# ---- B-4：unread 视图（与 archived 正交）----


async def test_unread_view_and_orthogonality(async_client, migrated_db):
    now = datetime.now(UTC)
    unread_active_old = await _insert_thread(
        title="未读活跃较早", read_at=None, updated_at=now - timedelta(minutes=10)
    )
    unread_active_pinned = await _insert_thread(
        title="未读活跃置顶",
        read_at=None,
        pinned=True,
        updated_at=now - timedelta(minutes=20),
    )
    read_active = await _insert_thread(title="已读活跃")
    unread_archived = await _insert_thread(
        title="未读归档", read_at=None, archived_at=now
    )

    # 未读视图=活跃集内 read_at IS NULL（排序仍 pinned DESC, updated_at DESC）
    data = _ok(await async_client.get("/api/v1/threads", params={"unread": "true"}))
    assert [i["thread_id"] for i in data["items"]] == [
        str(unread_active_pinned),
        str(unread_active_old),
    ]
    assert all(i["is_unread"] is True and i["archived"] is False for i in data["items"])
    assert read_active not in {i["thread_id"] for i in data["items"]}

    # 与 archived 正交：archived=true&unread=true 按语义返回（无组合特判）
    data = _ok(
        await async_client.get(
            "/api/v1/threads", params={"archived": "true", "unread": "true"}
        )
    )
    assert data["total"] == 1 and data["items"][0]["thread_id"] == str(unread_archived)
    assert str(read_active) not in {i["thread_id"] for i in data["items"]}


# ---- B-7：PATCH 部分更新语义（§2.10）----


async def test_patch_thread_partial_update_and_updated_at_frozen(async_client, migrated_db):
    fixed_updated_at = datetime(2026, 11, 1, 9, 40, 11, tzinfo=UTC)
    thread_id = await _insert_thread(title="原标题", updated_at=fixed_updated_at)

    # 重命名：trim 落库 + 响应=更新后投影 + updated_at 不随动（D-09）
    data = _ok(
        await async_client.patch(
            f"/api/v1/threads/{thread_id}", json={"title": "  LangGraph Send API 调研  "}
        )
    )
    assert data["title"] == "LangGraph Send API 调研"
    assert set(data) == _BRIEF_KEYS
    assert data["thread_id"] == str(thread_id)
    assert datetime.fromisoformat(data["updated_at"]) == fixed_updated_at

    # 置顶：活跃视图排序首位（§2.2 pinned DESC）
    other = await _insert_thread(
        title="更新的线程", updated_at=fixed_updated_at + timedelta(minutes=1)
    )
    data = _ok(
        await async_client.patch(f"/api/v1/threads/{thread_id}", json={"pinned": True})
    )
    assert data["pinned"] is True
    listing = _ok(await async_client.get("/api/v1/threads"))
    assert [i["thread_id"] for i in listing["items"]] == [str(thread_id), str(other)]

    # is_unread 双向：true→read_at NULL（未读视图可见）、false→置已读
    data = _ok(
        await async_client.patch(f"/api/v1/threads/{thread_id}", json={"is_unread": True})
    )
    assert data["is_unread"] is True
    unread_view = _ok(await async_client.get("/api/v1/threads", params={"unread": "true"}))
    assert str(thread_id) in {i["thread_id"] for i in unread_view["items"]}
    data = _ok(
        await async_client.patch(f"/api/v1/threads/{thread_id}", json={"is_unread": False})
    )
    assert data["is_unread"] is False

    # archived 双向：true→移出活跃视图、进入归档视图；false→返回活跃
    data = _ok(
        await async_client.patch(f"/api/v1/threads/{thread_id}", json={"archived": True})
    )
    assert data["archived"] is True
    active = _ok(await async_client.get("/api/v1/threads"))
    assert str(thread_id) not in {i["thread_id"] for i in active["items"]}
    archived = _ok(await async_client.get("/api/v1/threads", params={"archived": "true"}))
    assert str(thread_id) in {i["thread_id"] for i in archived["items"]}
    data = _ok(
        await async_client.patch(f"/api/v1/threads/{thread_id}", json={"archived": False})
    )
    assert data["archived"] is False

    # 组合更新合法（重命名+归档同请求）；updated_at 仍不变
    data = _ok(
        await async_client.patch(
            f"/api/v1/threads/{thread_id}", json={"title": "组合标题", "archived": True}
        )
    )
    assert data["title"] == "组合标题" and data["archived"] is True
    assert datetime.fromisoformat(data["updated_at"]) == fixed_updated_at

    # 404001：不存在 / 非法 UUID（同码不泄漏存在性）
    _err(
        await async_client.patch(
            f"/api/v1/threads/{uuid.uuid4()}", json={"pinned": True}
        ),
        status_code=404,
        code="404001",
    )
    _err(
        await async_client.patch("/api/v1/threads/not-a-uuid", json={"pinned": True}),
        status_code=404,
        code="404001",
    )

    # 400001：空对象 / title 空白 / 超长 / 非法布尔
    for body in (
        {},
        {"title": "   "},
        {"title": "长" * 121},
        {"pinned": "yes"},
    ):
        _err(
            await async_client.patch(f"/api/v1/threads/{thread_id}", json=body),
            status_code=400,
            code="400001",
        )


# ---- B-8：DELETE 级联与 409005（§2.11）----


async def test_delete_thread_cascades_related_rows(async_client, b2_rest, b2_runtime):
    """删除级联 + 自引用 parent_turn_id 同语句删除（D-03：一句 DELETE turns 清光从属）。

    轮次构造特意含**父挂载链**（deep 首轮 → fast 追问自动挂 parent_turn_id）：
    turns.parent_turn_id 是无级联自引用 FK，级联删除必须在单条 DELETE 语句内完成
    才不撞自引用约束——本用例即该口径的真库实证。
    """
    created = _ok(
        await async_client.post(
            "/api/v1/threads", json={"query": "待删线程：LangGraph 子图边界", "mode": "deep"}
        )
    )
    thread_id, turn1 = created["thread_id"], created["turn_id"]
    await b2_rest.wait_turn_status(turn1, {"completed"})

    followup = _ok(
        await async_client.post(
            f"/api/v1/threads/{thread_id}/turns", json={"query": "深度报告后追问", "mode": "fast"}
        )
    )
    turn2 = followup["turn_id"]
    await b2_rest.wait_turn_status(turn2, {"completed"})
    assert await _parent_turn_id(turn2) == uuid.UUID(turn1)  # REQ-017 自动挂载
    _ok(
        await async_client.post(f"/api/v1/turns/{turn2}/feedback", json={"helpful": True})
    )

    before = await _related_counts(uuid.UUID(thread_id))
    assert before["turns"] == 2
    assert before["turn_events"] > 0  # 桩流 stage/sources/done 落库
    assert before["sources"] >= 1  # fast 追问轮落来源（deep 桩期零来源）
    assert before["turn_feedback"] == 1

    data = _ok(await async_client.delete(f"/api/v1/threads/{thread_id}"))
    assert data == {"deleted": True}

    after = await _related_counts(uuid.UUID(thread_id))
    assert after == {
        "turns": 0,
        "turn_events": 0,
        "sources": 0,
        "turn_feedback": 0,
    }
    # 列表 total 减一（线程已物理删除）
    assert _ok(await async_client.get("/api/v1/threads"))["total"] == 0
    # 再删 404001（行已不存在，不泄漏）
    _err(
        await async_client.delete(f"/api/v1/threads/{thread_id}"),
        status_code=404,
        code="404001",
    )


async def test_delete_thread_with_in_flight_turn_409005(
    async_client, b2_rest, b2_runtime, wait_for, monkeypatch
):
    """running/queued 轮存在时删除被拒（409005），行不动；轮终态后可删。"""
    monkeypatch.setattr(stub_flow, "FAKE_DELAY_S", 2.0)
    post = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "进行中不可删", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})

        body = _err(
            await async_client.delete(f"/api/v1/threads/{thread_id}"),
            status_code=409,
            code="409005",
        )
        assert body["message"] == "线程有进行中轮次，不可删除"

        # queued 轮同口径（与 409001 同源状态集，D-10/D-11）
        post2 = asyncio.create_task(
            async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "排队轮", "mode": "fast", "on_conflict": "enqueue"},
            )
        )
        try:

            async def _second_queued() -> bool:
                detail = await async_client.get(f"/api/v1/threads/{thread_id}")
                if detail.status_code != 200:
                    return False
                turns = detail.json()["data"]["turns"]
                return len(turns) >= 2 and turns[1]["status"] == "queued"

            await wait_for(_second_queued, description="第二轮排队可见")
            _err(
                await async_client.delete(f"/api/v1/threads/{thread_id}"),
                status_code=409,
                code="409005",
            )
        finally:
            await asyncio.wait_for(post2, timeout=30)

        # 行不动（零删除）
        detail = _ok(await async_client.get(f"/api/v1/threads/{thread_id}"))
        assert detail["turn_count"] == 2
    finally:
        await asyncio.wait_for(post, timeout=30)

    # 全部终态后删除成功
    detail = _ok(await async_client.get(f"/api/v1/threads/{thread_id}"))
    for skeleton in detail["turns"]:
        await b2_rest.wait_turn_status(skeleton["turn_id"], {"completed"})
    assert _ok(await async_client.delete(f"/api/v1/threads/{thread_id}")) == {
        "deleted": True
    }


# ---- B-9：详情置已读（§2.3 v1.18 写副作用）----


async def test_detail_marks_unread_thread_read_idempotent(async_client, migrated_db):
    thread_id = await _insert_thread(title="未读线程", read_at=None)

    _ok(await async_client.get(f"/api/v1/threads/{thread_id}"))
    first = await _read_at(thread_id)
    assert first is not None  # 打开即已读

    # 幂等：重复 GET 零写（read_at 值不变）
    _ok(await async_client.get(f"/api/v1/threads/{thread_id}"))
    assert await _read_at(thread_id) == first

    # 未读视图不再包含
    data = _ok(await async_client.get("/api/v1/threads", params={"unread": "true"}))
    assert data["total"] == 0

    # 归档线程打开同样置已读
    archived_unread = await _insert_thread(
        title="归档未读线程", read_at=None, archived_at=datetime.now(UTC)
    )
    _ok(await async_client.get(f"/api/v1/threads/{archived_unread}"))
    assert await _read_at(archived_unread) is not None


# ---- B-10：追问自动取消归档（§2.4 v1.18 注）----


async def test_followup_on_archived_thread_unarchives(async_client, b2_rest, b2_runtime):
    created = _ok(
        await async_client.post(
            "/api/v1/threads", json={"query": "归档后追问的首问", "mode": "fast"}
        )
    )
    thread_id, turn_id = created["thread_id"], created["turn_id"]
    await b2_rest.wait_turn_status(turn_id, {"completed"})

    _ok(await async_client.patch(f"/api/v1/threads/{thread_id}", json={"archived": True}))
    assert _ok(await async_client.get("/api/v1/threads"))["total"] == 0
    assert _ok(await async_client.get("/api/v1/threads", params={"archived": "true"}))[
        "total"
    ] == 1

    followup = _ok(
        await async_client.post(
            f"/api/v1/threads/{thread_id}/turns",
            json={"query": "归档线程的追问", "mode": "fast"},
        )
    )
    await b2_rest.wait_turn_status(followup["turn_id"], {"completed"})

    # 对话活动解除归档：回到活跃视图且 archived=false 投影
    active = _ok(await async_client.get("/api/v1/threads"))
    assert active["total"] == 1
    assert active["items"][0]["thread_id"] == thread_id
    assert active["items"][0]["archived"] is False
    assert _ok(await async_client.get("/api/v1/threads", params={"archived": "true"}))[
        "total"
    ] == 0


# ---- P2：DELETE 与追问的线程行锁串行化（check-then-act 竞态 → 500001 的封堵）----


async def _lock_waiter_count() -> int:
    """当前库内等待锁（wait_event_type='Lock'）的其他后端数——锁串行化的确定性观测面。"""
    async with get_session_factory()() as session:
        value = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' "
                "AND pid <> pg_backend_pid()"
            )
        )
        return int(value or 0)


async def _has_lock_waiter() -> bool:
    return await _lock_waiter_count() > 0


@pytest.mark.requires_db
async def test_followup_post_serializes_behind_delete_row_lock(
    async_client, b2_rest, b2_runtime, wait_for
):
    """DELETE 先持线程行锁（未提交）→ 追问请求串行在后 → 锁释放后归属读 404001。

    锁语义证明：`delete_thread` 的归属读 `SELECT … FOR UPDATE` 与 `create_turn` 的
    同款锁读互斥——持锁方提交（删除线程）后，追问方重新读到的行已不存在，确定性
    得到 404001，而不是插入撞 FK 的 500001。
    """
    created = _ok(
        await async_client.post(
            "/api/v1/threads", json={"query": "锁语义：删除先持锁", "mode": "fast"}
        )
    )
    thread_id, turn_id = created["thread_id"], created["turn_id"]
    await b2_rest.wait_turn_status(turn_id, {"completed"})

    async with get_session_factory()() as holder:
        # 模拟 DELETE 事务处于「已锁行、未提交」窗口（生产 delete_thread 的首步同款）
        await holder.execute(
            select(Thread).where(Thread.id == uuid.UUID(thread_id)).with_for_update()
        )
        post_task = asyncio.create_task(
            async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "锁等待中的追问", "mode": "fast"},
            )
        )
        try:
            await wait_for(_has_lock_waiter, description="追问请求等待线程行锁")
            assert not post_task.done()  # 竞态窗口内被串行化（未放锁不推进）
        finally:
            # 持锁方完成删除并提交（等价 DELETE 的提交时刻）
            await holder.execute(delete(Turn).where(Turn.thread_id == uuid.UUID(thread_id)))
            await holder.execute(delete(Thread).where(Thread.id == uuid.UUID(thread_id)))
            await holder.commit()

    response = await asyncio.wait_for(post_task, timeout=30)
    _err(response, status_code=404, code="404001")  # 归属锁读看不到已删行
    # 零残影：线程行与原有轮次均已删除（未因竞态留下半删状态）
    assert await _turn_ids(uuid.UUID(thread_id)) == []


@pytest.mark.requires_db
async def test_delete_serializes_behind_followup_row_lock_409005(
    async_client, b2_rest, b2_runtime, wait_for
):
    """追问先持线程行锁（未提交 pending 轮）→ DELETE 等锁后见到该轮 → 409005。

    反向证明：DELETE 的 `FOR UPDATE` 归属读阻塞在追问事务上；追问提交后 DELETE 的
    in-flight 计数看到其已提交 pending 行，返回 409005（线程与轮次均不被删）。
    """
    created = _ok(
        await async_client.post(
            "/api/v1/threads", json={"query": "锁语义：追问先持锁", "mode": "fast"}
        )
    )
    thread_id, turn_id = created["thread_id"], created["turn_id"]
    await b2_rest.wait_turn_status(turn_id, {"completed"})

    pending_turn_id = uuid.uuid4()
    async with get_session_factory()() as holder:
        # 模拟追问事务处于「已锁行 + 已插 pending 轮、未提交」窗口
        await holder.execute(
            select(Thread).where(Thread.id == uuid.UUID(thread_id)).with_for_update()
        )
        holder.add(
            Turn(
                id=pending_turn_id,
                thread_id=uuid.UUID(thread_id),
                user_id=uuid.UUID(common_settings.default_user_id),
                mode="fast",
                status="pending",
                query="未提交的追问轮",
                chat_model_key="qwen",
                model_name="m",
            )
        )
        await holder.flush()
        delete_task = asyncio.create_task(
            async_client.delete(f"/api/v1/threads/{thread_id}")
        )
        try:
            await wait_for(_has_lock_waiter, description="DELETE 等待线程行锁")
            assert not delete_task.done()  # 锁未释放前 DELETE 不推进
        finally:
            await holder.commit()

    response = await asyncio.wait_for(delete_task, timeout=30)
    body = _err(response, status_code=409, code="409005")
    assert body["message"] == "线程有进行中轮次，不可删除"
    # 行不动：线程仍在、两轮都在（原轮 + 刚提交的 pending 轮）
    detail = _ok(await async_client.get(f"/api/v1/threads/{thread_id}"))
    assert detail["thread_id"] == thread_id
    assert set(await _turn_ids(uuid.UUID(thread_id))) == {
        uuid.UUID(turn_id),
        pending_turn_id,
    }
