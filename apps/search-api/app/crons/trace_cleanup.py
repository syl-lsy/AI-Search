"""Langfuse 过期 trace 清理（每日周期任务；ARCH §3.1 v1.19 占位转正 / §7 LANGFUSE_TRACE_RETENTION_DAYS）。

背景：Langfuse OSS 自部署无内置 trace 保留治理（EE 付费能力，官方文档定案），磁盘
治理由本任务自研承担——按 LANGFUSE_TRACE_RETENTION_DAYS（默认 7，core/config.py
LangfuseSettings.trace_retention_days）计算截止时间，经 Langfuse 公共 REST API 查询
过期 trace 并逐批删除，每批 ≤50（ARCH §3.1 crons 注）。

【为什么走 REST 不走 Langfuse SDK】公共 REST 是稳定契约面（Basic auth =
LANGFUSE_PUBLIC_KEY:LANGFUSE_SECRET_KEY，目标 LANGFUSE_HOST）；Langfuse SDK 5.2.0
API 面换代快且不提供删除能力（s0-batch1-handoff.md 续 10/13）。

【观察项（langfuse#8834）】本任务只经公共 API 删 trace 主记录；ClickHouse
（observations/scores）与 MinIO（blobs）的物理清理由 Langfuse 内部管道滞后处理，
v3 曾有 MinIO 残留 bug（langfuse#8834）——M1 部署后需实测磁盘回收效果并观察
ClickHouse/MinIO 清理滞后，不是本任务代码面能闭环的问题。

分批形态：GET /api/public/traces?toTimestamp=<cutoff>&limit=50&page=N 收齐全部过期
id（页不足 limit 即停）→ 按 BATCH_SIZE(50) 切批 → 逐条 DELETE /api/public/traces/{id}，
逐批记录结果（CronLog 结构化日志 + 返回值 TraceCleanupResult）。

失败语义：
- 查询失败（非 2xx / 传输错误）→ TraceCleanupException（保留 original_exception），
  由调度器（main.py lifespan 接线，B1 后续工单）处理、下个周期重试；
- 批内单条 DELETE 失败（非 2xx / 传输错误）→ 记入该批 failed（trace_id + 摘要），
  不中断批内其余条目与后续批次——清理是幂等收敛型任务，残余项下轮自然重试；
- DELETE 404 按「已删」收敛为成功：重入对已删项重复删除是正常路径，终态=不存在。

幂等与可重入：任务无本地状态——每轮独立查询「当前仍过期的 trace」再删除；重复
执行/中断重跑只会对剩余项重复查询与删除，不放大副作用。

出站安全：目标仅取自 core/config 的 LangfuseSettings.host（scheme 已在配置层
HttpUrlStr 校验），URL 不来自请求参数/用户输入（SSRF 面禁项，ARCH §7）；第一方
基建端点绑 127.0.0.1 属部署设计，不做内网地址拒绝。
"""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlparse

import httpx

from app.core.config import langfuse_settings
from app.core.logger import CronLog
from app.exception.base import BusinessException

# 环境无关的纯逻辑值收敛为常量（配置三分法 → 代码常量，语义化命名收敛于此）：
BATCH_SIZE: int = 50  # 单批删除上限（ARCH §3.1：每批 ≤50）
PAGE_SIZE: int = 50  # 列表分页大小（Langfuse 公共 API 单页 limit 上限即 50）
TRACE_CLEANUP_INTERVAL_SECONDS: int = 86_400  # 每日一次（注册表注册项，ARCH §3.1）
_REST_TIMEOUT_S: float = 10.0  # 单请求超时：内网自部署 REST，宽松但不悬挂
_TASK_NAME: str = "trace_cleanup"  # 注册表 name（CronLog 的 task 字段同源）


class TraceCleanupException(BusinessException):
    """过期 trace 查询失败（crons 运行态异常，非 HTTP 面）。

    不注册 ERROR_MAP 独立错误码——本异常只在任务进程内流转、不进 HTTP 响应；
    若未来经统一处理器兜底，按 MRO 落 Exception → 500001（内部错误）语义等价，
    不为非契约面新增错误码（api.md §0 错误码表冻结）。
    """


@dataclass(frozen=True)
class TraceCleanupBatchResult:
    """单批清理结果（≤BATCH_SIZE 条）：批号从 1 起、批内条数、成功/失败明细。

    failed 为 (trace_id, 失败摘要) 元组序列——摘要形如 "HTTP 500" / "ConnectError"，
    不含服务端响应体（防意外回显内部细节）。
    """

    batch_index: int
    total: int
    deleted_ids: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TraceCleanupResult:
    """整轮清理汇总：截止时间、过期总数、逐批结果（失败项下轮重入重试）。"""

    cutoff: datetime
    total_found: int
    batches: tuple[TraceCleanupBatchResult, ...] = ()

    @property
    def deleted_count(self) -> int:
        return sum(len(batch.deleted_ids) for batch in self.batches)

    @property
    def failed_count(self) -> int:
        return sum(len(batch.failed) for batch in self.batches)


def _validate_outbound_url(url: str) -> str:
    """出站 URL scheme 闸口：仅放行 http/https（URL 唯一来源是 settings，health.py 同款）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"出站 URL 必须 http/https，得到：{url!r}")
    return url


def _http_client(
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """出站 REST 客户端工厂：base_url 与 Basic auth 同源于 LangfuseSettings。

    单测经 transport 缝注入 httpx.MockTransport 假体（app/api/v1/health.py 同款做法）。
    """
    return httpx.AsyncClient(
        base_url=_validate_outbound_url(langfuse_settings.host),
        auth=(
            langfuse_settings.public_key,
            langfuse_settings.secret_key.get_secret_value(),
        ),
        timeout=httpx.Timeout(_REST_TIMEOUT_S),
        transport=transport,
    )


def compute_cutoff(now: datetime, retention_days: int) -> datetime:
    """截止时间 = now(UTC) - 保留天数；早于它的 trace 视为过期（§7 保留期语义）。"""
    if retention_days < 1:
        raise ValueError(f"保留天数必须 ≥1，得到：{retention_days}")
    return now - timedelta(days=retention_days)


def _format_utc(value: datetime) -> str:
    """ISO 8601 UTC（秒精度 Z 后缀）：Langfuse 服务端 zod 校验默认只收 Z 形态。"""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _list_expired_trace_ids(
    client: httpx.AsyncClient, cutoff: datetime
) -> list[str]:
    """收齐全部过期 trace id：toTimestamp=cutoff 分页拉取（页不足 PAGE_SIZE 即停）。"""
    ids: list[str] = []
    page = 1
    while True:
        try:
            response = await client.get(
                "/api/public/traces",
                params={
                    "toTimestamp": _format_utc(cutoff),
                    "limit": PAGE_SIZE,
                    "page": page,
                },
            )
            response.raise_for_status()
            data = response.json().get("data", [])
            page_ids = [item["id"] for item in data]
        except httpx.HTTPError as exc:
            raise TraceCleanupException(
                "查询 Langfuse 过期 trace 失败",
                detail=f"GET /api/public/traces 失败：{type(exc).__name__}",
                original_exception=exc,
            ) from exc
        except (ValueError, KeyError, TypeError) as exc:  # 非 JSON / 条目缺 id
            raise TraceCleanupException(
                "Langfuse 过期 trace 响应解析失败",
                detail=f"GET /api/public/traces 响应形态异常：{type(exc).__name__}",
                original_exception=exc,
            ) from exc
        ids.extend(page_ids)
        if len(data) < PAGE_SIZE:
            return ids
        page += 1


async def _delete_trace(client: httpx.AsyncClient, trace_id: str) -> str | None:
    """删除单条 trace。返回 None=成功（含 404 幂等收敛）；否则返回失败摘要。

    摘要只带状态码/异常类型名，不带响应体（防服务端内部细节回显进日志）。
    """
    url = f"/api/public/traces/{quote(trace_id, safe='')}"
    try:
        response = await client.delete(url)
        if response.status_code == 404:
            return None  # 已删：幂等重入的正常路径，按成功收敛（终态=不存在）
        if response.is_success:
            return None
        return f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return type(exc).__name__


async def _delete_batch(
    client: httpx.AsyncClient, batch_index: int, trace_ids: list[str]
) -> TraceCleanupBatchResult:
    """删除一批（≤BATCH_SIZE 条）：单条失败记入 failed、批内其余继续；结果落 CronLog。"""
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []
    for trace_id in trace_ids:
        reason = await _delete_trace(client, trace_id)
        if reason is None:
            deleted.append(trace_id)
        else:
            failed.append((trace_id, reason))

    batch = TraceCleanupBatchResult(
        batch_index=batch_index,
        total=len(trace_ids),
        deleted_ids=tuple(deleted),
        failed=tuple(failed),
    )
    # 每批结果结构化落日志（计数为 0 的字段被 _emit 假值过滤，不落盘）
    log = CronLog(
        message=(
            f"trace_cleanup 批次 {batch_index}：{len(deleted)}/{len(trace_ids)} 成功"
            + (f"，{len(failed)} 失败" if failed else "")
        ),
        task=_TASK_NAME,
        batch_index=batch_index,
        total=len(trace_ids),
        ok=len(deleted),
        failed=len(failed),
    )
    if failed:
        log.warning()
    else:
        log.info()
    return batch


def _log_summary(result: TraceCleanupResult) -> None:
    """整轮汇总落日志：全成功 success，有失败 warning（明细在批次日志与返回值里）。"""
    log = CronLog(
        message=(
            f"trace_cleanup 完成：cutoff={_format_utc(result.cutoff)}，"
            f"过期 {result.total_found}，删除 {result.deleted_count}，"
            f"失败 {result.failed_count}"
        ),
        task=_TASK_NAME,
        total=result.total_found,
        ok=result.deleted_count,
        failed=result.failed_count,
    )
    if result.failed_count:
        log.warning()
    else:
        log.success()


async def cleanup_expired_traces(
    client: httpx.AsyncClient | None = None,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> TraceCleanupResult:
    """清理过期 Langfuse trace（幂等可重入；ARCH §3.1 crons 注 / §7 保留期）。

    Args:
        client: 出站客户端（单测注入 MockTransport 假体）；缺省按 LangfuseSettings
            自建（本函数持有生命周期，结束后关闭；注入者自管）。
        retention_days: 保留天数；缺省读 LANGFUSE_TRACE_RETENTION_DAYS（默认 7）。
        now: 当前时间（单测注入固定时钟）；缺省取 datetime.now(UTC)。

    Returns:
        TraceCleanupResult：截止时间 + 逐批结果（失败明细含 trace_id，下轮重入重试）。

    Raises:
        TraceCleanupException: 查询阶段失败（删除阶段失败不抛——记入批次结果）。
    """
    days = (
        retention_days
        if retention_days is not None
        else langfuse_settings.trace_retention_days
    )
    cutoff = compute_cutoff(now or datetime.now(UTC), days)

    own_client = client is None
    if own_client:
        client = _http_client()
    try:
        try:
            trace_ids = await _list_expired_trace_ids(client, cutoff)
        except TraceCleanupException as exc:
            CronLog(
                message="trace_cleanup 查询过期 trace 失败——本周期放弃，待下周期重试",
                task=_TASK_NAME,
            ).error(exc)
            raise
        batch_results = [
            await _delete_batch(client, batch_index, chunk)
            for batch_index, chunk in enumerate(
                (
                    trace_ids[start : start + BATCH_SIZE]
                    for start in range(0, len(trace_ids), BATCH_SIZE)
                ),
                start=1,
            )
        ]
    finally:
        if own_client:
            await client.aclose()

    result = TraceCleanupResult(
        cutoff=cutoff, total_found=len(trace_ids), batches=tuple(batch_results)
    )
    _log_summary(result)
    return result
