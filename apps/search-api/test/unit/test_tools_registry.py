"""unit 层：tools/registry + schema_guard（B3 节点 web_search——ARCH §3.1/§4.1/§6.2、nacos-config §2）。

钉住的语义（冻结契约，逐条对应测试）：
- 熔断器：连续 failure_threshold 次失败 → open cooldown_s → 冷却期满半开（下次尝试放行）
  → 成功关闭 / 失败重开；成功重置连续失败计数；进程内状态（nacos-config §2 circuit 节）；
- 搜索链语言门控（REQ-002）：中文 → [tavily, bocha]、非中文 → [tavily]（BoCha 不跨语言兜用）；
  单主位断互兜底（zh：tavily 断→纯 bocha、bocha 断→纯 tavily——A10）；本轮适用主位全断 →
  条件装载备位链按链序取第一个可用者：searxng（fallback_search 链首）→ anysearch（次级，
  v1.18）；备位全断 → 链不可用（该轮 search 失败 500201）；
- enabled=false ≡ 熔断 open ≡ 凭证缺失（*_env 指向的 .env 值为空）≡ 该源本轮不装载；
- 链在轮启动时冻结（registry.resolve 读熔断实时态一次；轮内中途熔断不切换备位）；
- fast 工具面物理不含 firecrawl（url_reader 属 deep/读取链——白名单外不装载，测试锁死）；
- schema_guard 双向：入参 pydantic 校验后发调用（编程错误不进熔断）；出参校验失败计该源
  一次失败（进熔断），不写入 state、不污染上下文。
"""

import asyncio
import time
from typing import Any

import pytest
from pydantic import BaseModel, Field

from app.agent.tools.registry import (
    SEARCH_SOURCE_NAMES,
    CircuitBreaker,
    SearchChain,
    SearchChainUnavailable,
    ToolRegistry,
    resolve_search_chain,
)


# ---- 熔断器状态机 ----


def test_breaker_opens_after_consecutive_threshold() -> None:
    """连续失败达阈值 → open；成功重置连续计数。"""
    breaker = CircuitBreaker(failure_threshold=3, cooldown_s=60.0)
    now = 1000.0
    breaker.record_failure(now)
    breaker.record_failure(now + 1)
    assert breaker.is_open(now + 1) is False  # 未达阈值仍 closed
    breaker.record_failure(now + 2)
    assert breaker.is_open(now + 2) is True  # 第 3 次连续失败 → open
    # 冷却期内保持 open
    assert breaker.is_open(now + 2 + 59) is True
    # 冷却期满 → 半开（放行下一次尝试）
    assert breaker.is_open(now + 2 + 61) is False


def test_breaker_success_resets_and_closes() -> None:
    """成功重置连续计数；半开期成功 → 关闭。"""
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=10.0)
    breaker.record_failure(0.0)
    breaker.record_success(1.0)  # 计数重置
    breaker.record_failure(2.0)
    assert breaker.is_open(2.0) is False  # 只剩 1 次连续失败

    breaker.record_failure(3.0)  # 达阈值 → open
    assert breaker.is_open(3.0) is True
    breaker.record_success(3.0 + 11.0)  # 冷却期满后半开期的尝试成功 → 关闭
    assert breaker.is_open(3.0 + 11.0) is False


def test_breaker_half_open_failure_reopens() -> None:
    """半开期尝试失败 → 重新 open（新一轮冷却）。"""
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=10.0)
    breaker.record_failure(0.0)
    assert breaker.is_open(5.0) is True
    assert breaker.is_open(11.0) is False  # 半开
    breaker.record_failure(11.0)
    assert breaker.is_open(12.0) is True  # 重开
    assert breaker.is_open(20.0) is True  # 新冷却期内（11+10=21.0 期满）
    assert breaker.is_open(21.5) is False  # 新冷却期满


# ---- fast 工具面白名单（firecrawl 物理不含）----


def test_search_source_whitelist_excludes_firecrawl() -> None:
    """fast 搜索源集物理不含 firecrawl（url_reader 属读取链——共识的代码级保证）。"""
    assert SEARCH_SOURCE_NAMES == {"tavily", "bocha", "searxng", "anysearch"}
    assert "firecrawl" not in SEARCH_SOURCE_NAMES
    assert "crawl4ai" not in SEARCH_SOURCE_NAMES


# ---- 链序决策矩阵（registry.resolve）----


def _mcp_payload(**overrides: Any) -> dict[str, Any]:
    """契约 mcp.json 快照形态（nacos-config §2：{"servers": {...}}）；overrides 逐 server 覆盖。"""
    servers: dict[str, Any] = {
        server: {
            "enabled": True,
            "role": role,
            "timeout_s": timeout,
            "circuit": {"failure_threshold": 3, "cooldown_s": 60},
        }
        for server, role, timeout in [
            ("tavily", "primary_search", 10),
            ("bocha", "chinese_search", 10),
            ("searxng", "fallback_search", 5),
            ("anysearch", "fallback_search_secondary", 10),
        ]
    }
    for server, patch in overrides.items():
        servers[server].update(patch)
    return {"servers": servers}


class _Registry(ToolRegistry):
    """假体 registry：凭证在场性与调用面全部注入，零网络。"""

    def __init__(
        self,
        *,
        credentials: set[str] | None = None,
        open_sources: set[str] | None = None,
    ) -> None:
        super().__init__()
        self._credentials = {"tavily", "bocha", "searxng", "anysearch"} if credentials is None else credentials
        for name in open_sources or set():
            # 冷却 3600s ≫ clock(100s)：保证 resolve 时刻仍处 open 期
            self.breakers[name] = CircuitBreaker(failure_threshold=1, cooldown_s=3600.0)
            self.breakers[name].record_failure(0.0)

    def credential_ready(self, server: str) -> bool:
        return server in self._credentials


def test_chain_zh_both_primary_available() -> None:
    """中文双主位可用 → [tavily, bocha]，无备位、零降级。"""
    chain = resolve_search_chain(
        _mcp_payload(), "zh", _Registry(), clock=lambda: 100.0
    )
    assert isinstance(chain, SearchChain)
    assert chain.sources == ["tavily", "bocha"]
    assert chain.backup is None and chain.degraded is False


def test_chain_en_tavily_only() -> None:
    """非中文 → 仅 [tavily]（BoCha 不跨语言兜用，REQ-002）；可用即零降级。"""
    chain = resolve_search_chain(_mcp_payload(), "en", _Registry(), clock=lambda: 100.0)
    assert chain.sources == ["tavily"]
    assert chain.degraded is False


@pytest.mark.parametrize(
    ("open_sources", "expected_sources"),
    [
        ({"tavily"}, ["bocha"]),  # zh：tavily 断 → 纯 bocha（A10 反向）
        ({"bocha"}, ["tavily"]),  # zh：bocha 断 → 纯 tavily（A10）
    ],
)
def test_chain_zh_single_primary_down_falls_to_sibling(
    open_sources: set[str], expected_sources: list[str]
) -> None:
    """zh 单主位熔断 → 互兜底（剩余单源），degraded=True + detail 标注。"""
    chain = resolve_search_chain(
        _mcp_payload(), "zh", _Registry(open_sources=open_sources), clock=lambda: 100.0
    )
    assert chain.sources == expected_sources
    assert chain.backup is None
    assert chain.degraded is True
    assert "degraded" in chain.detail or "熔断" in chain.detail or "降级" in chain.detail


def test_chain_en_tavily_down_loads_searxng_chain_head() -> None:
    """非中文主位全断（仅 tavily）→ 备位链首 searxng 装载（v1.18 链式）。"""
    chain = resolve_search_chain(
        _mcp_payload(), "en", _Registry(open_sources={"tavily"}), clock=lambda: 100.0
    )
    assert chain.sources == ["searxng"]
    assert chain.backup == "searxng"
    assert chain.degraded is True


def test_chain_zh_both_down_searxng_first_by_chain_order() -> None:
    """zh 双主位断 → searxng（fallback_search 链首）先于 anysearch（v1.18 链序）。"""
    chain = resolve_search_chain(
        _mcp_payload(), "zh", _Registry(open_sources={"tavily", "bocha"}), clock=lambda: 100.0
    )
    assert chain.sources == ["searxng"]
    assert chain.backup == "searxng"


def test_chain_backup_falls_to_anysearch_when_searxng_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """searxng enabled=false / 熔断 / 凭证缺失 → 落 anysearch（次级）。"""
    payload = _mcp_payload(**{"searxng": {"enabled": False}})
    chain = resolve_search_chain(
        payload, "zh", _Registry(open_sources={"tavily", "bocha"}), clock=lambda: 100.0
    )
    assert chain.sources == ["anysearch"]
    assert chain.backup == "anysearch"

    # searxng 熔断 open 同型
    chain = resolve_search_chain(
        _mcp_payload(),
        "zh",
        _Registry(open_sources={"tavily", "bocha", "searxng"}),
        clock=lambda: 100.0,
    )
    assert chain.sources == ["anysearch"]

    # searxng 凭证缺失（wrapper 子进程 env 指针为空）同型
    chain = resolve_search_chain(
        _mcp_payload(),
        "zh",
        _Registry(open_sources={"tavily", "bocha"}, credentials={"tavily", "bocha", "anysearch"}),
        clock=lambda: 100.0,
    )
    assert chain.sources == ["anysearch"]


def test_chain_all_unavailable_returns_empty() -> None:
    """主位+备位全断 → 空链（该轮 search 失败 500201——节点侧短路）。"""
    chain = resolve_search_chain(
        _mcp_payload(),
        "zh",
        _Registry(open_sources={"tavily", "bocha", "searxng", "anysearch"}),
        clock=lambda: 100.0,
    )
    assert chain.sources == []
    assert chain.degraded is True


def test_chain_primary_recovery_unloads_backup_next_turn() -> None:
    """主位恢复（熔断冷却期满）→ 下一轮 resolve 回主位、卸载备位（轮启动实时态决策）。"""
    registry = _Registry(open_sources={"bocha"})  # bocha 断：zh → 纯 tavily
    chain = resolve_search_chain(_mcp_payload(), "zh", registry, clock=lambda: 100.0)
    assert chain.sources == ["tavily"]
    registry.breakers["bocha"].record_success(1.0)  # bocha 恢复
    chain2 = resolve_search_chain(_mcp_payload(), "zh", registry, clock=lambda: 200.0)
    assert chain2.sources == ["tavily", "bocha"]  # 主位双源回归


def test_chain_frozen_against_mid_turn_breaker_flip() -> None:
    """轮内中途熔断不切换备位：SearchChain 解析后不可变——熔断翻转不改已冻结链。"""
    registry = _Registry()
    chain = resolve_search_chain(_mcp_payload(), "zh", registry, clock=lambda: 100.0)
    frozen = list(chain.sources)
    registry.breakers["tavily"] = CircuitBreaker(failure_threshold=1, cooldown_s=60.0)
    registry.breakers["tavily"].record_failure(101.0)  # 轮内中途熔断
    assert chain.sources == frozen  # 已冻结链不受影响（下一轮 resolve 才反映）
    with pytest.raises((AttributeError, TypeError)):
        chain.sources = ["anysearch"]  # 不可变（防节点内改链）


# ---- guarded 调用：入参/出参双向 + 熔断联动 ----


class _Args(BaseModel):
    query: str = Field(min_length=1)
    count: int = Field(default=5, ge=1, le=10)


def test_guard_invokes_and_normalizes_output() -> None:
    """入参校验通过 → 调用 → 出参归一为 candidates；成功记熔断 reset。"""
    from app.agent.tools.registry import guarded_source_call

    calls: list[dict[str, Any]] = []

    async def invoke(args: dict[str, Any]) -> Any:
        calls.append(args)
        return {"results": [{"url": "https://a.io/1", "title": "A", "content": "正文", "score": 0.9}]}

    breaker = CircuitBreaker(failure_threshold=3, cooldown_s=60.0)
    breaker.record_failure(0.0)
    candidates = guarded_source_call(
        invoke=invoke,
        args_model=_Args,
        args={"query": "q", "count": 5},
        normalize=lambda raw: [
            {"url": r["url"], "title": r["title"], "snippet": r["content"], "score": r["score"], "origin": "tavily"}
            for r in raw["results"]
        ],
        breaker=breaker,
        timeout_s=5.0,
        clock=lambda: 50.0,
    )
    import asyncio

    candidates = asyncio.run(candidates)
    assert calls == [{"query": "q", "count": 5}]
    assert candidates[0]["origin"] == "tavily"
    assert breaker.consecutive_failures == 0  # 成功重置


def test_guard_output_failure_counts_breaker_and_not_pollutes() -> None:
    """出参校验失败：计该源一次失败（进熔断）；异常上抛由节点记 errors——不产 state 写入。"""
    from app.agent.tools.registry import SourceCallError, guarded_source_call

    async def invoke(args: dict[str, Any]) -> Any:
        return {"unexpected": "shape"}

    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=60.0)
    with pytest.raises(SourceCallError):
        asyncio.run(guarded_source_call(
            invoke=invoke,
            args_model=_Args,
            args={"query": "q"},
            normalize=lambda raw: (_ for _ in ()).throw(ValueError("出参缺 results")),
            breaker=breaker,
            timeout_s=5.0,
            clock=lambda: 0.0,
        ))
    assert breaker.consecutive_failures == 1


def test_guard_input_violation_is_programming_error_not_circuit() -> None:
    """入参 pydantic 校验失败（编程错误）→ ValueError 上抛、熔断计数不动。"""
    from app.agent.tools.registry import guarded_source_call

    async def invoke(args: dict[str, Any]) -> Any:  # pragma: no cover —— 不应到达
        raise AssertionError("入参校验失败不应发起调用")

    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=60.0)
    with pytest.raises(ValueError):
        asyncio.run(guarded_source_call(
            invoke=invoke,
            args_model=_Args,
            args={"query": ""},  # min_length=1 违例
            normalize=lambda raw: [],
            breaker=breaker,
            timeout_s=5.0,
            clock=lambda: 0.0,
        ))
    assert breaker.consecutive_failures == 0
