#!/usr/bin/env python3
"""light 现任者公平基线判分（Decision Model 工单批次 2.5 终局，2026-11-19 用户裁决③）。

生产 light 全职责路径**同形**判分——与 fast_rewrite.py 生产形态逐参一致：
- 模型实例：`build_light_model()`（alias=model.mapping.light=qwen3.5-4b-ollama，
  temperature=0 / max_tokens=8192 / extra_body think=false / disable_streaming=True，
  经网关 chat 别名——LITELLM_MASTER_KEY 从 .env 直提，零回显、零落盘）；
- prompt：生产 `_render_full_prompt`（rewrite_full.txt 全量模板 + {{type_rules}} 判据面
  （common.type_rules 现行值）+ query_lang/memory_hints/history/query 五变量同源渲染）；
- 结构化输出：`RewriteOutput` schema + `with_structured_output(method="function_calling")`
  （= 生产 light 全职责调用形态 fast_rewrite.py `with_structured_output(RewriteOutput,
  method="function_calling")`）；归一校验复用生产 `_extract`（queries 全空白视同失败）；
- 语言判定：`detect_query_lang`（turn_service 方案 A，与 TurnRunner 前置判定同源）。

四字段提取与 decision 同口径 exact-match：query_type / search_strategy /
answerable（=RewriteOutput.answerable_from_knowledge）/ safety（→ 维名 safety_shadow
对齐）；计分函数复用 score_decision_golden 的 dim_metrics/evaluate（单一事实源——
两侧数字可直接对照）。light 调用失败/解析失败条目如实计 error **不进分母**
（裁决口径「失败/解析失败条目如实计 error 不算错对」）。

结构差如实注记：RewriteOutput.search_strategy Literal 四值（direct/verify/web/recall，
**无 other**）、safety SafetyFlag 五值（**无 other**）——金标 expected 的
search_strategy.other（n=5）与 safety_shadow.other（n=2）对 light 结构性不可达
（decision 题面 options 含 other），属 schema 结构差非模型能力差，对照表单列归因。

判分形态差异（vs decision）：decision 一次前向四题、逐字段 confidence 阈值闸与
align_answerable 护栏；light 单次结构化输出全字段、**无阈值闸无护栏**（生产 light
全职责路径即如此——公平基线 = 各自生产形态对打，不给任何一侧加装评测侧补丁）。

用法（仓库根）：
    uv run --package search-api scripts/score_light_baseline.py --limit 3   # 冒烟
    uv run --package search-api scripts/score_light_baseline.py             # 全量 147 + 对照节
    # 对照节追加：--compare-details <decision 终态 details> --append-report <报告>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_decision_golden import (  # noqa: E402 —— 计分口径单一事实源
    DIMENSIONS, ROOT, evaluate, load_env,
)

from app.agent.nodes.fast_rewrite import (  # noqa: E402 —— 生产 light 全职责形态
    RewriteOutput, _extract, _render_full_prompt, build_light_model,
)
from app.services.turn_service import detect_query_lang  # noqa: E402

DEFAULT_GOLDEN = ROOT / "apps/search-api/test/fixtures/golden_decision_20261118.jsonl"
DEFAULT_DETAILS = ROOT / "docs/eval-light-baseline-20261119-details.json"
#: 单条调用超时包络：生产 fast.llm_timeout_s=60（defaults.py），评测取宽松余量——
#: light 无轮级预算约束，评测性超时不应污染基线（超时条目计 error 不进分母）。
DEFAULT_TIMEOUT_S = 90.0
#: 重试次数：生产 _invoke_with_retry 传输类可重试语义的评测近似（校验类失败重试
#: 同样重跑——light 无 decision 侧兜底链，评测如实记录终态失败）。
DEFAULT_RETRIES = 2


async def score_row(chain: Any, row: dict, timeout_s: float, retries: int) -> dict:
    """单条金标 → light 全职责判定。失败/解析失败 → error 条目（不进分母）。"""
    query = row["query"]
    state = {"query": query, "history": row.get("history")}
    prompt = _render_full_prompt(state, detect_query_lang(query))
    err = "unknown"
    pred = None
    latency_ms = None
    for attempt in range(1, retries + 1):
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                raw = await chain.ainvoke(prompt)
            parsed = _extract(raw)
            latency_ms = round((time.monotonic() - started) * 1000)
            pred = {
                "query_type": parsed.query_type,
                "search_strategy": parsed.search_strategy,
                "answerable": "yes" if parsed.answerable_from_knowledge else "no",
                "safety_shadow": parsed.safety,
            }
            err = None
            break
        except asyncio.TimeoutError:
            err = f"timeout({timeout_s}s)"
        except Exception as exc:  # noqa: BLE001 —— 结构化输出/校验/传输失败如实留痕
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
    return {"id": row["id"], "query": query, "source": row.get("source", ""),
            "has_history": bool(row.get("history")), "expected": row["expected"],
            "predicted": pred, "error": err, "latency_ms": latency_ms,
            "attempts": attempt}


def render_vs_light(dec_evalres: dict, light_evalres: dict, meta: dict) -> list[str]:
    """「decision+护栏 vs light」分维度对照节 + 批次 3 建议（维度级胜负口径）。"""
    L = [f"## 混合规则终态 vs light 现任者基线对照（批次 3 建议，{meta['run_at'][:10]}）", ""]
    L.append("- **对照口径**：同金标集 147 条、同 exact-match 计分函数（score_decision_golden."
             "dim_metrics/evaluate 单一事实源）；各自以**生产形态**对打——decision 走网关 "
             "/v1/systemone（v0 判据 + align_answerable 护栏 + confidence_floor=0.5 阈值闸），"
             "light 走生产全职责路径（build_light_model + rewrite_full.txt/type_rules 判据面 + "
             "RewriteOutput function_calling 结构化输出，extra_body think=false + disable_streaming，"
             "alias=qwen3.5-4b-ollama）。失败/解析失败条目两侧同口径计 error 不进分母（本轮两侧均 0 error）。")
    L.append("- **结构差注记**：light 的 search_strategy Literal 四值、safety 五值均**无 other 桶**"
             "（decision 题面 options 含 other）——金标 other 类（st n=5 / safety n=2）对 light 结构性"
             "不可达，属 schema 结构差非能力差，归因单列。")
    L.append("")
    L.append("### 分维度对照")
    L.append("")
    L.append("| 维度 | decision+护栏 | light 现任者 | Δ(decision−light) | 维度胜者 |")
    L.append("|---|---|---|---|---|")
    wins: Counter = Counter()
    for dim in DIMENSIONS:
        d = dec_evalres["dims"][dim]
        lit = light_evalres["dims"][dim]
        delta = d["accuracy"] - lit["accuracy"]
        winner = "decision" if delta > 0 else ("light" if delta < 0 else "持平")
        wins[winner] += 1
        L.append(f"| {dim} | {_pct(d['accuracy'])}（{d['correct']}/{d['total']}） | "
                 f"{_pct(lit['accuracy'])}（{lit['correct']}/{lit['total']}） | {delta * 100:+.2f}pp | {winner} |")
    dm, lm = dec_evalres["overall_macro"], light_evalres["overall_macro"]
    L.append(f"| **overall(macro 四维平均)** | **{_pct(dm)}** | **{_pct(lm)}** | {(dm - lm) * 100:+.2f}pp | "
             f"decision {wins['decision']} 维 / light {wins['light']} 维 |")
    L.append("")
    L.append("### 关键单类对照（切换价值面）")
    L.append("")
    L.append("| 类 | decision recall | light recall | 差值 |")
    L.append("|---|---|---|---|")
    focus = [("search_strategy", "verify"), ("search_strategy", "direct"),
             ("search_strategy", "recall"), ("search_strategy", "web"),
             ("search_strategy", "other"), ("query_type", "multi_facet"),
             ("query_type", "how_to"), ("query_type", "comparison"),
             ("answerable", "yes"), ("safety_shadow", "none"),
             ("safety_shadow", "politics"), ("safety_shadow", "other")]
    for dim, cls in focus:
        d = dec_evalres["dims"][dim]["per_class"].get(cls)
        lit = light_evalres["dims"][dim]["per_class"].get(cls)
        if d is None and lit is None:
            continue
        dr = d["recall"] if d else None
        lr = lit["recall"] if lit else None
        ds = f"{_pct(dr)}（n={d['support']}）" if d else "—（无该类）"
        ls = f"{_pct(lr)}（n={lit['support']}）" if lit else "—（无该类）"
        delta = f"{(dr - lr) * 100:+.1f}pp" if (dr is not None and lr is not None) else "—"
        L.append(f"| {dim}.{cls} | {ds} | {ls} | {delta} |")
    L.append("")
    return L


def _pct(x: float | None) -> str:
    return f"{x * 100:.2f}%" if x is not None else "—"


def main() -> int:
    parser = argparse.ArgumentParser(description="light 现任者公平基线判分（批次 2.5）")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--details", type=Path, default=DEFAULT_DETAILS)
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"单条调用超时秒（默认 {DEFAULT_TIMEOUT_S}，生产 fast.llm_timeout_s=60 的评测宽松包络）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--no-report", action="store_true", help="只算不写 details/对照节")
    parser.add_argument("--compare-details", type=Path, default=None,
                        help="decision+护栏终态 details JSON（对照节数据源）")
    parser.add_argument("--append-report", type=Path, default=None,
                        help="追加模式：对照节追加到该报告文件尾部")
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    if not env.get("LITELLM_MASTER_KEY"):
        print("[FAIL] .env 缺 LITELLM_MASTER_KEY（零回显纪律：只校验存在性）", file=sys.stderr)
        return 2

    rows = [json.loads(line) for line in args.golden.read_text(encoding="utf-8").splitlines() if line.strip()]
    chain = build_light_model().with_structured_output(RewriteOutput, method="function_calling")
    print(f"golden={len(rows)} chain=build_light_model+RewriteOutput(function_calling) "
          f"timeout={args.timeout}s retries={args.retries}", file=sys.stderr)

    async def _run() -> list:
        out = []
        for idx, row in enumerate(rows if args.limit is None else rows[:args.limit], 1):
            rec = await score_row(chain, row, args.timeout, args.retries)
            out.append(rec)
            status = "ok" if rec["predicted"] else f"ERR {rec['error']}"
            print(f"[{idx}] {rec['id']} {status} {rec.get('latency_ms')}ms", file=sys.stderr)
        return out

    results = asyncio.run(_run())
    evalres = evaluate(results)
    errors = [r for r in results if r["error"] or not r["predicted"]]
    lats = sorted(r["latency_ms"] for r in results if r.get("latency_ms"))
    p50 = lats[len(lats) // 2] if lats else None

    summary = {"totalCases": len(results), "errors": len(errors),
               "dims": {dim: {"total": d["total"], "correct": d["correct"],
                              "accuracy": round(d["accuracy"], 4)} for dim, d in evalres["dims"].items()},
               "overall_macro": round(evalres["overall_macro"], 4),
               "minClass": evalres["min_class_ref"],
               "minClassRecall": round(evalres["min_class"], 4) if evalres["min_class"] is not None else None,
               "gateVerdict": evalres["verdict"], "gateReasons": evalres["reasons"],
               "latency_p50_ms": p50}
    if not args.no_report:
        meta = {"run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cmd": " ".join(["uv run --package search-api scripts/score_light_baseline.py", *sys.argv[1:]])}
        args.details.write_text(json.dumps({"meta": meta, "summary": summary, "results": results},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"details -> {args.details}", file=sys.stderr)
        if args.append_report and args.compare_details:
            dec_doc = json.loads(Path(args.compare_details).read_text(encoding="utf-8"))
            dec_results = dec_doc.get("results") or []
            dec_evalres = evaluate(dec_results)
            section = render_vs_light(dec_evalres, evalres, meta)
            section.extend(_batch3_advice(dec_evalres, evalres, results))
            with args.append_report.open("a", encoding="utf-8") as fh:
                fh.write("\n" + "\n".join(section) + "\n")
            print(f"appended -> {args.append_report}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def _batch3_advice(dec_evalres: dict, light_evalres: dict, light_results: list) -> list[str]:
    """批次 3 建议（维度级胜负 + 条件灰度，非绝对门槛——2026-11-19 用户裁决④）。"""
    L = ["### 批次 3 建议（维度级胜负与条件灰度）", ""]
    dims = ("query_type", "search_strategy", "answerable", "safety_shadow")
    dec_wins, light_wins = [], []
    for dim in dims:
        d = dec_evalres["dims"][dim]["accuracy"]
        lit = light_evalres["dims"][dim]["accuracy"]
        (dec_wins if d > lit else light_wins if lit > d else []).append((dim, d - lit))
    L.append(f"- **维度级胜负**：decision 胜 {len(dec_wins)} 维（{'、'.join(f'{x[0]} {x[1] * 100:+.1f}pp' for x in dec_wins) or '—'}）；"
             f"light 胜 {len(light_wins)} 维（{'、'.join(f'{x[0]} {x[1] * 100:+.1f}pp' for x in light_wins) or '—'}）。")
    L.append("- **建议口径（用户裁决④）**：批次 3 去留基于本对照的维度级胜负与灰度条件设计，"
             "不套用「总体 ≥90% 且 search_strategy ≥95%」绝对门槛——该门槛是 decision**替代 light** "
             "的切换门禁；若 decision 相对 light 无维度级净优势，灰度价值须重新论证（含成本面："
             "decision 为轮内**新增**前置调用，light 基线零新增成本）。")
    L.append("- **条件灰度建议**：")
    for dim, gap in sorted(dec_wins, key=lambda x: -x[1]):
        L.append(f"  - {dim}：decision 领先 {gap * 100:+.1f}pp——若差距稳定且集中在高价值类"
                 f"（verify/direct/recall 路由面），可考虑按维度窄口径灰度（decision 仅回填该维、"
                 f"其余维仍走 light），但 decision 一次前向四题不支持按维裁剪——窄口径灰度需题面键"
                 f"裁剪支持，归批次 3 契约面裁决。")
    for dim, gap in sorted(light_wins, key=lambda x: -x[1]):
        L.append(f"  - {dim}：light 领先 {abs(gap) * 100:.1f}pp——decision 该维弱于现任者，灰度若继续，"
                 f"该维应由 light 判定面兜底（现行 v1.33 瘦身调用形态已把分类面迁 decision，"
                 f"回迁属契约面变更，归批次 3 裁决）。")
    if not dec_wins:
        L.append("  - decision 四维无净胜——按用户裁决④口径，批次 3 建议倾向**不灰度**"
                 "（decision-model kill switch 维持关闭），判据迭代与阈值闸投入仅作技术储备；"
                 "如仍灰度，须以 shadow 模式先行（fast.decision.shadow=true 只观测不生效）。")
    L.append("")
    return L


if __name__ == "__main__":
    sys.exit(main())
