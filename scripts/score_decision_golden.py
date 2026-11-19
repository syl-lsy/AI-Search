#!/usr/bin/env python3
"""decision-model 金标集离线判分（plan-decision-model-20261118 批次 2 / 评测线）。

A' 契约形态 a：自有进程、exact-match、分维度混淆矩阵。流程 = 读金标集 → 逐条经网关
/v1/systemone 调 decision-model（题面自 defaults.py 现行判据）→ 分维度 exact-match
计分 → 写报告（分维度表 / 混淆矩阵 / gate 判定 / 基线对照节）。

密钥纪律：LITELLM_MASTER_KEY 从 .env 直提（R19 排障提取式），零回显、零日志、零落盘。
题面：从 app.config_center.defaults PROMPTS_CONFIG["prompts"]["fast.decision.questions_json"]
读取（判据唯一事实源——判据修订即时生效；历史题面快照
apps/search-api/test/fixtures/decision_questions_20261118.json 仅作对照留档）。
state 装配：复用生产 app.agent.decision_client.build_state_text（单一事实源——评测
与生产同形，批次 2 评审 P2 修复；金标 history 为 {q, a_head} 生产形态）。

用法（仓库根）：
    uv run --package search-api scripts/score_decision_golden.py             # 全量+报告（默认批次 2 落档路径）
    uv run --package search-api scripts/score_decision_golden.py --limit 5   # 冒烟
    uv run --package search-api scripts/score_decision_golden.py --no-report # 只算不写档
    # 迭代基线跑：--report/--details 指新档位 + --title 改标题 + --compare-details 挂旧档对照节
    # 迭代重判分（append 模式）：--append-report 报告 --round N --compare-details 上轮 details
    #   + --criteria-note 判据修订摘要；details 仍写 --details 指定路径
    # 终态模式（round 5，2026-11-19 用户裁决「混合规则+补 light 基线」）：--guard
    #   = 生产 parse 语义重判分——decision_client.parse_answers（confidence_floor
    #   阈值闸 + align_answerable 混合规则护栏）与判定生效轮行为同形；题面源同步
    #   切 decision_client.load_questions()（生产回落路径：Nacos 快照 → 内置
    #   decision_questions_json.txt 现行完整判据——评测与生产同形单一事实源）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

from app.agent import decision_client
from app.agent.decision_client import QUESTIONS_KEY, build_state_text, load_questions
from app.config_center.defaults import PROMPTS_CONFIG  # noqa: F401 —— legacy（非 --guard）题面源

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = ROOT / "apps/search-api/test/fixtures/golden_decision_20261118.jsonl"
DEFAULT_REPORT = ROOT / "docs/eval-decision-batch2-20261118.md"
DEFAULT_DETAILS = ROOT / "docs/eval-decision-batch2-20261118-details.json"

QT_CLASSES = ["factoid", "list", "comparison", "multi_facet", "temporal", "how_to", "navigational", "other"]
ST_CLASSES = ["direct", "recall", "verify", "web", "other"]
SAF_CLASSES = ["none", "danger", "profanity", "privacy", "politics", "other"]
ANS_CLASSES = ["yes", "no"]
DIMENSIONS = ("query_type", "search_strategy", "answerable", "safety_shadow")

# 判分门禁（plan 裁决 4 数值）：总体 ≥90% 且 search_strategy ≥95% 且无单类 <80%。
# 单类塌方检查 = query_type / search_strategy / answerable / safety_shadow 全部单类，
# support < 5 的类不进塌方分母（safety 其他桶=2、薄类样本量不足时如实标注）。
GATE_OVERALL = 0.90
GATE_STRATEGY = 0.95
GATE_MIN_CLASS = 0.80
GATE_CLASS_MIN_SUPPORT = 5


def load_env(path: Path) -> dict:
    """R19 排障提取式：剥行内注释（仅 # 前有空白时）与包裹引号；值零打印。"""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        m = re.search(r"\s+#", val)
        if m:
            val = val[: m.start()]
        out[key.strip()] = val.strip().strip('"').strip("'").strip()
    return out


def call_decision(client: httpx.Client, state: str, questions: dict, retries: int = 3):
    """POST /v1/systemone（网关 decision-model 别名）。5xx/网络错误重试，4xx 不重试。"""
    payload = {"model": "decision-model", "state": state, "questions": questions}
    err = "unknown"
    for attempt in range(retries):
        try:
            resp = client.post("/v1/systemone", json=payload, timeout=30.0)
        except httpx.HTTPError as exc:
            err = f"http_error:{type(exc).__name__}"
            time.sleep(1.0 * (attempt + 1))
            continue
        if resp.status_code == 200:
            return resp.json(), None
        err = f"http_{resp.status_code}:{resp.text[:200]}"
        if resp.status_code >= 500:
            time.sleep(1.0 * (attempt + 1))
            continue
        break
    return None, err


def parse_answers(data: dict):
    """四题答案解析（legacy，round 0~4 口径）：choice 取 choice，noul 按 P(yes)≥0.5 判 yes/no。"""
    try:
        ans = data["answers"]
        qt = ans["query_type"]["choice"]
        st = ans["search_strategy"]["choice"]
        p_yes = float(ans["answerable"]["noul"])
        saf = ans["safety_shadow"]["choice"]
        st_conf = ans["search_strategy"].get("confidence")
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"schema:{type(exc).__name__}"
    pred = {"query_type": qt, "search_strategy": st, "answerable": "yes" if p_yes >= 0.5 else "no",
            "safety_shadow": saf, "_p_yes": round(p_yes, 4), "_st_conf": st_conf}
    return pred, None


def parse_answers_guard(data: dict, confidence_floor: float):
    """--guard 终态解析：生产 `decision_client.parse_answers` 同形——confidence_floor
    阈值闸（query_type→other / search_strategy→web 保守回落）+ align_answerable
    混合规则护栏（verify→yes / direct·recall→no / web 保留原值，override 以
    answerable_source=guard 标注）。safety_shadow 仅观测：None（题缺失/形态非法）
    映射 "__missing__" 记错例（不进混淆矩阵合法类）。raw 模型原始值另行保留供
    护栏翻转归因（_raw_*：阈值闸/护栏前的模型答案）。"""
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return None, "schema:answers_missing"
    try:
        outcome = decision_client.parse_answers(dict(answers), confidence_floor=confidence_floor)
    except Exception as exc:  # noqa: BLE001 —— 解析失败如实计 error，不静默
        return None, f"schema:{type(exc).__name__}"

    def _raw_choice(key: str):
        raw = decision_client._choice_value(answers.get(key))
        return raw[0] if raw else None

    def _raw_noul(key: str):
        ans = answers.get(key)
        v = ans.get("noul") if isinstance(ans, dict) else None
        return round(float(v), 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    pred = {
        "query_type": outcome.query_type,
        "search_strategy": outcome.search_strategy,
        "answerable": "yes" if outcome.answerable else "no",
        "safety_shadow": outcome.safety_shadow if outcome.safety_shadow is not None else "__missing__",
        "_p_yes": _raw_noul("answerable"),
        "_st_conf": (decision_client._choice_value(answers.get("search_strategy")) or (None, None))[1],
        "_answerable_source": outcome.answerable_source,
        "_raw_query_type": _raw_choice("query_type"),
        "_raw_search_strategy": _raw_choice("search_strategy"),
        "_raw_answerable": _raw_noul("answerable"),
        "_raw_safety": _raw_choice("safety_shadow"),
    }
    return pred, None


def run_scoring(rows: list, questions: dict, base_url: str, api_key: str, limit: int | None,
                sleep_s: float, guard: bool = False, confidence_floor: float = 0.5):
    """逐条判分主循环。错误案例保留 error 字段，不静默吞掉。guard=True 走生产
    parse 语义（阈值闸+护栏），False 走 legacy 解析（round 0~4 口径）。"""
    targets = rows if limit is None else rows[:limit]
    results: list[dict] = []
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(base_url=base_url, headers=headers) as client:
        for idx, row in enumerate(targets, 1):
            state = build_state_text(row["query"], row.get("history"))
            data, err = call_decision(client, state, questions)
            rec = {"id": row["id"], "query": row["query"], "source": row.get("source", ""),
                   "has_history": bool(row.get("history")), "expected": row["expected"],
                   "predicted": None, "error": err}
            if data is not None:
                pred, perr = (parse_answers_guard(data, confidence_floor) if guard
                              else parse_answers(data))
                rec["predicted"] = pred
                rec["error"] = perr
                rec["latency_ms"] = data.get("latency_ms")
                rec["input_tokens"] = (data.get("usage") or {}).get("input_tokens")
            results.append(rec)
            status = "ok" if rec["predicted"] else f"ERR {rec['error']}"
            print(f"[{idx}/{len(targets)}] {row['id']} {status}", file=sys.stderr)
            if sleep_s:
                time.sleep(sleep_s)
    return results


def dim_metrics(results: list, dim: str) -> dict:
    """单维度指标：总准确率 + 单类 support/recall + 混淆矩阵（行=expected，列=predicted）。"""
    judged = [r for r in results if r["predicted"] and not r["error"]]
    correct = sum(1 for r in judged if r["predicted"][dim] == r["expected"][dim])
    per_class: dict[str, dict] = {}
    for cls in sorted({r["expected"][dim] for r in judged}):
        sub = [r for r in judged if r["expected"][dim] == cls]
        hit = sum(1 for r in sub if r["predicted"][dim] == cls)
        per_class[cls] = {"support": len(sub), "correct": hit, "recall": hit / len(sub)}
    classes = sorted({r["expected"][dim] for r in judged} | {r["predicted"][dim] for r in judged},
                     key=lambda c: (c not in {"factoid", "list", "comparison", "multi_facet", "temporal",
                                              "how_to", "navigational", "other", "direct", "recall",
                                              "verify", "web", "yes", "no", "none", "danger",
                                              "profanity", "privacy", "politics"}, c))
    matrix = {e: {p: 0 for p in classes} for e in classes}
    for r in judged:
        matrix[r["expected"][dim]][r["predicted"][dim]] += 1
    return {"dim": dim, "total": len(judged), "correct": correct,
            "accuracy": correct / len(judged) if judged else 0.0,
            "per_class": per_class, "matrix": matrix, "classes": classes}


def evaluate(results: list) -> dict:
    """四维指标 + 门禁判定。返回供报告与 stdout 摘要共用的结构。"""
    dims = {dim: dim_metrics(results, dim) for dim in DIMENSIONS}
    judged_n = next(d["total"] for d in dims.values())
    errors = [r for r in results if r["error"] or not r["predicted"]]
    overall_macro = sum(d["accuracy"] for d in dims.values()) / len(dims)
    micro_correct = sum(d["correct"] for d in dims.values())
    micro_total = sum(d["total"] for d in dims.values())

    # 单类塌方检查：support ≥ 5 的所有单类（含 safety/answerable）
    min_class = None
    min_class_ref = None
    for dim, d in dims.items():
        for cls, m in d["per_class"].items():
            if m["support"] >= GATE_CLASS_MIN_SUPPORT:
                if min_class is None or m["recall"] < min_class:
                    min_class = m["recall"]
                    min_class_ref = f"{dim}.{cls}(n={m['support']})"
    reasons = []
    if overall_macro < GATE_OVERALL:
        reasons.append(f"总体(macro) {overall_macro:.4f} < {GATE_OVERALL}")
    if dims["search_strategy"]["accuracy"] < GATE_STRATEGY:
        reasons.append(f"search_strategy {dims['search_strategy']['accuracy']:.4f} < {GATE_STRATEGY}")
    if min_class is not None and min_class < GATE_MIN_CLASS:
        reasons.append(f"最小单类 recall {min_class:.4f} < {GATE_MIN_CLASS}（{min_class_ref}）")
    verdict = "pass" if not reasons else ("fail" if overall_macro < GATE_OVERALL else "partial")

    # answerable 阈值敏感性：P(yes) 阈值扫描（--guard 模式为护栏前 raw 概率；缺失=不进扫描）
    p_yes_judged = [(r["expected"]["answerable"], r["predicted"]["_p_yes"])
                    for r in results if r["predicted"] and r["predicted"].get("_p_yes") is not None]
    sweep = {}
    for th in (0.3, 0.5, 0.7):
        hit = sum(1 for exp, p in p_yes_judged if (("yes" if p >= th else "no") == exp))
        sweep[th] = round(hit / len(p_yes_judged), 4) if p_yes_judged else 0.0

    # search_strategy confidence 阈值闸模拟（plan §2.2：conf<τ 回落 web）
    st_judged = [(r["expected"]["search_strategy"], r["predicted"]["search_strategy"],
                  r["predicted"].get("_st_conf")) for r in results if r["predicted"]]
    conf_sweep = {}
    for th in (0.3, 0.4, 0.5, 0.6):
        hit = sum(1 for exp, pr, cf in st_judged
                  if (pr if (cf is not None and cf >= th) else "web") == exp)
        conf_sweep[th] = round(hit / len(st_judged), 4) if st_judged else 0.0
    conf_ok = [cf for exp, pr, cf in st_judged if cf is not None and pr == exp]
    conf_bad = [cf for exp, pr, cf in st_judged if cf is not None and pr != exp]
    return {"dims": dims, "judged": judged_n, "errors": errors, "overall_macro": overall_macro,
            "overall_micro_correct": micro_correct, "overall_micro_total": micro_total,
            "min_class": min_class, "min_class_ref": min_class_ref,
            "verdict": verdict, "reasons": reasons,
            "ans_sweep": sweep, "conf_sweep": conf_sweep,
            "conf_mean_ok": sum(conf_ok) / len(conf_ok) if conf_ok else None,
            "conf_mean_bad": sum(conf_bad) / len(conf_bad) if conf_bad else None}


def compare_with_old(old_results: list, new_results: list) -> dict:
    """旧档 details results ↔ 本轮 results 逐维对照：accuracy 与预测翻转归因。

    返回 {dim: {old_acc, new_acc, flips, flip_ids, better, worse, flip_outside_history}}；
    flip_outside_history 为历史外翻转条数（state 文本仅带 history 的轮不同——
    生产 build_state_text 与旧 compose_state 在无历史轮同样输出 query 原文，
    故该值应恒为 0，非零= 归因口径失效，须如实上报）。
    """
    old_by_id = {r["id"]: r for r in old_results if isinstance(r, dict) and r.get("id")}
    out: dict[str, dict] = {}
    for dim in DIMENSIONS:
        flips: list[str] = []
        better = worse = flip_outside_history = 0
        for r in new_results:
            if not r.get("predicted") or r.get("error"):
                continue
            o = old_by_id.get(r["id"])
            if not o or not o.get("predicted") or o.get("error"):
                continue
            if o["predicted"][dim] != r["predicted"][dim]:
                flips.append(r["id"])
                if not r.get("has_history"):
                    flip_outside_history += 1
                o_ok = o["predicted"][dim] == r["expected"][dim]
                n_ok = r["predicted"][dim] == r["expected"][dim]
                if n_ok and not o_ok:
                    better += 1
                elif o_ok and not n_ok:
                    worse += 1
        out[dim] = {"flips": len(flips), "flip_ids": flips,
                    "better": better, "worse": worse,
                    "flip_outside_history": flip_outside_history}
    return out


def render_baseline_compare(evalres: dict, results: list, meta: dict) -> list[str]:
    """基线声明 + 批次 2 旧形态数字对照节（--compare-details 提供时渲染）。"""
    old_doc = json.loads(Path(meta["compare_details"]).read_text(encoding="utf-8"))
    old_results = old_doc.get("results") or []
    comp = compare_with_old(old_results, results)
    L: list[str] = []
    L.append("## 〇、基线声明与形态差影响对照（round 0 / 判据 v0）")
    L.append("")
    L.append("- **基线声明**：本轮 = 判据 v0（defaults.py `fast.decision.questions_json` 现行值，未做任何判据修订）+ 生产 `build_state_text` state 形态的正式基线跑（round 0）；校准修复（state 形态/题面单源）已就位后执行；金标 expected 与判据 v0 均未因模型表现修改（反应试纪律——本轮数字为判据修订前的诚实基线）。")
    L.append(f"- **对照来源**：批次 2 旧形态判分 `{meta['compare_details']}`（{old_doc.get('meta', {}).get('run_at', '')}，自有 compose_state「用户：/助手：+摘编+query 后置」形态）。")
    L.append("- **归因口径**：新旧 state 文本仅在带 history 的 12 条上不同（无历史轮两形态同样输出 query 原文），一切预测翻转必落于 rc01~rc12；对照实测历史外翻转条数见下表（非 0 = 归因口径失效须人工核查）。")
    L.append("")
    L.append("| 维度 | 批次 2（旧形态） | round 0（本轮） | Δ accuracy | 旧对→新错 | 旧错→新对 | 净效果 | 历史外翻转 |")
    L.append("|---|---|---|---|---|---|---|---|")
    old_macro = 0.0
    for dim in DIMENSIONS:
        d = evalres["dims"][dim]
        c = comp[dim]
        old_judged = [r for r in old_results if r.get("predicted") and not r.get("error")]
        old_correct = sum(1 for r in old_judged if r["predicted"][dim] == r["expected"][dim])
        old_acc = old_correct / len(old_judged) if old_judged else 0.0
        old_macro += old_acc
        delta = d["accuracy"] - old_acc
        net = c["better"] - c["worse"]
        net_s = f"{net:+d} 条" if net else "持平"
        L.append(f"| {dim} | {_pct(old_acc)}（{old_correct}/{len(old_judged)}） | {_pct(d['accuracy'])}（{d['correct']}/{d['total']}） | {delta * 100:+.2f}pp | {c['worse']} | {c['better']} | {net_s} | {c['flip_outside_history']} |")
    old_macro /= len(DIMENSIONS)
    L.append(f"| **overall(macro 四维平均)** | **{_pct(old_macro)}** | **{_pct(evalres['overall_macro'])}** | {(evalres['overall_macro'] - old_macro) * 100:+.2f}pp | — | — | — | — |")
    L.append("")
    flip_lines = [f"{dim}：{'、'.join(comp[dim]['flip_ids']) or '（无翻转）'}"
                  for dim in DIMENSIONS if comp[dim]["flip_ids"]]
    if flip_lines:
        L.append("- **翻转条目（旧→新预测变化，可归因 state 形态）**：" + "；".join(flip_lines) + "。")
    L.append("- **解读口径**：各维 accuracy 变化全部由上表翻转条目贡献；翻转仅发生于带 history 的轮（历史外翻转=0 时成立），属 state 形态修复的形态差效应，非判据效应——判据修订的增量评估以本轮为基线、以判据 v0→v1 差值计，不与批次 2 旧形态数字直接混比。")
    L.append("")
    return L


def _dim_acc(results: list, dim: str) -> tuple[int, int, float]:
    """单维 (判定位, 判对数, accuracy)——对照表与 round 节共用。"""
    judged = [r for r in results if r.get("predicted") and not r.get("error")]
    correct = sum(1 for r in judged if r["predicted"][dim] == r["expected"][dim])
    return len(judged), correct, (correct / len(judged) if judged else 0.0)


def render_dims_summary_table(evalres: dict, heading: str) -> list[str]:
    """分维度总表（总表 + macro/micro 行 + 判分失败行），全量报告与 round 节共用。"""
    dims = evalres["dims"]
    L = [heading, "", "| 维度 | total | correct | accuracy |", "|---|---|---|---|"]
    for dim in DIMENSIONS:
        d = dims[dim]
        L.append(f"| {dim} | {d['total']} | {d['correct']} | {_pct(d['accuracy'])} |")
    L.append(f"| **overall(macro 四维平均)** | — | — | **{_pct(evalres['overall_macro'])}** |")
    L.append(f"| overall(micro 全判定位) | {evalres['overall_micro_total']} | {evalres['overall_micro_correct']} | {_pct(evalres['overall_micro_correct'] / evalres['overall_micro_total'] if evalres['overall_micro_total'] else 0.0)} |")
    L.append("")
    L.append(f"判分失败（请求/解析错误，不进分母）：{len(evalres['errors'])} 条" + (f"（{', '.join(r['id'] for r in evalres['errors'])}）" if evalres["errors"] else "。"))
    L.append("")
    return L


def render_dim_tables(d: dict, heading: str) -> list[str]:
    """单维 per-class 表 + 混淆矩阵，全量报告与 round 节共用。"""
    L = [heading, "", "| 类 | support | correct | recall |", "|---|---|---|---|"]
    for cls, m in sorted(d["per_class"].items(), key=lambda kv: -kv[1]["support"]):
        flag = " ⚠️" if (m["support"] >= GATE_CLASS_MIN_SUPPORT and m["recall"] < GATE_MIN_CLASS) else ""
        L.append(f"| {cls}{flag} | {m['support']} | {m['correct']} | {_pct(m['recall'])} |")
    L += ["", _fmt_conf_matrix(d), ""]
    return L


def render_key_errors(results: list, evalres: dict) -> list[str]:
    """错例归因节：逐维混淆对（含代表条目 id）+ 低于门线单类——判据修订的输入。"""
    L = ["### 错例归因（keyErrors，供下一轮判据修订）", ""]
    for dim in DIMENSIONS:
        d = evalres["dims"][dim]
        pairs: dict[tuple[str, str], list[str]] = {}
        for r in results:
            if r.get("predicted") and not r.get("error") and r["predicted"][dim] != r["expected"][dim]:
                pairs.setdefault((r["expected"][dim], r["predicted"][dim]), []).append(r["id"])
        L.append(f"- **{dim}**（acc {_pct(d['accuracy'])}）混淆对：")
        for (e, p), ids in sorted(pairs.items(), key=lambda kv: -len(kv[1])):
            id_s = "，".join(ids[:8]) + ("…" if len(ids) > 8 else "")
            L.append(f"  - {e}→{p}×{len(ids)}：{id_s}")
        L.append("")
    weak = [(dim, cls, m) for dim in DIMENSIONS for cls, m in evalres["dims"][dim]["per_class"].items()
            if m["support"] >= GATE_CLASS_MIN_SUPPORT and m["recall"] < GATE_MIN_CLASS]
    if weak:
        L.append("- **低于 80% 门线的单类**：" + "；".join(f"{dim}.{cls}（{_pct(m['recall'])}，n={m['support']}）" for dim, cls, m in weak))
        L.append("")
    return L


def render_round_section(evalres: dict, results: list, meta: dict, round_no: int,
                         prev_results: list, criteria_note: str,
                         questions_src: str | None = None,
                         cond_note: str | None = None,
                         section_label: str = "判据修订重判分") -> list[str]:
    """round N 重判分节（判据修订重跑）：与上轮对照 + 分维度 + 混淆矩阵 + gate + keyErrors。

    归因口径：state 文本与上轮逐字节相同（同金标 fixture/同 build_state_text 装配/同
    网关别名），预测翻转全部归因判据修订（判据效应），与批次 2 的 state 形态差归因不同。
    round 5 终态轮经 questions_src/cond_note/section_label 覆写为「判据回滚 + 护栏 +
    生产 parse 语义」形态（翻转归因 = 判据回滚 + parse 语义变更的复合效应）。
    """
    src = questions_src or f"`app.config_center.defaults PROMPTS_CONFIG[\"prompts\"][\"{QUESTIONS_KEY}\"]`"
    L = [f"## Round {round_no} 节——{section_label}（{meta['run_at'][:10]}）", ""]
    L.append(f"- **判据修订声明**：{criteria_note}（题面源 {src}；金标 expected 与历史数据未动——反应试纪律）。")
    L.append("- **判分条件**：" + (cond_note or "与上轮全同——同金标 fixture、同 `build_state_text` state 装配、同网关别名/端点，仅判据文本变更；故下表全部预测翻转可归因判据修订。"))
    L.append("")
    L.append("### 与上轮对照（round " + str(round_no - 1) + f" → round {round_no}）")
    L.append("")
    L.append("| 维度 | 上轮 acc | 本轮 acc | Δ accuracy | 旧对→新错 | 旧错→新对 | 净效果 | 翻转数 |")
    L.append("|---|---|---|---|---|---|---|---|")
    old_macro = 0.0
    for dim in DIMENSIONS:
        old_n, old_correct, old_acc = _dim_acc(prev_results, dim)
        new_n, new_correct, new_acc = _dim_acc(results, dim)
        old_macro += old_acc
        comp = compare_with_old([r for r in prev_results], results)[dim]
        net = comp["better"] - comp["worse"]
        net_s = f"{net:+d} 条" if net else "持平"
        L.append(f"| {dim} | {_pct(old_acc)}（{old_correct}/{old_n}） | {_pct(new_acc)}（{new_correct}/{new_n}） | {(new_acc - old_acc) * 100:+.2f}pp | {comp['worse']} | {comp['better']} | {net_s} | {comp['flips']} |")
    old_macro /= len(DIMENSIONS)
    L.append(f"| **overall(macro 四维平均)** | **{_pct(old_macro)}** | **{_pct(evalres['overall_macro'])}** | {(evalres['overall_macro'] - old_macro) * 100:+.2f}pp | — | — | — | — |")
    L.append("")
    flip_ids = compare_with_old(prev_results, results)
    flip_lines = [f"{dim}：{'、'.join(v['flip_ids']) or '（无翻转）'}" for dim, v in flip_ids.items() if v["flip_ids"]]
    L.append("- **翻转条目（上轮→本轮预测变化，全部归因判据修订）**：" + ("；".join(flip_lines) + "。" if flip_lines else "本轮零翻转——判据修订对本金标集预测无影响，数字与上轮逐维相同。"))
    L.append("")
    guard_hits = [r for r in results if r.get("predicted") and r["predicted"].get("_answerable_source") == "guard"]
    if guard_hits:
        by_flip = Counter()
        for r in guard_hits:
            raw = r["predicted"].get("_raw_answerable")
            raw_s = "yes" if raw is True or raw == "yes" or (isinstance(raw, float) and raw >= 0.5) else "no"
            by_flip[(r["predicted"]["search_strategy"], f"raw={raw_s}→guard={'yes' if r['predicted']['answerable']=='yes' else 'no'}")] += 1
        L.append("- **护栏 override 统计（answerable_source=guard）**：" + f"{len(guard_hits)} 条被护栏改写——" +
                 "，".join(f"st={k[0]}（{k[1]}）×{v}" for k, v in by_flip.most_common()) + "。")
        L.append("")
    L.extend(render_dims_summary_table(evalres, "### 分维度总表"))
    for dim in DIMENSIONS:
        L.extend(render_dim_tables(evalres["dims"][dim], f"### {dim} 单类与混淆矩阵"))
    L.append("### 门禁判定")
    L.append("")
    L.append("- 切换门禁：总体(macro) ≥90% 且 search_strategy ≥95% 且无任何单类 recall <80%（support≥5 单类全查）。")
    L.append(f"**gateVerdict = {evalres['verdict']}**")
    if evalres["reasons"]:
        for reason in evalres["reasons"]:
            L.append(f"- ❌ {reason}")
    else:
        L.append("- ✅ 三条件全部满足")
    L.append("")
    L.append("### 阈值校准（供批次 3 参数初值参考）")
    L.append("")
    L.append("- **answerable P(yes) 阈值扫描**（当前 0.5）：" +
             "，".join(f"τ={th} → {_pct(acc)}" for th, acc in evalres["ans_sweep"].items()))
    if evalres["conf_mean_ok"] is not None:
        L.append(f"- **search_strategy confidence 校准**：判对均值 {_pct(evalres['conf_mean_ok'])}，判错均值 {_pct(evalres['conf_mean_bad'])}（confidence≠max(probabilities)，独立校准量）")
    L.append("- **confidence<τ 回落 web 模拟**（plan §2.2 阈值闸）：" +
             "，".join(f"τ={th} → {_pct(acc)}" for th, acc in evalres["conf_sweep"].items()))
    L.append("")
    L.extend(render_key_errors(results, evalres))
    return L


def _pct(x: float | None) -> str:
    return f"{x * 100:.2f}%" if x is not None else "—"


def _fmt_conf_matrix(d: dict) -> str:
    """混淆矩阵 → markdown 表（行=expected，列=predicted，含行 support/recall）。"""
    classes = d["classes"]
    head = "| expected\\pred | " + " | ".join(classes) + " | support | recall |"
    sep = "|---" * (len(classes) + 3) + "|"
    lines = [head, sep]
    for e in classes:
        row = d["matrix"][e]
        sup = d["per_class"].get(e, {}).get("support", 0)
        rec = d["per_class"].get(e)
        rec_s = f"{rec['recall'] * 100:.0f}%" if rec and rec["support"] else "—"
        cells = []
        for p in classes:
            v = row.get(p, 0)
            mark = f"**{v}**" if e == p and v else str(v)
            cells.append(mark)
        lines.append(f"| **{e}** | " + " | ".join(cells) + f" | {sup} | {rec_s} |")
    return "\n".join(lines)


def render_report(evalres: dict, results: list, meta: dict) -> str:
    """评测报告 markdown。数据驱动骨架 + 约定注记。"""
    dims = evalres["dims"]
    L: list[str] = []
    L.append(f"# {meta.get('title', 'Decision-model 金标集离线判分报告（批次 2，2026-11-18）')}")
    L.append("")
    L.append(f"- 判分时间：{meta['run_at']}；命令：`{meta['cmd']}`")
    L.append(f"- 金标集：`{meta['golden_rel']}`（{meta['golden_n']} 条；带 history {meta['hist_n']} 条）")
    L.append(f"- 题面：`{meta['questions_src']}`（判据唯一事实源，判据修订即时生效；四题一次前向）")
    L.append("- 网关端点：`POST {LITELLM_BASE_URL}/v1/systemone`，model=decision-model（密钥 .env 直提，零回显）")
    L.append("")
    if meta.get("compare_details"):
        L.extend(render_baseline_compare(evalres, results, meta))
    L.append("## 一、方法与口径")
    L.append("")
    L.append("- **判分形态**：A' 契约形态 a——自有进程、exact-match、分维度混淆矩阵；不进 CI 硬门禁（同 DEV §6.3 口径）。")
    L.append("- **state 组装**：复用生产 `app.agent.decision_client.build_state_text`（单一事实源，批次 2 评审 P2 修复）——无历史 = query 原文；带历史 = query 全文前置 + 空行 + `近轮对话历史摘编（供回顾/重复提问判定参考）：` + 逐轮 `Q: {q}` / `A: {a_head}`（金标 history 为 {q, a_head} 生产形态；R19 实证 recall 无历史不可判，故金标 recall 样例全部带 history）。")
    L.append("- **answerable**：noul 概率 P(yes)≥0.5 判 yes。")
    L.append("- **门禁（plan 裁决 4 数值）**：总体(macro 四维平均) ≥90% 且 search_strategy ≥95% 且无单类 recall <80%（support≥5 的单类全查）。")
    L.append("- **标注约定（构造依据可溯源）**：①verify⇒answerable=yes（criteria 一致性约束明文）；②零事实面题（寒暄/创作/翻译/纯计算/辱骂/改写）不属「定义/原理/公认事实/成熟操作步骤」→ answerable=no（noul 判据「仅当……才答 yes」严格读法）；③safety 正例次级维度按判据机械化推导（sf16~sf21 的 search_strategy=direct 为约定标注，判据未显式覆盖社交发泄/图像改写，属最不确定标注族）；④来源标签逐条落 fixture `source` 字段（type_rules-判例/判据推导、b9-research、b12-recall-A48、r19-probe、pr-a50-safety）。")
    L.append("- **A32 污染防护注记**：本集为离线构造 + git 快照治理；题面与金标查询未写入任何 Nacos 键 / 热门池 / 提示词 / 缓存。")
    L.append("")
    L.extend(render_dims_summary_table(evalres, "## 二、分维度总表"))
    for dim in DIMENSIONS:
        d = dims[dim]
        L.extend(render_dim_tables(d, f"## 三.{DIMENSIONS.index(dim) + 1} {dim} 单类与混淆矩阵"))
    L.append("## 四、阈值校准（供批次 2/3 参数初值参考）")
    L.append("")
    L.append("- **answerable P(yes) 阈值扫描**（当前 0.5）：" +
             "，".join(f"τ={th} → {_pct(acc)}" for th, acc in evalres["ans_sweep"].items()))
    if evalres["conf_mean_ok"] is not None:
        L.append(f"- **search_strategy confidence 校准**：判对均值 {_pct(evalres['conf_mean_ok'])}，判错均值 {_pct(evalres['conf_mean_bad'])}（confidence≠max(probabilities)，独立校准量，R19 §四注记）")
    L.append("- **confidence<τ 回落 web 模拟**（plan §2.2 阈值闸）：" +
             "，".join(f"τ={th} → {_pct(acc)}" for th, acc in evalres["conf_sweep"].items()))
    L.append("")
    L.append("## 五、错例清单（exact-match 不一致；全量见 details JSON）")
    L.append("")
    wrong = [r for r in results if r["predicted"] and not r["error"]]
    mismatches = [r for r in wrong if any(r["predicted"][dim] != r["expected"][dim] for dim in DIMENSIONS)]
    L.append(f"至少一维不一致：{len(mismatches)} / {len(wrong)} 条。前 30 条：")
    L.append("")
    L.append("| id | 维度(期望→实判) | query（截断 40 字） |")
    L.append("|---|---|---|")
    for r in mismatches[:30]:
        diffs = [f"{dim}:{r['expected'][dim]}→{r['predicted'][dim]}" for dim in DIMENSIONS
                 if r["predicted"][dim] != r["expected"][dim]]
        q = r["query"].replace("|", "／")[:40]
        L.append(f"| {r['id']} | {'；'.join(diffs)} | {q} |")
    L.append("")
    L.append("## 六、门禁判定")
    L.append("")
    L.append(f"**gateVerdict = {evalres['verdict']}**")
    if evalres["reasons"]:
        for reason in evalres["reasons"]:
            L.append(f"- ❌ {reason}")
    else:
        L.append("- ✅ 总体 ≥90%、search_strategy ≥95%、无单类 <80% 三条件全部满足")
    L.append("")
    L.append("## 七、差距与题面热更建议（迭代走 Nacos 热更归批次 3，本批只报告）")
    L.append("")
    for dim in DIMENSIONS:
        d = dims[dim]
        pairs = Counter()
        for r in results:
            if r["predicted"] and not r["error"] and r["predicted"][dim] != r["expected"][dim]:
                pairs[(r["expected"][dim], r["predicted"][dim])] += 1
        if pairs:
            tops = "，".join(f"{e}→{p}×{n}" for (e, p), n in pairs.most_common(5))
            L.append(f"- **{dim}**（acc {_pct(d['accuracy'])}）主要混淆对：{tops}")
    weak = [(dim, cls, m) for dim in DIMENSIONS for cls, m in dims[dim]["per_class"].items()
            if m["support"] >= GATE_CLASS_MIN_SUPPORT and m["recall"] < GATE_MIN_CLASS]
    if weak:
        L.append("- **低于 80% 门线的单类**：" + "；".join(f"{dim}.{cls}（{_pct(m['recall'])}，n={m['support']}）" for dim, cls, m in weak))
    L.append("- 若 answerable 在零事实面题（寒暄/创作/计算）上系统性判 yes：属 noul 判据与标注约定②的口径差，建议热更 `fast.decision.answerable` instructions 增补零事实面显式判例（如「你好→no」「帮我写诗→no」）或钉「该位仅服务 verify 轮」语义注，归批次 3 裁决。")
    L.append("- 若 direct 类 recall 低：R19 探针已观察到裸「你好」被判 web（criteria 全量版寒暄判据权重被稀释），建议在 direct criteria 增补「单句寒暄（你好/在吗/早）」判例。")
    L.append("- 热更载体：Nacos prompts dataId `fast.decision.*` 题面键（v1.33 契约），criteria 热更后以新题面快照重跑本脚本回归。")
    L.append("")
    L.append("## 八、溯源")
    L.append("")
    L.append("- 判据文档：`docs/research/query-classification-research-20261114.md`（B9 §5.1 题型映射）、B12 recall 判据（type_rules 现行值，工单题面同源）、`docs/PRD.md` A48/A50、`docs/research/decision-model-r19-probe-20261118.md`（R19 §5 样例族）。")
    L.append("- 金标逐条 `source` 字段标注构造依据族；报告 §一 列明标注约定。")
    L.append("- trace 回捞（线上真实 query agreement 对照）为本批可选增强，状态见报告补记节（未执行时注明未执行）。")
    L.append("- **补记——trace 回捞状态：未执行**。批次 2 已实证 Langfuse traces 查询端点 404 不可行（`docs/decision-model-batch2-20261118.md` 评测补记），线上真实 query agreement 对照缺席如实注记；判据修订验证以金标集重跑为唯一口径。")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="decision-model 金标集离线判分")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--details", type=Path, default=DEFAULT_DETAILS)
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟）")
    parser.add_argument("--sleep", type=float, default=0.05, help="请求间隔秒")
    parser.add_argument("--no-report", action="store_true", help="只算不写报告/详情")
    parser.add_argument("--title", default="Decision-model 金标集离线判分报告（批次 2，2026-11-18）",
                        help="报告标题（迭代基线跑用）")
    parser.add_argument("--compare-details", type=Path, default=None,
                        help="旧形态/上一轮判分 details JSON；全量报告模式追加基线对照节，append 模式作上轮对照")
    parser.add_argument("--append-report", type=Path, default=None,
                        help="追加模式：不写全量报告，把 round N 节追加到该报告文件尾部")
    parser.add_argument("--round", type=int, default=0, help="append 模式的轮次号（节标题与对照口径用）")
    parser.add_argument("--criteria-note", default="",
                        help="append 模式的判据修订声明摘要（写入 round 节声明行）")
    parser.add_argument("--guard", action="store_true",
                        help="终态模式：生产 parse 语义（decision_client.parse_answers——confidence_floor 阈值闸 + align_answerable 护栏）+ 题面源走 load_questions() 生产回落路径")
    parser.add_argument("--confidence-floor", type=float, default=None,
                        help="guard 模式 confidence_floor（缺省= decision_client.DEFAULT_CONFIDENCE_FLOOR=0.5，与 defaults fast.decision.confidence_floor 现行值一致）")
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    base_url = env.get("LITELLM_BASE_URL", "http://127.0.0.1:4100").rstrip("/")
    api_key = env.get("LITELLM_MASTER_KEY")
    if not api_key:
        print("[FAIL] .env 缺 LITELLM_MASTER_KEY（零回显纪律：只校验存在性）", file=sys.stderr)
        return 2

    rows = [json.loads(line) for line in args.golden.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.guard:
        questions = load_questions()
        questions_src = ("decision_client.load_questions()（生产回落路径：Nacos 快照 → 内置 "
                         "prompts/fast/decision_questions_json.txt 现行完整判据）")
        floor = args.confidence_floor if args.confidence_floor is not None else decision_client.DEFAULT_CONFIDENCE_FLOOR
    else:
        questions = json.loads(PROMPTS_CONFIG["prompts"][QUESTIONS_KEY])
        questions_src = f'app.config_center.defaults PROMPTS_CONFIG["prompts"]["{QUESTIONS_KEY}"]'
        floor = decision_client.DEFAULT_CONFIDENCE_FLOOR
    if not isinstance(questions, dict) or not questions:
        print(f"[FAIL] 题面键缺失或非法：{QUESTIONS_KEY}", file=sys.stderr)
        return 2
    print(f"golden={len(rows)} questions={sorted(questions)} guard={args.guard} floor={floor}", file=sys.stderr)

    results = run_scoring(rows, questions, base_url, api_key, args.limit, args.sleep,
                          guard=args.guard, confidence_floor=floor)
    evalres = evaluate(results)
    meta = {"run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cmd": " ".join(["uv run --package search-api scripts/score_decision_golden.py", *sys.argv[1:]]),
            "golden_rel": str(args.golden.relative_to(ROOT)),
            "questions_src": questions_src,
            "golden_n": len(rows), "hist_n": sum(1 for r in rows if r.get("history")),
            "title": args.title,
            "compare_details": (
                str(args.compare_details.relative_to(ROOT))
                if args.compare_details and args.compare_details.is_relative_to(ROOT)
                else str(args.compare_details) if args.compare_details else ""
            )}

    if not args.no_report:
        if args.append_report is not None:
            prev_results: list = []
            if args.compare_details is not None:
                prev_doc = json.loads(Path(meta["compare_details"]).read_text(encoding="utf-8"))
                prev_results = prev_doc.get("results") or []
            section = render_round_section(
                evalres, results, meta, args.round, prev_results, args.criteria_note,
                questions_src=meta.get("questions_src") if args.guard else None,
                cond_note=("与上轮的差异三件：①判据回滚 v0（题面源走生产回落路径内置现行判据）；"
                           "②代码护栏 align_answerable（verify→yes / direct·recall→no / web 保留原值）；"
                           "③parse 语义切生产 decision_client.parse_answers（confidence_floor=0.5 阈值闸，"
                           "query_type→other / search_strategy→web 保守回落）——故下表预测翻转归因"
                           "「判据回滚 + 护栏 + 阈值闸」复合效应，不再归因单一判据修订。")
                if args.guard else None,
                section_label="混合规则终态重判分（decision+护栏 vs 上轮判据 v4）" if args.guard else "判据修订重判分",
            )
            with args.append_report.open("a", encoding="utf-8") as fh:
                fh.write("\n" + "\n".join(section) + "\n")
            print(f"appended -> {args.append_report}", file=sys.stderr)
        else:
            args.report.write_text(render_report(evalres, results, meta), encoding="utf-8")
            print(f"report -> {args.report}", file=sys.stderr)
        args.details.write_text(json.dumps({"meta": meta, "summary": {
            "totalCases": len(results), "dims": {dim: {k: v for k, v in d.items() if k != "matrix"} for dim, d in evalres["dims"].items()},
            "overall_macro": evalres["overall_macro"], "verdict": evalres["verdict"],
            "ans_sweep": evalres["ans_sweep"], "conf_sweep": evalres["conf_sweep"]},
            "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"details -> {args.details}", file=sys.stderr)

    summary = {"totalCases": len(results), "errors": len(evalres["errors"]),
               "dims": {dim: {"total": d["total"], "correct": d["correct"], "accuracy": round(d["accuracy"], 4)} for dim, d in evalres["dims"].items()},
               "overall_macro": round(evalres["overall_macro"], 4),
               "minClass": evalres["min_class_ref"], "minClassRecall": round(evalres["min_class"], 4) if evalres["min_class"] is not None else None,
               "gateVerdict": evalres["verdict"], "gateReasons": evalres["reasons"]}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
