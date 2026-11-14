"""unit 层：Langfuse 过期 trace 清理（app/crons/trace_cleanup.py，ARCH §3.1 v1.19 / §7）。

零网络全 mock：Langfuse 公共 REST 假体经 httpx.MockTransport 注入 client 缝
（app/api/v1/health.py 同款做法）。覆盖点（任务书 4 + 2026-11-13 v4-native 迁移）：
- 保留期计算：cutoff = now(UTC) - LANGFUSE_TRACE_RETENTION_DAYS（默认 7，
  core/config.py LangfuseSettings.trace_retention_days）；
- 查询形态（v2 端点）：GET /api/public/v2/observations?toStartTime=<Z 形态 ISO>
  &limit=1000，cursor 链式翻页（meta.cursor 缺失即停），Basic auth = pk:sk
  （LangfuseSettings 派生）；
- 观测行 → trace id 推导：一行一条观测（id≠traceId），同 trace 多观测保序去重
  只推导一次（v2 迁移新增语义）；
- 分批切分：超过 50 条拆多批（BATCH_SIZE 硬上限 ≤50），逐批 DELETE；
- 批内失败语义：单条 DELETE 失败记入该批 failed（含 trace_id 与错误摘要），不中断
  批内其余条目与后续批次；查询失败（非 2xx / 传输错误）→ TraceCleanupException
  （保留 original_exception）；
- 幂等可重入：DELETE 404（已删）按成功收敛——重入对已删项重复删除是正常路径
  （v2 推导超集=已删 trace 孤儿观测，正靠此语义兜住）；
- 响应形态健壮性：meta 显式 null 不炸且正常终止翻页；顶层非 dict（JSON 数组）按
  响应解析失败收敛；服务端恒返非空 cursor 时达 MAX_PAGES 上限即停（CronLog
  warning 含上限与已收条数，按已收结果返回，不挂起）；
- 空列表：无过期 trace → 零 DELETE、total_found=0；
- 每批结果落结构化日志（CronLog）。

假体游标说明：真实 cursor 是 base64 不透明串；假体用页号字符串自洽模拟（给
cursor→实现带参继续拉，无 cursor→停），分页机制语义与真实一致、仅编码不同。
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
    LIST_PAGE_SIZE,
    TraceCleanupException,
    cleanup_expired_traces,
    compute_cutoff,
)

# 固定时钟：保留期计算与 toStartTime 断言的确定性基线
_FIXED_NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


class _FakeLangfuse:
    """Langfuse 公共 REST 假体（v2 形态）：按脚本应答 list / delete，记录收到的请求。

    - list_pages：GET /api/public/v2/observations 的应答队列（每项为该页的 trace id
      列表，每 trace 生成 2 条观测行暴露「观测行→traceId 去重」语义）；队列耗尽后
      一律回空页（模拟「翻到无数据页」）——勿回 v1 traces：2026-11-13 探针实测
      server v4 的 v2 参数面（toStartTime 真过滤、cursor 链式分页、traceId 字段）
      已钉死并迁移，实现走 v2 推导（见 trace_cleanup._list_expired_trace_ids 探针注）；
    - delete_status：trace_id → DELETE 应答码（缺省 204 成功）。
    """

    def __init__(
        self,
        list_pages: list[list[str]] | None = None,
        delete_status: dict[str, int] | None = None,
        *,
        endless_cursor: bool = False,
        null_meta_pages: set[int] | None = None,
    ) -> None:
        self.list_pages = list_pages or []
        self.delete_status = delete_status or {}
        self.endless_cursor = endless_cursor
        self.null_meta_pages = null_meta_pages or set()
        self.list_requests: list[httpx.Request] = []
        self.delete_requests: list[httpx.Request] = []
        self._seen_cursor: str | None = None

    @staticmethod
    def _observation_rows(page: list[str]) -> list[dict]:
        """把该页 trace id 列表展开成观测行：每 trace 2 条（SPAN+GENERATION）。"""
        return [
            {
                "id": f"obs-{trace_id}-{index}",
                "traceId": trace_id,
                "startTime": "2026-11-01T00:00:00.000Z",
                "type": "SPAN" if index == 0 else "GENERATION",
            }
            for trace_id in page
            for index in range(2)
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if (
            request.method == "GET"
            and request.url.path == "/api/public/v2/observations"
        ):
            self.list_requests.append(request)
            page_index = int(request.url.params.get("cursor", "0"))
            self._seen_cursor = request.url.params.get("cursor")
            page = (
                self.list_pages[page_index] if page_index < len(self.list_pages) else []
            )
            has_next = 0 < len(page) and page_index + 1 < len(self.list_pages)
            if self.endless_cursor:  # 退化服务端：页恒非空、cursor 恒非空（永不终止）
                page = page or [f"t-{page_index}"]
                has_next = True
            meta: dict | None = {"cursor": str(page_index + 1)} if has_next else {}
            if page_index in self.null_meta_pages:  # 显式 null meta 形态
                meta = None
            return httpx.Response(
                200,
                json={"data": self._observation_rows(page), "meta": meta},
            )
        if request.method == "DELETE" and request.url.path.startswith(
            "/api/public/traces/"
        ):
            self.delete_requests.append(request)
            trace_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(self.delete_status.get(trace_id, 204))
        return httpx.Response(404, json={"error": "unexpected route"})

    def delete_ids(self) -> list[str]:
        return [request.url.path.rsplit("/", 1)[-1] for request in self.delete_requests]


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
    assert fake.list_requests[0].url.params["toStartTime"] == "2026-11-04T12:00:00Z"


# ---- 查询形态（REST + Basic auth + v2 cursor 分页）----


def test_http_client_targets_settings_host_with_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
):
    """出站客户端同源于 LangfuseSettings：base_url=LANGFUSE_HOST、Basic auth=pk:sk。"""
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
            await client.get("/api/public/v2/observations")
        finally:
            await client.aclose()

    asyncio.run(_run())
    expected = "Basic " + base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    # base_url 断言锚 settings.host 本身（同源语义）：MockTransport 忽略 _client 的
    # 占位 base_url，_http_client 自建时 base_url 必为 LangfuseSettings.host
    assert str(captured[0].url).startswith(str(trace_cleanup.langfuse_settings.host))
    assert captured[0].headers["Authorization"] == expected


async def test_list_queries_cutoff_limit_and_cursor_paging():
    """查询带 toStartTime（Z 形态 ISO）+ limit=LIST_PAGE_SIZE，cursor 链式翻页。"""
    fake = _FakeLangfuse(list_pages=[_ids("a", 5), _ids("b", 2), []])
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    first, second = fake.list_requests[0], fake.list_requests[1]
    assert first.url.params["toStartTime"] == "2026-10-31T12:00:00Z"
    assert first.url.params["limit"] == str(LIST_PAGE_SIZE)
    assert "cursor" not in first.url.params  # 首页不带 cursor
    assert second.url.params["cursor"] == "1"  # 链式带上一页 meta.cursor
    assert len(fake.list_requests) == 3  # 第 3 页空页（meta 无 cursor）后停
    assert result.cutoff == _FIXED_NOW - timedelta(days=7)
    assert result.total_found == 7


async def test_dedupes_trace_ids_across_observations():
    """观测行 → trace id 推导去重：同 trace 多观测（页内重复）只推导一次、只删一次。"""
    fake = _FakeLangfuse(list_pages=[["t-1", "t-2", "t-1"]])  # t-1 页内两条观测
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    assert result.total_found == 2  # t-1/t-2 去重后
    assert sorted(fake.delete_ids()) == ["t-1", "t-2"]
    assert result.deleted_count == 2


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

    两批构造 = 满批 50（第 1 页）+ 余批 2（第 2 页）——cursor 链把两页观测行
    拼成 52 条总量切出（50/2）。
    """
    fake = _FakeLangfuse(
        list_pages=[_ids("a", BATCH_SIZE), _ids("b", 2)], delete_status={"a-24": 500}
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
    """响应 200 但条目缺 traceId（响应形态异常）→ TraceCleanupException，不进入删除循环。"""
    fake = _FakeLangfuse()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"no_traceId": 1}], "meta": {}})

    with pytest.raises(TraceCleanupException) as exc_info:
        await cleanup_expired_traces(client=_client(handler), now=_FIXED_NOW)
    assert isinstance(exc_info.value.original_exception, KeyError)
    assert fake.delete_requests == []


# ---- 响应形态健壮性：meta null / 顶层非 dict / 翻页上限 ----


async def test_null_meta_terminates_paging_without_attribute_error():
    """meta 显式 null（非 dict）：不抛 AttributeError，按「无 cursor」正常终止翻页。"""
    fake = _FakeLangfuse(
        list_pages=[["t-1"], ["t-2"]], null_meta_pages={1}
    )  # 第 2 页 meta=null
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    assert len(fake.list_requests) == 2  # meta=null 后终止，无第 3 次请求
    assert result.total_found == 2


async def test_non_dict_top_level_payload_raises_trace_cleanup_exception():
    """顶层非 dict（如 JSON 数组）→ 按响应形态异常收敛为 TraceCleanupException。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"traceId": "t-1"}])

    with pytest.raises(TraceCleanupException) as exc_info:
        await cleanup_expired_traces(client=_client(handler), now=_FIXED_NOW)
    assert isinstance(exc_info.value.original_exception, AttributeError)


async def test_paging_capped_at_max_pages_with_warning(
    monkeypatch: pytest.MonkeyPatch, log_records
):
    """服务端恒返非空 cursor：达 MAX_PAGES 即停（不挂起），warning 落日志、按已收返回。"""
    monkeypatch.setattr(trace_cleanup, "MAX_PAGES", 3)  # 小上限：语义与 10000 页同
    fake = _FakeLangfuse(endless_cursor=True)
    result = await cleanup_expired_traces(
        client=_client(fake.handler), retention_days=7, now=_FIXED_NOW
    )
    assert len(fake.list_requests) == 3  # 恰好 3 页后收敛
    assert result.total_found == 3
    caps = [
        record
        for record in log_records
        if record["extra"].get("task") == "trace_cleanup"
        and record["level"].name == "WARNING"
        and "分页上限" in record["message"]
    ]
    assert len(caps) == 1
    assert "分页上限 3 页" in caps[0]["message"]
    assert caps[0]["extra"]["total"] == 3  # 已收条数入结构化字段


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
        list_pages=[_ids("a", BATCH_SIZE), _ids("b", 1)], delete_status={"a-24": 500}
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
