"""integration 层：Langfuse 过期 trace 清理（app/crons/trace_cleanup.py）——真 Langfuse。

口径（任务书 4）：经公共 REST 造一条「过期」trace（timestamp 回填 = 保留天数 + 1h 前，
ingestion API trace-create 事件）→ 等其可见 → 跑 cleanup_expired_traces()（真
settings 路径）→ 断言该 trace 落在删除结果里且 GET 单查 404。

凭证纪律：连接信息只取仓库根 .env.test 的 LANGFUSE_ 组（dotenv_values 直读；清理
函数走 core/config 的 langfuse_settings 单例，同一 .env.test 来源）；凭据值只在内存
流转，绝不打印、绝不进断言消息。缺凭证时 skip（给 reason，不让命令红掉）；Langfuse
栈连接不可达 skip（环境未起），HTTP 4xx/5xx 照常 fail（配置/代码问题不掩蔽）。

删除可见性（langfuse#8834）：DELETE 受理 ≠ 查询立即可见——Langfuse 的删除经队列落
ClickHouse 后异步收敛，单查在收敛前仍可能 200。故第 ④ 步按 ARCH §7「滞后观察」做有界
轮询（deadline 120s / 间隔 2s），并打印实测滞后秒数作为观察数据（提额依据：实测 6.3s
直跑 vs 87.5s 全量跑——Langfuse 删除收敛波动大，90s 会贴边误红）。

副作用边界：cleanup_expired_traces 是全量幂等清理——跑一轮会顺带删除真实栈中其他
已过期 trace（M1 开发期数据留存 <7 天，预期为零）；测试 trace 自身由清理收尾删除，
无残留。
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from app.crons.trace_cleanup import cleanup_expired_traces

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_TEST_PATH = REPO_ROOT / ".env.test"

REQUIRED_KEYS = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
TRACE_NAME = "trace-cleanup-itest"
INGESTION_DEADLINE_S = 60  # v3 ingestion 为异步管道（队列→ClickHouse），可见轮询给宽预算
DELETION_DEADLINE_S = 120  # 删除受理→查询 404 的异步收敛（langfuse#8834），ARCH §7 滞后观察
# 为何 120s：实测收敛滞后 6.3s（单跑）/ 87.5s（全量 integration 跑）——波动极大，90s 贴边误红
POLL_INTERVAL_S = 2.0


def _iso_z(value: datetime) -> str:
    """ISO 8601 UTC（Z 形态）——与 Langfuse 服务端 zod 校验形态一致。"""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


async def _wait_visible(client: httpx.AsyncClient, trace_id: str) -> None:
    """轮询单查端点直至测试 trace 可见（ingestion 异步管道收敛）。"""
    deadline = time.monotonic() + INGESTION_DEADLINE_S
    url = f"/api/public/traces/{trace_id}"
    while time.monotonic() < deadline:
        response = await client.get(url)
        if response.status_code == 200:
            return
        await asyncio.sleep(POLL_INTERVAL_S)
    pytest.fail(f"测试 trace 在 {INGESTION_DEADLINE_S}s 内未见于 Langfuse（ingestion 管道滞后？）")


async def _wait_deleted(client: httpx.AsyncClient, trace_id: str) -> None:
    """轮询单查直至 404（langfuse#8834：删除受理≠查询立即可见，ClickHouse 异步收敛）。

    打印实测滞后秒数（ARCH §7「滞后观察」的量化数据，随 `-rP` 在 passed 输出里可见）；
    超时仍非 404 则 fail 并带上最后观测状态与滞后，不把「未收敛」误判成「未删除」。
    """
    started = time.monotonic()
    deadline = started + DELETION_DEADLINE_S
    url = f"/api/public/traces/{trace_id}"
    status_code: int | None = None
    while time.monotonic() < deadline:
        response = await client.get(url)
        status_code = response.status_code
        if status_code == 404:
            lag_s = time.monotonic() - started
            print(f"[lag] trace {trace_id} 删除收敛耗时 {lag_s:.1f}s")
            return
        await asyncio.sleep(POLL_INTERVAL_S)
    lag_s = time.monotonic() - started
    pytest.fail(
        f"trace 删除后 {DELETION_DEADLINE_S}s 内单查仍非 404（最后状态 {status_code}，"
        f"实测滞后 {lag_s:.1f}s——langfuse#8834 收敛未完成或删除未生效）"
    )


@pytest.mark.requires_langfuse
async def test_cleanup_deletes_expired_trace(langfuse_env):
    """经 REST 造过期 trace → 跑清理 → 断言已删除（结果明细命中 + GET 404）。"""
    retention_days = int(
        dotenv_values(ENV_TEST_PATH).get("LANGFUSE_TRACE_RETENTION_DAYS") or 7
    )
    trace_id = str(uuid.uuid4())
    backdated = datetime.now(UTC) - timedelta(days=retention_days, hours=1)

    async with httpx.AsyncClient(
        base_url=langfuse_env["LANGFUSE_HOST"],
        auth=(langfuse_env["LANGFUSE_PUBLIC_KEY"], langfuse_env["LANGFUSE_SECRET_KEY"]),
        timeout=httpx.Timeout(10.0),
    ) as client:
        # ① 造一条过期 trace（trace-create 事件，timestamp 回填）
        try:
            response = await client.post(
                "/api/public/ingestion",
                json={
                    "batch": [
                        {
                            "id": str(uuid.uuid4()),
                            "type": "trace-create",
                            "timestamp": _iso_z(backdated),
                            "body": {
                                "id": trace_id,
                                "name": TRACE_NAME,
                                "timestamp": _iso_z(backdated),
                            },
                        }
                    ]
                },
            )
        except httpx.HTTPError as exc:
            pytest.skip(f"Langfuse 栈不可达（{type(exc).__name__}）——requires_langfuse 用例跳过")
        response.raise_for_status()
        outcome = response.json()
        assert outcome.get("errors", []) == [], f"ingestion 报错：{outcome.get('errors')}"

        # ② 等其可见（列表与单查同源，可见即可被清理查询命中）
        await _wait_visible(client, trace_id)

        # ③ 跑清理（真 settings 路径：client=None → LangfuseSettings 自建客户端）
        result = await cleanup_expired_traces()

        # ④ 断言：落在删除结果里 + 单查 404
        deleted_ids = {
            trace for batch in result.batches for trace in batch.deleted_ids
        }
        assert trace_id in deleted_ids, (
            f"清理结果未命中测试 trace（total_found={result.total_found}，"
            f"failed={result.failed_count}）"
        )
        await _wait_deleted(client, trace_id)  # 有界轮询至 404 并打印实测滞后
