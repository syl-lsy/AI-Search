"""unit 层：Langfuse 过期 trace 清理（app/crons/trace_cleanup.py，ARCH §3.1 v1.19 / §7）。

零网络全 mock：Langfuse 公共 REST 假体经 httpx.MockTransport 注入 client 缝
（app/api/v1/health.py 同款做法）。覆盖点（任务书 4）：
- 保留期计算：cutoff = now(UTC) - LANGFUSE_TRACE_RETENTION_DAYS（默认 7，
  core/config.py LangfuseSettings.trace_retention_days）；
- 查询形态：GET /api/public/traces?toTimestamp=<Z 形态 ISO>&limit=50&page=N，
  Basic auth = pk:sk（LangfuseSettings 派生）；
- 分批切分：超过 50 条拆多批（BATCH_SIZE 硬上限 ≤50），逐批 DELETE；
- 批内失败语义：单条 DELETE 失败记入该批 failed（含 trace_id 与错误摘要），不中断
  批内其余条目与后续批次；查询失败（非 2xx / 传输错误）→ TraceCleanupException
  （保留 original_exception）；
- 幂等可重入：DELETE 404（已删）按成功收敛——重入对已删项重复删除是正常路径；
- 空列表：无过期 trace → 零 DELETE、total_found=0；
- 每批结果落结构化日志（CronLog）。
"""

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.crons import trace_cleanup
from app.crons.trace_cleanup import (
    BATCH_SIZE,
    PAGE_SIZE,
    TRACE_CLEANUP_INTERVAL_SECONDS,
    TraceCleanupException,
    cleanup_expired_traces,
    compute_cutoff,
)

# 固定时钟：保留期计算与 toTimestamp 断言的确定性基线
_FIXED_NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


class _FakeLangfuse:
    """Langfuse 公共 REST 假体：按脚本应答 list / delete，记录收到的请求。

    - list_pages：GET /api/public/traces 的应答队列（每项为该页的 trace id 列表），
      队列耗尽后一律回空页（模拟「翻到无数据页」）；
    - delete_status：trace_id → DELETE 应答码（缺省 204 成功）。
    """

    def __init__(
        self,
        list_pages: list[list[str]] | None = None,
        delete_status: dict[str, int] | None = None,
    ) -> None:
        self.list_pages = list_pages or []
        self.delete_status = delete_status or {}
        self.list_requests: list[httpx.Request] = []
        self.delete_requests: list[httpx.Request] = []
        self._page_cursor = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/public/traces":
            self.list_requests.append(request)
            page = (
                self.list_pages[self._page_cursor]
                if self._page_cursor < len(self.list_pages)
                else []
            )
            self._page_cursor += 1
            return httpx.Response(
                200, json={"data": [{"id": trace_id} for trace_id in page], "meta": {}}
            )
        if request.method == "DELETE" and request.url.path.startswith(
            "/api/public/traces/"
        ):
            self.delete_requests.append(request)
            trace_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(self.delete_status.get(trace_id, 204))
        return httpx.Response(404, json={"error": "unexpected route"})

    def delete_ids(self) -> list[str]:
        return [
            request.url.path.rsplit("/", 1)[-1] for request in self.delete_requests
        ]


def _client(handler) -> httpx.AsyncClient:
    """注入 MockTransport 的测试客户端（base_url 任意——真实拼接见 test_http_client_*）。"""
    return httpx.AsyncClient(
        base_url="http://langfuse.test", transport=httpx.MockTransport(handler)
    )


def _ids(prefix: str, count: int) -> list[str]:
    return [f"{prefix}-{index}" for index in range(count)]


# ---- 保留期计算（LANGFUSE_TRACE_RETENTION_DAYS，默认 7）----


def test_compute_cutoff_subtracts_retention_days():
    """截止时间 = now(UTC) - 保留天数；早于它的 trace 视为过期。"""
    assert compute_cutoff(_FIXED_NOW, 7) == _FIXED_NOW - timedelta(days=7)


def test_compute_cutoff_rejects_non_positive_retention():
    """保留天数非正即拒绝（settings 层 ge=1，此处为函数级兜底防线）。"""
    with pytest.raises(ValueError):
        compute_cutoff(_FIXED_NOW, 0)


def test_retention_days_default_reads_settings(monkeypatch: pytest.MonkeyPatch):
    """retention_days 缺省读 LangfuseSettings.trace_retention_days（.env LANGFUSE_TRACE_RETENTION_DAYS）。"""
    monkeypatch.setattr(trace_cleanup.langfuse_settings, "trace_retention_days", 3)
    fake = _FakeLangfuse(list_pages=[[]])

    async def _run() -> None:
        await cleanup_expired_traces(client=_client(fake.handler), now=_FIXED_NOW)

    asyncio.run(_run())
    assert fake.list_requests[0].url.params["toTimestamp"] == "2026-11-04T12:00:00Z"


# ---- 查询形态（REST + Basic auth + 分页）----


def test_http_client_targets_settings_host_with_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
):
    """出站客户端同源于 LangfuseSettings：base_url=LANGFUSE_HOST、Basic auth=pk:sk。"""
    monkeypatch.setattr(trace_cleanup.langfuse_settings, "host", "http://langfuse.test")
    monkeypatch.setattr(trace_cleanup.langfuse_settings, "public_key", "pk-lf-test")
    monkeypatch.setattr(
        trace_cleanup.langfuse_settings, "secret_key", SecretStr("sk-lf-test")
    )
    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": []})

    async def _run() -> None:
        client = trace_cleanup._http_client(transport=httpx.MockTransport(_capture))
        try:
            await client.get("/api/public/traces")
        finally:
            await client.aclose()

    asyncio.run(_run())
    expected = "Basic " + base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert str(captured[0].url).startswith("http://langfuse.test")
    assert captured[0].headers["Authorization"] == expected


async def test_list_queries_cutoff_limit_and_pages():
    """查询带 toTimestamp（Z 形态 ISO）+ limit=50 + page 递增翻页。"""
    fake = _FakeLangfuse(list_pages=[_ids("a", PAGE_SIZE), []])
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    first = fake.list_requests[0]
    assert first.url.params["toTimestamp"] == "2026-10-31T12:00:00Z"
    assert first.url.params["limit"] == str(PAGE_SIZE)
    assert first.url.params["page"] == "1"
    assert fake.list_requests[1].url.params["page"] == "2"
    assert result.cutoff == _FIXED_NOW - timedelta(days=7)


# ---- 分批切分（超过 50 拆批，每批 ≤50）----


async def test_deletes_chunked_into_batches_of_at_most_fifty():
    """120 条过期 trace → 3 批（50/50/20），DELETE 共 120 次。"""
    fake = _FakeLangfuse(list_pages=[_ids("t", 120)])
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    assert [batch.total for batch in result.batches] == [50, 50, 20]
    assert [batch.batch_index for batch in result.batches] == [1, 2, 3]
    assert len(fake.delete_requests) == 120
    assert result.total_found == 120
    assert result.deleted_count == 120
    assert result.failed_count == 0


# ---- 批内失败语义 ----


async def test_in_batch_delete_failure_recorded_without_aborting():
    """单条 DELETE 失败：记入该批 failed（id+摘要）、批内其余继续、后续批次照常。

    两批构造 = 满页 50（第 1 页）+ 余页 2（第 2 页）——页不足 limit 即停是分页
    停止语义，两批须靠 52 条总量切出（50/2）。
    """
    fake = _FakeLangfuse(
        list_pages=[_ids("a", PAGE_SIZE), _ids("b", 2)], delete_status={"a-24": 500}
    )
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    assert len(result.batches) == 2  # 后续批次未被中断
    assert result.batches[0].total == 50
    assert result.batches[0].failed == (("a-24", "HTTP 500"),)
    assert len(result.batches[0].deleted_ids) == 49
    assert result.batches[1].failed == ()
    assert result.batches[1].deleted_ids == ("b-0", "b-1")
    assert result.deleted_count == 51
    assert result.failed_count == 1
    assert len(fake.delete_requests) == 52


async def test_delete_404_counts_as_deleted_idempotent():
    """DELETE 404（已删）按成功收敛：幂等重入时对已删项重复删除是正常路径。"""
    fake = _FakeLangfuse(list_pages=[["t-1"]], delete_status={"t-1": 404})
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    assert result.batches[0].failed == ()
    assert result.batches[0].deleted_ids == ("t-1",)
    assert result.failed_count == 0


async def test_delete_transport_failure_recorded_without_raising():
    """单条 DELETE 传输错误（连接层）：同样记 failed 不炸批（与 HTTP 失败同语义）。"""
    fake = _FakeLangfuse(list_pages=[["t-1", "t-2"]])

    original_handler = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.endswith("t-1"):
            raise httpx.ConnectError("connection reset")
        return original_handler(request)

    result = await cleanup_expired_traces(
        client=_client(handler), retention_days=7, now=_FIXED_NOW
    )
    assert len(result.batches[0].failed) == 1
    assert result.batches[0].failed[0][0] == "t-1"
    assert result.deleted_count == 1  # t-2 照常删除


# ---- 查询失败语义 ----


async def test_list_http_error_raises_trace_cleanup_exception():
    """查询非 2xx（如鉴权失败 401）→ TraceCleanupException，不进入删除循环。"""
    fake = _FakeLangfuse()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(204)

    with pytest.raises(TraceCleanupException):
        await cleanup_expired_traces(client=_client(handler), now=_FIXED_NOW)
    assert fake.delete_requests == []


async def test_list_transport_error_wraps_original_exception():
    """查询传输错误 → TraceCleanupException 且保留 original_exception（根因追溯）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(TraceCleanupException) as exc_info:
        await cleanup_expired_traces(client=_client(handler), now=_FIXED_NOW)
    assert isinstance(exc_info.value.original_exception, httpx.ConnectError)


async def test_list_malformed_payload_raises_trace_cleanup_exception():
    """响应 200 但条目缺 id（响应形态异常）→ TraceCleanupException，不进入删除循环。"""
    fake = _FakeLangfuse()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"no_id": 1}], "meta": {}})

    with pytest.raises(TraceCleanupException) as exc_info:
        await cleanup_expired_traces(client=_client(handler), now=_FIXED_NOW)
    assert isinstance(exc_info.value.original_exception, KeyError)
    assert fake.delete_requests == []


# ---- 空列表 ----


async def test_empty_list_makes_no_delete_requests():
    """无过期 trace：零 DELETE、结果 total_found=0 且无批次。"""
    fake = _FakeLangfuse(list_pages=[[]])
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    assert result.total_found == 0
    assert result.batches == ()
    assert fake.delete_requests == []
    assert result.deleted_count == 0


# ---- 每批结果落结构化日志 ----


async def test_each_batch_result_logged(log_records):
    """每批结果经 CronLog 结构化落日志（task/batch_index/total/ok/failed）。"""
    fake = _FakeLangfuse(
        list_pages=[_ids("a", PAGE_SIZE), _ids("b", 1)], delete_status={"a-24": 500}
    )
    await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    batch_logs = [
        record
        for record in log_records
        if record["extra"].get("task") == "trace_cleanup"
        and "batch_index" in record["extra"]
    ]
    assert len(batch_logs) == 2  # 两批各落一条
    first = batch_logs[0]["extra"]
    assert first["batch_index"] == 1
    assert first["total"] == 50
    assert first["ok"] == 49
    assert first["failed"] == 1
