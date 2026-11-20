"""safety.content_guard：B15 内容侧护栏**接线面**（内容进上下文的最早公共点消费件）。

定位（plan-b15 §2 接入点表）：模块一/二提供判定原语（validate_url / scan_injection），
本模块提供接线原语——async 包络（DNS/正则不阻塞事件循环）、候选/报告/图片三个过滤
原语与统一的 errors[] 留痕形态。消费面：
- fast 搜索摘要（web_search 节点逐候选扫 title+snippet+content）；
- deep 搜索闭包（add_candidates 前——drop 不占 ref_no）；
- deep 读窗（包裹前扫窗口文本——顺序锁：原始文本可检出、转义后种子消失）；
- 子代理报告（summary/hard_facts/quotes）；图片收口（fuse_rerank images 出侧）。

fail-open 语义（安全检查不挡检索同构）：包络超预算放行/直通 + safety 域告警一次；
guard None（机制关/不可用）原样直通零成本。

告警纪律（PRD REQ-066）：AgentLog.warning 只记 rule_id/host/字符数——**零 payload
原文、零完整 URL**；errors[] reason ∈ {url_rejected, injection_blocked}（state
errors 非封闭枚举——api.md 仅 degraded_reason 封闭 timeout|budget|source_failure，
本模块零 degraded 触碰）。resolver 缝：`DEFAULT_RESOLVER` 模块属性（生产 =
socket.getaddrinfo；测试经 conftest autouse 离线化——零网络纪律）。
"""

import asyncio
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from app.agent.safety.config import ContentGuardConfig, InjectionFilterConfig
from app.agent.safety.injection import InjectionScan, scan_injection
from app.agent.safety.url_guard import Resolver, UrlGuardConfig, UrlGuardResult, validate_url
from app.core.logger import AgentLog

__all__ = [
    "DEFAULT_RESOLVER",
    "GUARD_ENVELOPE_S",
    "UrlRejectedError",
    "filter_candidates",
    "filter_image_items",
    "filter_report",
    "scan_fields",
    "scan_text_async",
    "url_host",
    "validate_url_async",
]

#: 接线面包络（DNS 解析/批量扫描线程预算；超预算 fail-open——护栏不阻塞主链路）。
#: 纯逻辑守卫预算=代码常量（配置三分法：与环境无关；入参 URL 形态有界）
GUARD_ENVELOPE_S = 3.0

def _socket_resolver(host: str) -> list[str]:
    """socket.getaddrinfo 全地址族（IPv4/IPv6）去重保序（生产缺省 resolver）。"""
    infos = socket.getaddrinfo(host, None)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


#: 缺省 resolver（模块缝：生产 = socket.getaddrinfo 全地址族；测试 autouse 离线化）
DEFAULT_RESOLVER: Resolver = _socket_resolver


class UrlRejectedError(Exception):
    """URL 被护栏拒收（reason 携带枚举词——零 URL/host 回显）。未接线工具面抛出、
    接线面转为系统注 + errors 留痕（未来接线沿同语义消费）。"""


def _warn(message: str) -> None:
    AgentLog(message=message, graph="safety", node="content_guard").warning()


def url_host(url: str) -> str:
    """host 提取（告警只记 host——零完整 URL）。"""
    try:
        return urlparse(url).hostname or "-"
    except ValueError:
        return "-"


def scan_fields(fields: Iterable[Any], config: InjectionFilterConfig) -> InjectionScan:
    """多字段注入扫描（逐字段独立扫——拼接会产生跨字段误匹配面）；非字符串安全收口，
    hits 按 (rule_id) 首见序并集。"""
    hits: list[str] = []
    for field in fields:
        if not isinstance(field, str) or not field:
            continue
        for rule_id in scan_injection(field, config).hits:
            if rule_id not in hits:
                hits.append(rule_id)
    return InjectionScan(hits=tuple(hits), truncated=False)


async def validate_url_async(
    url: str,
    guard: UrlGuardConfig,
    *,
    timeout_s: float = GUARD_ENVELOPE_S,
    resolver: Resolver | None = None,
) -> UrlGuardResult:
    """validate_url 的线程包络（to_thread + wait_for）：DNS 不阻塞事件循环。

    **包络超时 fail-closed（评审 P2）**：超时可被攻击者廉价诱导（护栏侧首解拖
    >timeout_s、抓取侧后续快解回恶意 IP——比 R21 rebinding 更廉价的穿越面），超时
    一律拒收（dns_unresolved）+ 告警一次；基础设施故障类（resolver 抛非超时异常）
    仍由 url_guard._check_dns 内部裁定（fail-open）。"""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(validate_url, url, guard, resolver=resolver or DEFAULT_RESOLVER),
            timeout_s,
        )
    except TimeoutError:
        _warn(
            f"url_guard 校验超时（fail-closed 拒收，超时面可被诱导）："
            f"host={url_host(url)} timeout_s={timeout_s}"
        )
        return UrlGuardResult(ok=False, reason="dns_unresolved")


async def scan_text_async(
    text: str,
    config: InjectionFilterConfig,
    *,
    timeout_s: float = GUARD_ENVELOPE_S,
) -> InjectionScan:
    """文本注入扫描线程包络（读窗/报告文本——ReDoS 残余第二道防线）；超预算 fail-open
    零命中 + 告警一次。"""
    try:
        return await asyncio.wait_for(asyncio.to_thread(scan_injection, text, config), timeout_s)
    except TimeoutError:
        _warn(f"注入扫描超预算（fail-open 直通）：字符数={len(text)}")
        return InjectionScan()


def filter_candidates(
    candidates: list[Any],
    guards: ContentGuardConfig | None,
    *,
    stage: str = "search",
) -> tuple[list[Any], list[dict[str, Any]]]:
    """搜索候选逐条注入扫描（title+snippet+content——content 上游携带才参与）。

    flag=保留 + errors 留痕；drop=丢弃（来源卡/[n] 自然一致——调用方以返回的 kept
    列表为准）；guard None（机制关）原样直通零成本。errors 聚合单条：
    {stage, reason: injection_blocked, count=命中候选数}（零 payload 零 URL）。
    """
    inj = guards.injection_filter if guards is not None else None
    if inj is None:
        return list(candidates), []
    kept: list[Any] = []
    hit_count = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            kept.append(candidate)
            continue
        scan = scan_fields(
            (candidate.get("title"), candidate.get("snippet"), candidate.get("content")),
            inj,
        )
        if scan.hits:
            hit_count += 1
            if inj.on_hit == "drop":
                _warn(
                    f"候选注入特征命中已丢弃（on_hit=drop）：rule_id={','.join(scan.hits)} "
                    f"host={url_host(str(candidate.get('url') or ''))}"
                )
                continue  # drop：丢弃（不进 kept——ref/来源卡/[n] 自然一致）
            _warn(
                f"候选注入特征命中（on_hit=flag 保留留痕）：rule_id={','.join(scan.hits)} "
                f"host={url_host(str(candidate.get('url') or ''))}"
            )
        kept.append(candidate)
    errors = (
        [{"stage": stage, "reason": "injection_blocked", "count": hit_count}]
        if hit_count
        else []
    )
    return kept, errors


def filter_report(
    report: dict[str, Any],
    guards: ContentGuardConfig | None,
) -> tuple[dict[str, Any], bool]:
    """子代理报告扫描（summary/hard_facts/quotes——sources URL 不扫，读取面已各自
    过 guard）→ (report, hits)。仅判定+告警：drop 置换为 failed 占位由调用方（门控）
    消费——置换形态属 deep_research._failed_report 单一事实源。"""
    inj = guards.injection_filter if guards is not None else None
    if inj is None:
        return report, False
    scanned = [
        report.get("summary"),
        *(report.get("hard_facts") or []),
        *(report.get("quotes") or []),
    ]
    scan = scan_fields(scanned, inj)
    if not scan.hits:
        return report, False
    chars = sum(len(f) for f in scanned if isinstance(f, str))
    _warn(f"子代理报告注入特征命中：rule_id={','.join(scan.hits)} 字符数={chars}")
    return report, True


async def filter_image_items(
    items: list[dict[str, Any]],
    guard: UrlGuardConfig | None,
    *,
    stage: str = "search",
    timeout_s: float = GUARD_ENVELOPE_S,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """图片收口（fuse_rerank images 事件出侧）：url + source_url 全判据（同判据于
    主 url——此前只查协议白名单）。批量单线程包络；拒收条目剔除（已选 topN 不回补——
    安全优先）；errors 聚合 url_rejected；guard None 原样直通。"""
    if guard is None or not items:
        return list(items), []

    def _batch() -> list[bool]:
        verdicts: list[bool] = []
        for item in items:
            ok = True
            for key in ("url", "source_url"):
                value = item.get(key)
                if isinstance(value, str) and value and not validate_url(
                    value, guard, resolver=DEFAULT_RESOLVER
                ).ok:
                    ok = False
                    break
            verdicts.append(ok)
        return verdicts

    try:
        verdicts = await asyncio.wait_for(asyncio.to_thread(_batch), timeout_s)
    except TimeoutError:
        _warn(f"图片收口校验超预算（fail-open 放行）：count={len(items)}")
        return list(items), []
    kept = [item for item, ok in zip(items, verdicts) if ok]
    rejected = len(items) - len(kept)
    errors = [{"stage": stage, "reason": "url_rejected", "count": rejected}] if rejected else []
    return kept, errors
