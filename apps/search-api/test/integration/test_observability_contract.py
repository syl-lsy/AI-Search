"""integration 层：Langfuse 观测契约回归（app/core/observability.py + TurnRunner 注入形态）。

口径（任务书 3，2026-11-13 v4-native 迁移配套）：RunnableLambda 仿真一轮（复刻
turn_service.py astream config 注入形态：callbacks=build_turn_callbacks 产物 +
metadata langfuse_* 契约键）→ flush → v2 observations 按 traceId 读回 → 断言
观测契约三件：
- trace id == 轮 turn_id 的 32hex 形态（对账键：Langfuse trace 与 turns 行同 id
  可查——trace_id_for hex 化语义不漂移）；
- sessionId == thread_id（session 维度聚合不失效——CallbackHandler 根链
  propagate_attributes 承担，metadata langfuse_session_id 契约键承载）；
- 观测树非空（root span 落表——观测接入面工作）。

把 2026-11-13 探针结论（trace_context 仍收 / 契约键保留 / session 落根）固化为
自动化回归：langfuse 升级或接入面变动时此处红（R8 护栏的真栈延伸）。

凭证纪律与 skip 口径同 test_trace_cleanup.py（.env.test LANGFUSE_ 组；缺凭证/
栈不可达 skip，凭据值绝不打印）。清理：finally 幂等 DELETE 测试 trace。
"""

import asyncio
import time
import uuid
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values
from langchain_core.runnables import RunnableLambda

from app.core.observability import build_turn_callbacks, trace_id_for

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_TEST_PATH = REPO_ROOT / ".env.test"

REQUIRED_KEYS = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
VISIBILITY_DEADLINE_S = 60  # batch exporter 异步导出（flush 已同步等，轮询留余量）
POLL_INTERVAL_S = 2.0

_V2_OBSERVATIONS = "/api/public/v2/observations"


@pytest.fixture
def langfuse_env() -> dict[str, str]:
    """LANGFUSE_ 组凭证：只认 .env.test；缺任一关键变量 → 本模块用例 skip。"""
    if not ENV_TEST_PATH.exists():
        pytest.skip(
            "缺 .env.test（cp .env.test.example .env.test 并填 LANGFUSE_ 凭证）"
            "——requires_langfuse 用例跳过"
        )
    values = dotenv_values(ENV_TEST_PATH)
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        pytest.skip(f".env.test 缺 {', '.join(missing)}——requires_langfuse 用例跳过")
    return {key: values[key] for key in REQUIRED_KEYS}


@pytest.mark.requires_langfuse
async def test_runnable_turn_emits_contract_trace(langfuse_env):
    """RunnableLambda 仿真轮 → trace id/sessionId/观测树三断言（观测契约回归）。"""

    from langfuse import get_client

    turn_id = str(uuid.uuid4())
    thread_id = f"thread-obs-contract-{uuid.uuid4().hex[:8]}"
    user_id = "obs-contract-user"
    expected_trace_id = trace_id_for(turn_id)  # 32hex 对账键
    assert len(expected_trace_id) == 32 and "-" not in expected_trace_id

    # 观测缝：build_turn_callbacks 走 langfuse_settings（.env）+ os.environ 回填
    # （SDK 全局客户端同源）；.env.test 与 .env 同一部署栈（凭证纪律同 trace_cleanup）
    callbacks = build_turn_callbacks(
        thread_id=thread_id,
        turn_id=turn_id,
        user_id=user_id,
        mode="fast",
    )
    assert callbacks, (
        "LANGFUSE_ 配置齐全时必须产出 CallbackHandler（栈缺凭证见 skip 口径）"
    )

    # RunnableLambda 仿真轮：config 注入形态复刻 turn_service.py astream 段（metadata
    # langfuse_* 契约键 + configurable 轮/线程 id）
    RunnableLambda(lambda payload: {"echo": payload["q"]}).invoke(
        {"q": "observability contract probe"},
        config={
            "callbacks": callbacks,
            "metadata": {
                "langfuse_session_id": thread_id,
                "langfuse_user_id": user_id,
                "langfuse_tags": ["mode:fast"],
                "turn_id": turn_id,
                "mode": "fast",
            },
            "configurable": {"thread_id": thread_id, "turn_id": turn_id},
        },
    )
    get_client().flush()  # 同步等 batch exporter 发完

    try:
        async with httpx.AsyncClient(
            base_url=langfuse_env["LANGFUSE_HOST"],
            auth=(
                langfuse_env["LANGFUSE_PUBLIC_KEY"],
                langfuse_env["LANGFUSE_SECRET_KEY"],
            ),
            timeout=httpx.Timeout(10.0),
        ) as client:
            # 等导出收敛后读回（v2 observations 按 traceId）
            deadline = time.monotonic() + VISIBILITY_DEADLINE_S
            rows: list[dict] = []
            while time.monotonic() < deadline:
                response = await client.get(
                    _V2_OBSERVATIONS,
                    params={"traceId": expected_trace_id, "limit": 10},
                )
                response.raise_for_status()
                rows = response.json().get("data", [])
                if rows:
                    break
                await asyncio.sleep(POLL_INTERVAL_S)
            assert rows, (
                f"{VISIBILITY_DEADLINE_S}s 内 v2 observations 未读回仿真轮观测"
                f"（traceId={expected_trace_id}）——观测接入面断裂"
            )

            # 契约一：trace id == turn 的 32hex（对账键不漂移）
            trace_ids = {row["traceId"] for row in rows}
            assert trace_ids == {expected_trace_id}, (
                f"trace id 漂移：期望 {expected_trace_id}，实测 {trace_ids}"
            )

            # 契约二：sessionId == thread_id（session 维度聚合）
            session_ids = {row.get("sessionId") for row in rows}
            assert session_ids == {thread_id}, (
                f"sessionId 漂移：期望 {thread_id}，实测 {session_ids}"
            )

            # 契约三：观测树非空（root span 落表，观测接入面工作）
            assert all(row.get("id") for row in rows), "观测行缺 id（形态异常）"
    finally:
        # 幂等清理：测试 trace 不残留栈中（DELETE 200/204/404 均收敛）
        async with httpx.AsyncClient(
            base_url=langfuse_env["LANGFUSE_HOST"],
            auth=(
                langfuse_env["LANGFUSE_PUBLIC_KEY"],
                langfuse_env["LANGFUSE_SECRET_KEY"],
            ),
            timeout=httpx.Timeout(10.0),
        ) as client:
            response = await client.delete(f"/api/public/traces/{expected_trace_id}")
            if response.status_code not in (200, 204, 404):
                print(f"[cleanup] 兜底 DELETE 未收敛：HTTP {response.status_code}")
