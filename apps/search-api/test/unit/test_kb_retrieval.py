"""unit 层：KB 检索通道封装（app/rag/retrieval.py，plan-m3-b19 §2.2 B1 / ADR-017 决策⑨）。

契约锚点：
- ADR-017 §4.1 漏斗：每 query **ES BM25 top30 ‖ pgvector top30**（kb.per_query_top_k=30）
  → RRF(k=60)——本模块只封装单通道检索，融合归 merge_dedupe.rrf_fuse_kb；
- BM25 查询体固定先例（基线探针 §3.1 / data-model §1.19；**§11 增补 fields 随
  mapping 版本化 bump：content.en 经内置 english analyzer 独立分词，multi_match
  逐字段用各自 search_analyzer 分析查询——同份 query 天然双路**）：bool.must =
  multi_match(query, fields=["content","heading_path^2","content.en"],
  type="best_fields") + bool.filter =
  term(user_id)——**user_id 恒携带**；multi_match 默认 OR 语义注记（基线报告 §5）；
- 向量检索语句模板（data-model §1.8 v1.21）：`1 - (embedding <=> :qvec) AS score …
  WHERE user_id = :uid ORDER BY embedding <=> :qvec LIMIT :k`——参数绑定禁拼接；
  查询向量侧零轨迹前缀（plan §3.2：查询文本仅内容 embed）；调用前提 =
  kb.embedding_enabled=true（false 灰度态向量腿短路不发本查询——通道级判定）；
- **修订注 1（架构师冷读 2026-11-26）**：kb_available = es_enabled ∨
  embedding_enabled（任一通道开启即 KB 可用），各腿按自身开关独立短路——禁用 ∧
  组合（「ES 已回填 25979 chunks 但 embedding 未回填」的现实状态整腿误判不可用）；
  通道判定矩阵断言=仅双 false 才 unavailable。

TDD 红绿：unit 全 mock 零网络（ES 假 client / session 假体 / embed 缝注入——
test_kb_es_indexer 假 client 先例；SQL 语句经 capture 断言参数绑定形态）。
"""

import uuid
from typing import Any

import pytest

from app.rag import retrieval
from app.rag.retrieval import KbRetriever, kb_available, kb_channels_enabled

_USER_ID = "00000000-0000-4000-8000-000000000001"


# ---------------------------------------------------------------------------
# 通道开关判定（修订注 1：∨ 组合——仅双 false 才 unavailable）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("es_enabled", "embedding_enabled", "expected"),
    [
        (False, False, (False, False)),
        (True, False, (True, False)),
        (False, True, (False, True)),
        (True, True, (True, True)),
    ],
)
def test_kb_channels_enabled_matrix(es_enabled, embedding_enabled, expected):
    """通道判定矩阵（四态）：严格布尔读取（非 True 值一律 false——灰度安全）。"""
    spec = {"kb": {"es_enabled": es_enabled, "embedding_enabled": embedding_enabled}}
    assert kb_channels_enabled(spec) == expected


@pytest.mark.parametrize(
    ("es_enabled", "embedding_enabled"),
    [(True, False), (False, True), (True, True)],
)
def test_kb_available_or_combination(es_enabled, embedding_enabled):
    """修订注 1：任一通道开启即 available（∨）——单通道开启状态不得整腿误判。"""
    spec = {"kb": {"es_enabled": es_enabled, "embedding_enabled": embedding_enabled}}
    assert kb_available(spec) is True


def test_kb_available_dual_false_only():
    """仅双 false 才 unavailable（修订注 1 断言口径）。"""
    assert kb_available({"kb": {"es_enabled": False, "embedding_enabled": False}}) is False
    assert kb_available({}) is False
    assert kb_available(None) is False


def test_kb_channels_non_bool_values_gray_safe():
    """非严格布尔值（字符串/None/缺键）一律 false——沿 read_es_enabled 灰度先例。"""
    spec = {"kb": {"es_enabled": "true", "embedding_enabled": None}}
    assert kb_channels_enabled(spec) == (False, False)
    assert kb_channels_enabled({"kb": {}}) == (False, False)


# ---------------------------------------------------------------------------
# bm25_search：multi_match content+heading_path^2+content.en + term user_id 恒携带
# ---------------------------------------------------------------------------


class _FakeEsClient:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits
        self.search_calls: list[dict[str, Any]] = []

    async def search(self, *, index: str, query: dict[str, Any], size: int) -> dict[str, Any]:
        self.search_calls.append({"index": index, "query": query, "size": size})
        return {"hits": {"hits": self._hits}}


def _es_hit(chunk_index: int, score: float) -> dict[str, Any]:
    return {
        "_score": score,
        "_source": {
            "chunk_id": f"chunk-{chunk_index}",
            "kb_doc_id": f"doc-{chunk_index}",
            "content": f"KB 切片正文{chunk_index}",
            "heading_path": "示例文档 > 第一章 > 小节",
            "node_id": f"loc:blk:{chunk_index}",
        },
    }


async def test_bm25_search_query_shape_and_user_filter():
    """查询体固定先例：multi_match(content+heading_path^2+content.en〔§11 增补〕,
    best_fields) + bool.filter term(user_id) 恒携带 + index 名 + size=k；
    行投影 channel=bm25。content.en 与主字段同份 query 双路（multi_match 逐字段
    各自 search_analyzer——content→jieba_search、content.en→english）。"""
    client = _FakeEsClient([_es_hit(0, 12.3), _es_hit(1, 8.1)])
    retriever = KbRetriever(es_client_factory=lambda: client, session_factory=None, embed_fn=None)
    rows = await retriever.bm25_search("混合检索", user_id=_USER_ID, k=30)
    call = client.search_calls[0]
    assert call["index"] == "ai_search_kb_chunks"
    assert call["size"] == 30
    must = call["query"]["bool"]["must"]
    assert must == [
        {
            "multi_match": {
                "query": "混合检索",
                "fields": ["content", "heading_path^2", "content.en"],
                "type": "best_fields",
            }
        }
    ]
    assert call["query"]["bool"]["filter"] == [{"term": {"user_id": _USER_ID}}]
    assert len(rows) == 2
    assert rows[0]["chunk_id"] == "chunk-0"
    assert rows[0]["channel"] == "bm25"
    assert rows[0]["heading_path"] == ["示例文档", "第一章", "小节"]
    assert rows[0]["node_id"] == "loc:blk:0"  # bm25 行与 vector 行投影对齐（溯源寻址）
    assert rows[0]["score"] == pytest.approx(12.3)


async def test_bm25_search_zero_hits():
    """零命中 → 空表（KB 空=降级链第一级——纯网络回退由融合面承担）。"""
    client = _FakeEsClient([])
    retriever = KbRetriever(es_client_factory=lambda: client, session_factory=None, embed_fn=None)
    assert await retriever.bm25_search("任何查询", user_id=_USER_ID, k=30) == []


# ---------------------------------------------------------------------------
# corpus_chunk_count：语料探针 user 过滤（SA-4——当前用户语料空 + 消除跨用户聚合暴露）
# ---------------------------------------------------------------------------


class _CountingEsClient:
    def __init__(self, count: int) -> None:
        self._count = count
        self.count_calls: list[dict[str, Any]] = []

    async def count(self, *, index: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        self.count_calls.append({"index": index, "query": query})
        return {"count": self._count}


async def test_corpus_chunk_count_es_filters_user():
    """ES 探针：_count 携带 bool.filter term(user_id)（与检索同域——跨用户聚合
    信息暴露面消除；SA-4）。"""
    client = _CountingEsClient(7)
    retriever = KbRetriever(es_client_factory=lambda: client)
    total = await retriever.corpus_chunk_count(prefer_es=True, user_id=_USER_ID)
    assert total == 7
    call = client.count_calls[0]
    assert call["index"] == "ai_search_kb_chunks"
    assert call["query"] == {"bool": {"filter": [{"term": {"user_id": _USER_ID}}]}}


async def test_corpus_chunk_count_pg_filters_user():
    """pg 探针：count(*) 带 WHERE user_id = :uid 绑定参数（零拼接）。"""
    session = _FakeSession([])

    async def _scalar(stmt: Any, params: dict[str, Any] | None = None) -> int:
        session.executed.append((stmt, params))
        return 3

    session.scalar = _scalar  # type: ignore[attr-defined]
    retriever = KbRetriever(
        session_factory=_FakeSessionFactory(session), embed_fn=None
    )
    total = await retriever.corpus_chunk_count(prefer_es=False, user_id=_USER_ID)
    assert total == 3
    stmt, params = session.executed[0]
    sql = str(stmt)
    assert "WHERE user_id = :uid" in sql
    assert params == {"uid": _USER_ID}


# ---------------------------------------------------------------------------
# vector_search：查询文本自身 embed → pgvector cosine top-k（参数绑定）
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[Any]) -> None:
        self.executed: list[tuple[Any, dict[str, Any] | None]] = []
        self._rows = rows

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        self.executed.append((stmt, params))
        return _FakeResult(self._rows)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


class _Row:
    def __init__(self) -> None:
        self.id = "22222222-2222-4222-8222-000000000001"
        self.kb_doc_id = "33333333-3333-4333-8333-000000000001"
        self.content = "KB 向量切片正文"
        self.score = 0.87
        self.meta = {"heading_path": ["示例文档", "第二章"], "node_id": "loc:blk:9"}


async def test_vector_search_embeds_query_and_binds_params():
    """查询文本自身 embed（零轨迹前缀——build_embedding_input 空轨迹=原文）→
    pg cosine 语句参数绑定（:qvec/:uid/:k 三绑定值；SQL 无拼接断言）+ user 过滤。"""
    session = _FakeSession([_Row()])
    embed_inputs: list[list[str]] = []

    async def fake_embed(texts, *, mapping=None):
        embed_inputs.append(list(texts))
        return [[0.1, 0.2, 0.3]]

    retriever = KbRetriever(
        es_client_factory=None,
        session_factory=_FakeSessionFactory(session),
        embed_fn=fake_embed,
    )
    rows = await retriever.vector_search("pgvector 混合检索", user_id=_USER_ID, k=30)
    assert embed_inputs == [["pgvector 混合检索"]]  # 查询侧零轨迹前缀
    assert len(session.executed) == 1
    stmt, params = session.executed[0]
    sql = str(stmt)
    assert "CAST(:qvec AS vector)" in sql  # 绑定值显式 cast（非拼接）
    assert "user_id = :uid" in sql
    assert "LIMIT :k" in sql
    assert params == {
        "qvec": "[0.100000,0.200000,0.300000]",
        "uid": _USER_ID,
        "k": 30,
    }
    assert rows[0]["chunk_id"] == "22222222-2222-4222-8222-000000000001"
    assert rows[0]["channel"] == "vector"
    assert rows[0]["heading_path"] == ["示例文档", "第二章"]
    assert rows[0]["node_id"] == "loc:blk:9"
    assert rows[0]["score"] == pytest.approx(0.87)


# ---------------------------------------------------------------------------
# 生产装配缝
# ---------------------------------------------------------------------------


def test_get_kb_retriever_singleton(monkeypatch: pytest.MonkeyPatch):
    """get_kb_retriever 进程级单例（get_kb_es_indexer 先例；测试后复位）。"""
    monkeypatch.setattr(retrieval, "_kb_retriever_instance", None)
    retriever = retrieval.get_kb_retriever()
    assert retriever is retrieval.get_kb_retriever()
    monkeypatch.setattr(retrieval, "_kb_retriever_instance", None)
