"""unit 层：KB ES BM25 写入通道（app/rag/indexers/，data-model §1.19 / plan B18 §4）。

覆盖面（TDD 红绿；全假体零网络）：
- **纯函数面**（es_mapping）：索引名拼接 `{prefix}_kb_chunks`、索引 settings 快照
  （单分片/零副本/refresh 1s）、mapping 快照（jieba_index/jieba_search analyzer、
  heading_path keyword 副本、loc/references enabled:false——存 _source 不索引）、
  确定性 `_id = {kb_doc_id}:{chunk_index}`、bulk action 构造（meta 七键 → 字段逐键
  映射 + doc_kind/catalog_path/kind 注入 + created_at ISO 归一）、bulk 尺寸窗切器
  （≤500 actions 或 ≤8MB 先到者切分）；
- **操作面**（es_indexer，假 client 零网络）：ensure_index 幂等（已存在零 create）、
  delete_by_query body（term kb_doc_id）与 failures 判定、count_by_doc、index_chunks
  「先删后 bulk + 逐批判定」（async_bulk 模块缝 monkeypatch）；
- **客户端工厂面**（es_client）：basic_auth 仅 username 非空（build_client_kwargs
  纯函数）、惰性单例稳定性；
- **重试集合判定**：elastic_transport.ConnectionError + TransportError(429/5xx)
  retryable；400/其他异常不可 retryable（pipeline 层重试的判据面）。

data-model §1.19 事实源分层条款：mapping 细节落 `app/rag/indexers/`，本测即其
测试侧镜像。测试值一律为无意义假值，零凭据零内容回显。
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from elastic_transport import ConnectionError as EsConnectionError
from pydantic import SecretStr

from app.rag.indexers import es_client, es_indexer, es_mapping

_KB_DOC_ID = "7d2f0a1e-0000-4000-8000-0000000000b1"
_USER_ID = "00000000-0000-4000-8000-000000000001"


def _chunk_row(index: int = 0, **overrides: Any) -> dict[str, Any]:
    """kb_chunks 行投影假体（KbDocumentRepository.list_chunks_by_doc 同形）。"""
    row: dict[str, Any] = {
        "chunk_id": uuid.UUID(f"11111111-1111-4111-8111-{index:012d}"),
        "kb_doc_id": uuid.UUID(_KB_DOC_ID),
        "user_id": uuid.UUID(_USER_ID),
        "chunk_index": index,
        "content": f"切片正文{index}",
        "token_count": 4,
        "meta": {
            "node_id": f"loc:blk:{index}",
            "heading_path": ["示例文档", "第一章"],
            "loc": {"page": index + 1},
            "references": [{"node_id": "loc:blk:0", "kind": "paragraph", "label": "示例"}],
            "raw_type": "p",
            "char_start": 0,
            "char_end": 4,
        },
        "created_at": datetime(2026, 11, 25, tzinfo=UTC),
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# es_mapping 纯函数面
# ---------------------------------------------------------------------------


def test_index_name_uses_prefix():
    """索引名 = `{prefix}_kb_chunks`（data-model §1.19 字面；缺省 ai_search）。"""
    assert es_mapping.index_name() == "ai_search_kb_chunks"
    assert es_mapping.index_name("ai_search_test") == "ai_search_test_kb_chunks"


def test_index_settings_snapshot():
    """索引 settings 快照：单分片/零副本（单节点 green）/refresh 1s；零自定义 analysis
    （jieba 两 analyzer 由插件注册，mapping 直接引用——零 analyzer 配置漂移面）。"""
    assert es_mapping.INDEX_SETTINGS == {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "refresh_interval": "1s",
    }
    assert "analysis" not in es_mapping.INDEX_SETTINGS


def test_index_mappings_snapshot():
    """mapping 快照：analyzer 名/keyword 副本/enabled:false 逐项锁定（契约 §1.19）。"""
    props = es_mapping.INDEX_MAPPINGS["properties"]
    # jieba 双 analyzer：索引时 jieba_index / 检索时 jieba_search（探针 §5.1 实测名）
    for field in ("content", "heading_path"):
        assert props[field]["analyzer"] == "jieba_index", field
        assert props[field]["search_analyzer"] == "jieba_search", field
    # §11 英文词法加固（plan-m3-b19 §11，2026-11-26 用户裁决；CR-2 修复轮实施）：
    # content.en multi-field——ES 内置 english analyzer（standard tokenizer+
    # lowercase+porter 词干化），零新依赖零自定义 analysis；主字段 jieba 双查不变
    assert props["content"]["fields"] == {"en": {"type": "text", "analyzer": "english"}}
    assert props["catalog_path"]["fields"]["txt"]["analyzer"] == "jieba_index"
    # heading_path keyword 精确过滤副本；catalog_path 主字段即 keyword
    assert props["heading_path"]["fields"]["keyword"]["type"] == "keyword"
    assert props["catalog_path"]["type"] == "keyword"
    # keyword 过滤面（term 恒携带/删除寻址）
    for field in ("chunk_id", "kb_doc_id", "user_id"):
        assert props[field]["type"] == "keyword", field
    # loc/references 存 _source 不索引（溯源权威在 pg——plan B18 §4.3 取舍）
    assert props["loc"] == {"type": "object", "enabled": False}
    assert props["references"] == {"type": "object", "enabled": False}
    # 数值/日期面
    assert props["chunk_index"]["type"] == "integer"
    assert props["char_start"]["type"] == "integer"
    assert props["char_end"]["type"] == "integer"
    assert props["token_count"]["type"] == "integer"
    assert props["created_at"]["type"] == "date"


def test_es_doc_id_deterministic():
    """`_id = {kb_doc_id}:{chunk_index}` 确定性复合键（重写幂等——重复执行零重复）。"""
    assert es_mapping.es_doc_id(_KB_DOC_ID, 3) == f"{_KB_DOC_ID}:3"
    assert es_mapping.es_doc_id(uuid.UUID(_KB_DOC_ID), 0) == f"{_KB_DOC_ID}:0"


def test_build_chunk_doc_maps_meta_seven_keys():
    """bulk doc 构造：meta 七键 → 字段逐键映射 + doc 级三键（catalog_path/doc_kind/
    kind 从 node_kinds 派生——与 chunker 同一事实源 IR node.type）。"""
    doc = es_mapping.build_chunk_doc(
        _chunk_row(0),
        kb_doc_id=_KB_DOC_ID,
        user_id=_USER_ID,
        doc_meta={
            "catalog_path": "研发 > 手册",
            "doc_kind": "upload",
            "node_kinds": {"loc:blk:0": "paragraph"},
        },
    )
    assert doc["chunk_id"] == str(_chunk_row(0)["chunk_id"])
    assert doc["kb_doc_id"] == _KB_DOC_ID
    assert doc["user_id"] == _USER_ID
    assert doc["content"] == "切片正文0"
    assert doc["heading_path"] == "示例文档 > 第一章"  # root→leaf join " > "
    assert doc["catalog_path"] == "研发 > 手册"
    assert doc["doc_kind"] == "upload"
    assert doc["kind"] == "paragraph"  # IR node.type 派生（Chunk.kind 同源）
    assert doc["raw_type"] == "p"
    assert doc["chunk_index"] == 0
    assert doc["char_start"] == 0
    assert doc["char_end"] == 4
    assert doc["token_count"] == 4
    assert doc["created_at"] == "2026-11-25T00:00:00+00:00"  # ISO 归一
    assert doc["node_id"] == "loc:blk:0"
    assert doc["loc"] == {"page": 1}
    assert doc["references"] == [{"node_id": "loc:blk:0", "kind": "paragraph", "label": "示例"}]


def test_build_chunk_doc_degrades_gracefully():
    """缺 meta/空标题链/未知 node_id/缺 created_at → None 缺省，不炸不伪造。"""
    row = _chunk_row(
        1,
        meta={"node_id": "loc:blk:missing", "raw_type": None, "heading_path": []},
        created_at=None,
    )
    doc = es_mapping.build_chunk_doc(
        row, kb_doc_id=_KB_DOC_ID, user_id=_USER_ID, doc_meta={}
    )
    assert doc["heading_path"] is None
    assert doc["catalog_path"] is None
    assert doc["doc_kind"] is None
    assert doc["kind"] is None  # node_kinds 无命中 → None（IR 不可达态不阻断）
    assert doc["raw_type"] is None
    assert doc["created_at"] is None


def test_build_bulk_actions_shape():
    """bulk action NDJSON 构造：_op_type=index + _index + 确定性 _id + doc 字段。"""
    actions = es_mapping.build_bulk_actions(
        [_chunk_row(0), _chunk_row(1)],
        kb_doc_id=_KB_DOC_ID,
        user_id=_USER_ID,
        doc_meta={"node_kinds": {}},
    )
    assert [a["_id"] for a in actions] == [f"{_KB_DOC_ID}:0", f"{_KB_DOC_ID}:1"]
    assert all(a["_op_type"] == "index" for a in actions)
    assert all(a["_index"] == "ai_search_kb_chunks" for a in actions)
    assert actions[0]["content"] == "切片正文0"


def test_split_bulk_batches_by_count():
    """窗切器（数量轴）：>500 actions 先到者切分（500+100）。"""
    actions = es_mapping.build_bulk_actions(
        [_chunk_row(i) for i in range(600)],
        kb_doc_id=_KB_DOC_ID,
        user_id=_USER_ID,
        doc_meta={"node_kinds": {}},
    )
    batches = es_mapping.split_bulk_batches(actions)
    assert [len(b) for b in batches] == [500, 100]
    assert sum(len(b) for b in batches) == 600


def test_split_bulk_batches_by_bytes():
    """窗切器（体积轴）：单批序列化 >8MB 先到者切分（纯函数，构造大 content）。"""
    big = "x" * (5 * 1024 * 1024)  # 每条 ~5MB——两条同批超 8MB
    rows = [
        _chunk_row(0, content=big),
        _chunk_row(1, content=big),
    ]
    actions = es_mapping.build_bulk_actions(
        rows, kb_doc_id=_KB_DOC_ID, user_id=_USER_ID, doc_meta={"node_kinds": {}}
    )
    batches = es_mapping.split_bulk_batches(actions)
    assert [len(b) for b in batches] == [1, 1]


def test_split_bulk_batches_single_oversized_stays_one_batch():
    """单条超限不可再切——独立成批（防御性兜底，不丢数据）。"""
    big_row = _chunk_row(0, content="x" * (9 * 1024 * 1024))
    actions = es_mapping.build_bulk_actions(
        [big_row], kb_doc_id=_KB_DOC_ID, user_id=_USER_ID, doc_meta={"node_kinds": {}}
    )
    batches = es_mapping.split_bulk_batches(actions)
    assert len(batches) == 1 and len(batches[0]) == 1


def test_split_bulk_batches_actions_in_order():
    """切分保持文档内 chunk_index 顺序（拼接回全集=原序，零丢失零重排）。"""
    actions = es_mapping.build_bulk_actions(
        [_chunk_row(i) for i in range(501)],
        kb_doc_id=_KB_DOC_ID,
        user_id=_USER_ID,
        doc_meta={"node_kinds": {}},
    )
    batches = es_mapping.split_bulk_batches(actions)
    flattened = [a for batch in batches for a in batch]
    assert flattened == actions
    assert json.dumps(flattened[0]) == json.dumps(actions[0])  # 可序列化（NDJSON 面前提）


# ---------------------------------------------------------------------------
# es_client 工厂面
# ---------------------------------------------------------------------------


def test_build_client_kwargs_no_auth_when_username_empty():
    """username 空 = 不带 basic_auth（compose 侧 security 关闭形态）。"""
    kwargs = es_client.build_client_kwargs(
        "127.0.0.1", 19200, "", SecretStr("")
    )
    assert kwargs["hosts"] == [{"host": "127.0.0.1", "port": 19200, "scheme": "http"}]
    assert "basic_auth" not in kwargs


def test_build_client_kwargs_auth_only_when_username_set():
    """username 非空才携带 basic_auth（未来开 security 应用零改码）。"""
    kwargs = es_client.build_client_kwargs(
        "127.0.0.1", 19200, "elastic", SecretStr("es-only-secret")
    )
    assert kwargs["basic_auth"] == ("elastic", "es-only-secret")


def test_get_es_client_lazy_singleton(monkeypatch: pytest.MonkeyPatch):
    """get_es_client 惰性单例：两次调用同对象（get_engine/get_kb_minio_client 先例）；
    测试后复位全局持有者（不污染其他用例）。"""
    monkeypatch.setattr(es_client, "_es_client_instance", None)
    client = es_client.get_es_client()
    assert client is es_client.get_es_client()
    monkeypatch.setattr(es_client, "_es_client_instance", None)


# ---------------------------------------------------------------------------
# 重试集合判定（pipeline 层重试的判据面——plan B18 §5.3）
# ---------------------------------------------------------------------------


def _es_api_error(status: int) -> Any:
    """构造 elasticsearch ApiError 族异常（带 meta.status，零网络）。

    elasticsearch-py 8.4.1 × elastic-transport 8.19 异常形态：HTTP 错误 = ApiError
    族（status 在 meta.status，兼容属性 status_code）；连接失败 = ConnectionError /
    ConnectionTimeout 族（无 status）。
    """
    from elastic_transport import ApiResponseMeta
    from elasticsearch.exceptions import ApiError as EsApiError

    meta = ApiResponseMeta(
        status=status, http_version="1.1", headers={}, duration=0, node=None
    )
    return EsApiError("boom", meta, {})


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_is_retryable_http_5xx_and_429(status: int):
    assert es_indexer.is_retryable_es_error(_es_api_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_is_not_retryable_http_client_errors(status: int):
    assert es_indexer.is_retryable_es_error(_es_api_error(status)) is False


def test_is_retryable_connection_family():
    from elastic_transport import ConnectionTimeout

    assert es_indexer.is_retryable_es_error(EsConnectionError("connection refused")) is True
    assert es_indexer.is_retryable_es_error(ConnectionTimeout("timed out")) is True


def test_is_not_retryable_generic_exception():
    assert es_indexer.is_retryable_es_error(RuntimeError("logic bug")) is False
    assert es_indexer.is_retryable_es_error(es_indexer.EsIndexError("bulk 失败 1/2 item")) is False


# ---------------------------------------------------------------------------
# es_indexer 操作面（假 client 零网络）
# ---------------------------------------------------------------------------


class _FakeIndices:
    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.created: list[dict[str, Any]] = []

    async def exists(self, index: str) -> bool:
        return self._exists

    async def create(self, index: str, *, settings: Any = None, mappings: Any = None) -> Any:
        self.created.append({"index": index, "settings": settings, "mappings": mappings})
        return {"acknowledged": True}


class _FakeEsClient:
    """AsyncElasticsearch 假体：indices/delete_by_query/count 三面记录调用（零网络）。"""

    def __init__(self, index_exists: bool = False, *, dbq_failures: list | None = None) -> None:
        self.indices = _FakeIndices(index_exists)
        self.dbq_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []
        self._dbq_failures = dbq_failures or []

    async def delete_by_query(self, index: str, **kwargs: Any) -> dict[str, Any]:
        self.dbq_calls.append({"index": index, **kwargs})
        return {"failures": list(self._dbq_failures)}

    async def count(self, index: str, **kwargs: Any) -> dict[str, Any]:
        self.count_calls.append({"index": index, **kwargs})
        return {"count": 3}


def _make_indexer(client: _FakeEsClient) -> es_indexer.KbEsIndexer:
    return es_indexer.KbEsIndexer(client=lambda: client)


async def test_ensure_index_creates_when_missing():
    client = _FakeEsClient(index_exists=False)
    indexer = _make_indexer(client)
    await indexer.ensure_index()
    assert client.indices.created == [
        {
            "index": "ai_search_kb_chunks",
            "settings": es_mapping.INDEX_SETTINGS,
            "mappings": es_mapping.INDEX_MAPPINGS,
        }
    ]


async def test_ensure_index_idempotent_when_exists():
    """索引已存在 → 零 create（幂等；mapping 变更不自动迁移——OQ-28 重建语义）。"""
    client = _FakeEsClient(index_exists=True)
    indexer = _make_indexer(client)
    await indexer.ensure_index()
    await indexer.ensure_index()
    assert client.indices.created == []


async def test_delete_document_sends_term_query_and_passes_on_no_failures():
    client = _FakeEsClient()
    indexer = _make_indexer(client)
    await indexer.delete_document(kb_doc_id=_KB_DOC_ID, user_id=_USER_ID)
    assert len(client.dbq_calls) == 1
    call = client.dbq_calls[0]
    assert call["index"] == "ai_search_kb_chunks"
    assert call["query"] == {"term": {"kb_doc_id": _KB_DOC_ID}}


async def test_delete_document_raises_on_failures():
    """delete_by_query failures 非空 → EsIndexError（部分分片失败不静默）。"""
    client = _FakeEsClient(dbq_failures=[{"shard": 0, "reason": "x"}])
    indexer = _make_indexer(client)
    with pytest.raises(es_indexer.EsIndexError):
        await indexer.delete_document(kb_doc_id=_KB_DOC_ID, user_id=_USER_ID)


async def test_count_by_doc_term_query():
    client = _FakeEsClient()
    indexer = _make_indexer(client)
    assert await indexer.count_by_doc(_KB_DOC_ID) == 3
    assert client.count_calls[0]["query"] == {"term": {"kb_doc_id": _KB_DOC_ID}}


async def test_index_chunks_deletes_first_then_bulks(monkeypatch: pytest.MonkeyPatch):
    """index_chunks：先 delete_by_query（清旧版残留——重切分不留高序号残影）后 bulk；
    逐批判定 errors 为空即成功（refresh=false ack 即确认）。"""
    client = _FakeEsClient()
    indexer = _make_indexer(client)
    bulk_calls: list[int] = []

    async def _fake_async_bulk(cl: Any, actions: list[dict], **kwargs: Any) -> tuple[int, list]:
        assert kwargs.get("raise_on_error") is False
        assert kwargs.get("refresh") is False
        bulk_calls.append(len(actions))
        return (len(actions), [])

    monkeypatch.setattr(es_indexer, "async_bulk", _fake_async_bulk)
    rows = [_chunk_row(i) for i in range(3)]
    await indexer.index_chunks(
        _KB_DOC_ID, user_id=_USER_ID, chunks=rows, doc_meta={"node_kinds": {}}
    )
    assert bulk_calls == [3]  # 3 条单批（<500/<8MB）
    # 先删后 bulk：delete_by_query 在 bulk 之前恰一次（先删清残留）
    assert len(client.dbq_calls) == 1
    assert client.dbq_calls[0]["query"] == {"term": {"kb_doc_id": _KB_DOC_ID}}


async def test_index_chunks_raises_with_counts_on_item_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    """bulk 逐 item 失败 → EsIndexError 带失败计数（零 chunk 内容回显）。"""
    client = _FakeEsClient()
    indexer = _make_indexer(client)

    async def _fake_async_bulk(cl: Any, actions: list[dict], **kwargs: Any) -> tuple[int, list]:
        return (1, ["error detail"])  # 2 条 1 失败

    monkeypatch.setattr(es_indexer, "async_bulk", _fake_async_bulk)
    with pytest.raises(es_indexer.EsIndexError) as excinfo:
        await indexer.index_chunks(
            _KB_DOC_ID,
            user_id=_USER_ID,
            chunks=[_chunk_row(0), _chunk_row(1)],
            doc_meta={"node_kinds": {}},
        )
    message = str(excinfo.value)
    assert "2" in message and "1" in message  # 失败/总数计数
    assert "切片正文" not in message  # 零 chunk 内容回显（安全纪律）


async def test_index_chunks_empty_chunks_skips_bulk_but_still_cleans(
    monkeypatch: pytest.MonkeyPatch,
):
    """零 chunk 行（异常态防御）：先删后直接成功，零 bulk 调用。"""
    client = _FakeEsClient()
    indexer = _make_indexer(client)

    async def _fake_async_bulk(cl: Any, actions: list[dict], **kwargs: Any) -> tuple[int, list]:
        raise AssertionError("零 chunk 不应有 bulk 调用")

    monkeypatch.setattr(es_indexer, "async_bulk", _fake_async_bulk)
    await indexer.index_chunks(_KB_DOC_ID, user_id=_USER_ID, chunks=[], doc_meta={})
    assert len(client.dbq_calls) == 1


async def test_index_chunks_splits_large_batches(monkeypatch: pytest.MonkeyPatch):
    """bulk 尺寸窗切器接线：601 条 → 两批（500+100）逐批判定。"""
    client = _FakeEsClient()
    indexer = _make_indexer(client)
    batch_sizes: list[int] = []

    async def _fake_async_bulk(cl: Any, actions: list[dict], **kwargs: Any) -> tuple[int, list]:
        batch_sizes.append(len(actions))
        return (len(actions), [])

    monkeypatch.setattr(es_indexer, "async_bulk", _fake_async_bulk)
    rows = [_chunk_row(i) for i in range(601)]
    await indexer.index_chunks(
        _KB_DOC_ID, user_id=_USER_ID, chunks=rows, doc_meta={"node_kinds": {}}
    )
    assert batch_sizes == [500, 101]


def test_get_kb_es_indexer_singleton(monkeypatch: pytest.MonkeyPatch):
    """get_kb_es_indexer 进程级单例（生产装配缝；测试后复位）。"""
    monkeypatch.setattr(es_indexer, "_kb_es_indexer_instance", None)
    indexer = es_indexer.get_kb_es_indexer()
    assert indexer is es_indexer.get_kb_es_indexer()
    monkeypatch.setattr(es_indexer, "_kb_es_indexer_instance", None)


# ---------------------------------------------------------------------------
# P3#1 bulk-item 429/503 分型重试（plan 必答项③建议形态——item-status 分型）
# ---------------------------------------------------------------------------


def _bulk_error_item(status: int) -> dict[str, Any]:
    """async_bulk errors 明细单条形态（helpers 聚合：{op: {_id, status, error}}）。"""
    return {
        "index": {
            "_id": f"{_KB_DOC_ID}:0",
            "status": status,
            "error": {"type": "es_rejected_execution_exception", "reason": "rejected"},
        }
    }


def _make_bulk_error_stubs(statuses: list[int]):
    """async_bulk 缝假体：返回带指定 item status 的错误明细。"""

    async def _fake_async_bulk(cl: Any, actions: list[dict], **kwargs: Any) -> tuple[int, list]:
        return (len(actions) - len(statuses), [_bulk_error_item(s) for s in statuses])

    return _fake_async_bulk


async def test_index_chunks_item_errors_all_429_typed_retryable(
    monkeypatch: pytest.MonkeyPatch,
):
    """失败 item status 全 ∈ {429,502,503,504} → EsIndexError 携带 item_statuses
    （瞬态分型——pipeline 既有 is_retryable_es_error × 1s/3s 退避预算复用，
    不逐 item 重投）。"""
    monkeypatch.setattr(es_indexer, "async_bulk", _make_bulk_error_stubs([429, 429]))
    client = _FakeEsClient()
    indexer = _make_indexer(client)
    with pytest.raises(es_indexer.EsIndexError) as excinfo:
        await indexer.index_chunks(
            _KB_DOC_ID,
            user_id=_USER_ID,
            chunks=[_chunk_row(0), _chunk_row(1)],
            doc_meta={"node_kinds": {}},
        )
    exc = excinfo.value
    assert exc.item_statuses == (429, 429)
    assert "429" in str(exc)  # 状态码留痕（零 chunk 内容回显不变）
    assert "切片正文" not in str(exc)


@pytest.mark.parametrize("statuses", [[429, 400], [413], [400]])
async def test_index_chunks_item_errors_with_client_status_not_retryable(
    statuses, monkeypatch: pytest.MonkeyPatch
):
    """含其他 4xx（400/413 等确定性失败）→ EsIndexError 零分型（item_statuses=()
    ——不可重试立即 failed；消息保持批次 4 计数形态）。"""
    monkeypatch.setattr(es_indexer, "async_bulk", _make_bulk_error_stubs(statuses))
    client = _FakeEsClient()
    indexer = _make_indexer(client)
    with pytest.raises(es_indexer.EsIndexError) as excinfo:
        await indexer.index_chunks(
            _KB_DOC_ID,
            user_id=_USER_ID,
            chunks=[_chunk_row(0), _chunk_row(1)],
            doc_meta={"node_kinds": {}},
        )
    exc = excinfo.value
    assert exc.item_statuses == ()
    message = str(exc)
    assert "429" not in message and "400" not in message and "413" not in message
    # 失败/总数计数形态不变（批次 4 原样）
    assert f"bulk 失败 {len(statuses)}/2 item" in message


async def test_index_chunks_item_error_malformed_item_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
):
    """item 无 status 键（形态异常防御）→ 零分型不可重试（不猜不伪造）。"""

    async def _fake_async_bulk(cl: Any, actions: list[dict], **kwargs: Any) -> tuple[int, list]:
        return (1, [{"index": {"_id": "x", "error": {"type": "boom"}}}])

    monkeypatch.setattr(es_indexer, "async_bulk", _fake_async_bulk)
    client = _FakeEsClient()
    indexer = _make_indexer(client)
    with pytest.raises(es_indexer.EsIndexError) as excinfo:
        await indexer.index_chunks(
            _KB_DOC_ID,
            user_id=_USER_ID,
            chunks=[_chunk_row(0), _chunk_row(1)],
            doc_meta={"node_kinds": {}},
        )
    assert excinfo.value.item_statuses == ()


async def test_is_retryable_es_error_item_typed_vs_exception_level_regression():
    """回归锁死（ask 点名）：item 级分型与批次 4「异常级重试」两判据并存不互扰。

    - 批次 4 语义不变：聚合 EsIndexError（无分型，item_statuses=()）→ 不可重试
      （B18 哨兵——test_kb_pipeline 既有 retry 语义保持绿）；
    - item 级新面：EsIndexError(item_statuses 全瞬态) → retryable；
    - 防御面：分型混入非瞬态（不可能由本模块构造，判据仍拒绝）；
    - 异常级原面不动：ApiError 族（meta.status）/连接族判定由既有参数化用例覆盖。
    """
    typed = es_indexer.EsIndexError("ES 写入失败: bulk 失败 2/2 item status=429")
    typed.item_statuses = (429, 503)
    assert es_indexer.is_retryable_es_error(typed) is True
    untyped = es_indexer.EsIndexError("ES 写入失败: bulk 失败 1/2 item")
    assert untyped.item_statuses == ()
    assert es_indexer.is_retryable_es_error(untyped) is False
    mixed = es_indexer.EsIndexError("mixed")
    mixed.item_statuses = (429, 400)
    assert es_indexer.is_retryable_es_error(mixed) is False
    # 异常级与 item 级互不穿透：ApiError 带 429 仍走异常级判据（True）
    assert es_indexer.is_retryable_es_error(_es_api_error(429)) is True


async def test_delete_document_silent_on_index_not_found(monkeypatch: pytest.MonkeyPatch):
    """P3#2：删除腿 404 index_not_found → 静默返回（灰度期/重建窗口 WARN 噪音
    消除；删除幂等语义——索引不存在即无残留可删）。index_chunks 先删腿被吸收后
    bulk 照常继续。"""
    from elastic_transport import ApiResponseMeta
    from elasticsearch.exceptions import NotFoundError as EsNotFoundError

    meta = ApiResponseMeta(
        status=404, http_version="1.1", headers={}, duration=0, node=None
    )
    client = _FakeEsClient()

    async def _raise_not_found(index: str, **kwargs: Any) -> dict[str, Any]:
        client.dbq_calls.append({"index": index, **kwargs})
        raise EsNotFoundError("index_not_found_exception", meta, {})

    client.delete_by_query = _raise_not_found
    indexer = _make_indexer(client)
    await indexer.delete_document(kb_doc_id=_KB_DOC_ID, user_id=_USER_ID)  # 不抛

    # index_chunks 先删腿静默短路后继续 bulk（404 不再打断写入腿）
    async def _ok_bulk(cl: Any, actions: list[dict], **kwargs: Any) -> tuple[int, list]:
        return (len(actions), [])

    monkeypatch.setattr(es_indexer, "async_bulk", _ok_bulk)
    await indexer.index_chunks(
        _KB_DOC_ID,
        user_id=_USER_ID,
        chunks=[_chunk_row(0)],
        doc_meta={"node_kinds": {}},
    )
    assert client.dbq_calls  # 删除腿确实发起过（404 被吸收）


async def test_delete_document_other_api_error_propagates():
    """非 404 ApiError（如 500）原样上抛——短路仅覆盖 index_not_found 面。"""
    client = _FakeEsClient()

    async def _raise_500(index: str, **kwargs: Any) -> dict[str, Any]:
        raise _es_api_error(500)

    client.delete_by_query = _raise_500
    indexer = _make_indexer(client)
    with pytest.raises(Exception) as excinfo:
        await indexer.delete_document(kb_doc_id=_KB_DOC_ID, user_id=_USER_ID)
    assert es_indexer._es_error_status(excinfo.value) == 500
