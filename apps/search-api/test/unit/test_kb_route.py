"""unit 层：kb_route 三档路由与确定性供给 gate（app/agent/nodes/kb_route.py，
plan-m3-b19 §4.4 / §5 必答项④ + 修订注 1/4/6）。

契约锚点：
- 路由矩阵（plan §4.4；①direct/recall→synthesis 与 ②空 queries→merge_dedupe 两级
  不动，归 test_nodes_search 既有面）：hybrid（缺省灰度）=web+kb 双 Send；
  kb_first/kb_only=仅 kb Send；缺省/非法/两兜底路径一律 route.default（初值 hybrid，
  nacos v1.46 措辞统一）；纠错第二趟强制 hybrid（轮帽 1）；
- **强制回 hybrid**：retriever 不可用（修订注 1 口径 = 双通道全关）→ kb_only/
  kb_first 路由降级为 hybrid Send 形态（web 照跑）；**CR-3 灰度组合**：
  correction_enabled=false（缺省灰度）→ kb_first/kb_only 一并降级 hybrid——
  路由与纠错同开关灰度（灰度关=路由面也灰度，web 照跑保「存量轮行为面仅增
  KB 候选」承诺；单 kb Send 直达 synthesis=零 web 供给非现状路径）；
- 确定性信号 gate（零 LLM）：零 KB 命中 ∨ top1 KB RRF 分 < min_top1_score ∨
  KB 来源数 < min_kb_sources → 不足；**修订注 4**：min_top1_score=0.012（plan 0.05
  推导有误——单 query 双通道同命 rank1 恒为 1/61≈0.0164，0.05 不可达会致门控/纠错
  恒触发）；纠错轮帽 1 次；correction_enabled=false 灰度缺省（路由降级 hybrid，
  不足直达 synthesis 语义仅在灰度开态成立）。
"""

from typing import Any

import pytest

from app.agent.nodes.kb_route import (
    resolve_kb_route,
    kb_supply_verdict,
    correction_round_of,
)
from app.agent.nodes.merge_dedupe import rrf_fuse_kb


def _spec(**route: Any) -> dict[str, Any]:
    # 通道默认开启（ES 单开形态——修订注 1：kb_available=∨；kb 路由存活的现实前提）
    return {"kb": {"es_enabled": True, "embedding_enabled": False,
                   "route": {"default": "hybrid", "correction_enabled": False,
                             "min_kb_sources": 2, "min_top1_score": 0.012, **route}}}


# ---------------------------------------------------------------------------
# resolve_kb_route：缺省/非法/纠错第二趟/不可用降级
# ---------------------------------------------------------------------------


def test_route_missing_and_illegal_clamp_to_default():
    """缺省/非法 → route.default（初值 hybrid，修订注 3 措辞统一）。"""
    assert resolve_kb_route({}, _spec()) == "hybrid"
    assert resolve_kb_route({"kb_route": "bogus"}, _spec()) == "hybrid"
    assert resolve_kb_route({"kb_route": None}, _spec()) == "hybrid"


def test_route_valid_values_pass_through():
    assert resolve_kb_route({"kb_route": "hybrid"}, _spec()) == "hybrid"
    assert resolve_kb_route({"kb_route": "kb_only"}, _spec(correction_enabled=True)) == "kb_only"
    assert resolve_kb_route({"kb_route": "kb_first"}, _spec(correction_enabled=True)) == "kb_first"


def test_route_gray_off_downgrades_kb_routes_to_hybrid():
    """CR-3 灰度组合（路由与纠错同开关）：correction_enabled=false（缺省灰度）→
    kb_first/kb_only 一并降级 hybrid 双 Send——灰度关=路由面也灰度，LLM 输出
    kb_first/kb_only 不得绕过（单 kb Send 下「不足直达 synthesis」=零 web 供给
    非现状路径；灰度承诺=存量轮行为面仅增 KB 候选，plan §4.4 灰度安全句）。"""
    assert resolve_kb_route({"kb_route": "kb_only"}, _spec()) == "hybrid"
    assert resolve_kb_route({"kb_route": "kb_first"}, _spec()) == "hybrid"


def test_route_gray_on_keeps_kb_routes():
    """灰度开（correction_enabled=true）+ retriever 可用 → kb_first/kb_only 生效。"""
    spec = _spec(correction_enabled=True)
    assert resolve_kb_route({"kb_route": "kb_only"}, spec) == "kb_only"
    assert resolve_kb_route({"kb_route": "kb_first"}, spec) == "kb_first"


def test_route_correction_second_pass_forced_hybrid():
    """纠错第二趟（correction_round ≥ 1）强制 hybrid——轮帽语义的 Route 侧承载体。"""
    assert resolve_kb_route({"kb_route": "kb_only", "correction_round": 1}, _spec()) == "hybrid"


def test_route_retriever_unavailable_downgrades_to_hybrid():
    """双通道全关（修订注 1 口径）→ kb_only/kb_first 降级 hybrid（web 照跑）。"""
    spec = {"kb": {"es_enabled": False, "embedding_enabled": False,
                   "route": {"default": "hybrid"}}}
    assert resolve_kb_route({"kb_route": "kb_only"}, spec) == "hybrid"
    assert resolve_kb_route({"kb_route": "kb_first"}, spec) == "hybrid"


def test_route_single_channel_on_keeps_kb_route():
    """修订注 1：单通道开启即 retriever 可用——灰度开态下 kb 路由不降级。"""
    spec = {"kb": {"es_enabled": True, "embedding_enabled": False,
                   "route": {"default": "hybrid", "correction_enabled": True}}}
    assert resolve_kb_route({"kb_route": "kb_only"}, spec) == "kb_only"


def test_route_default_key_respected():
    """route.default 热更为 kb_first 时：缺省/非法按该值（非硬编码 hybrid）；
    灰度组合——default=kb_first 同受 correction_enabled 开关约束（灰度关降级
    hybrid，灰度开生效）。"""
    spec_gray_on = _spec(default="kb_first", correction_enabled=True)
    assert resolve_kb_route({}, spec_gray_on) == "kb_first"
    assert resolve_kb_route({"kb_route": "nope"}, spec_gray_on) == "kb_first"
    assert resolve_kb_route({}, _spec(default="kb_first")) == "hybrid"  # 灰度关降级


# ---------------------------------------------------------------------------
# kb_supply_verdict：确定性 gate（零 LLM；修订注 4 阈值 0.012）
# ---------------------------------------------------------------------------


def _kb_card(score: float) -> dict[str, Any]:
    return {"origin": "kb", "score": score, "chunk_id": "c1"}


def _kb_hit(chunk_id: str, channel: str, rank: int, section: str) -> dict[str, Any]:
    """rrf_fuse_kb 输入行（通道命中——真实融合公式喂 gate，非手工分）。"""
    return {
        "chunk_id": chunk_id,
        "kb_doc_id": "doc-1",
        "channel": channel,
        "channel_rank": rank,
        "heading_path": ["示例文档", section, "小节"],
        "origin": "kb",
    }


def test_verdict_sufficient_when_top1_dual_channel_rank1():
    """修订注 4 + 阈值通道缩放：双通道同命 rank1 由 `rrf_fuse_kb` 真实输出喂 gate
    （= w_bm25/61 + w_vec/61 = 1/61 ≈ 0.0164）；双通道全开时缩放因子 1.0（阈值
    0.012 不变）→ 判足。"""
    spec = _spec(correction_enabled=True)
    spec["kb"]["embedding_enabled"] = True  # 双通道
    kb_hits = [
        _kb_hit("c1", "bm25", 1, "第一章"),
        _kb_hit("c1", "vector", 1, "第一章"),
        _kb_hit("c2", "bm25", 2, "第二章"),
        _kb_hit("c2", "vector", 2, "第二章"),
    ]
    fused = rrf_fuse_kb(kb_hits, weights=(0.5, 0.5))
    assert fused[0]["score"] == pytest.approx(1 / 61)
    state = {"spec": spec, "fused": fused, "kb_route": "kb_first", "query_type": "other"}
    assert kb_supply_verdict(state) is True


def test_verdict_sufficient_es_only_rank1_scaled_threshold():
    """P1 单通道可达性：es-only（embedding_enabled=false）常态下 rank1 =
    w_es/61（multi_facet 权重 0.3 → ≈0.0049）；阈值随启用通道权重缩放
    （0.012 × 0.3/1.0 = 0.0036）→ 判足（不再恒判不足触发纠错）。"""
    spec = _spec(correction_enabled=True)
    spec["kb"]["type_weights"] = {"multi_facet": [0.3, 0.7]}
    kb_hits = [
        _kb_hit("c1", "bm25", 1, "第一章"),
        _kb_hit("c2", "bm25", 2, "第二章"),
    ]
    fused = rrf_fuse_kb(kb_hits, weights=(0.3, 0.7))
    assert fused[0]["score"] == pytest.approx(0.3 / 61)
    state = {
        "spec": spec,
        "fused": fused,
        "kb_route": "kb_first",
        "query_type": "multi_facet",
    }
    assert kb_supply_verdict(state) is True


def test_verdict_insufficient_zero_kb_hit():
    spec = _spec(correction_enabled=True)
    assert kb_supply_verdict({"spec": spec, "fused": [], "kb_route": "kb_first"}) is False


def test_verdict_insufficient_top1_below_threshold():
    """top1 RRF 分 < 缩放后阈值 → 不足。es-only + 默认权重（0.5,0.5）→ 有效阈值
    = 0.012 × 0.5 = 0.006；0.003/0.002 低于该值 → 判不足。"""
    spec = _spec(correction_enabled=True, min_top1_score=0.012)
    fused = [_kb_card(0.003), _kb_card(0.002)]
    assert kb_supply_verdict({"spec": spec, "fused": fused, "kb_route": "kb_first"}) is False


def test_verdict_insufficient_kb_sources_below_min():
    """KB 来源数 < min_kb_sources（缺省 2）→ 不足。"""
    spec = _spec(correction_enabled=True)
    fused = [_kb_card(0.9)]
    assert kb_supply_verdict({"spec": spec, "fused": fused, "kb_route": "kb_only"}) is False
    fused2 = [_kb_card(0.9), _kb_card(0.8)]
    assert kb_supply_verdict({"spec": spec, "fused": fused2, "kb_route": "kb_only"}) is True


def test_verdict_hybrid_route_never_corrects():
    """hybrid 路由零纠错（纠错只属 kb_first/kb_only——hybrid=现状+KB 增量）。"""
    spec = _spec(correction_enabled=True)
    state = {"spec": spec, "fused": [], "kb_route": "hybrid"}
    assert kb_supply_verdict(state) is True  # True=足（不回边）


def test_verdict_correction_disabled_gray_default():
    """correction_enabled=false（灰度缺省）→ 不足直达 synthesis（现状路径）。"""
    spec = _spec(correction_enabled=False)
    state = {"spec": spec, "fused": [], "kb_route": "kb_only"}
    assert kb_supply_verdict(state) is True


def test_verdict_correction_cap_one_round():
    """轮帽 1 次：correction_round ≥ 1 后不再回边（第二次 gate 不足直达 synthesis）。"""
    spec = _spec(correction_enabled=True)
    state = {"spec": spec, "fused": [], "kb_route": "kb_only", "correction_round": 1}
    assert kb_supply_verdict(state) is True


def test_correction_round_of_state():
    assert correction_round_of({}) == 0
    assert correction_round_of({"correction_round": 2}) == 2
    assert correction_round_of({"correction_round": "x"}) == 0
