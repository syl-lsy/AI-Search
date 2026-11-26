"""KB 检索通道封装（plan-m3-b19 §2.2 B1 / ARCH ADR-017 决策⑨——检索通道可替换接口）。

fast kb_search 节点与 deep 子代理 kb_search 工具共用同一封装（供 fast/deep 复用）。

契约锚点：
- ARCH §4.1 漏斗：每 query **ES BM25 top30 ‖ pgvector top30**（kb.per_query_top_k=30）
  并行 → RRF(k=60)——本模块只封装单通道检索与通道开关判定，跨 query×通道融合收口
  归 merge_dedupe.rrf_fuse_kb（ADR-017 决策②字面「全副本完成后的单点」）；
- BM25 查询体**固定先例**（docs/research/m3-b18-golden-bm25-baseline-20261125.md §3.1 /
  data-model §1.19）：`bool.must = multi_match(query, fields=["content","heading_path^2",
  "content.en"], type="best_fields")` + `bool.filter = term(user_id)`——**user_id 恒携带**；
  multi_match 默认 OR 语义注记（基线报告 §5 解读前提）；权重数值契约未钉、基线探针固定
  `^2` 一次性钉定先例；**§11 增补（plan-m3-b19，2026-11-26 用户裁决）**：content.en
  multi-field（english analyzer）随 mapping 版本化 bump 进 fields——词干形态差词法可命中，
  best_fields 取优、中文主字段行为不挤出；
- 向量检索语句模板（data-model §1.8 v1.21）：`1 - (embedding <=> :qvec) AS score …
  WHERE user_id = :uid ORDER BY embedding <=> :qvec LIMIT :k`——**参数绑定禁拼接**
  （qvec 为绑定值的 CAST 显式定型——asyncpg 文本形态 vector 绑定所需，绑定纪律不破）；
  查询向量侧**零轨迹前缀**（plan §3.2：查询文本仅内容 embed，轨迹前置只作用于入库侧）；
- **通道开关判定（修订注 1，架构师冷读 2026-11-26）**：`kb_available = es_enabled ∨
  embedding_enabled`（任一通道开启即 KB 可用）——**禁用 ∧ 组合**（「ES 已回填 25979
  chunks 但 embedding 未回填」的现实状态不得整腿误判不可用）；各腿按自身开关独立
  短路（短路判定在消费面 kb_search 节点，本模块提供矩阵读取纯函数）。

装配纪律：ES 走 get_es_client、DB 走 get_session_factory、embed 走 app.rag.embedding
——与生产路径同一工厂零分叉；凭据零打印（es_settings/litellm_settings 单例出口）；
单测经构造参数注入假体（test_kb_retrieval / test_kb_search_node 零网络）。
"""

from collections.abc import Mapping
from typing import Any, Callable

from sqlalchemy import text as sql_text

from app.db.session import get_session_factory
from app.rag.embedding import embed_texts
from app.rag.indexers.es_client import get_es_client
from app.rag.indexers.es_mapping import index_name

__all__ = [
    "KbRetriever",
    "kb_available",
    "kb_channels_enabled",
    "get_kb_retriever",
]

#: BM25 multi_match 字段固定先例（基线探针 §3.1——heading_path 独立字段权重 ^2；
#: §11 增补 content.en multi-field 随 mapping 版本化 bump——english analyzer 独立
#: 分词，multi_match 逐字段用各自 search_analyzer，同份 query 天然双路）
_BM25_FIELDS: tuple[str, ...] = ("content", "heading_path^2", "content.en")

_kb_retriever_instance: "KbRetriever | None" = None


def kb_channels_enabled(spec: Mapping[str, Any] | None) -> tuple[bool, bool]:
    """通道开关矩阵读取（严格布尔；非 True 值/缺键一律 false——read_es_enabled
    灰度先例同判）。返回 (es_on, vec_on)。"""
    kb = spec.get("kb") if isinstance(spec, Mapping) else None
    kb = kb if isinstance(kb, Mapping) else {}
    es_on = kb.get("es_enabled") is True
    vec_on = kb.get("embedding_enabled") is True
    return es_on, vec_on


def kb_available(spec: Mapping[str, Any] | None) -> bool:
    """KB 可用判定（修订注 1）：es_enabled ∨ embedding_enabled——任一通道开启即
    可用，仅双 false 才 False。"""
    es_on, vec_on = kb_channels_enabled(spec)
    return es_on or vec_on


class KbRetriever:
    """双通道检索封装（单通道检索 + 通道开关矩阵读取；融合不在本类——ADR-017
    决策②「跨 query×通道合并」收口在 merge_dedupe.rrf_fuse_kb）。

    依赖全注入（ES 客户端工厂 / session 工厂 / embed 函数）——单测假体零网络；
    生产经 `get_kb_retriever` 装配单例、TurnRunner 经 configurable 注入。
    """

    def __init__(
        self,
        *,
        es_client_factory: Callable[[], Any] | None = None,
        session_factory: Callable[[], Any] | None = None,
        embed_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._es_factory = es_client_factory or get_es_client
        # session_factory 形参口径 = 「可调用对象，调用即返回 session 上下文管理器」
        # （缺省惰性包一层——get_session_factory() 返回 sessionmaker，再调用一次得
        # Session 异步上下文；单测注入假体同口径）
        self._session_factory = session_factory or (
            lambda: get_session_factory()()
        )
        self._embed_fn = embed_fn or embed_texts

    async def bm25_search(self, query: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        """ES BM25 单通道检索（multi_match content+heading_path^2+content.en
        best_fields + term user_id 恒携带；data-model §1.19 固定先例 + §11 增补
        content.en 双查）。

        行投影：{chunk_id, kb_doc_id, content, score, heading_path(list), node_id,
        channel:"bm25"}——heading_path 由 ES 联结串按 " > " 还原列表
        （es_mapping.build_chunk_doc 同一 join 口径逆变换）；node_id 与 vector 行
        投影对齐（溯源寻址）；零命中 → 空表（KB 空=降级链第一级）。
        """
        client = self._es_factory()
        es_query = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": list(_BM25_FIELDS),
                            "type": "best_fields",
                        }
                    }
                ],
                "filter": [{"term": {"user_id": str(user_id)}}],
            }
        }
        response = await client.search(index=index_name(), query=es_query, size=int(k))
        hits = (response.get("hits") or {}).get("hits") or []
        rows: list[dict[str, Any]] = []
        for hit in hits:
            if not isinstance(hit, Mapping):
                continue
            source = hit.get("_source") or {}
            joined = str(source.get("heading_path") or "")
            rows.append(
                {
                    "chunk_id": source.get("chunk_id"),
                    "kb_doc_id": source.get("kb_doc_id"),
                    "content": str(source.get("content") or ""),
                    "score": float(hit.get("_score") or 0.0),
                    "heading_path": [seg for seg in joined.split(" > ") if seg],
                    "node_id": source.get("node_id"),
                    "channel": "bm25",
                }
            )
        return rows

    async def vector_search(
        self, query_text: str, *, user_id: Any, k: int
    ) -> list[dict[str, Any]]:
        """pgvector cosine 单通道检索（查询文本自身 embed → data-model §1.8 语句
        模板；**调用前提 = kb.embedding_enabled=true**——false 灰度态消费面短路
        不发本查询，通道级判定而非行级零向量过滤）。

        参数绑定：qvec（向量文本形态绑定值，CAST 显式定型）/ uid / k 三绑定；
        user_id 过滤恒携带；行投影同 bm25_search（channel:"vector"，heading_path/
        node_id 直读 meta 七键物化列）。
        """
        vectors = await self._embed_fn([query_text])
        vector = vectors[0]
        qvec = "[" + ",".join(f"{float(v):.6f}" for v in vector) + "]"
        stmt = sql_text(
            "SELECT id, kb_doc_id, content, meta,"
            " 1 - (embedding <=> CAST(:qvec AS vector)) AS score"
            " FROM kb_chunks"
            " WHERE user_id = :uid"
            " ORDER BY embedding <=> CAST(:qvec AS vector)"
            " LIMIT :k"
        )
        async with self._session_factory() as session:
            result = await session.execute(
                stmt, {"qvec": qvec, "uid": str(user_id), "k": int(k)}
            )
            rows = result.all()
        out: list[dict[str, Any]] = []
        for row in rows:
            meta = row.meta if isinstance(row.meta, Mapping) else {}
            path = meta.get("heading_path")
            out.append(
                {
                    "chunk_id": str(row.id),
                    "kb_doc_id": str(row.kb_doc_id),
                    "content": str(row.content or ""),
                    "score": float(row.score) if row.score is not None else 0.0,
                    "heading_path": [str(seg) for seg in path] if isinstance(path, list) else [],
                    "node_id": meta.get("node_id"),
                    "channel": "vector",
                }
            )
        return out

    async def corpus_chunk_count(self, *, prefer_es: bool, user_id: Any) -> int:
        """语料规模探针（修订注 6：kb_search 入口零文档/零 chunk 短路判定）——
        优先通道计数（ES _count / pg count；均只读单查）。

        **SA-4 用户域收口**：语义=「当前用户语料空」（与检索同域）——ES `_count`
        携带 `bool.filter term(user_id)`、pg `count(*)` 携带 `WHERE user_id = :uid`
        绑定参数；消除跨用户语料规模聚合信息暴露面。
        """
        if prefer_es:
            client = self._es_factory()
            response = await client.count(
                index=index_name(),
                query={"bool": {"filter": [{"term": {"user_id": str(user_id)}}]}},
            )
            return int(response.get("count") or 0)
        stmt = sql_text("SELECT count(*) FROM kb_chunks WHERE user_id = :uid")
        async with self._session_factory() as session:
            total = await session.scalar(stmt, {"uid": str(user_id)})
        return int(total or 0)


def get_kb_retriever() -> KbRetriever:
    """进程级单例（TurnRunner configurable 注入缝；get_kb_es_indexer 先例）。"""
    global _kb_retriever_instance
    if _kb_retriever_instance is None:
        _kb_retriever_instance = KbRetriever()
    return _kb_retriever_instance
