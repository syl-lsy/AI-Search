"""integration：KB ES BM25 写入通道真链路（app/rag/indexers/，B18 §8.2）。

覆盖面（真 ES 127.0.0.1:19200 + 真 pg；skip 门控非绿不可的是 unit 面）：
- ensure_index 幂等（二次不报错、mapping 不漂）；
- 真库建 1 文档 3 chunks（requires_db + requires_es 双门控）→ index_chunks →
  `_count` ==3 → 重跑 index_chunks（resume 模拟）仍 ==3——确定性 _id 幂等实测；
- jieba analyzer `_analyze` 黄金断言（`中华人民共和国国歌` → ['中华人民共和国', '国歌']，
  探针 m3-probe-es-jieba §5.1 输出逐字锁定——分词器回归哨兵）；
- user_id term 隔离（他 user 查询零命中——契约 §1.19「term 过滤恒携带」）；
- delete_document 后 `_count` ==0；ES 停机形态（client 指错误端口）→
  delete_document 抛异常（供 best-effort 上层捕获）。

测试索引用专用前缀 `ai_search_it_es_channel`（隔离生产索引），用前后删索引；
真库 kb 行自管清理（kb_documents/kb_chunks 不在 conftest 截断清单内）。
ES 不可达/鉴权不通过 → pytest.skip（给 reason，不让命令红掉）。
"""

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.requires_es

_INDEX_PREFIX = "ai_search_it_es_channel"
_USER_ID = "00000000-0000-4000-8000-000000000001"
_OTHER_USER_ID = "99999999-9999-4999-8999-999999999999"
_DOC_ID = uuid.uuid4()


@pytest.fixture(scope="session")
def es_gate() -> None:
    """真 ES 可达性门控（requires_es 用例专用）：带凭据探测；不可达/鉴权不通过 → skip。

    凭据形态复用 build_client_kwargs 口径（username 非空才带 basic auth）；
    401 不冒充「不可达」——skip 理由单列凭据面并 warnings.warn 出口提示
    （xpack security 开启后无凭据探测必 401）；连接类异常收窄
    （TransportError/OSError），编程错误不再冒充「不可达」。
    """
    import warnings

    import httpx

    from app.core.config import es_settings

    base = f"http://{es_settings.host}:{es_settings.port}"
    auth: tuple[str, str] | None = None
    if es_settings.username:
        auth = (es_settings.username, es_settings.password.get_secret_value())
    creds_missing = auth is None or not es_settings.password.get_secret_value()
    try:
        response = httpx.get(base, timeout=3.0, auth=auth)
    except (httpx.TransportError, OSError) as exc:
        pytest.skip(
            f"ES 不可达（{base}；{type(exc).__name__}；make infra-up 起 elasticsearch）"
            "——requires_es 用例跳过"
        )
    if response.status_code == 401:
        if creds_missing:
            reason = (
                f"ES 鉴权不通过（401；{base}）——凭据未配置（username 或 password 为空）"
                "——requires_es 用例跳过"
            )
        else:
            reason = (
                f"ES 鉴权不通过（401；{base}）——凭据与容器 ELASTIC_PASSWORD 不一致"
                "——requires_es 用例跳过"
            )
        warnings.warn(reason)
        pytest.skip(reason)
    if response.status_code >= 400:
        pytest.skip(
            f"ES 探测异常（HTTP {response.status_code}；{base}）——requires_es 用例跳过"
        )


def _make_client(**overrides: Any) -> Any:
    from elasticsearch import AsyncElasticsearch

    from app.core.config import es_settings
    from app.rag.indexers.es_client import build_client_kwargs

    kwargs = build_client_kwargs(
        es_settings.host, es_settings.port, es_settings.username, es_settings.password
    )
    kwargs.update(overrides)
    return AsyncElasticsearch(**kwargs)


@pytest.fixture
async def es_env(monkeypatch: pytest.MonkeyPatch, es_gate: None) -> Any:
    """测试索引隔离：专用前缀 + 用前后删索引；yield 出已配置 client（用后关闭）。"""
    from app.core.config import es_settings
    from app.rag.indexers import es_mapping

    monkeypatch.setattr(es_settings, "index_prefix", _INDEX_PREFIX)
    index = es_mapping.index_name()
    client = _make_client()
    if await client.indices.exists(index=index):
        await client.indices.delete(index=index)
    try:
        yield client
    finally:
        if await client.indices.exists(index=index):
            await client.indices.delete(index=index)
        await client.close()


def _chunk(index: int) -> Any:
    from app.rag.chunker import Chunk

    return Chunk(
        document_id=str(_DOC_ID),
        chunk_index=index,
        content=f"知识库切片正文第{index}段：机器学习改变搜索引擎的知识库检索。",
        token_count=20,
        heading_path=["示例文档", f"第一章·第{index}节"],
        kind="paragraph",
        raw_type="p",
        node_id=f"loc:blk:{index}",
        loc={"page": index + 1},
        references=[],
        char_start=index * 50,
        char_end=index * 50 + 30,
        content_hash="c" * 64,
    )


@pytest.fixture
async def kb_doc_3chunks(migrated_db: Any) -> Any:
    """真库 1 文档 + 3 chunks（pg 事实源走真 replace_chunks 写入）；用后自管清理。"""
    from sqlalchemy import delete

    from app.db.models import KbDocument
    from app.db.session import get_session_factory
    from app.rag.pipeline import KbDocumentRepository

    async with get_session_factory()() as session:
        session.add(
            KbDocument(
                id=_DOC_ID,
                user_id=uuid.UUID(_USER_ID),
                doc_type="md",
                title="ES 通道集成示例",
                status="indexing",
                chunk_count=0,
            )
        )
        await session.commit()
    repo = KbDocumentRepository()
    await repo.replace_chunks(
        _DOC_ID, user_id=_USER_ID, chunks=[_chunk(i) for i in range(3)], content_hash="c" * 64
    )
    try:
        yield repo
    finally:
        async with get_session_factory()() as session:
            await session.execute(delete(KbDocument).where(KbDocument.id == _DOC_ID))
            await session.commit()


async def test_ensure_index_idempotent(es_env: Any) -> None:
    """ensure_index 二次调用不报错、mapping 不漂（analyzer 面仍在位）。"""
    from app.rag.indexers import es_mapping
    from app.rag.indexers.es_indexer import KbEsIndexer

    indexer = KbEsIndexer(client=lambda: es_env)
    await indexer.ensure_index()
    await indexer.ensure_index()  # 幂等：已存在零 create
    mapping = await es_env.indices.get_mapping(index=es_mapping.index_name())
    props = mapping[es_mapping.index_name()]["mappings"]["properties"]
    assert props["content"]["analyzer"] == "jieba_index"
    assert props["content"]["search_analyzer"] == "jieba_search"


async def test_index_chunks_count_and_rerun_idempotent(
    es_env: Any, kb_doc_3chunks: Any
) -> None:
    """真库 3 chunks → index_chunks → count==3；重跑（resume 模拟）仍 ==3。"""
    from app.rag.indexers.es_indexer import KbEsIndexer

    indexer = KbEsIndexer(client=lambda: es_env)
    await indexer.ensure_index()
    rows = await kb_doc_3chunks.list_chunks_by_doc(_DOC_ID)
    assert len(rows) == 3
    doc_meta = {
        "catalog_path": None,
        "doc_kind": "upload",
        "node_kinds": {"loc:blk:0": "paragraph", "loc:blk:1": "paragraph", "loc:blk:2": "paragraph"},
    }
    await indexer.index_chunks(_DOC_ID, user_id=_USER_ID, chunks=rows, doc_meta=doc_meta)
    await es_env.indices.refresh(index=es_mapping_index())  # ack≠可见——refresh 后断言
    assert await indexer.count_by_doc(_DOC_ID) == 3
    # 重跑（resume-from-indexing 同形态）：确定性 _id 覆盖 → 零重复
    await indexer.index_chunks(_DOC_ID, user_id=_USER_ID, chunks=rows, doc_meta=doc_meta)
    await es_env.indices.refresh(index=es_mapping_index())
    assert await indexer.count_by_doc(_DOC_ID) == 3


async def test_jieba_analyzer_golden_tokens(es_env: Any) -> None:
    """jieba_search 分词黄金断言（探针 §5.1 逐字锁定）：词级分词非单字撒网。"""
    from app.rag.indexers import es_mapping

    await es_env.indices.create(
        index=es_mapping.index_name(),
        settings=es_mapping.INDEX_SETTINGS,
        mappings=es_mapping.INDEX_MAPPINGS,
    )
    response = await es_env.indices.analyze(
        index=es_mapping.index_name(), analyzer="jieba_search", text="中华人民共和国国歌"
    )
    tokens = [t["token"] for t in response["tokens"]]
    assert tokens == ["中华人民共和国", "国歌"]


async def test_user_id_term_isolation(es_env: Any, kb_doc_3chunks: Any) -> None:
    """user_id term 隔离：他 user 检索零命中、属主检索命中（契约 §1.19）。"""
    from app.rag.indexers.es_indexer import KbEsIndexer

    indexer = KbEsIndexer(client=lambda: es_env)
    await indexer.ensure_index()
    rows = await kb_doc_3chunks.list_chunks_by_doc(_DOC_ID)
    await indexer.index_chunks(
        _DOC_ID, user_id=_USER_ID, chunks=rows, doc_meta={"node_kinds": {}}
    )
    await es_env.indices.refresh(index=es_mapping_index())
    query = {"match": {"content": "机器学习"}}
    own = await es_env.search(
        index=es_mapping_index(),
        query={"bool": {"must": [query], "filter": [{"term": {"user_id": _USER_ID}}]}},
    )
    other = await es_env.search(
        index=es_mapping_index(),
        query={"bool": {"must": [query], "filter": [{"term": {"user_id": _OTHER_USER_ID}}]}},
    )
    assert own["hits"]["total"]["value"] == 3
    assert other["hits"]["total"]["value"] == 0  # 他 user 零命中


def es_mapping_index() -> str:
    from app.rag.indexers import es_mapping

    return es_mapping.index_name()


async def test_delete_document_clears_and_down_shape(
    es_env: Any, kb_doc_3chunks: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete_document 后 count==0；ES 停机形态（错误端口）→ delete_document 抛异常。"""
    from elastic_transport import ConnectionError as EsConnectionError

    from app.rag.indexers.es_indexer import KbEsIndexer

    indexer = KbEsIndexer(client=lambda: es_env)
    await indexer.ensure_index()
    rows = await kb_doc_3chunks.list_chunks_by_doc(_DOC_ID)
    await indexer.index_chunks(
        _DOC_ID, user_id=_USER_ID, chunks=rows, doc_meta={"node_kinds": {}}
    )
    await es_env.indices.refresh(index=es_mapping_index())
    assert await indexer.count_by_doc(_DOC_ID) == 3
    await indexer.delete_document(kb_doc_id=_DOC_ID, user_id=_USER_ID)
    await es_env.indices.refresh(index=es_mapping_index())
    assert await indexer.count_by_doc(_DOC_ID) == 0

    # 停机形态：client 指错误端口（连接拒绝）→ delete_document 抛连接异常（上层 best-effort 捕获面）
    from elasticsearch import AsyncElasticsearch

    from app.core.config import es_settings

    down_client = AsyncElasticsearch(
        hosts=[{"host": es_settings.host, "port": 1, "scheme": "http"}],
        max_retries=0,
        request_timeout=1,
    )
    down_indexer = KbEsIndexer(client=lambda: down_client)
    try:
        with pytest.raises(EsConnectionError):
            await down_indexer.delete_document(kb_doc_id=_DOC_ID, user_id=_USER_ID)
    finally:
        await down_client.close()
