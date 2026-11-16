"""integration 层：轮反馈端点真库链路（REQ-057 / api.md §2.9 v1.17 / data-model.md §1.16）。

真依赖口径（.env.test 测试库；`migrated_db` 负责 upgrade head + 默认用户补种 + 用后截断）：
- 首次反馈 → 落一行；重复提交（改判）→ **仍一行**、helpful 覆盖、updated_at 刷新
  （UNIQUE(turn_id) 承载 upsert）；
- **null = 清除（v1.17）**：删除该轮 turn_feedback 行（行消失），§2.5 读回自然回 null
  （null=未反馈，A41 读通道兼容）；清除响应 = {turn_id, helpful: null, updated_at: null}；
  无行时清除 no-op 仍 200（幂等）；清除后再 upsert 正常落行（改判/清除共用端点往返）；
- §2.5 读回 `feedback_helpful`：无行 null、有行现值（回看/刷新还原通道 A41）；
- 404004（轮不存在）与 400001（body 校验）走真实路由与全局异常处理器；
- turns 级联删除（thread 删除 → turns 级联 → turn_feedback 级联）。

行插入走管理面 psycopg2 固定 SQL（标识符写死、值全参数绑定），不依赖调度器/图运行。
"""

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest

pytestmark = pytest.mark.requires_db

_THREAD_ID = uuid.UUID("7d2f0a1e-0000-4000-8000-0000000000f1")
_TURN_ID = uuid.UUID("c31b9d74-0000-4000-8000-0000000000f2")
#: 他人用户/线程/轮（越权面种子：轮存在但归属他人 → 404004）
_OTHER_USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
_OTHER_THREAD_ID = uuid.UUID("7d2f0a1e-0000-4000-8000-0000000000f3")
_OTHER_TURN_ID = uuid.UUID("c31b9d74-0000-4000-8000-0000000000f4")


def _connect(info: Any):
    return psycopg2.connect(
        host=info.host,
        port=int(info.port),
        user=info.user,
        password=info.password,
        dbname=info.name,
        connect_timeout=5,
    )


@pytest.fixture
def seeded_turn(migrated_db) -> Iterator[Any]:
    """插入一条 completed 快速轮（归属默认用户）供反馈端点写入；用后随 truncate 清理。"""
    from app.core.config import common_settings  # noqa: PLC0415 —— DB_ 注入后读配置

    conn = _connect(migrated_db)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, mode_summary, turn_count) "
                "VALUES (%s, %s, %s, 'fast', 1)",
                (str(_THREAD_ID), common_settings.default_user_id, "反馈测试线程"),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name) "
                "VALUES (%s, %s, %s, 'fast', 'completed', '问题', '[]'::jsonb, 'qwen', "
                "'chat-standard')",
                (str(_TURN_ID), str(_THREAD_ID), common_settings.default_user_id),
            )
        conn.commit()
    finally:
        conn.close()
    yield migrated_db


@pytest.fixture
def foreign_turn(migrated_db) -> Iterator[Any]:
    """插入另一用户 + 其线程与 completed 轮（轮存在但归属他人）；用后随 truncate 清理。"""
    conn = _connect(migrated_db)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (str(_OTHER_USER_ID), "他人用户"),
            )
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, mode_summary, turn_count) "
                "VALUES (%s, %s, %s, 'fast', 1)",
                (str(_OTHER_THREAD_ID), str(_OTHER_USER_ID), "他人线程"),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name) "
                "VALUES (%s, %s, %s, 'fast', 'completed', '问题', '[]'::jsonb, 'qwen', "
                "'chat-standard')",
                (str(_OTHER_TURN_ID), str(_OTHER_THREAD_ID), str(_OTHER_USER_ID)),
            )
        conn.commit()
    finally:
        conn.close()
    yield migrated_db


def _feedback_rows(info: Any, turn_id: uuid.UUID = _TURN_ID) -> list[tuple[Any, ...]]:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT turn_id, helpful, created_at, updated_at FROM turn_feedback "
                "WHERE turn_id = %s",
                (str(turn_id),),
            )
            return cursor.fetchall()
    finally:
        conn.close()


async def test_feedback_upsert_and_judgment_flip(
    seeded_turn: Any, async_client: Any
) -> None:
    """首次 true → 改判 false：恒一行、helpful 覆盖、updated_at 刷新。"""
    first = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": True}
    )
    assert first.status_code == 200
    body = first.json()
    assert body["code"] == "0"
    assert body["data"]["turn_id"] == str(_TURN_ID)
    assert body["data"]["helpful"] is True

    rows = _feedback_rows(seeded_turn)
    assert len(rows) == 1
    turn_id, helpful, created_at, updated_at_first = rows[0]
    assert str(turn_id) == str(_TURN_ID)
    assert helpful is True

    second = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": False}
    )
    assert second.status_code == 200
    assert second.json()["data"]["helpful"] is False

    rows = _feedback_rows(seeded_turn)
    assert len(rows) == 1  # 改判不新增行
    _, helpful, created_at_after, updated_at_second = rows[0]
    assert helpful is False
    assert created_at_after == created_at  # 首次反馈时间不变（data-model §1.16）
    assert updated_at_second >= updated_at_first  # 改判刷新


async def test_feedback_helpful_read_back_via_turn_detail(
    seeded_turn: Any, async_client: Any
) -> None:
    """§2.5 feedback_helpful：未反馈 null → POST 后现值 → 改判后跟随。"""
    before = await async_client.get(f"/api/v1/turns/{_TURN_ID}")
    assert before.status_code == 200
    assert before.json()["data"]["feedback_helpful"] is None

    await async_client.post(f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": True})
    after = await async_client.get(f"/api/v1/turns/{_TURN_ID}")
    assert after.json()["data"]["feedback_helpful"] is True

    await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": False}
    )
    flipped = await async_client.get(f"/api/v1/turns/{_TURN_ID}")
    assert flipped.json()["data"]["feedback_helpful"] is False


# ---- 清除语义（api.md §2.9 v1.17：helpful=null 删行）----


async def test_feedback_clear_true_path_removes_row_and_reads_null(
    seeded_turn: Any, async_client: Any
) -> None:
    """置 true → 发 null：200 清除形态响应、turn_feedback 行消失、§2.5 读回 null。"""
    set_true = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": True}
    )
    assert set_true.status_code == 200
    assert len(_feedback_rows(seeded_turn)) == 1  # 前置：清除前确有一行

    clear = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": None}
    )
    assert clear.status_code == 200
    clear_body = clear.json()
    assert clear_body["code"] == "0"
    assert clear_body["data"] == {
        "turn_id": str(_TURN_ID),
        "helpful": None,
        "updated_at": None,
    }  # 清除形态：三键恒在、helpful/updated_at 双 null（行已删除）

    assert _feedback_rows(seeded_turn) == []  # 清除 = 删行（UNIQUE(turn_id) 口径不变）

    readback = await async_client.get(f"/api/v1/turns/{_TURN_ID}")
    assert readback.status_code == 200
    assert readback.json()["data"]["feedback_helpful"] is None  # A41 读通道兼容


async def test_feedback_clear_false_path_removes_row_and_reads_null(
    seeded_turn: Any, async_client: Any
) -> None:
    """置 false → 发 null：同样删行、读回 null（清除不区分此前判定方向）。"""
    set_false = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": False}
    )
    assert set_false.status_code == 200
    assert set_false.json()["data"]["helpful"] is False
    assert len(_feedback_rows(seeded_turn)) == 1

    clear = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": None}
    )
    assert clear.status_code == 200
    assert clear.json()["data"]["helpful"] is None
    assert clear.json()["data"]["updated_at"] is None

    assert _feedback_rows(seeded_turn) == []

    readback = await async_client.get(f"/api/v1/turns/{_TURN_ID}")
    assert readback.json()["data"]["feedback_helpful"] is None


async def test_feedback_clear_then_upsert_roundtrip(
    seeded_turn: Any, async_client: Any
) -> None:
    """清除与改判共用端点且幂等往返：null（无行 no-op 200）→ true → null（删行）
    → false（再 upsert 正常落行）。"""
    clear_on_empty = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": None}
    )
    assert clear_on_empty.status_code == 200  # 无行清除 = no-op，幂等
    assert clear_on_empty.json()["data"]["helpful"] is None
    assert _feedback_rows(seeded_turn) == []

    upsert = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": True}
    )
    assert upsert.status_code == 200
    assert upsert.json()["data"]["helpful"] is True
    assert len(_feedback_rows(seeded_turn)) == 1  # 清除后 upsert 正常落行

    clear_again = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": None}
    )
    assert clear_again.status_code == 200
    assert _feedback_rows(seeded_turn) == []

    reupsert = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": False}
    )
    assert reupsert.status_code == 200
    assert reupsert.json()["data"]["helpful"] is False
    rows = _feedback_rows(seeded_turn)
    assert len(rows) == 1  # 仍一轮一行（UNIQUE(turn_id)）
    assert rows[0][1] is False


async def test_feedback_unknown_turn_404004(
    migrated_db: Any, async_client: Any
) -> None:
    """轮不存在 → HTTP 404 + code 404004（api.md §0 v1.16）。"""
    missing = uuid.uuid4()
    response = await async_client.post(
        f"/api/v1/turns/{missing}/feedback", json={"helpful": True}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "404004"
    assert response.json()["message"] == "轮次不存在"


async def test_feedback_foreign_turn_404004_and_no_row_written(
    foreign_turn: Any, async_client: Any
) -> None:
    """轮存在但归属他人 → 404004（越权与不存在同码，不泄漏存在性），且零写。

    归属校验 = id + user_id 双条件（与 get_turn_detail/cancel_turn 同口径）。
    安全审计 low 缺口：此前 integration 只覆盖「轮不存在」，归属不匹配面（真实
    WHERE 过滤形态）未钉住；此处 upsert 与清除两路径都在归属校验处短路。
    """
    response = await async_client.post(
        f"/api/v1/turns/{_OTHER_TURN_ID}/feedback", json={"helpful": True}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "404004"
    assert response.json()["message"] == "轮次不存在"
    assert _feedback_rows(foreign_turn, _OTHER_TURN_ID) == []  # 归属不匹配即零写

    clear = await async_client.post(
        f"/api/v1/turns/{_OTHER_TURN_ID}/feedback", json={"helpful": None}
    )
    assert clear.status_code == 404
    assert clear.json()["code"] == "404004"
    assert _feedback_rows(foreign_turn, _OTHER_TURN_ID) == []


async def test_feedback_invalid_body_400001(
    seeded_turn: Any, async_client: Any
) -> None:
    """helpful 非布尔 → 400001；零写库。"""
    response = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": "yes"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "400001"
    assert _feedback_rows(seeded_turn) == []


async def test_feedback_cascades_on_turn_delete(
    seeded_turn: Any, async_client: Any
) -> None:
    """turn_feedback 的 FK 级联（data-model §1.16：`turn_id` FK ON DELETE CASCADE
    ——线程删除经 turns 级联；M1 DDL 的 turns.thread_id 无 ondelete，故这里以
    删除轮行验证级联链末端）。"""
    response = await async_client.post(
        f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": True}
    )
    assert response.status_code == 200
    assert len(_feedback_rows(seeded_turn)) == 1

    conn = _connect(seeded_turn)
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM turns WHERE id = %s", (str(_TURN_ID),))
        conn.commit()
    finally:
        conn.close()

    assert _feedback_rows(seeded_turn) == []
