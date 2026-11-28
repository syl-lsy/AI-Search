"""B22 shadow 记录面标定与统计（只读；批次 5 收口·OQ-17 标定前置）。

用法（仓库根）：
  uv run --package search-api python scripts/analyze_hitl_shadow.py                  # db 运行态面
  双源支持：--source probe-jsonl --probe-path <path>（批次 0 探针面，非运行态）。

- db 源=turn_approvals 运行态 shadow/no_hit 审计行（生产标定面；hitl.enabled 翻键后
  积累，数据积累后重跑同脚本即得正式判定输入）；
- probe-jsonl 源=批次 0 探针档（探针自持面——非运行态，标定结论只作初判参考并须
  显式标注）。

统计面：弹窗率（verdict=has_hit 占比）/ rerank top1 腿分布分位数 / 向量腿分布
（hits_summary[].top1_vector_sim——批次 5 清点确认该字段运行态未落行，可用即出数、
不可用如实报 0 样本）/ 判无轮清单与抽样证据包（≤--sample-limit 条；db 源附双通道
库面复查：chunk 总量/零向量计数/关键词 ILIKE 命中）。

SQL 纪律：全参数绑定（%s + jsonb_exists 函数式），零拼接零 f-string SQL。
凭据零打印。零写库（只 SELECT；可选 --json-out 落盘到参数指定路径）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _percentile(sorted_vals: list, q: float):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _dist_summary(vals: list) -> dict:
    vs = sorted(vals)
    return {
        "n": len(vs),
        "min": vs[0] if vs else None,
        "p5": _percentile(vs, 0.05),
        "p25": _percentile(vs, 0.25),
        "p50": _percentile(vs, 0.50),
        "p75": _percentile(vs, 0.75),
        "p90": _percentile(vs, 0.90),
        "p95": _percentile(vs, 0.95),
        "max": vs[-1] if vs else None,
    }


def _fmt(v):
    return "-" if v is None else f"{v:.4f}"


def _connect(env_path: Path):
    import psycopg2

    env = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    conn = psycopg2.connect(
        host=env.get("DB_HOST", "127.0.0.1"),
        port=int(env.get("DB_PORT", "15432")),
        user=env.get("DB_USER", ""),
        password=env.get("DB_PASSWORD", ""),
        database=env.get("DB_NAME", ""),
        connect_timeout=5,
    )
    return conn, env


def analyze_db(env_path: Path, sample_limit: int) -> dict:
    conn, env = _connect(env_path)
    result = {"source": "db", "database": env.get("DB_NAME", "")}
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, verdict, count(*) FROM turn_approvals WHERE status IN (%s, %s) GROUP BY status, verdict ORDER BY status, verdict",
                ("shadow", "no_hit"),
            )
            by_status_verdict = [
                {"status": r[0], "verdict": r[1], "n": r[2]} for r in cursor.fetchall()
            ]
            total = sum(item["n"] for item in by_status_verdict)
            has_hit = sum(item["n"] for item in by_status_verdict if item["verdict"] == "has_hit")
            result["totals"] = {
                "by_status_verdict": by_status_verdict,
                "gated_rounds": total,
                "popup_rate": (has_hit / total) if total else None,
            }

            cursor.execute(
                "SELECT s.sub->>'top1_score' FROM turn_approvals ta, jsonb_array_elements(ta.hits_summary) AS s(sub) WHERE ta.status IN (%s, %s) AND jsonb_typeof(ta.hits_summary) = 'array' AND jsonb_exists(s.sub, 'top1_score') AND s.sub->>'top1_score' IS NOT NULL",
                ("shadow", "no_hit"),
            )
            result["rerank_top1_dist"] = _dist_summary([float(r[0]) for r in cursor.fetchall()])

            cursor.execute(
                "SELECT s.sub->>'top1_vector_sim' FROM turn_approvals ta, jsonb_array_elements(ta.hits_summary) AS s(sub) WHERE ta.status IN (%s, %s) AND jsonb_typeof(ta.hits_summary) = 'array' AND jsonb_exists(s.sub, 'top1_vector_sim') AND s.sub->>'top1_vector_sim' IS NOT NULL",
                ("shadow", "no_hit"),
            )
            result["vector_top1_dist"] = _dist_summary([float(r[0]) for r in cursor.fetchall()])

            cursor.execute(
                "SELECT ta.turn_id, t.query, ta.verdict, ta.requested_at, ta.hits_summary FROM turn_approvals ta JOIN turns t ON t.id = ta.turn_id WHERE ta.status = %s ORDER BY ta.requested_at DESC LIMIT %s",
                ("no_hit", sample_limit),
            )
            no_hit_rows = [
                {
                    "turn_id": str(r[0]),
                    "query": r[1],
                    "verdict": r[2],
                    "requested_at": str(r[3]),
                    "hits_summary": r[4],
                }
                for r in cursor.fetchall()
            ]
            result["no_hit_rounds"] = no_hit_rows

            rechecks = []
            for row in no_hit_rows[:sample_limit]:
                query = row["query"] or ""
                keywords = [kw for kw in query.replace("？", " ").split() if len(kw) >= 2][:4]
                cursor.execute("SELECT count(*) FROM kb_chunks")
                count_row = cursor.fetchone()
                chunk_total = int(count_row[0]) if count_row is not None else 0
                cursor.execute("SELECT count(*) FROM kb_chunks WHERE embedding IS NOT NULL")
                count_row = cursor.fetchone()
                embedded = int(count_row[0]) if count_row is not None else 0
                kw_hits = []
                for kw in keywords:
                    cursor.execute(
                        "SELECT count(*) FROM kb_chunks WHERE content ILIKE %s",
                        (f"%{kw}%",),
                    )
                    count_row = cursor.fetchone()
                    kw_hits.append(
                        {
                            "keyword": kw,
                            "chunk_hits": int(count_row[0]) if count_row is not None else 0,
                        }
                    )
                rechecks.append(
                    {
                        "turn_id": row["turn_id"],
                        "query": query,
                        "corpus_chunks": chunk_total,
                        "embedded_chunks": embedded,
                        "keyword_recheck": kw_hits,
                    }
                )
            result["no_hit_recheck"] = rechecks
    finally:
        conn.close()
    return result


def analyze_probe_jsonl(path: Path) -> dict:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    total = len(records)
    has_hit = sum(1 for r in records if r.get("verdict") == "has_hit")
    rerank_vals = []
    vector_vals = []
    query_types = {}
    for r in records:
        for sub in r.get("subqueries") or []:
            score = sub.get("top1_rerank_score")
            if isinstance(score, (int, float)):
                rerank_vals.append(float(score))
            vsim = sub.get("top1_vector_sim")
            if isinstance(vsim, (int, float)):
                vector_vals.append(float(vsim))
            qt = sub.get("query_type")
            if qt:
                query_types[qt] = query_types.get(qt, 0) + 1
    no_hit = [
        {"query": r.get("question", ""), "verdict": r.get("verdict", "")}
        for r in records
        if r.get("verdict") == "no_hit"
    ][:8]
    return {
        "source": "probe-jsonl",
        "path": str(path),
        "totals": {
            "gated_rounds": total,
            "has_hit_rounds": has_hit,
            "popup_rate": (has_hit / total) if total else None,
            "subquery_query_types": query_types,
        },
        "rerank_top1_dist": _dist_summary(rerank_vals),
        "vector_top1_dist": _dist_summary(vector_vals),
        "no_hit_rounds": no_hit,
        "no_hit_recheck": [],
    }


def suggest_floor(dist: dict) -> dict:
    """噪声地板 T 建议值（初判）：地板语义=子问题 top1<T 视同该子问题为空（题级
    任一子问题 ≥T 即弹窗）——T 取分布下缘噪声带与真实带之间 0.05 网格化。样本不足
    时返回 none 不硬凑。正式判定须按题型分组分布复核（OQ-17 mandate）。"""
    if not dist or not dist.get("n"):
        return {"T_suggested": None, "note": "分布样本不足——不硬凑"}
    p25, p50 = dist.get("p25"), dist.get("p50")
    if p25 is None or p50 is None:
        return {"T_suggested": None, "note": "分位数不可得——不硬凑"}
    t = max(0.05, round(min(p25, p50) * 20) / 20)
    return {
        "T_suggested": t,
        "note": (
            f"初判 T≈{t:.2f}（P25 下缘 0.05 网格化；与 kb.route.min_top1_score 量纲不同"
            "不复用 0.012——OQ-17）。样本为初判面，正式判定待运行态分布积累后重跑本脚本"
            "按题型分组复核。"
        ),
    }


def render(result: dict) -> str:
    lines = []
    totals = result.get("totals", {})
    lines.append(f"source={result['source']} gated_rounds={totals.get('gated_rounds')}")
    rate = totals.get("popup_rate")
    lines.append("popup_rate(has_hit 占比)=" + ("-" if rate is None else f"{rate:.3f}"))
    rd = result.get("rerank_top1_dist", {})
    lines.append(
        "rerank_top1: n={n} min={a} p5={b} p25={c} p50={d} p75={e} p90={f} p95={g} max={h}".format(
            n=rd.get("n", 0),
            a=_fmt(rd.get("min")), b=_fmt(rd.get("p5")), c=_fmt(rd.get("p25")),
            d=_fmt(rd.get("p50")), e=_fmt(rd.get("p75")), f=_fmt(rd.get("p90")),
            g=_fmt(rd.get("p95")), h=_fmt(rd.get("max")),
        )
    )
    vd = result.get("vector_top1_dist", {})
    lines.append(
        "vector_top1: n={n} p50={a} p90={b}（不可用=运行态未落行，见批次 5 清点）".format(
            n=vd.get("n", 0), a=_fmt(vd.get("p50")), b=_fmt(vd.get("p90"))
        )
    )
    no_hit = result.get("no_hit_rounds") or []
    lines.append(f"no_hit_rounds_listed={len(no_hit)}")
    for row in no_hit:
        lines.append(f"  - query={row['query'][:60]} verdict={row['verdict']}")
    suggestion = suggest_floor(rd)
    lines.append(f"floor_suggestion: T={suggestion['T_suggested']} note={suggestion['note']}")
    label = "非运行态（批次 0 探针面）" if result["source"] == "probe-jsonl" else "运行态"
    lines.append(
        f"样本量口径：初判样本量 {totals.get('gated_rounds')} 条（{label}）——正式判定待运行态积累"
        "（hitl.enabled 翻键后重跑本脚本即得正式判定输入）"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="B22 shadow 标定与统计（只读）")
    parser.add_argument("--source", choices=("db", "probe-jsonl"), default="db")
    parser.add_argument("--probe-path", default=".zcode/tmp/hitl_shadow_results.jsonl")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--sample-limit", type=int, default=8)
    parser.add_argument("--json-out", default=None, help="机器可读结果落盘路径（可选）")
    args = parser.parse_args()

    if args.source == "db":
        result = analyze_db(REPO / args.env, args.sample_limit)
    else:
        result = analyze_probe_jsonl(REPO / args.probe_path)

    print(render(result))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        print(f"json_out={args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
