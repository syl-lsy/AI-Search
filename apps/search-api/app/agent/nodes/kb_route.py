"""kb_route 三档路由与确定性供给 gate（plan-m3-b19 §4.4 / §5 必答项④ + 修订注 1/4/6）。

纯函数模块（零 IO 零 LLM——search_fanout 判定序与 fuse_rerank 后条件边共用）。

路由矩阵（plan §4.4；①direct/recall→synthesis 与 ②空 queries→merge_dedupe 两级不动，
归 web_search.search_fanout 前置判定）：

| kb_route（rewrite 输出，缺省/非法/两兜底一律 route.default〔初值 hybrid〕） | Send 形态 |
|---|---|
| `hybrid`（缺省灰度） | web_search×N + kb_search×N 双 Send |
| `kb_first`（先 KB 判足不足再 web） | 仅 kb_search×N（不足→纠错回边一次，强制 hybrid 第二趟） |
| `kb_only`（显式内部知识信号只查 KB） | 仅 kb_search×N（零候选兜底=纠错回边一次补 web，errors kb_only_fallback_web） |

**强制回 hybrid**（修订注 1 口径）：retriever 不可用 = `kb_available=false`（es_enabled
与 embedding_enabled 双关）→ kb_only/kb_first 降级 hybrid Send 形态（web 照跑）。
**CR-3 灰度组合（路由与纠错同开关）**：`correction_enabled=false`（缺省灰度）→
kb_first/kb_only 一并降级 hybrid——灰度关=路由面也灰度，LLM 输出 kb_first/kb_only
不得绕过（否则单 kb Send 下「不足直达 synthesis」=零 web 供给非现状路径，违反
plan §4.4「存量轮行为面仅增 KB 候选」与 ARCH B19 行「全部灰度默认关」承诺；
灰度开态纠错/兜底语义才成立）。
**纠错第二趟强制 hybrid**：correction_round ≥ 1 → 一律 hybrid（轮帽 1 语义的路由侧
承载体——rewrite 侧 update 同步钉 hybrid，双防线同谓词）。

确定性信号 gate（`kb_supply_verdict`，零 LLM）——判定输入=fused KB 卡集：**修订注 4**
`min_top1_score=0.012`（plan 0.05 推导有误：单 query 双通道同命 rank1 恒为
w_bm25/61+w_vec/61=1/61≈0.0164，0.05 不可达会致门控/纠错恒触发）；**有效阈值随
启用通道权重缩放**（P1 单通道可达性：`min_top1_score × 启用通道权重和 / 全通道
权重和`——es-only 常态下 rank1=w_es/61 可达，双通道全开因子 1.0 阈值不变；语义=
「全通道同命 rank1 等效分」）；足 ⇔ KB 命中 ∧ top1 RRF 分 ≥ 有效阈值 ∧ KB 来源数
≥ min_kb_sources。纠错轮帽 1 次 + `correction_enabled` 缺省 false 灰度（**路由与
纠错同开关**：false 时 kb_first/kb_only 整体降级 hybrid，不足直达 synthesis 语义
仅在灰度开态成立，M4 把关后放开）。
"""

from collections.abc import Mapping
from typing import Any

from app.rag.retrieval import kb_available

__all__ = [
    "KB_ROUTE_VALUES",
    "ROUTE_FALLBACK",
    "correction_round_of",
    "kb_route_config",
    "kb_supply_verdict",
    "resolve_kb_route",
]

#: kb_route 合法值域（api.md v1.33 判定行值域字面）
KB_ROUTE_VALUES: frozenset[str] = frozenset({"kb_only", "kb_first", "hybrid"})

#: 兜底路由（plan §5 必答项④：rewrite 输出缺省/非法/两兜底路径一律 hybrid）
ROUTE_FALLBACK: str = "hybrid"

#: correction_round 缺省（轮帽 1：≥1 即不再回边）
_CORRECTION_CAP: int = 1


def kb_route_config(spec: Mapping[str, Any] | None) -> dict[str, Any]:
    """spec `kb.route` 子节读取（缺省回落 plan §5 必答项④建议值——灰度安全）。"""
    kb = spec.get("kb") if isinstance(spec, Mapping) else None
    route = kb.get("route") if isinstance(kb, Mapping) else None
    route = route if isinstance(route, Mapping) else {}
    return {
        "default": str(route.get("default") or ROUTE_FALLBACK),
        "correction_enabled": route.get("correction_enabled") is True,
        "min_kb_sources": _positive_number(route.get("min_kb_sources"), 2),
        "min_top1_score": _positive_number(route.get("min_top1_score"), 0.012),
    }


def _positive_number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if value > 0 else default


def correction_round_of(state: Mapping[str, Any]) -> int:
    """correction_round 读取（缺省/非法 → 0——纠错轮帽判据面）。"""
    value = state.get("correction_round")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def resolve_kb_route(state: Mapping[str, Any], spec: Mapping[str, Any] | None) -> str:
    """kb_route 解析（search_fanout 判定序 ③ 的路由谓词）。

    判定序（先到先断）：
    ① 纠错第二趟（correction_round ≥ 1）→ 一律 `hybrid`（强制 web+kb 并行补查）；
    ② state `kb_route` 合法值 → 透传；缺省/非法 → `route.default`（修订注 3：
       与 nacos v1.46 措辞统一，default 初值 hybrid）；
    ③ kb_only/kb_first 降级 `hybrid`（web 照跑）两组灰度判据（CR-3：路由与纠错
       同开关）——retriever 不可用（修订注 1：kb_available=false=双通道全关，
       kb_only/kb_first 无 KB 可查时不允许零源轮）；`correction_enabled=false`
       （缺省灰度——灰度关=路由面也灰度，LLM 输出不得绕过路由面让 web 缺席）。
    """
    route_cfg = kb_route_config(spec)
    if correction_round_of(state) >= _CORRECTION_CAP:
        return ROUTE_FALLBACK
    raw = state.get("kb_route")
    route = str(raw) if isinstance(raw, str) and raw in KB_ROUTE_VALUES else route_cfg["default"]
    if route in ("kb_only", "kb_first") and (
        not route_cfg["correction_enabled"] or not kb_available(spec)
    ):
        return ROUTE_FALLBACK
    return route


def _enabled_channel_weight_factor(spec: Mapping[str, Any] | None, query_type: str) -> float:
    """启用通道权重和 / 全通道权重和（阈值缩放因子——P1 单通道可达性修正）。

    阈值语义 = 「全通道同命 rank1 等效分」：`min_top1_score` 按启用通道权重和
    缩放，es-only（embedding_enabled=false 翻键前常态）时 rank1 = w_es/61 可达。
    权重取 `kb.type_weights[query_type]`（缺省/形态异常回落 (0.5,0.5)，与消费面
    同判据）；全通道关（factor=0）/权重和 ≤0 → 1.0（不改阈值，路由面已在别处
    降级 hybrid）。
    """
    kb = spec.get("kb") if isinstance(spec, Mapping) else None
    kb = kb if isinstance(kb, Mapping) else {}
    weights = kb.get("type_weights")
    weights = weights if isinstance(weights, Mapping) else {}
    pair = weights.get(query_type)
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        try:
            bm25_w = float(pair[0])
            vec_w = float(pair[1])
        except (TypeError, ValueError):
            bm25_w = vec_w = 0.5
    else:
        bm25_w = vec_w = 0.5
    total = bm25_w + vec_w
    if total <= 0 or bm25_w < 0 or vec_w < 0:
        return 1.0
    enabled = (bm25_w if kb.get("es_enabled") is True else 0.0) + (
        vec_w if kb.get("embedding_enabled") is True else 0.0
    )
    return enabled / total


def kb_supply_verdict(state: Mapping[str, Any]) -> bool:
    """确定性供给 gate（fuse_rerank 后条件边谓词；返回 True=足/直达 synthesis，
    False=不足/纠错回边）。**零 LLM 纯确定性**（plan §4.4）。

    判定：route ∈ {kb_first, kb_only} ∧ correction_enabled ∧ correction_round==0
    ∧（零 KB 命中 ∨ top1 KB RRF 分 < 有效阈值 ∨ KB 来源数 < min_kb_sources）
    → False（不足）；其余一律 True——hybrid 路由零纠错（现状+KB 增量）。灰度关闭
    （correction_enabled=false）时 resolve_kb_route 已把 kb_first/kb_only 降级 hybrid，
    本函数不再见该两路由（correction_enabled 二次校验=纵深防御）；轮帽命中同样
    直达 synthesis（现状路径）。

    **有效阈值随启用通道权重缩放**（P1）：`min_top1_score × 启用通道权重和 /
    全通道权重和`——语义=「全通道同命 rank1 等效分」；es-only 常态下单通道
    rank1（w_es/61）不再恒判不足，双通道全开时因子 1.0（阈值不变）。
    """
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else None
    route_cfg = kb_route_config(spec)
    route = resolve_kb_route(state, spec)
    if route not in ("kb_first", "kb_only"):
        return True
    if not route_cfg["correction_enabled"]:
        return True
    if correction_round_of(state) >= _CORRECTION_CAP:
        return True
    raw_fused = state.get("fused")
    fused = raw_fused if isinstance(raw_fused, list) else []
    kb_cards = [
        item
        for item in fused
        if isinstance(item, Mapping)
        and (item.get("origin") == "kb" or (isinstance(item.get("origin"), list) and "kb" in item.get("origin", [])))
    ]
    if not kb_cards:
        return False
    top1 = max(float(item.get("score") or 0.0) for item in kb_cards)
    factor = _enabled_channel_weight_factor(spec, str(state.get("query_type") or "other"))
    effective_min = route_cfg["min_top1_score"] * factor
    if top1 < effective_min:
        return False
    if len(kb_cards) < route_cfg["min_kb_sources"]:
        return False
    return True
