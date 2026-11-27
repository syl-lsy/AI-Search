"""B22 HITL 审批门 shadow 探针（批次 0——plan-hitl-kb-gate-20261126.md §12.2 批次 0②）。

**开发探针，不进生产链；只读 dev 库（SELECT + 参数绑定），零 git/零产品代码改动。**

复现 plan §4.1 判定树的「逐子问题 KB 混合检索 → Top K」段（纯积木拼装，不复用
kb_search 节点本体——规避 stream-writer 图外边界，盘点结论直接采用）：

    bm25_search ‖ vector_search（各腿 asyncio.timeout(kb.dual_channel_timeout_s)）
      → _candidate_from_row（保留 channel_rank）
      → filter_candidates（B15 顺序锁第一半——drop 不计入命中）
      → rrf_fuse_kb（RRF + type_weights 加权 + 同章节归并 + kb_fuse_top_k 截取）
      → rerank_via_gateway（qwen3-rerank，documents=title 形态与图级 fuse_rerank
        :326-328 逐字节一致）→ _rerank_order → Top K（rerank 后全序，「有值」=
        条目数 > 0，不设分数阈值——用户裁决口径）

子问题来源 = light 全职责 rewrite 复现（decision 映射运行态为空串 = kill switch
关闭，生产走 light 全路径——探针与生产同路径）：`_load_full_rewrite_template()` +
type_rules（Nacos 磁盘快照 → 内置回落，同 resolve_prompt 回落链）+ RewriteOutput
结构化输出 + `_clamp_queries`（type_query_map 夹取）。rewrite 失败降级为原问题
单子问题（报告注明降级影响）。

判定语义（plan §4.0/§4.1 逐条落地）：
- 任一子问题 Top K 有值 → has_hit（弹窗）；
- 每个子问题至少一路通道成功 且 全部 Top K 严格为空 → no_hit（web 直达）；
- 任一子问题全部启用通道失败 / rerank 失败 / 门内异常 → fallback_hybrid（不判无）。

运行态配置取本地 Nacos 磁盘快照（`data/nacos_snapshot`——dev 服务同源落盘，只读；
缺失回落 config_center.defaults，同 AssetCenter L3 预热语义）。观测零污染：探针
进程内显式关 LangSmith/LangChain tracing；rewrite 调用 config=None（零 CallbackHandler
→ 零 Langfuse trace）；embed/rerank 走裸 httpx（无观测钩子）。

用法（全部仓库根执行）::

    uv run --package search-api python scripts/probe_hitl_gate_shadow.py pick --limit 60
    uv run --package search-api python scripts/probe_hitl_gate_shadow.py run --limit 20 --skip 0
    uv run --package search-api python scripts/probe_hitl_gate_shadow.py rerank-empty
    uv run --package search-api python scripts/probe_hitl_gate_shadow.py aggregate \
        [--samples docs/research/b22-zerohit-samples-20261127.md]

凭据纪律：DB/ES/litellm 凭据只经 settings 单例读取，零打印；SQL 一律参数绑定。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# 观测零污染（先于任何 app/langchain import）：探针进程内强制关 tracing
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.setdefault("LOG_LEVEL", "WARNING")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "search-api"))

RESULTS_PATH = REPO_ROOT / ".zcode" / "tmp" / "hitl_shadow_results.jsonl"
QUESTIONS_PATH = REPO_ROOT / ".zcode" / "tmp" / "hitl_shadow_questions.json"

PROMPTS_DATA_ID = "ai_search.agent.prompts.json"
SPEC_DATA_ID = "ai_search.agent.spec.json"
MAPPING_DATA_ID = "ai_search.model.mapping.json"

#: 探针级子问题上限（rerank 调用预算控制——production 无此帽；报告注明）
DEFAULT_SQ_CAP = 3
#: 走 light 全路径复现的单调用超时（= fast.llm_timeout_s 运行值，快照缺失回落 60）
_REWRITE_TIMEOUT_FALLBACK_S = 60.0


# ---------------------------------------------------------------------------
# 运行态配置（磁盘快照 → defaults 回落，同 resolve_prompt / AssetCenter L3 语义）
# ---------------------------------------------------------------------------
def _load_runtime() -> dict[str, Any]:
    from app.config_center.cache import read_disk_snapshot
    from app.config_center.defaults import MODEL_MAPPING_CONFIG, PROMPTS_CONFIG, SPEC_CONFIG
    from app.core.config import nacos_settings

    snap_dir = Path(nacos_settings.snapshot_dir)

    def _snap(data_id: str, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = read_disk_snapshot(snap_dir, data_id)
        except Exception:  # noqa: BLE001 —— 损坏快照按缺失回落（L3 同语义）
            payload = None
        return payload if isinstance(payload, dict) else fallback

    return {
        "spec": _snap(SPEC_DATA_ID, SPEC_CONFIG),
        "mapping": _snap(MAPPING_DATA_ID, MODEL_MAPPING_CONFIG),
        "prompts": _snap(PROMPTS_DATA_ID, PROMPTS_CONFIG),
        "snapshot_dir": str(snap_dir),
    }


def _resolve_type_rules(runtime: dict[str, Any]) -> str:
    from app.agent.prompts import load_builtin_prompt

    prompts = runtime["prompts"].get("prompts")
    value = prompts.get("common.type_rules") if isinstance(prompts, dict) else None
    if isinstance(value, str) and value:
        return value
    return load_builtin_prompt("common.type_rules")


# ---------------------------------------------------------------------------
# 题库：dev 库已完成真实查询（只读 + 参数绑定）
# ---------------------------------------------------------------------------
def _pick_questions(limit: int) -> list[dict[str, Any]]:
    from sqlalchemy import text

    from app.db.session import get_session_factory  # 凭据只经 settings 单例，零打印

    uid = os.environ.get("HITL_PROBE_USER_ID", "").strip()

    async def _query() -> list[dict[str, Any]]:
        nonlocal uid
        sf = get_session_factory()
        async with sf() as s:
            if not uid:
                # 缺省取 ready 语料最大的用户（与检索同域——SA-4 用户域收口同语义）
                row = (
                    await s.execute(
                        text(
                            "SELECT user_id FROM kb_documents WHERE status = :st"
                            " GROUP BY user_id ORDER BY count(*) DESC LIMIT 1"
                        ),
                        {"st": "ready"},
                    )
                ).first()
                uid = str(row.user_id) if row else ""
            if not uid:
                return []
            rows = (
                await s.execute(
                    text(
                        "SELECT id, thread_id, user_id, query, status, mode, created_at"
                        " FROM turns WHERE user_id = :uid AND mode = :mode"
                        " AND status = :st AND query IS NOT NULL"
                        " ORDER BY created_at DESC, id"
                    ),
                    {"uid": uid, "mode": "fast", "st": "completed"},
                )
            ).all()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows:
            q = str(r.query or "").strip()
            if not q or q in seen:
                continue
            seen.add(q)
            out.append(
                {
                    "turn_id": str(r.id),
                    "thread_id": str(r.thread_id),
                    "user_id": str(r.user_id),
                    "question": q,
                    "turn_status": r.status,
                    "created_at": str(r.created_at),
                }
            )
            if len(out) >= limit:
                break
        return out

    questions = asyncio.run(_query())
    QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUESTIONS_PATH.write_text(
        json.dumps({"user_id": uid, "picked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "questions": questions}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"[pick] user={uid[:8]}*** 冻结题清单 {len(questions)} 条 → {QUESTIONS_PATH.relative_to(REPO_ROOT)}")
    return questions


def _load_questions() -> list[dict[str, Any]]:
    if not QUESTIONS_PATH.is_file():
        raise SystemExit("[run] 题清单缺失——先执行 pick")
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    return payload["questions"]


# ---------------------------------------------------------------------------
# rewrite 复现（light 全职责路径——与生产 kill switch 态逐字节同路径）
# ---------------------------------------------------------------------------
async def _rewrite_subqueries(
    question: str, runtime: dict[str, Any]
) -> dict[str, Any]:
    from langchain_core.exceptions import OutputParserException
    from pydantic import ValidationError

    from app.agent.nodes.fast_rewrite import (
        RewriteOutput,
        _clamp_queries,
        _dedupe_queries,
        _load_full_rewrite_template,
        build_light_model,
    )
    from app.agent.prompts import render_prompt
    from app.services.turn_service import detect_query_lang

    spec = runtime["spec"]
    fast_section = spec.get("fast") if isinstance(spec.get("fast"), dict) else {}
    llm_timeout = float(fast_section.get("llm_timeout_s") or _REWRITE_TIMEOUT_FALLBACK_S)
    query_lang = detect_query_lang(question)
    prompt_text = render_prompt(
        _load_full_rewrite_template(),
        {
            "type_rules": _resolve_type_rules(runtime),
            "query_lang": query_lang,
            "memory_hints": "",
            "history": "",
            "query": question,
        },
    )
    started = time.perf_counter()
    try:
        chain = build_light_model(runtime["mapping"]).with_structured_output(
            RewriteOutput, method="function_calling"
        )

        async def _invoke() -> Any:
            async with asyncio.timeout(llm_timeout):
                return await chain.ainvoke(prompt_text, config=None)

        raw = await _invoke()
        parsed = raw if isinstance(raw, RewriteOutput) else RewriteOutput.model_validate(raw)
        if not _dedupe_queries(parsed.queries):
            raise OutputParserException("queries 全为空白")
        queries = _dedupe_queries(
            _clamp_queries(parsed.queries, spec, parsed.query_type)
        )
        return {
            "ok": True,
            "degraded": False,
            "query_type": parsed.query_type,
            "search_strategy": parsed.search_strategy,
            "queries": queries,
            "ms": round((time.perf_counter() - started) * 1000),
        }
    except (Exception, ValidationError, OutputParserException) as exc:  # noqa: BLE001
        return {
            "ok": False,
            "degraded": True,
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            "query_type": "other",
            "queries": [question],
            "ms": round((time.perf_counter() - started) * 1000),
        }


# ---------------------------------------------------------------------------
# 门段：单子问题 双通道 → RRF → rerank → Top K（plan §4.1 第三级管道）
# ---------------------------------------------------------------------------
async def _gate_one(
    subq: str,
    query_type: str,
    *,
    user_id: str,
    retriever: Any,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    from app.agent.nodes.fuse_rerank import (
        _rerank_order,
        rerank_via_gateway,
    )
    from app.agent.nodes.kb_search import _candidate_from_row
    from app.agent.nodes.merge_dedupe import rrf_fuse_kb
    from app.agent.safety.config import load_content_guard_config
    from app.agent.safety.content_guard import filter_candidates
    from app.config_center.defaults import MODEL_MAPPING_CONFIG
    from app.rag.retrieval import kb_channels_enabled

    spec = runtime["spec"]
    mapping = runtime["mapping"]
    kb = spec.get("kb") if isinstance(spec.get("kb"), dict) else {}
    es_on, vec_on = kb_channels_enabled(spec)
    timeout_s = float(kb.get("dual_channel_timeout_s") or 3)
    per_query_top_k = int(kb.get("per_query_top_k") or 30)
    rrf_k = int(kb.get("rrf_k") or 60)
    kb_fuse_top_k = int(kb.get("kb_fuse_top_k") or 20)
    weights_raw = (kb.get("type_weights") or {}).get(query_type) or [0.5, 0.5]
    weights = (float(weights_raw[0]), float(weights_raw[1]))
    rerank_timeout = float(kb.get("rerank_timeout_s") or 2)
    alias = str(mapping.get("rerank") or MODEL_MAPPING_CONFIG["rerank"])

    bm_rows: list[dict[str, Any]] = []
    vec_rows: list[dict[str, Any]] = []
    channel_errors: list[str] = []

    async def _run_bm25() -> list[dict[str, Any]]:
        async with asyncio.timeout(timeout_s):
            return await retriever.bm25_search(subq, user_id=user_id, k=per_query_top_k)

    async def _run_vector() -> list[dict[str, Any]]:
        async with asyncio.timeout(timeout_s):
            return await retriever.vector_search(subq, user_id=user_id, k=per_query_top_k)

    async def _retrieve() -> None:
        nonlocal bm_rows, vec_rows
        legs: list[tuple[str, Any]] = []
        if es_on:
            legs.append(("bm25", _run_bm25()))
        if vec_on:
            legs.append(("vector", _run_vector()))
        outcomes = await asyncio.gather(*(leg for _, leg in legs), return_exceptions=True)
        for (channel, _), outcome in zip(legs, outcomes):
            if isinstance(outcome, BaseException):
                channel_errors.append(
                    f"kb_{'es' if channel == 'bm25' else 'vector'}_unavailable"
                    f":{type(outcome).__name__}"
                )
            elif channel == "bm25":
                bm_rows = outcome
            else:
                vec_rows = outcome

    t0 = time.perf_counter()
    await _retrieve()
    retrieve_ms = round((time.perf_counter() - t0) * 1000)

    candidates = [
        _candidate_from_row(row, query_index=0, rank=rank)
        for rows in (bm_rows, vec_rows)
        for rank, row in enumerate(rows, start=1)
    ]
    guards = load_content_guard_config(spec)
    candidates, _filter_errors = filter_candidates(candidates, guards, stage="search")

    fused = rrf_fuse_kb(
        candidates, rrf_k=rrf_k, kb_fuse_top_k=kb_fuse_top_k, weights=weights
    )

    result: dict[str, Any] = {
        "query": subq,
        "query_type": query_type,
        "es_on": es_on,
        "vec_on": vec_on,
        "bm25_hits": len(bm_rows),
        "vec_hits": len(vec_rows),
        "filtered_drops": len(
            [
                _
                for rows in (bm_rows, vec_rows)
                for _ in rows
            ]
        )
        - len(candidates),
        "rrf_count": len(fused),
        "channel_errors": channel_errors,
        "retrieve_ms": retrieve_ms,
        "rerank_ms": 0,
        "top_k_count": 0,
        "top1_rerank_score": None,
        "top1_title": None,
        "top3": [],
        "rerank_failed": False,
    }
    channels_failed = len(channel_errors)
    channels_enabled = int(es_on) + int(vec_on)
    if channels_failed >= channels_enabled:
        result["status"] = "all_channels_failed"
        return result
    result["status"] = "ok"
    if not fused:
        result["zero_reason"] = (
            "both_channels_zero_recall"
            if not candidates
            else "recall_then_filtered_or_rrf_empty"
        )
        return result

    documents = [str(c.get("title") or c.get("url") or "") for c in fused]
    for i, c in enumerate(fused):
        c["_rr_idx"] = i
    t1 = time.perf_counter()
    try:
        scores = await rerank_via_gateway(
            subq, documents, model_alias=alias, timeout_s=rerank_timeout
        )
    except Exception as exc:  # noqa: BLE001 —— rerank 失败 ≠ 判无（plan §4.0 原则 3/4）
        result["status"] = "rerank_failed"
        result["rerank_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        result["rerank_ms"] = round((time.perf_counter() - t1) * 1000)
        return result
    result["rerank_ms"] = round((time.perf_counter() - t1) * 1000)

    ordered = _rerank_order(scores, fused)
    result["top_k_count"] = len(ordered)
    for c in ordered[:3]:
        idx = c.pop("_rr_idx", None)
        score = scores[idx] if idx is not None and idx < len(scores) else None
        result["top3"].append(
            {
                "title": str(c.get("title") or ""),
                "rerank_score": score,
            }
        )
        if idx is not None:
            c["_rr_idx"] = idx  # 还原（对象复用面）
    top1_idx = ordered[0].get("_rr_idx") if ordered else None
    if top1_idx is not None and top1_idx < len(scores):
        result["top1_rerank_score"] = scores[top1_idx]
        result["top1_title"] = str(ordered[0].get("title") or "")
    for c in fused:
        c.pop("_rr_idx", None)
    return result


async def _run_one_question(item: dict[str, Any], runtime: dict[str, Any], sq_cap: int) -> dict[str, Any]:
    from app.rag.retrieval import get_kb_retriever

    retriever = get_kb_retriever()
    rewrite = await _rewrite_subqueries(item["question"], runtime)
    queries = rewrite["queries"][:sq_cap]
    t0 = time.perf_counter()
    # 生产形态：n 个子问题并发（plan §4.1 第三级「asyncio.gather，墙钟 ≈ 单次包络」）
    subresults = list(
        await asyncio.gather(
            *(
                _gate_one(
                    q,
                    rewrite["query_type"],
                    user_id=item["user_id"],
                    retriever=retriever,
                    runtime=runtime,
                )
                for q in queries
            )
        )
    )
    statuses = [s["status"] for s in subresults]
    rerank_calls = sum(1 for s in subresults if s.get("rerank_ms") or s.get("rerank_failed"))
    wall_ms = round((time.perf_counter() - t0) * 1000)  # 门段串行复现墙钟（生产为子问题并发）
    # 判定树（plan §4.1）：任一子问题全部通道失败 / rerank 失败 → 不判无回落 hybrid；
    # 每子问题至少一路成功且 Top K 全空 → no_hit；任一 Top K 有值 → has_hit
    if any(s in ("all_channels_failed", "rerank_failed") for s in statuses):
        verdict = "fallback_hybrid"
    elif any(s["top_k_count"] > 0 for s in subresults):
        verdict = "has_hit"
    else:
        verdict = "no_hit"
    return {
        "turn_id": item["turn_id"],
        "thread_id": item["thread_id"],
        "question": item["question"],
        "created_at": item["created_at"],
        "verdict": verdict,
        "rewrite": {
            "ok": rewrite["ok"],
            "degraded": rewrite["degraded"],
            "query_type": rewrite["query_type"],
            "search_strategy": rewrite.get("search_strategy"),
            "n_queries": len(rewrite["queries"]),
            "n_run": len(subresults),
            "error": rewrite.get("error"),
            "ms": rewrite["ms"],
        },
        "subqueries": subresults,
        "gate_wall_ms": wall_ms,
        "rerank_calls": rerank_calls,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _cmd_run(limit: int, skip: int, sq_cap: int) -> int:
    runtime = _load_runtime()
    questions = _load_questions()[skip : skip + limit]
    done_ids: set[str] = set()
    if RESULTS_PATH.is_file():
        for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                done_ids.add(json.loads(line).get("turn_id", ""))
            except json.JSONDecodeError:
                continue
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    uid = questions[0]["user_id"] if questions else ""
    todo = [q for q in questions if q["turn_id"] not in done_ids]
    print(f"[run] 本批 {len(todo)} 题（skip={skip} limit={limit} sq_cap={sq_cap}；已完成 {len(done_ids)} 题跳过）")
    rerank_budget = 0

    async def _run_all() -> None:
        nonlocal rerank_budget
        # 语料探针一次（plan §4.1 第二级——语料空 → 全部 web 直达；记录供报告核对）。
        # 必须与门段同一事件循环：ES 客户端单例跨 loop 复用会「Future attached to
        # a different loop」——探针进程内全量 IO 收口单循环（生产=服务进程单长循环同构）
        if uid:
            from app.rag.retrieval import get_kb_retriever

            try:
                async with asyncio.timeout(1.0):
                    total = await get_kb_retriever().corpus_chunk_count(prefer_es=True, user_id=uid)
            except Exception:  # noqa: BLE001 —— 探针超时按非空处理（plan 同语义）
                total = None
            print(f"[run] 语料探针 corpus_chunk_count={total}（None=超时按非空处理）")
        for i, item in enumerate(todo):
            started = time.perf_counter()
            try:
                record = await _run_one_question(item, runtime, sq_cap)
            except Exception as exc:  # noqa: BLE001 —— 门内代码异常留痕（plan R38：异常≠判无）
                record = {
                    "turn_id": item["turn_id"],
                    "question": item["question"],
                    "verdict": "probe_error",
                    "probe_error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            rerank_budget += int(record.get("rerank_calls") or 0)
            with RESULTS_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            sub = record.get("subqueries") or []
            print(
                f"[run] {i + 1}/{len(todo)} verdict={record.get('verdict')}"
                f" subs={len(sub)}"
                + " ".join(
                    f"[bm{s['bm25_hits']}/vec{s['vec_hits']}/top{s['top_k_count']}/top1={s['top1_rerank_score']}]"
                    for s in sub
                    if isinstance(s, dict) and "bm25_hits" in s
                )
                + f" {time.perf_counter() - started:.1f}s",
                flush=True,
            )

    asyncio.run(_run_all())
    print(f"[run] 本批 rerank 调用 {rerank_budget} 次（累计口径见 aggregate）")
    return 0


# ---------------------------------------------------------------------------
# rerank 空集行为专项（plan 文末未核实清单 #3）
# ---------------------------------------------------------------------------
def _cmd_rerank_empty() -> int:
    from app.agent.nodes.fuse_rerank import rerank_via_gateway

    runtime = _load_runtime()
    mapping = runtime["mapping"]
    kb = runtime["spec"].get("kb") or {}
    alias = str(mapping.get("rerank") or "qwen3-rerank")
    timeout_s = float(kb.get("rerank_timeout_s") or 2)
    uid = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))["user_id"]
    from app.rag.retrieval import get_kb_retriever

    retriever = get_kb_retriever()
    rows = asyncio.run(retriever.bm25_search("python", user_id=uid, k=5))
    documents = [str((r.get("heading_path") or ["知识库内容"])[-1]) for r in rows]
    print(f"[rerank-empty] 真实语料文档 {len(documents)} 条（title 形态；与图级 rerank 输入同构）")
    cases = [
        ("real", "python 异步编程有哪些最佳实践", documents),
        ("garbage_punct", "！！！？？？？", documents),
        ("garbage_ascii", "asdf qwerty zxcv 12345", documents),
        ("meaningless_cjk", "的了的了呀", documents),
        ("real_empty_docs", "python 异步编程", []),
        ("garbage_empty_docs", "！！！？？", []),
    ]
    for name, query, docs in cases:
        started = time.perf_counter()
        try:
            scores = asyncio.run(
                rerank_via_gateway(query, docs, model_alias=alias, timeout_s=timeout_s)
            )
            nonnull = [s for s in scores if s is not None]
            print(
                f"[rerank-empty] {name}: docs={len(docs)} scores={len(scores)}"
                f" nonnull={len(nonnull)}"
                f" max={max(nonnull) if nonnull else None}"
                f" min={min(nonnull) if nonnull else None}"
                f" {time.perf_counter() - started:.2f}s"
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[rerank-empty] {name}: docs={len(docs)} EXC {type(exc).__name__}: {str(exc)[:160]}"
                f" {time.perf_counter() - started:.2f}s"
            )
    return 0


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def _pctl(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, round(q * (len(vs) - 1))))
    return vs[idx]


def _cmd_aggregate(samples_out: str | None) -> int:
    records: list[dict[str, Any]] = []
    for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    # real=dev 库真实轮；control=离题对照组（turn_id 前缀 control-，零命中分支实测面）
    real = [r for r in records if not str(r.get("turn_id", "")).startswith("control-")]
    control = [r for r in records if str(r.get("turn_id", "")).startswith("control-")]
    records = real + control
    valid = [r for r in records if r.get("verdict") != "probe_error"]
    errors = [r for r in records if r.get("verdict") == "probe_error"]
    n = len(valid)
    nohits = [r for r in valid if r["verdict"] == "no_hit"]
    fallbacks = [r for r in valid if r["verdict"] == "fallback_hybrid"]
    subs = [s for r in valid for s in r.get("subqueries", []) if isinstance(s, dict)]
    real_n = len([r for r in valid if not str(r.get("turn_id", "")).startswith("control-")])
    real_hits = len([
        r for r in valid
        if r["verdict"] == "has_hit" and not str(r.get("turn_id", "")).startswith("control-")
    ])
    real_nohits = [
        r for r in valid
        if r["verdict"] == "no_hit" and not str(r.get("turn_id", "")).startswith("control-")
    ]
    gate_ms = [
        s["retrieve_ms"] + s["rerank_ms"]
        for s in subs
        if s.get("status") == "ok"
    ]
    rerank_only = [s["rerank_ms"] for s in subs if s.get("status") == "ok" and s.get("top_k_count", 0) > 0]
    top1 = [
        s["top1_rerank_score"]
        for s in subs
        if s.get("top1_rerank_score") is not None
    ]
    rw_ok = sum(1 for r in valid if r.get("rewrite", {}).get("ok"))
    n_sq = [r.get("rewrite", {}).get("n_run", 0) for r in valid]
    total_rerank_calls = sum(r.get("rerank_calls", 0) for r in records)
    print("=== B22 HITL 门 shadow 探针汇总 ===")
    print(f"题数（有效）: {n}（真实 {real_n} + 对照 {n - real_n}；probe_error {len(errors)} 条不计入）")
    print(f"弹窗率 popupRate（真实题）: {real_hits}/{real_n} = {real_hits / real_n:.3f}" if real_n else "无真实题")
    print(f"零命中率 zeroHitRate（真实题）: {len(real_nohits)}/{real_n} = {len(real_nohits) / real_n:.3f}" if real_n else "")
    ctrl_hits = [r for r in control if r.get("verdict") == "has_hit"]
    ctrl_nohits = [r for r in control if r.get("verdict") == "no_hit"]
    print(f"对照组: {len(control)} 条 → has_hit {len(ctrl_hits)} / no_hit {len(ctrl_nohits)}"
          f" / 其他 {len(control) - len(ctrl_hits) - len(ctrl_nohits)}")
    print(f"fallback_hybrid（不判无回落）: {len(fallbacks)}")
    print(f"rewrite 成功率: {rw_ok}/{n}（降级轮以原问题单子问题进门）")
    print(f"子问题数/题: min={min(n_sq) if n_sq else 0} max={max(n_sq) if n_sq else 0}"
          f" avg={sum(n_sq) / len(n_sq):.2f}" if n_sq else "")
    print(f"rerank 累计调用: {total_rerank_calls} 次")
    print(f"门段墙钟/子问题 ms（检索+rerank, ok 态 n={len(gate_ms)}）:"
          f" P50={_pctl(gate_ms, 0.5):.0f} P95={_pctl(gate_ms, 0.95):.0f}"
          f" min={min(gate_ms):.0f} max={max(gate_ms):.0f}" if gate_ms else "无门段时延")
    print(f"rerank 段 ms（有命中子问题 n={len(rerank_only)}）:"
          f" P50={_pctl(rerank_only, 0.5):.0f} P95={_pctl(rerank_only, 0.95):.0f}" if rerank_only else "")
    if top1:
        buckets = {"<0.05": 0, "0.05-0.2": 0, "0.2-0.5": 0, "0.5-0.8": 0, ">=0.8": 0}
        for v in top1:
            if v < 0.05:
                buckets["<0.05"] += 1
            elif v < 0.2:
                buckets["0.05-0.2"] += 1
            elif v < 0.5:
                buckets["0.2-0.5"] += 1
            elif v < 0.8:
                buckets["0.5-0.8"] += 1
            else:
                buckets[">=0.8"] += 1
        print(f"top1 rerank 分分布（n={len(top1)}）: min={min(top1):.4f}"
              f" P25={_pctl(top1, 0.25):.4f} P50={_pctl(top1, 0.5):.4f}"
              f" P75={_pctl(top1, 0.75):.4f} P95={_pctl(top1, 0.95):.4f} max={max(top1):.4f}")
        print(f"  分桶: {buckets}")
    # 通道级
    bm_any = sum(1 for s in subs if s.get("bm25_hits", 0) > 0)
    vec_any = sum(1 for s in subs if s.get("vec_hits", 0) > 0)
    ch_err = sum(1 for s in subs if s.get("channel_errors"))
    print(f"子问题通道面: bm25 有召回 {bm_any}/{len(subs)} · vector 有召回 {vec_any}/{len(subs)}"
          f" · 通道异常 {ch_err}/{len(subs)}")
    if samples_out:
        lines = [
            "# B22 零命中轮人工抽样清单（shadow 探针实测）",
            "",
            f"- 生成：{time.strftime('%Y-%m-%dT%H:%M:%S')} · 数据源 `.zcode/tmp/hitl_shadow_results.jsonl`",
            "- 用途：plan hitl-OQ-16——零命中轮人工抽样验误判率（门槛 ≤5%）。"
            "逐条给出问题 / 逐通道召回数 / 判无理由，供人工核对「KB 真没有 vs 召回失败」。",
            "",
            "| # | 来源 | 问题 | bm25 召回 | vector 召回 | RRF 后 | 判无理由 |",
            "|---|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(nohits, 1):
            src = "control" if str(r.get("turn_id", "")).startswith("control-") else "real"
            for j, s in enumerate(r.get("subqueries", [])):
                reason = s.get("zero_reason", "-")
                lines.append(
                    f"| {i}.{j + 1} | {src} | {s.get('query', r['question'])[:80]} | {s.get('bm25_hits')} |"
                    f" {s.get('vec_hits')} | {s.get('rrf_count')} | {reason} |"
                )
            if not r.get("subqueries"):
                lines.append(f"| {i} | {src} | {r['question'][:80]} | - | - | - | rewrite 降级缺失 |")
        Path(samples_out).parent.mkdir(parents=True, exist_ok=True)
        Path(samples_out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[aggregate] 零命中抽样清单 → {samples_out}（{len(nohits)} 轮）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B22 HITL 审批门 shadow 探针（只读）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pick = sub.add_parser("pick", help="冻结题清单（dev 库 completed fast 轮去重）")
    p_pick.add_argument("--limit", type=int, default=60)
    p_pick.add_argument("--user-id", dest="user_id", default="")
    p_run = sub.add_parser("run", help="跑一批（≤20 题；jsonl 续跑）")
    p_run.add_argument("--limit", type=int, default=20)
    p_run.add_argument("--skip", type=int, default=0)
    p_run.add_argument("--sq-cap", type=int, default=DEFAULT_SQ_CAP)
    sub.add_parser("rerank-empty", help="rerank 空集行为专项（plan 清单 #3）")
    p_agg = sub.add_parser("aggregate", help="汇总指标")
    p_agg.add_argument("--samples", default="")
    args = parser.parse_args(argv)
    if args.cmd == "pick":
        if args.user_id:
            os.environ["HITL_PROBE_USER_ID"] = args.user_id
        _pick_questions(args.limit)
        return 0
    if args.cmd == "run":
        return _cmd_run(args.limit, args.skip, args.sq_cap)
    if args.cmd == "rerank-empty":
        return _cmd_rerank_empty()
    if args.cmd == "aggregate":
        return _cmd_aggregate(args.samples or None)
    return 2


if __name__ == "__main__":
    sys.exit(main())
