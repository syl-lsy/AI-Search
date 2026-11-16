"""OQ-34 探针：deep 子代理模型档位对比——light(qwen3.5-4b-ollama) vs Standard(chat-standard)。

规模硬上限：1 题 × 2 档 × ≤3 轮 = ≤6 次子代理运行（超出即停，本脚本结构上锁死）。
凭证通道：.env.test 的 LITELLM_BASE_URL/LITELLM_API_KEY 进 os.environ（脚本进程内，
不打印值）；先 bootstrap 再惰性 import app.* 工厂（light=fast_rewrite.build_light_model，
standard=llm.build_chat_model("chat-standard")——批次 2 research 将消费的生产形态）。

同题同工具序控变量：
- 同一子课题任务文本（deep.subagent.system 骨架 + 显式 JSON 回传 schema 附录渲染）；
- 3 工具 mock（web_search/firecrawl_read/save_snapshot）确定性 fixture：同参同果、
  两档看到完全相同语料；<external_content> 包裹同生产形态；
- 结构化回传合法率 = 末条 AIMessage 文本经 SummarySchema（ARCH §4.2 :484-486 形态
  {subtopic, summary, hard_facts[], quotes[], sources[], failed}）校验；
  口径注：本探针测「无机制辅助的模型能力地板」（prompt-json），与 response_format
  机制辅助路径（deepagents SubAgent response_format→AutoStrategy）不同口径，
  装配落地后可再测机制增量。

运行：
  uv run --package search-api python scripts/probe_oq34_model_tier.py            # 真 LLM 6 轮
  uv run --package search-api python scripts/probe_oq34_model_tier.py --selftest # 离线桩自检
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_TEST = REPO_ROOT / ".env.test"
SUBAGENT_PROMPT_TXT = REPO_ROOT / "apps/search-api/app/agent/prompts/deep/subagent_system.txt"

CONTRACT_TOOL_NAMES = {"web_search", "firecrawl_read", "save_snapshot"}
READ_ONLY_FS_TOOLS = ["ls", "read_file", "glob", "grep"]
ROUNDS_CAP = 3  # 规模硬上限：每档 ≤3 轮
QUESTION_SUBTOPIC = "Milvus 与 Qdrant 在部署形态、性能与运维成本上的差异及各自适用场景"
SUMMARY_MAX_TOKENS = 1200  # ARCH §4.2 摘要预算

SCHEMA_APPENDIX = """

【回传格式（最后一条消息必须是如下 JSON，不加其他文字）】
{"subtopic": "<子课题原文>", "summary": "<要点+硬事实+引文摘录，每条可对应到来源>",
 "hard_facts": ["<数字/日期/结论级硬事实>"], "quotes": ["<原文引文>"],
 "sources": [{"url": "<你实际读过的URL>", "note": "<该来源支撑了什么>"}],
 "failed": false}
"""

# ---- 确定性 fixture 语料（两档完全同源；数字为探针 fixture，不作事实依据） ----

_SEARCH_HITS = [
    {"title": "Milvus 官网：部署形态", "url": "https://milvus.co/deploy",
     "snippet": "Milvus 支持 Lite（单机嵌入式）、Standalone（Docker 单机）、Distributed（K8s 集群）三种部署形态。"},
    {"title": "Qdrant 官网：部署形态", "url": "https://qdrant.tech/deploy",
     "snippet": "Qdrant 以单二进制镜像提供 Docker/K8s 部署，主打轻量横向扩展与 Rust 运行时。"},
    {"title": "向量库基准对比 2026", "url": "https://example.com/vector-bench-2026",
     "snippet": "ANN 基准：百万级 768 维向量下 Qdrant P99 延迟 11ms，Milvus Distributed P99 14ms；单机 QPS Milvus 2100 / Qdrant 2600。"},
    {"title": "选型指南：运维成本", "url": "https://example.com/vector-ops-guide",
     "snippet": "Milvus Distributed 依赖 etcd/Pulsar/MinIO 等 6 组件，K8s 运维复杂度高；Qdrant 单进程+可选副本，运维面窄。"},
]

_PAGES: dict[str, str] = {
    "https://milvus.co/deploy": (
        "# Milvus 部署形态\n"
        "Milvus 提供三种部署形态：Milvus Lite（pip 安装、嵌入式、适合原型，≤百万向量）；"
        "Milvus Standalone（单机 Docker，依赖内置 etcd+MinIO）；Milvus Distributed（K8s，"
        "组件含 etcd/Pulsar/MinIO/Query/Data/Index 节点等 6 类组件，2.4 起支持 SPIFFE 认证）。\n"
        "适用场景：Lite 适合笔记本原型；Standalone 适合中小规模生产（<1000 万向量）；"
        "Distributed 适合亿级向量、多租户 SaaS、需弹性扩缩的搜索推荐场景。\n"
        "许可：Apache-2.0。GitHub 星标 2026-06 快照约 34.2k。"
    ),
    "https://qdrant.tech/deploy": (
        "# Qdrant 部署形态\n"
        "Qdrant 以 Rust 单二进制交付：Docker 单容器即生产可用；K8s 经 Helm/Operator，"
        "分片+副本配置声明式。无外部依赖组件（存储内置，快照可推 S3）。\n"
        "适用场景：边缘/本地 RAG（内存占用低，4GB 可跑百万级 768 维）；中小团队自托管；"
        "需要混合检索（稠密+稀疏 named vectors）的应用层搜索。\n"
        "许可：Apache-2.0。GitHub 星标 2026-06 快照约 25.8k。"
    ),
    "https://example.com/vector-bench-2026": (
        "# 向量库基准 2026（探针 fixture 数据）\n"
        "数据集：LAION 768 维 100 万向量，HNSW 参数 m=16/ef=128，单副本。\n"
        "召回@10=0.95 时：Qdrant P99=11ms QPS 2600；Milvus Standalone P99=13ms QPS 1900；"
        "Milvus Distributed（3 节点）P99=14ms QPS 2100（横向扩展后 8000+）。\n"
        "亿级（1 亿 768 维）：Milvus Distributed 稳定服务（Query 节点分片），Qdrant 需 32GB+"
        "内存单分片或分集群。过滤检索（标量+向量混合）：Qdrant payload 索引 P99 增幅 8%，"
        "Milvus 标量过滤增幅 15%。"
    ),
    "https://example.com/vector-ops-guide": (
        "# 运维成本选型指南（探针 fixture 数据）\n"
        "Milvus Distributed：K8s 最小生产拓扑 10+ pod（6 类组件），需要 etcd 运维经验与 "
        "Pulsar 消息队列调优；版本升级组件兼容矩阵检查；监控项 30+。\n"
        "Qdrant：单 StatefulSet 起步，副本数横向伸缩；内存=向量数×维度×4B×1.5（HNSW 图）；"
        "版本升级滚动重启即可；监控项 8 个核心指标。\n"
        "结论倾向：亿级+多团队共享集群选 Milvus；中小规模、运维人力有限、边缘部署选 Qdrant。"
    ),
}

_UNKNOWN_PAGE = "# 未收录页面\n（探针 fixture：该 URL 不在语料内，仅返回占位正文。）"


# ---- harness（可测核心） ----


def make_mock_tools() -> tuple[list[Any], list[tuple[str, dict]]]:
    """3 契约工具 mock：确定性 fixture + 调用序记录。返回 (tools, call_log)。"""
    from langchain_core.tools import tool

    log: list[tuple[str, dict]] = []

    @tool
    def web_search(query: str, time_range: str | None = None) -> str:  # type: ignore[empty-body]
        """Mock 搜索：返回确定性候选来源列表（JSON 文本）。"""
        log.append(("web_search", {"query": query, "time_range": time_range}))
        return json.dumps(_SEARCH_HITS, ensure_ascii=False)

    @tool
    def firecrawl_read(url: str, offset: int = 0, limit: int | None = None) -> str:
        """Mock 读取：按 URL 返回确定性页面正文，<external_content> 包裹（生产同形）。"""
        log.append(("firecrawl_read", {"url": url, "offset": offset, "limit": limit}))
        body = _PAGES.get(url, _UNKNOWN_PAGE)
        return f"<external_content>\n{body}\n</external_content>"

    @tool
    def save_snapshot(url: str, content: str) -> str:
        """Mock 快照：返回登记结果（JSON 文本，ok+object_key）。"""
        log.append(("save_snapshot", {"url": url}))
        import hashlib

        key = f"snapshots/probe/{hashlib.sha256(url.encode()).hexdigest()[:16]}.md"
        return json.dumps({"ok": True, "object_key": key, "oversize": False}, ensure_ascii=False)

    return [web_search, firecrawl_read, save_snapshot], log


def render_subagent_prompt(subtopic: str, summary_max_tokens: int = SUMMARY_MAX_TOKENS) -> str:
    """deep.subagent.system 骨架（内置 txt，批次 1 PARITY 三源一致）+ 回传 schema 附录。"""
    tpl = SUBAGENT_PROMPT_TXT.read_text(encoding="utf-8")
    body = re.sub(r"\{\{(\w+)\}\}", lambda m: {"subtopic": subtopic, "summary_max_tokens": str(summary_max_tokens)}.get(m.group(1), m.group(0)), tpl)
    if "{{" in body:
        raise ValueError(f"骨架占位符未消解：{body[body.index('{{'):body.index('{{') + 40]!r}")
    return body + SCHEMA_APPENDIX


def validate_summary(text: str) -> dict[str, Any] | None:
    """SummarySchema 校验（ARCH §4.2 形态）；合法返回 dict，非法返回 None。"""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("subtopic"), str) or not payload["subtopic"]:
        return None
    if not isinstance(payload.get("summary"), str) or not payload["summary"]:
        return None
    if not isinstance(payload.get("hard_facts", []), list):
        return None
    if not isinstance(payload.get("quotes", []), list):
        return None
    sources = payload.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(s, dict) or not isinstance(s.get("url"), str) for s in sources):
        return None
    payload.setdefault("failed", False)
    return payload


def build_probe_subagent(model: Any, tag: str) -> Any:
    """批次 2 同形装配：3 契约工具 + 只读文件中间件（OQ-33 契约形态）。"""
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.subagents import create_sub_agent

    tools, _ = make_mock_tools()
    spec = {
        "name": f"probe-researcher-{tag}",
        "description": "probe subagent",
        "system_prompt": render_subagent_prompt(QUESTION_SUBTOPIC),
        "model": model,
        "tools": tools,
        "middleware": [FilesystemMiddleware(backend=StateBackend(), tools=READ_ONLY_FS_TOOLS)],
    }
    return create_sub_agent(spec)


def run_round(runnable: Any, subtopic: str) -> dict[str, Any]:
    """单轮运行 + 指标采集：延迟/工具序/结构化合法/usage/摘要面。"""
    from langchain_core.messages import AIMessage, HumanMessage

    t0 = time.perf_counter()
    error: str | None = None
    result: dict[str, Any] = {}
    try:
        result = runnable.invoke({"messages": [HumanMessage(subtopic)]})
    except Exception as exc:  # noqa: BLE001 —— 探针记录失败不炸矩阵
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
    latency = time.perf_counter() - t0

    msgs = result.get("messages", [])
    tool_calls = [tc["name"] for m in msgs if isinstance(m, AIMessage) for tc in (m.tool_calls or [])]
    usage = {"input": 0, "output": 0, "total": 0}
    for m in msgs:
        um = getattr(m, "usage_metadata", None)
        if um:
            usage["input"] += int(um.get("input_tokens") or 0)
            usage["output"] += int(um.get("output_tokens") or 0)
            usage["total"] += int(um.get("total_tokens") or 0)
    finals = [m for m in msgs if isinstance(m, AIMessage) and (m.text or "").strip()]
    final_text = finals[-1].text if finals else ""
    payload = validate_summary(final_text)
    return {
        "latency_s": round(latency, 2),
        "tool_calls": tool_calls,
        "read_count": tool_calls.count("firecrawl_read"),
        "search_count": tool_calls.count("web_search"),
        "snapshot_count": tool_calls.count("save_snapshot"),
        "structured_valid": payload is not None,
        "payload": payload,
        "summary_chars": len(payload["summary"]) if payload else 0,
        "hard_facts_count": len(payload.get("hard_facts", [])) if payload else 0,
        "sources_count": len(payload.get("sources", [])) if payload else 0,
        "cited_known_urls": len({s["url"] for s in payload.get("sources", []) if s["url"] in _PAGES}) if payload else 0,
        "usage": usage,
        "error": error,
    }


# ---- 环境与执行面 ----


def _bootstrap_env() -> None:
    """凭证通道：.env.test 的 LITELLM_* 进 os.environ（进程内，不打印值）。"""
    if not ENV_TEST.exists():
        print(f"[skip] 缺 {ENV_TEST}", file=sys.stderr)
        raise SystemExit(2)
    from dotenv import dotenv_values

    for key, value in dotenv_values(ENV_TEST).items():
        if key in ("LITELLM_BASE_URL", "LITELLM_API_KEY") and value:
            os.environ[key] = value
    for must in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
        if not os.environ.get(must):
            print(f"[skip] .env.test 缺 {must}", file=sys.stderr)
            raise SystemExit(2)


def _gateway_preflight() -> dict[str, Any]:
    """网关就绪预检：可达 + 两别名在清单。返回 {'ok', 'reason', 'base_url_host'}。"""
    import httpx

    base = os.environ["LITELLM_BASE_URL"].rstrip("/")
    try:
        headers = {"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"}
        resp = httpx.get(f"{base}/v1/models", headers=headers, timeout=10)
        resp.raise_for_status()
        ids = {m.get("id") for m in resp.json().get("data", [])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"网关不可达（{type(exc).__name__}）", "base_url_host": base}
    missing = {"qwen3.5-4b-ollama", "chat-standard"} - ids
    if missing:
        return {"ok": False, "reason": f"别名未装载：{sorted(missing)}", "base_url_host": base}
    return {"ok": True, "reason": "", "base_url_host": base}


def _build_tiers() -> dict[str, Any]:
    """生产工厂形态装配两档（lazy import，bootstrap 之后调用）。"""
    from app.agent.llm import build_chat_model
    from app.agent.nodes.fast_rewrite import build_light_model

    return {
        "light": build_light_model(),
        "standard": build_chat_model("chat-standard"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rounds", type=int, default=ROUNDS_CAP)
    ap.add_argument("--selftest", action="store_true", help="离线桩自检（零网络）")
    args = ap.parse_args()
    rounds = max(1, min(args.rounds, ROUNDS_CAP))  # 结构性锁死上限

    if args.selftest:
        tools, log = make_mock_tools()
        assert {t.name for t in tools} == CONTRACT_TOOL_NAMES
        ok = validate_summary(json.dumps({
            "subtopic": "s", "summary": "要点", "hard_facts": ["1"], "quotes": [],
            "sources": [{"url": "https://milvus.co/deploy", "note": "部署"}], "failed": False,
        }, ensure_ascii=False))
        assert ok is not None and validate_summary("不是 JSON") is None
        print(f"[selftest] tools={sorted({t.name for t in tools})} call_log={len(log)} schema_ok={ok is not None}")
        print("[selftest] PASS（harness 语义离线自检通过）")
        return 0

    _bootstrap_env()
    pre = _gateway_preflight()
    if not pre["ok"]:
        print(f"[skip] {pre['reason']}", file=sys.stderr)
        return 2
    print(f"[preflight] 网关就绪 host={pre['base_url_host']}")

    tiers = _build_tiers()
    report: dict[str, Any] = {
        "question": QUESTION_SUBTOPIC,
        "rounds_per_tier": rounds,
        "scale_cap": f"1题×2档×≤{ROUNDS_CAP}轮（硬上限）",
        "tiers": {},
    }
    for tier_name in ("light", "standard"):
        runnable = build_probe_subagent(tiers[tier_name], tier_name)
        rows = []
        for i in range(rounds):
            metrics = run_round(runnable, QUESTION_SUBTOPIC)
            metrics["round"] = i + 1
            rows.append(metrics)
            print(
                f"[{tier_name}#{i + 1}] latency={metrics['latency_s']}s "
                f"tools={metrics['tool_calls']} valid={metrics['structured_valid']} "
                f"summary_chars={metrics['summary_chars']} facts={metrics['hard_facts_count']} "
                f"sources={metrics['sources_count']}(known:{metrics['cited_known_urls']}) "
                f"usage={metrics['usage']} error={metrics['error']}"
            )
        legal = sum(1 for r in rows if r["structured_valid"])
        complete = sum(1 for r in rows if r["search_count"] >= 1 and r["read_count"] >= 1)
        report["tiers"][tier_name] = {
            "rows": rows,
            "legal_rate": legal / len(rows),
            "complete_rate": complete / len(rows),
            "latency_avg_s": round(sum(r["latency_s"] for r in rows) / len(rows), 2),
            "usage_total_sum": sum(r["usage"]["total"] for r in rows),
        }
    print("==== SUMMARY ====")
    print(json.dumps({
        t: {k: v for k, v in s.items() if k != "rows"} for t, s in report["tiers"].items()
    }, ensure_ascii=False, indent=2))
    Path(REPO_ROOT / "tmp").mkdir(exist_ok=True)
    out = REPO_ROOT / "tmp" / "probe_oq34_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] 全量指标落盘 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
