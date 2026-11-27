"""integration 层：Langfuse 过期 trace 清理（app/crons/trace_cleanup.py）——真 Langfuse。

口径（任务书 4 + 2026-11-13 v4-native 迁移）：SDK-native 播种一条「过期」trace
（get_client() 底层 OTel tracer 造 root span，start_time 回填 = 被测同源的保留天数
+1 天缓冲之前，flush() 同步等导出）→ 等其可见（v2 observations 按 traceId 查）→ 跑
cleanup_expired_traces()（真 settings 路径）→ 断言该 trace 落在删除结果里且其
观测行经 v2 查询清空（删除收敛）。

【为何播种走 OTel 底层 tracer 而非 start_observation】SDK 的 start_observation
不暴露 span start_time（span.py:973 start_span(name=name) 不传，默认 now），过期
回填必须 OTel start_time——实测（2026-11-13）：start_time=过去 ns + 指定 trace_id
的裸 span 经 flush 后 trace.timestamp 精确回填（毫秒级一致），且该通路是 v4
OTel 原生消费（OTLP /api/public/otel/v1/traces），events_only 收口后依然可用
（legacy POST /api/public/ingestion 则会被拒）。flush() 是 Langfuse 客户端的
同步等导出出口。

【轮询读为何用 v2 observations】events_only 收口后 legacy 读面不可依赖（任务书
口径）；v2 /api/public/v2/observations?traceId=<id> 已探针验证（2026-11-13）。
可见信号=data 非空；删除收敛信号=data 清空（v2 直读事件表，trace 行删除 +
观测清理都收敛才空——比 legacy 单查 404 更强的终态信号）。

凭证纪律：连接信息只取仓库根 .env.test 的 LANGFUSE_ 组（dotenv_values 直读；
get_client() 走 os.environ 回填，cleanup_expired_traces 走 core/config 的
langfuse_settings 单例，同一部署栈）；凭据值只在内存流转，绝不打印、绝不进
断言消息。缺凭证时 skip（给 reason，不让命令红掉）；Langfuse 栈连接不可达
skip（环境未起），HTTP 4xx 照常 fail（配置/代码问题不掩蔽）——例外：两条轮询面
（`_wait_visible`/`_wait_deleted`）把 5xx 视为可重试瞬态（langfuse#8834 删除收敛窗
实测 2026-11-27：删除侧「过期 65，删除 65，失败 0」而 v2 /observations 查询间歇
500，且同一家族 500 也命中清理前可见性轮询），死线兜底不变。

删除可见性（langfuse#8834）：DELETE 受理 ≠ 查询立即可见——Langfuse 的删除经
队列落 ClickHouse 后异步收敛，v2 查询在观测清理收敛前仍可能非空。故第 ④ 步按
ARCH §7「滞后观察」做有界轮询（deadline 180s / 间隔 2s），并打印实测滞后秒数
作为观察数据（提额依据：实测 trace 行删除收敛 6.3s 直跑 / 87.5s 全量跑——
观测表清理滞后 ≥ trace 行，legacy 时代 120s 预算已贴边，v2 终态信号放宽到
180s，超时即红不掩蔽）。

副作用边界：cleanup_expired_traces 是全量幂等清理——跑一轮会顺带删除真实栈中
其他已过期 trace（M1 开发期数据留存 <7 天，预期为零）；测试 trace 自身由清理
收尾删除，无残留。
"""

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    set_span_in_context,
)

from app.core.config import langfuse_settings
from app.crons.trace_cleanup import cleanup_expired_traces

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_TEST_PATH = REPO_ROOT / ".env.test"

REQUIRED_KEYS = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
TRACE_NAME = "trace-cleanup-itest"
INGESTION_DEADLINE_S = (
    60  # SDK OTel 导出为异步管道（batch exporter→ClickHouse），可见轮询给宽预算
)
DELETION_DEADLINE_S = (
    180  # 删除受理→v2 观测清空的异步收敛（langfuse#8834），ARCH §7 滞后观察
)
# 为何 180s：legacy 时代 trace 行 404 收敛实测 6.3s（单跑）/ 87.5s（全量）已贴 120s；
# v2 信号=观测清理收敛（滞后 ≥ trace 行），放宽到 180s 保留误红余量
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


def _seed_expired_trace(trace_id: str, name: str, started_at: datetime) -> None:
    """SDK-native 播种：get_client() 底层 OTel tracer 造 root span + flush。

    start_time 回填过去（OTel 一等公民，SDK start_observation 不暴露）；trace_id
    经 SpanContext 指定（32hex，对账键语义与生产 trace_id_for 一致）；end_time =
    start + 1s（根 span 短闭合，导出即收敛）。flush() 同步等待 batch exporter
    发完（Langfuse.flush 语义）。
    """

    from langfuse import get_client

    client = get_client()
    context = set_span_in_context(
        NonRecordingSpan(
            SpanContext(
                trace_id=int(trace_id, 16),
                span_id=int(uuid.uuid4().hex[:16], 16),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
    )
    start_ns = int(started_at.timestamp() * 1e9)
    span = client._otel_tracer.start_span(
        name=name, start_time=start_ns, context=context
    )
    span.set_attribute("langfuse.trace.name", name)
    span.end(end_time=start_ns + 1_000_000_000)
    client.flush()


async def _v2_rows(client: httpx.AsyncClient, trace_id: str) -> list[dict]:
    """v2 observations 按 traceId 查（limit=1 足以判空/非空）。"""
    response = await client.get(
        _V2_OBSERVATIONS, params={"traceId": trace_id, "limit": 1}
    )
    response.raise_for_status()
    return response.json().get("data", [])


async def _wait_visible(client: httpx.AsyncClient, trace_id: str) -> None:
    """轮询 v2 observations 直至测试 trace 可见（OTel 导出异步收敛）。

    5xx 耐受与 `_wait_deleted` 同口径（langfuse#8834 收敛窗家族：v2 查询间歇 500
    实测同样命中清理前可见性轮询——2026-11-27 `_wait_deleted` 修复后复跑红一次，
    裸 HTTPStatusError 500 排除法定位到本函数）；4xx 照常上抛；可见死线不变。
    """
    deadline = time.monotonic() + INGESTION_DEADLINE_S
    while time.monotonic() < deadline:
        try:
            visible = bool(await _v2_rows(client, trace_id))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise  # 4xx=凭证/请求面问题，照常 fail
            print(
                f"[lag] v2 observations HTTP {exc.response.status_code}"
                "（服务端瞬态，langfuse#8834 收敛窗）——按可重试继续轮询"
            )
        else:
            if visible:
                return
        await asyncio.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"测试 trace 在 {INGESTION_DEADLINE_S}s 内未见于 Langfuse v2 observations（导出管道滞后？）"
    )


async def _wait_deleted(client: httpx.AsyncClient, trace_id: str) -> None:
    """轮询 v2 observations 直至清空（langfuse#8834：删除受理≠立即可见，ClickHouse 异步收敛）。

    打印实测滞后秒数（ARCH §7「滞后观察」的量化数据，随 `-rP` 在 passed 输出里可见）；
    超时仍非空则 fail 并带上滞后，不把「未收敛」误判成「未删除」。

    5xx 耐受（2026-11-27 真实栈实证，langfuse#8834 收敛窗家族）：删除受理侧已成功
    （日志「过期 65，删除 65，失败 0」）而 v2 /observations 查询间歇 500——轮询面把
    5xx 视为可重试瞬态继续轮询；4xx 照常立即上抛（不掩蔽凭证/配置问题）；死线内
    不清空仍 fail（清空终态断言不变）。
    """
    started = time.monotonic()
    deadline = started + DELETION_DEADLINE_S
    rows: list[dict] | None = None
    while time.monotonic() < deadline:
        try:
            rows = await _v2_rows(client, trace_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise  # 4xx=凭证/请求面问题，照常 fail
            print(
                f"[lag] v2 observations HTTP {exc.response.status_code}"
                "（服务端瞬态，langfuse#8834 收敛窗）——按可重试继续轮询"
            )
            await asyncio.sleep(POLL_INTERVAL_S)
            continue
        if not rows:
            lag_s = time.monotonic() - started
            print(f"[lag] trace {trace_id} 删除收敛（v2 观测清空）耗时 {lag_s:.1f}s")
            return
        await asyncio.sleep(POLL_INTERVAL_S)
    lag_s = time.monotonic() - started
    pytest.fail(
        f"trace 删除后 {DELETION_DEADLINE_S}s 内 v2 observations 仍非空"
        f"（实测滞后 {lag_s:.1f}s——langfuse#8834 收敛未完成或删除未生效）"
    )


# ---- _wait_deleted 轮询语义单测（mock 客户端零网络，锁鲁棒性修复的语义面）----
# 2026-11-27 真实栈实证（langfuse#8834 删除收敛窗家族）：删除侧日志「过期 65，删除 65，
# 失败 0」而 v2 /observations 查询间歇 500——旧实现把轮询直接红掉（全量套件与本用例
# 单跑两次复现）。以下三例锁定：5xx=可重试继续轮询 / 4xx=立即 fail（不掩蔽配置问题）/
# 死线内不清空仍 fail（清空终态断言不变）。


class _StubResponse:
    """httpx.Response 最小形态：raise_for_status 走真 httpx（HTTPStatusError 带 response）。"""

    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self._inner = httpx.Response(
            status_code,
            request=httpx.Request("GET", "http://langfuse.test/api/public/v2/observations"),
        )
        self._payload = payload if payload is not None else {}

    def raise_for_status(self) -> None:
        self._inner.raise_for_status()

    def json(self) -> dict:
        return self._payload


class _StubObservationsClient:
    """前 N 次 GET 返回 5xx、之后返回 final_payload（terminal_status 非空则恒返回该状态码）。"""

    def __init__(
        self,
        server_errors: int = 0,
        terminal_status: int | None = None,
        final_payload: dict | None = None,
    ) -> None:
        self.calls = 0
        self._server_errors = server_errors
        self._terminal_status = terminal_status
        self._final_payload = final_payload if final_payload is not None else {"data": []}

    async def get(self, url: str, params=None):  # noqa: ANN001, ARG002 —— 测试桩
        self.calls += 1
        if self.calls <= self._server_errors:
            return _StubResponse(500)
        if self._terminal_status is not None:
            return _StubResponse(self._terminal_status)
        return _StubResponse(200, self._final_payload)


async def test_wait_deleted_treats_5xx_polls_as_retryable(monkeypatch):
    """v2 observations 间歇 500 → 按可重试瞬态继续轮询至清空（不立即红）。"""
    monkeypatch.setitem(globals(), "POLL_INTERVAL_S", 0.001)
    stub = _StubObservationsClient(server_errors=2)
    await _wait_deleted(stub, "a" * 32)  # 不抛即通过
    assert stub.calls == 3  # 500×2 被耐受，第 3 次清空收敛


async def test_wait_deleted_4xx_fails_fast(monkeypatch):
    """4xx（凭证/请求面）仍立即上抛——不掩蔽配置问题，不作重试。"""
    monkeypatch.setitem(globals(), "POLL_INTERVAL_S", 0.001)
    stub = _StubObservationsClient(terminal_status=401)
    with pytest.raises(httpx.HTTPStatusError):
        await _wait_deleted(stub, "a" * 32)
    assert stub.calls == 1  # 未重试


async def test_wait_deleted_deadline_fail_semantics_unchanged(monkeypatch):
    """死线内观测行始终不清空 → 仍 fail（清空终态断言不变，修复不得放宽）。"""
    monkeypatch.setitem(globals(), "POLL_INTERVAL_S", 0.001)
    monkeypatch.setitem(globals(), "DELETION_DEADLINE_S", 0.05)

    class _NeverClear(_StubObservationsClient):
        async def get(self, url: str, params=None):  # noqa: ANN001, ARG002 —— 测试桩
            self.calls += 1
            return _StubResponse(200, {"data": [{"id": "o1"}]})

    stub = _NeverClear()
    with pytest.raises(pytest.fail.Exception):
        await _wait_deleted(stub, "a" * 32)


async def test_wait_visible_treats_5xx_polls_as_retryable(monkeypatch):
    """`_wait_visible` 同口径 5xx 耐受：间歇 500 → 继续轮询至可见（不立即红）。

    2026-11-27 实证：`_wait_deleted` 修复后复跑红一次（3.34s，裸 HTTPStatusError
    500——crons 侧 500 会包成 TraceCleanupException，排除法唯一落点=可见性轮询），
    同一环境性家族两条轮询面都命中。
    """
    monkeypatch.setitem(globals(), "POLL_INTERVAL_S", 0.001)
    stub = _StubObservationsClient(
        server_errors=2, final_payload={"data": [{"id": "o1"}]}
    )
    await _wait_visible(stub, "a" * 32)  # 不抛即通过
    assert stub.calls == 3  # 500×2 被耐受，第 3 次可见收敛


async def test_wait_visible_4xx_fails_fast(monkeypatch):
    """`_wait_visible` 4xx（凭证/请求面）仍立即上抛——不掩蔽配置问题。"""
    monkeypatch.setitem(globals(), "POLL_INTERVAL_S", 0.001)
    stub = _StubObservationsClient(terminal_status=401)
    with pytest.raises(httpx.HTTPStatusError):
        await _wait_visible(stub, "a" * 32)
    assert stub.calls == 1  # 未重试


async def test_wait_visible_deadline_fail_semantics_unchanged(monkeypatch):
    """死线内始终不可见 → 仍 fail（可见死线断言不变，修复不得放宽）。"""
    monkeypatch.setitem(globals(), "POLL_INTERVAL_S", 0.001)
    monkeypatch.setitem(globals(), "INGESTION_DEADLINE_S", 0.05)
    stub = _StubObservationsClient()  # 200 但 data 恒空 = 永不可见
    with pytest.raises(pytest.fail.Exception):
        await _wait_visible(stub, "a" * 32)


@pytest.mark.requires_langfuse
async def test_cleanup_deletes_expired_trace(
    langfuse_env, monkeypatch: pytest.MonkeyPatch
):
    """SDK-native 播种过期 trace → 跑清理 → 断言已删除（结果明细命中 + v2 观测清空）。"""
    # 回溯天数与被测同源：cleanup_expired_traces() 缺省读 langfuse_settings.
    # trace_retention_days（.env 路径），播种必须用同一单源；+1 天缓冲保证必过期。
    # LANGFUSE_TRACE_RETENTION_DAYS 只留在 .env.test.example 模板里、不作回溯依据
    # （回溯已改读 langfuse_settings 同源）——两处各读一份时单边改动会把「用本身例」
    # 误红成失败（解耦点）。
    retention_days = langfuse_settings.trace_retention_days + 1
    trace_id = uuid.uuid4().hex  # 32hex（对账键形态，与生产 trace_id_for 一致）
    backdated = datetime.now(UTC) - timedelta(days=retention_days)

    # get_client() 走环境变量：.env.test 的 LANGFUSE_ 组回填（已有值不覆盖）。
    # 经 monkeypatch 写入（用例结束自动还原，不污染进程 env 与后续用例），
    # 与 test/unit/test_observability.py 的 setenv/delenv 还原纪律对齐。
    for key in REQUIRED_KEYS:
        if key not in os.environ:
            monkeypatch.setenv(key, str(langfuse_env[key]))

    try:
        # ① SDK-native 播种（OTel start_time 回填 + flush 同步等导出）
        try:
            _seed_expired_trace(trace_id, TRACE_NAME, backdated)
        except Exception as exc:  # noqa: BLE001 —— 栈不可达按 skip 口径，不掩蔽 4xx/5xx
            pytest.skip(
                f"Langfuse 栈不可达（{type(exc).__name__}）——requires_langfuse 用例跳过"
            )

        async with httpx.AsyncClient(
            base_url=langfuse_env["LANGFUSE_HOST"],
            auth=(
                langfuse_env["LANGFUSE_PUBLIC_KEY"],
                langfuse_env["LANGFUSE_SECRET_KEY"],
            ),
            timeout=httpx.Timeout(10.0),
        ) as client:
            # ② 等其可见（v2 observations 按 traceId；可见即可被清理查询命中）
            await _wait_visible(client, trace_id)

            # ③ 跑清理（真 settings 路径：client=None → LangfuseSettings 自建客户端）
            result = await cleanup_expired_traces()

            # ④ 断言：落在删除结果里 + v2 观测清空（删除收敛终态）
            deleted_ids = {
                trace for batch in result.batches for trace in batch.deleted_ids
            }
            assert trace_id in deleted_ids, (
                f"清理结果未命中测试 trace（total_found={result.total_found}，"
                f"failed={result.failed_count}）"
            )
            await _wait_deleted(client, trace_id)  # 有界轮询至清空并打印实测滞后
    finally:
        # 兜底清理：清理未命中/断言失败路径下测试 trace 不残留栈中（幂等 DELETE）
        async with httpx.AsyncClient(
            base_url=langfuse_env["LANGFUSE_HOST"],
            auth=(
                langfuse_env["LANGFUSE_PUBLIC_KEY"],
                langfuse_env["LANGFUSE_SECRET_KEY"],
            ),
            timeout=httpx.Timeout(10.0),
        ) as client:
            response = await client.delete(f"/api/public/traces/{trace_id}")
            if response.status_code not in (200, 204, 404):
                print(f"[cleanup] 兜底 DELETE 未收敛：HTTP {response.status_code}")
