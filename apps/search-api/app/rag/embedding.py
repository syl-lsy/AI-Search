"""embedding 输入构造与网关客户端（plan-m3-b19 §2.1 A1 / §3.2~3.3——索引面）。

契约锚点：
- plan §3.2（heading_path 前置进 embedding 输入——2026-11-26 用户裁决带护栏）：
  输入文本 = `标题轨迹：{" > ".join(heading_path)}\n{content}`；轨迹串截断 ≤
  `EMBEDDING_TRAJECTORY_MAX_CHARS`（200）字符**保叶端**（leaf 侧语义最具体；
  root 侧文档题名已由 doc 级承担——B18 plan §9① 原裁决护栏字面）。常量落模块头
  （配置三分法第三行——纯逻辑防御值不入 Nacos，沿 IMAGES_MAX_COUNT 判例）；
- plan §3.3 / §5 必答项②：模型别名 = 轮内映射快照 `model.mapping.embedding`
  （缺省回落内置 text-embedding-v4——nacos-config.md §5 :379 已钉）；维度 =
  kb_settings.embedding_dim（1024——data-model §1.8 :316 / config.py 已钉）；
  响应维度≠维度常量 → EmbeddingError（防静默脏向量）；批窗切 ≤
  EMBEDDING_BATCH_SIZE（10，代码常量）；429/5xx/连接类重试恰 1 次（退避 1s，
  沿 pipeline `_ES_RETRY_BACKOFF` 首档节奏）；耗尽 EmbeddingError 上抛（消息仅
  类型名/HTTP 状态/attempts——**零 content 回显**，与 es_indexer 同纪律）；
- 消费点单一事实源：pipeline `_run_chunking`（chunking 段接线）与存量回填脚本
  （scripts/kb_embedding_backfill.py，输入=新口径同一 build_embedding_input——
  混用两口径向量会撕裂向量空间）与检索查询侧向量化（plan §3.2——查询文本无
  轨迹，仅内容侧 embed 零前缀）。

边界纪律：rag 层纯模块零 DB 零全局状态；网关凭据只经 `litellm_settings` 单例
读取（配置三分法第一行），凭据零打印零落日志；错误面 EmbeddingError 仅类型名/
状态/attempts/维度数（零文本片段回显）。
"""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from app.config_center.defaults import MODEL_MAPPING_CONFIG
from app.core.config import kb_settings, litellm_settings

__all__ = [
    "EMBEDDING_BATCH_SIZE",
    "EMBEDDING_GATEWAY_TIMEOUT_S",
    "EMBEDDING_TRAJECTORY_MAX_CHARS",
    "EmbeddingError",
    "build_embedding_input",
    "embed_texts",
    "resolve_embedding_alias",
]

#: 标题轨迹护栏上限（plan §3.2 钉字：200 字符保叶端——代码常量不入 Nacos）
EMBEDDING_TRAJECTORY_MAX_CHARS = 200

#: 批窗切尺寸（plan §7 T1「批窗切（≤10 代码常量）」——单请求条数上限）
EMBEDDING_BATCH_SIZE = 10

#: 网关请求超时（代码常量——契约未钉 embedding 超时键，纯防御值沿
#: `_DEFAULT_RERANK_TIMEOUT_S` 判例；不冒进长等待，失败走重试面）
EMBEDDING_GATEWAY_TIMEOUT_S = 10.0

#: 内置回落别名（nacos-config.md §5 :379 `embedding: "text-embedding-v4"`；
#: mapping 快照缺键时回落——与 defaults.MODEL_MAPPING_CONFIG 同值双投影）
_DEFAULT_EMBEDDING_ALIAS = "text-embedding-v4"

#: retryable HTTP 状态族（429 + 5xx——与 es_indexer.is_retryable_es_error 同族；
#: 其余 4xx 不可重试立即失败）
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: 重试退避（恰 1 次——plan A1「失败重试 1 次」；1s 沿 pipeline 退避首档）
_EMBEDDING_RETRY_BACKOFF_S: tuple[float, ...] = (1.0,)

#: 退避 sleep 缝（模块级名——单测 monkeypatch 免真等 1s）
_retry_sleep = asyncio.sleep


class EmbeddingError(Exception):
    """embedding 通道业务失败（消息仅类型名/HTTP 状态/attempts/维度数——零
    content 回显）。"""


def build_embedding_input(content: str, heading_path: Sequence[str]) -> str:
    """embedding 输入构造（plan §3.2 单一事实源）。

    输入 = `标题轨迹：{" > ".join(heading_path)}\n{content}`；轨迹 >200 字符
    截断**保叶端**（`[-200:]`——leaf 侧语义最具体）；空 heading_path → 仅
    content（零前缀——查询侧向量化同款形态）。
    """
    trajectory = " > ".join(heading_path)
    if not trajectory:
        return content
    if len(trajectory) > EMBEDDING_TRAJECTORY_MAX_CHARS:
        trajectory = trajectory[-EMBEDDING_TRAJECTORY_MAX_CHARS:]
    return f"标题轨迹：{trajectory}\n{content}"


def resolve_embedding_alias(mapping: Mapping[str, Any] | None = None) -> str:
    """embedding 别名解析：轮内映射快照 `model.mapping.embedding` 优先，缺键/
    快照缺失回落内置 text-embedding-v4（OQ-11 同源——快照只读不回写）。"""
    source = mapping if isinstance(mapping, Mapping) else MODEL_MAPPING_CONFIG
    alias = source.get("embedding")
    return str(alias) if alias else _DEFAULT_EMBEDDING_ALIAS


def _parse_vectors(response: httpx.Response, *, expected: int) -> list[list[float]]:
    """出参解析（OpenAI /embeddings 形态）：data[i] 按 index 对位排序 → 向量列表。

    形态异常/维度不符 → EmbeddingError（零重试——确定性失败；消息仅维度数）。
    """
    try:
        payload = response.json()
        data = payload["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise EmbeddingError("embedding 出参形态异常（data 缺失或非 JSON）") from exc
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError(
            f"embedding 出参形态异常（data 数量不符：got={len(data) if isinstance(data, list) else '非列表'} expect={expected}）"
        )
    ordered = sorted(data, key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0)
    vectors: list[list[float]] = []
    for item in ordered:
        vector = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector, list):
            raise EmbeddingError("embedding 出参形态异常（embedding 缺失）")
        if len(vector) != kb_settings.embedding_dim:
            raise EmbeddingError(
                f"embedding 维度不符: got={len(vector)} "
                f"expect={kb_settings.embedding_dim}"
            )
        vectors.append([float(x) for x in vector])
    return vectors


async def _embed_batch(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    model: str,
    batch: list[str],
) -> list[list[float]]:
    """单批调用（429/5xx/连接类重试恰 1 次；耗尽 EmbeddingError 零 content）。"""
    attempts_total = 1 + len(_EMBEDDING_RETRY_BACKOFF_S)
    detail = ""
    for attempt in range(attempts_total):
        try:
            response = await client.post(
                url, json={"model": model, "input": batch}, headers=headers
            )
        except httpx.TransportError:
            detail = "TransportError"
            if attempt < attempts_total - 1:
                await _retry_sleep(_EMBEDDING_RETRY_BACKOFF_S[attempt])
                continue
            break
        if response.status_code in _RETRYABLE_STATUS:
            detail = f"HTTP {response.status_code}"
            if attempt < attempts_total - 1:
                await _retry_sleep(_EMBEDDING_RETRY_BACKOFF_S[attempt])
                continue
            break
        if response.status_code != 200:
            raise EmbeddingError(
                f"embedding 调用失败: HTTP {response.status_code}（不可重试）"
                f" attempts={attempt + 1}"
            )
        return _parse_vectors(response, expected=len(batch))
    raise EmbeddingError(f"embedding 调用失败: {detail} attempts={attempts_total}")


async def embed_texts(
    texts: Sequence[str],
    *,
    model: str | None = None,
    mapping: Mapping[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """文本批向量化（LiteLLM 网关 /embeddings 透传）。

    - 模型别名：`model` 显式形参 > `mapping` 快照 > 内置 text-embedding-v4；
    - 批窗切：每 ≤EMBEDDING_BATCH_SIZE 条一请求，结果按输入序拼接；
    - 失败语义：429/5xx/连接类重试恰 1 次；耗尽/不可重试/维度不符 →
      EmbeddingError 上抛（消息零 content 回显）——调用方（pipeline/回填脚本）
      显性转 failed，不静默占位；
    - `client` 形参为测试缝（MockTransport 注入）；缺省自建 AsyncClient 并
      归还关闭（注入态由调用方管生命周期）。
    """
    if not texts:
        return []
    alias = model or resolve_embedding_alias(mapping)
    url = str(litellm_settings.base_url).rstrip("/") + "/embeddings"
    headers = {
        "Authorization": "Bearer " + litellm_settings.api_key.get_secret_value()
    }
    own_client = client is None
    http_client = client if client is not None else httpx.AsyncClient(
        timeout=EMBEDDING_GATEWAY_TIMEOUT_S
    )
    try:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = list(texts[start : start + EMBEDDING_BATCH_SIZE])
            vectors.extend(
                await _embed_batch(
                    http_client, url=url, headers=headers, model=alias, batch=batch
                )
            )
        return vectors
    finally:
        if own_client:
            await http_client.aclose()
