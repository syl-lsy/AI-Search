#!/usr/bin/env python
"""M4 线上评估器试点 Langfuse 数据集播种（幂等可复跑；app/benchmarks/）。

目的
    在自部署 Langfuse（:3100，server v4.56.0，OTel 原生写通路）上创建数据集
    ``m4-online-eval-pilot`` 并播种 ≥6 条 items，供 M4 线上评估器试点
    （LLM-as-Judge / Rules 通道，docs/research/deepeval-evaluators-feasibility-
    research-20261116.md §十二）做 dataset run 实验。

真实数据三级降级链（真实 fast 成功轮的 query/answer 从哪来）
    1. Langfuse REST ``GET /api/public/v2/observations``（apps/search-api/app/
       crons/trace_cleanup.py 同款 cursor 链翻页）拉带 input/output 的 GENERATION
       观测——本部署实测全量 70,299 行观测 input/output/metadata 全空
       （events_only 写模式，2026-11-17 探针），恒零行 → 自然落到 2；
    2. 项目 Postgres ``turns`` 表（mode='fast' AND status='completed' AND
       answer_md 非空，created_at 倒序）——真实成功轮的权威落库（app/db/models/
       turn.py §1.3），同数据、绕开观测侧 content 缺失；
    3. 两级都异常/零行 → 全合成（6 条 synthetic），summary ``real_source``
       置 ``none``，由调用方 notes 说明。

字段映射（Langfuse DatasetItem 只有 input / expected_output / metadata 三槽）
    - input = 轮次 query（纯字符串，dataset 表直读）；
    - expected_output 一律留空（按工单口径：试点期判分走线上评估器，不依赖
      参考答案）；真实轮的 answer 保存于 ``metadata.answer`` 供评估器试点读取；
    - metadata = {"kind": "real"|"synthetic", "note": ..., ...溯源键}。
    - 真实 item 附 ``source_trace_id``（turns.langfuse_trace_id）做溯源跳转。

幂等
    - dataset 按名复用：先 get_dataset(name)，ApiError 404 才 create；
    - item 按 input 文本查重：回读现有 items 建 input 键集合，已存在即跳过；
    - 复跑只会对缺失项补种，不重建 dataset、不重复 item。

自校验（收尾硬门）
    回读 dataset items 计数：< 6 → 打印错误并 exit 1；≥ 6 → 打印 count，exit 0。
    全程无密钥字面量：凭据只从环境变量读（LANGFUSE_*、DB_*），缺失时回落仓库根
    .env（scripts/nacos_init.py 同款宽容解析），任何值都不打印、不写入产物。

运行（仓库根）
    uv run --package search-api python -m app.benchmarks.seed_online_eval_dataset
    （-m 形态：app 包根在 apps/search-api 下；依赖 langfuse==4.18.0 / asyncpg 来自
    search-api 环境；脚本内置 .env 回退，无需手工 source。exit 0=自校验通过，
    1=播种或自校验失败。）
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg
from langfuse import Langfuse
from langfuse.api.core.api_error import ApiError

# 环境无关的纯逻辑值收敛为常量（配置三分法 → 代码常量）：
DATASET_NAME = "m4-online-eval-pilot"
MIN_ITEM_COUNT = 6  # 自校验硬门：回读 count < 6 即失败
REAL_ITEM_COUNT = 3  # 真实轮条数（三级降级链产出上限）
SYNTHETIC_ITEM_COUNT = 3  # 合成条数（多面 / 英文语言跟随 / recall 重问）
DB_REAL_TURN_LIMIT = 10  # DB 候选池取最近 N 条，去重后截取 REAL_ITEM_COUNT 条
LF_SCAN_PAGE_SIZE = 1000  # v2 observations 单页 limit（trace_cleanup 同款）
LF_SCAN_MAX_PAGES = 20  # 只扫最近 ~2 万行：真实数据探测面，非全量治理任务
REST_TIMEOUT_S = 15.0
DB_CONNECT_TIMEOUT_S = 8

_DATASET_DESCRIPTION = (
    "M4 线上评估器试点数据集：3 条真实 fast 成功轮 + 3 条合成题"
    "（多面/英文语言跟随/recall 重问）；expected_output 试点期留空，"
    "真实轮 answer 在 metadata.answer。播种脚本 app/benchmarks/seed_online_eval_dataset.py"
)

# 合成 items（固定文案，域内自造，不取自任何真实轮）：
SYNTHETIC_SEEDS: tuple[dict[str, str], ...] = (
    {
        "input": (
            "北京和上海哪座城市更适合应届生定居？请从生活成本、工作机会、落户难度"
            "三个角度对比，再各推荐两个适合年轻人居住的城区。"
        ),
        "note": "synthetic multifacet：多面复合题（多维度+多产出），考察篇幅控制与分节组织",
    },
    {
        "input": (
            "What are the key differences between HTTP/2 and HTTP/3, and which "
            "one is better suited for SSE streaming applications?"
        ),
        "note": "synthetic en_language_follow：英文题考察语言跟随（回答应整篇英文）",
    },
    {
        "input": "我们刚才都聊了哪些内容？帮我简要回顾一下。",
        "note": "synthetic recall_reask：recall 类回顾重问（B12 recall 分支判据面），考察免检索直达与零 [n] 引用",
    },
)


def _repo_root() -> Path:
    """仓库根（脚本在 apps/search-api/app/benchmarks/ 下，上溯四级）。"""
    return Path(__file__).resolve().parents[4]


def _parse_env_file(path: Path) -> dict[str, str]:
    """宽容解析 .env（nacos_init.py 同款）：跳过注释/空行，剥引号；不打印内容。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.split(" #")[0].strip().strip("'\"")
        if key:
            out[key] = value
    return out


def load_repo_env() -> None:
    """环境变量缺失时回落仓库根 .env（os.environ.setdefault：已设值优先）。"""
    for key, value in _parse_env_file(_repo_root() / ".env").items():
        os.environ.setdefault(key, value)


def _input_key(value: Any) -> str:
    """item.input 查重键：字符串原样，其他 JSON 形态规范化序列化。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _validate_outbound_url(url: str) -> str:
    """出站 URL scheme 闸口（trace_cleanup.py:108 同款）：仅放行 http/https。

    SSRF 面：目标只取自环境变量 LANGFUSE_HOST（部署配置，非用户输入），且为
    第一方基建端点（绑 127.0.0.1 属部署设计，ARCH §7 同口径，不做内网拒绝）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"出站 URL 必须 http/https，得到：{url!r}")
    return url


def fetch_real_from_langfuse() -> list[dict[str, str]]:
    """降级链第 1 级：v2 observations 翻页找带 input/output 的 GENERATION 观测。

    形态同 trace_cleanup.py：cursor 链式翻页（meta.cursor 缺失即停）、时间倒序
    由服务端保证（新→旧）、逐行防御性抽取 query/answer。本部署 events_only
    写模式下恒返空列表（探针：全量观测零 content），异常向上抛由调用方降级。
    """
    host = _validate_outbound_url(os.environ["LANGFUSE_HOST"].rstrip("/"))
    public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
    secret_key = os.environ["LANGFUSE_SECRET_KEY"]
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    turns: list[dict[str, str]] = []
    url = f"{host}/api/public/v2/observations?limit={LF_SCAN_PAGE_SIZE}"
    for _ in range(LF_SCAN_MAX_PAGES):
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(request, timeout=REST_TIMEOUT_S) as response:
            payload = json.load(response)
        for obs in payload.get("data", []):
            if obs.get("type") != "GENERATION" or not obs.get("input") or not obs.get("output"):
                continue
            query = _extract_text(obs["input"])
            answer = _extract_text(obs["output"])
            if not query or not answer:
                continue
            turns.append(
                {"query": query, "answer": answer, "trace_id": obs.get("traceId") or ""}
            )
            if len(turns) >= DB_REAL_TURN_LIMIT:
                return turns
        cursor = (payload.get("meta") or {}).get("cursor") if isinstance(payload.get("meta"), dict) else None
        if not cursor:
            return turns
        url = f"{host}/api/public/v2/observations?limit={LF_SCAN_PAGE_SIZE}&cursor={cursor}"
    return turns


def _extract_text(value: Any) -> str:
    """从 GENERATION input/output 防御性抽正文：str 直取；messages/单键 dict 兜底。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if isinstance(value.get("content"), str) and value["content"].strip():
            return value["content"].strip()
        messages = value.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict) and isinstance(last.get("content"), str):
                return last["content"].strip()
    return ""


def fetch_real_from_db() -> list[dict[str, str]]:
    """降级链第 2 级：Postgres turns 表取最近 fast 成功轮（只读 SELECT）。"""
    query = (
        "SELECT t.query, t.answer_md, coalesce(t.langfuse_trace_id, '') AS trace_id "
        "FROM turns t WHERE t.mode = 'fast' AND t.status = 'completed' "
        "AND t.answer_md IS NOT NULL ORDER BY t.created_at DESC LIMIT $1"
    )

    async def _run() -> list[dict[str, str]]:
        conn = await asyncpg.connect(
            host=os.environ.get("DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("DB_PORT", "15432")),
            user=os.environ.get("DB_USER", "ai_search"),
            password=os.environ.get("DB_PASSWORD", ""),
            database=os.environ.get("DB_NAME", "ai_search"),
            timeout=DB_CONNECT_TIMEOUT_S,
        )
        try:
            rows = await conn.fetch(query, DB_REAL_TURN_LIMIT)
        finally:
            await conn.close()
        return [
            {
                "query": row["query"].strip(),
                "answer": row["answer_md"].strip(),
                "trace_id": row["trace_id"] or "",
            }
            for row in rows
        ]

    return asyncio.run(_run())


def build_real_items() -> tuple[list[dict[str, Any]], str]:
    """真实 items 构建（三级降级链）。返回 (items, 实际来源标识)。"""
    chain = (
        ("langfuse.v2_observations", fetch_real_from_langfuse),
        ("postgres.turns", fetch_real_from_db),
    )
    for source, fetch in chain:
        try:
            turns = fetch()
        except Exception as exc:  # noqa: BLE001 —— 降级链语义：本级失败落下一级
            print(f"[seed] 真实数据源 {source} 失败，降级下一级：{type(exc).__name__}")
            continue
        if not turns:
            print(f"[seed] 真实数据源 {source} 零可用行，降级下一级")
            continue
        items: list[dict[str, Any]] = []
        seen_queries: set[str] = set()
        for turn in turns:
            key = _input_key(turn["query"])
            if not key or key in seen_queries:
                continue
            seen_queries.add(key)
            items.append(
                {
                    "input": turn["query"],
                    "metadata": {
                        "kind": "real",
                        "note": f"最近 fast 成功轮（mode=fast status=completed，来源 {source}）",
                        "answer": turn["answer"],
                        "trace_id": turn["trace_id"],
                    },
                    "source_trace_id": turn["trace_id"] or None,
                }
            )
            if len(items) >= REAL_ITEM_COUNT:
                break
        if items:
            return items, source
    return [], "none"


def build_synthetic_items() -> list[dict[str, Any]]:
    """合成 items：expected_output 留空，metadata 只带 kind/note。"""
    return [
        {
            "input": seed["input"],
            "metadata": {"kind": "synthetic", "note": seed["note"]},
            "source_trace_id": None,
        }
        for seed in SYNTHETIC_SEEDS
    ]


def ensure_dataset(client: Langfuse) -> None:
    """dataset 按名复用：已存在不重建；404 才 create。"""
    try:
        client.get_dataset(DATASET_NAME)
        print(f"[seed] dataset {DATASET_NAME!r} 已存在，复用不重建")
        return
    except ApiError as exc:
        if exc.status_code != 404:
            raise
    client.create_dataset(name=DATASET_NAME, description=_DATASET_DESCRIPTION)
    print(f"[seed] dataset {DATASET_NAME!r} 已创建")


def existing_input_keys(client: Langfuse) -> set[str]:
    """回读现有 items 的 input 键集合（幂等查重依据）。"""
    dataset = client.get_dataset(DATASET_NAME)
    return {_input_key(item.input) for item in dataset.items}


def seed_items(client: Langfuse, items: list[dict[str, Any]]) -> tuple[int, int]:
    """播种缺失 items：按 input 查重跳过。返回 (added, skipped)。"""
    existing = existing_input_keys(client)
    added = skipped = 0
    for item in items:
        key = _input_key(item["input"])
        if key in existing:
            skipped += 1
            continue
        client.create_dataset_item(
            dataset_name=DATASET_NAME,
            input=item["input"],
            expected_output=None,  # 试点期留空（工单口径）
            metadata=item["metadata"],
            source_trace_id=item["source_trace_id"],
        )
        existing.add(key)
        added += 1
    return added, skipped


def main() -> int:
    load_repo_env()
    missing = [
        var
        for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")
        if not os.environ.get(var)
    ]
    if missing:
        print(f"[seed] 缺少环境变量 {missing}（.env 亦未提供），无法连接 Langfuse", file=sys.stderr)
        return 1

    client = Langfuse()
    try:
        ensure_dataset(client)

        real_items, real_source = build_real_items()
        items = real_items + build_synthetic_items()
        print(
            f"[seed] 待播种 {len(items)} 条（真实 {len(real_items)} 条来源={real_source}，"
            f"合成 {SYNTHETIC_ITEM_COUNT} 条）"
        )
        added, skipped = seed_items(client, items)
        print(f"[seed] 新增 {added} 条，按 input 查重跳过 {skipped} 条")

        # 自校验：回读 dataset items 计数（< 6 即失败，exit 1）
        count = len(existing_input_keys(client))
        summary = {
            "dataset": DATASET_NAME,
            "items": count,
            "added": added,
            "skipped": skipped,
            "real_items": len(real_items),
            "real_source": real_source,
        }
        if count < MIN_ITEM_COUNT:
            print(
                f"[seed] 自校验失败：dataset 仅 {count} 条 < {MIN_ITEM_COUNT}",
                json.dumps(summary, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1
        print(f"[seed] 自校验通过：dataset 共 {count} 条 items")
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    finally:
        client.flush()


if __name__ == "__main__":
    sys.exit(main())
