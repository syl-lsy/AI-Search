"""unit 层：kb_only 裁决语义完整性（B22 批次 4 第四段——评审前置修复）。

契约锚点（见各用例 docstring）：
- 修复①补搜绕桩：kb_only 裁决下 reflect→supplement 回路直连 registry.search
  （deep_supplement.py:230）不受 research 工具面桩化约束——tool_scope=kb_only 时
  supplement 网络补搜**短路**（固定系统注零插值零 payload 回显〔桩注常量族
  deep_research.py:229-250 同款安全管道〕+ errors[]/AgentLog 留痕）；短路点在
  chain/registry 校验**之前**（裁决>链状态——段二 web_search/kb_search 桩同款
  优先序）；web_only/both/no_hit/None 补搜行为不变；
- 终止性：reflect_round 递增不依赖补搜产出（deep_supplement.py:196/:28-30 有界性
  先例）——短路轮零 stage 帧零搜索零批次，回路经轮次帽+600s 包络兜底不空转；
- 修复②读原文面：kb_only 下 firecrawl_read 桩化（KB 切片文本外链可被子代理直读
  ——外部 web 内容绕进仅 KB 轮违背用户裁决）；save_snapshot_tool 直接收 url 参数
  （deep_research.py:784）→ 同桩非「自然短路」；**桩化矩阵只动 kb_only 行**：
  kb_only 下 kb_search 仍可用（段二 test_kb_only_keeps_kb_search_live 锁死）、
  web_only 下 firecrawl_read/save_snapshot 正常（web 允许）；
- G3：deep_enabled=false → approval_choice/kb_gate_action 恒缺席 → scope=None →
  零桩化零短路（存量 deep 测试零改动由既有套件承担）。
"""

from typing import Any

import pytest

from app.agent.nodes import deep_research, deep_supplement
from app.agent.tools.registry import SearchChain
from test.unit.test_deep_research import _make_deps


# ---- 假体 ----


class _CountingRegistry:
    """可计数搜索假体（零候选返回——执行面断言用）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def search(
        self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5,
        retry_sink: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(f"{query}|{source}")
        return []


def _supplement_state(**extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "gaps": [{"gap": "缺口一", "query": "补搜词", "origin": "web"}],
        "searched_queries": [],
        "visited_urls": [],
        "collected_sources": [],
        "reflect_round": 0,
        "spec": {},
        "time_range": None,
    }
    state.update(extra)
    return state


# ---- 修复①：supplement kb_only 短路 ----


async def test_supplement_kb_only_short_circuits_before_chain() -> None:
    """kb_only 短路点在 chain/registry 校验之前（裁决>链状态）：config 零注入
    （chain=None——无短路时本用例将 raise SearchChainUnavailable）→ 返回短路更新：
    reflect_round+1（有界性）、原值并集回写、errors[] 留痕
    supplement_stubbed_kb_only+固定注 detail。"""
    result = await deep_supplement.supplement(
        _supplement_state(approval_choice="kb_only"),
        {"configurable": {}},  # chain/registry 全缺——短路须先于校验
    )
    assert result["reflect_round"] == 1  # 有界性：递增不依赖补搜产出
    assert result["searched_queries"] == []  # 原值并集（零登记）
    assert result["collected_sources"] == []
    errors = result.get("errors") or []
    assert len(errors) == 1
    assert errors[0]["stage"] == "search"
    assert errors[0]["reason"] == "supplement_stubbed_kb_only"
    assert errors[0]["detail"] == deep_research._STUB_SUPPLEMENT_NOTE_KB_ONLY


async def test_supplement_short_circuit_zero_stage_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """短路轮零 stage 帧零搜索（不伪造搜索发生——§7.4 典型序列非全量义务）：
    usable gaps 在场也不发 search running/done、不触 registry；终止性=单次调用
    即返回（reflect_round+1），回路轮次帽+600s 包络兜底不空转。"""
    frames: list[tuple[str, str]] = []
    monkeypatch.setattr(
        deep_supplement, "emit_stage", lambda key, status, **kw: frames.append((key, status))
    )
    monkeypatch.setattr(deep_supplement, "emit_sources", lambda **kw: None)
    registry = _CountingRegistry()
    chain = SearchChain(sources=["tavily"], backup=None, degraded=False, detail="tavily")
    result = await deep_supplement.supplement(
        _supplement_state(approval_choice="kb_only", reflect_round=1),
        {"configurable": {"search_chain": chain, "tools_registry": registry}},
    )
    assert frames == []  # 零 stage 帧
    assert registry.calls == []  # 零物理搜索
    assert result["reflect_round"] == 2  # 轮次帽推进（回路终止性）


# ---- 修复①反向量：web_only/both/no_hit/None 补搜行为不变 ----


@pytest.mark.parametrize(
    "state_extra",
    [
        {"approval_choice": "web_only"},
        {"approval_choice": "both"},
        {"kb_gate_action": "web_only"},  # no_hit——门判零命中补搜合理
        {},  # None——现状（含 G3 deep_enabled=false 缺席形态）
    ],
    ids=["web_only", "both", "no_hit", "none_g3"],
)
async def test_supplement_non_kb_only_unaffected(
    monkeypatch: pytest.MonkeyPatch, state_extra: dict[str, Any]
) -> None:
    """非 kb_only 形态补搜行为不变：照常走执行面（registry.search 被调、零短路
    留痕、stage 帧照发）。"""
    monkeypatch.setattr(deep_supplement, "emit_stage", lambda key, status, **kw: None)
    monkeypatch.setattr(deep_supplement, "emit_sources", lambda **kw: None)
    registry = _CountingRegistry()
    chain = SearchChain(sources=["tavily"], backup=None, degraded=False, detail="tavily")
    result = await deep_supplement.supplement(
        _supplement_state(**state_extra),
        {"configurable": {"search_chain": chain, "tools_registry": registry}},
    )
    assert registry.calls, "补搜执行面应被触达（行为不变）"
    reasons = {e.get("reason") for e in result.get("errors") or []}
    assert "supplement_stubbed_kb_only" not in reasons


# ---- 修复②：firecrawl_read / save_snapshot kb_only 桩化 ----


async def test_firecrawl_read_kb_only_stub_zero_read() -> None:
    """kb_only：firecrawl_read 调用即返回桩注（仅限知识库）——短路与 URL 安全校验
    之前（裁决优先），registry.read 零调用（零物理读取）。"""
    deps = _make_deps()
    deps.tool_scope = "kb_only"
    tools = {t.name: t for t in deep_research._build_contract_tools(deps)}
    result = await tools["firecrawl_read"].ainvoke({"url": "ftp://x.invalid"})
    assert "仅限知识库" in result
    assert deps.registry.read_calls == []  # 零物理读取


async def test_firecrawl_read_web_only_live_guard_path() -> None:
    """矩阵 web_only 行：firecrawl_read 正常（web 允许）——同 URL 走真路径（非法
    scheme 命中 URL 安全校验拒收注，非桩注）——桩化只作用于 kb_only 行。"""
    deps = _make_deps()
    deps.tool_scope = "web_only"
    tools = {t.name: t for t in deep_research._build_contract_tools(deps)}
    result = await tools["firecrawl_read"].ainvoke({"url": "ftp://x.invalid"})
    assert "仅限知识库" not in result
    assert "仅支持 http/https" in result  # URL 守卫早退注（live 路径形态）


async def test_save_snapshot_kb_only_stub() -> None:
    """kb_only：save_snapshot_tool 直接收 url 参数（deep_research.py:784）→ 同桩
    非「自然短路」——调用即返回桩注，save_snapshot 落盘函数零调用。"""
    saved: list[Any] = []

    async def _fake_save(**kw: Any) -> dict[str, Any]:
        saved.append(kw)
        return {"object_key": "k", "size_bytes": 1, "reused": False}

    origin = deep_research.save_snapshot
    deep_research.save_snapshot = _fake_save  # type: ignore[assignment]
    try:
        deps = _make_deps()
        deps.tool_scope = "kb_only"
        tools = {t.name: t for t in deep_research._build_contract_tools(deps)}
        result = await tools["save_snapshot"].ainvoke({"url": "https://x.invalid", "content": "c"})
    finally:
        deep_research.save_snapshot = origin  # type: ignore[assignment]
    assert "仅限知识库" in result
    assert saved == []  # 零落盘


async def test_save_snapshot_web_only_live_path() -> None:
    """矩阵 web_only 行：save_snapshot 正常（web 允许）——真路径走 URL 校验+落盘
    （假体），返回既有成功形态非桩注。"""
    saved: list[Any] = []

    async def _fake_save(**kw: Any) -> dict[str, Any]:
        saved.append(kw)
        return {"object_key": "k", "size_bytes": 1, "reused": False}

    origin = deep_research.save_snapshot
    deep_research.save_snapshot = _fake_save  # type: ignore[assignment]
    try:
        deps = _make_deps()
        deps.tool_scope = "web_only"
        tools = {t.name: t for t in deep_research._build_contract_tools(deps)}
        result = await tools["save_snapshot"].ainvoke({"url": "https://ok.example", "content": "c"})
    finally:
        deep_research.save_snapshot = origin  # type: ignore[assignment]
    assert "仅限知识库" not in result
    assert "快照已保存" in result  # live 成功形态
    assert saved and saved[0]["url"] == "https://ok.example"


# ---- 安全管道 + G3 ----


async def test_read_stub_note_fixed_no_payload_echo() -> None:
    """桩注安全管道：固定常量零插值——对抗性 URL 零回显，返回值逐字节等于桩注
    常量（deep_research.py:229-250 桩注常量族同款）。"""
    deps = _make_deps()
    deps.tool_scope = "kb_only"
    tools = {t.name: t for t in deep_research._build_contract_tools(deps)}
    hostile = "https://evil.invalid/" + "?q=" + "长" * 300
    result = await tools["firecrawl_read"].ainvoke({"url": hostile})
    assert result == deep_research._STUB_READ_NOTE_KB_ONLY
    assert "evil" not in result


def test_g3_none_scope_no_read_stub() -> None:
    """G3：scope=None（deep_enabled=false 缺席形态）→ firecrawl_read/save_snapshot
    零桩化——kb_only 桩注不可达（现状路径逐字节）。"""
    deps = _make_deps()
    deps.tool_scope = None
    tools = {t.name: t for t in deep_research._build_contract_tools(deps)}
    assert tools["firecrawl_read"].coroutine is not None  # 工具在表（R36 桩非摘除）
    names = {t.name for t in deep_research._build_contract_tools(deps)}
    assert names == {"web_search", "kb_search", "firecrawl_read", "save_snapshot"}
