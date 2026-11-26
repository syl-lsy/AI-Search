"""KbEsIndexer：KB ES BM25 写入通道操作面（plan B18 §4.4——四方法协议）。

| 方法 | 语义 |
|---|---|
| ensure_index | index exists? 否则 create（幂等；mapping 变更不自动迁移——OQ-28 重建语义） |
| index_chunks | 先 delete_by_query（清旧版残留）→ bulk（窗切逐批）→ 逐批 errors 判定 |
| delete_document | delete_by_query 按 kb_doc_id（签名与 import_worker.py 级联缝钉死一致） |
| count_by_doc | _count term 查询（回填 skip 判定 + audit） |

失败语义：bulk 逐 item 失败 / delete_by_query failures 非空 → EsIndexError
（detail 仅计数零 chunk 内容回显）；连接类/429/5xx 异常级 + **bulk item 全瞬态
分型（B19 必答项③：item status ∈ {429,502,503,504} → item_statuses 随异常携带，
`is_retryable_es_error` 识别）** 可 retryable 判定供 pipeline 层重试
（plan B18 §5.3 / B19 §5 必答项③——重试中状态仍 indexing，耗尽 →
mark_failed("indexing: ES 写入失败: …") 不静默 ready）；删除腿 404
index_not_found 短路静默（P3#2——删除幂等语义）。
"""

from typing import Any, Callable

from elastic_transport import ConnectionError as EsConnectionError
from elastic_transport import ConnectionTimeout as EsConnectionTimeout
from app.core.logger import BusinessLog
from app.rag.indexers.es_client import get_es_client
from app.rag.indexers.es_mapping import (
    INDEX_MAPPINGS,
    INDEX_SETTINGS,
    build_bulk_actions,
    index_name,
    split_bulk_batches,
)

#: helpers.async_bulk（elasticsearch 8.4.1）——逐 item 错误聚合是失败判定的依赖面
#: （plan B18 §4.5：自研易错被否）；module 级名字供单测 seam 替换
from elasticsearch.helpers import async_bulk  # noqa: F401 —— re-export（测试缝）
from elasticsearch.exceptions import NotFoundError as EsNotFoundError

_kb_es_indexer_instance: "KbEsIndexer | None" = None

#: 连接类异常族（连接拒/超时——retryable；plan B18 §5.3「连接拒/超时」）
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (EsConnectionError, EsConnectionTimeout)

#: bulk item 瞬态失败 status 分型集合（plan B19 必答项③：429/503 与连接类同属
#: 瞬态——ES 索引段重试预算已在位零新增；400/413 等其他 4xx=确定性失败不重试）
_RETRYABLE_BULK_ITEM_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})


class EsIndexError(Exception):
    """ES 写入通道业务失败（detail 仅类型名/状态码/计数——零 chunk 内容回显）。

    `item_statuses`：bulk 逐 item 失败的 status 元组（plan B19 必答项③ item 级
    分型）——**仅当全部失败 item status ∈ `_RETRYABLE_BULK_ITEM_STATUSES` 时非
    空**（`is_retryable_es_error` 据此识别为瞬态，复用 pipeline 既有 1s/3s 退避
    预算，不逐 item 重投）；缺省空元组 = 批次 4 原语义（聚合失败不可重试）。
    """

    item_statuses: tuple[int, ...] = ()


def _es_error_status(exc: BaseException) -> int | None:
    """ApiError 族异常的 HTTP 状态码提取（兼容属性 → meta.status 兜底）。"""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    meta = getattr(exc, "meta", None)
    status = getattr(meta, "status", None)
    return status if isinstance(status, int) else None


def _bulk_item_statuses(error_items: list[Any]) -> tuple[int, ...]:
    """async_bulk 错误明细的 item status 提取（{op: {_id, status, error}} 形态）。

    非 dict item / op 体缺 status / status 非整数 → 该条不计（防御不猜不伪造）。
    """
    statuses: list[int] = []
    for item in error_items:
        if not isinstance(item, dict):
            continue
        for op_body in item.values():
            if not isinstance(op_body, dict):
                continue
            status = op_body.get("status")
            if isinstance(status, int) and not isinstance(status, bool):
                statuses.append(status)
            break
    return tuple(statuses)


def is_retryable_es_error(exc: BaseException) -> bool:
    """retryable 集合判定（plan B18 §5.3 + B19 必答项③ item 级分型）。

    - 连接族（拒/超时）+ HTTP 429/5xx（ApiError 族，status 在 meta.status /
      兼容属性 status_code）——批次 4 异常级原面不变；
    - **item 级（B19 新面）**：EsIndexError 携带非空 item_statuses 且全部 ∈
      {429,502,503,504} → retryable（pipeline 既有 1s/3s 退避预算复用）；空元组
      = 聚合失败原语义不可重试；混入任何非瞬态 status → 拒绝（防御面）。

    400 等请求错误不可 retryable（重试无收益——直接 failed 留原因）。
    """
    if isinstance(exc, _CONNECTION_ERRORS):
        return True
    if isinstance(exc, EsIndexError) and exc.item_statuses:
        return all(
            status in _RETRYABLE_BULK_ITEM_STATUSES for status in exc.item_statuses
        )
    status = _es_error_status(exc)
    return isinstance(status, int) and (status == 429 or 500 <= status <= 599)


class KbEsIndexer:
    """KB ES 索引器（client 工厂全注入——单测假 client 零网络；生产经
    `get_kb_es_indexer` 装配单例）。"""

    def __init__(self, *, client: Callable[[], Any] | None = None) -> None:
        self._client_factory = client or get_es_client

    def _client(self) -> Any:
        return self._client_factory()

    async def ensure_index(self) -> None:
        """索引确保（幂等）：不存在才 create——mapping 变更不自动迁移（OQ-28：
        pg 事实源、ES 可整体重建——rebuild 模式负责重建 mapping）。"""
        client = self._client()
        name = index_name()
        if not await client.indices.exists(index=name):
            await client.indices.create(
                index=name, settings=INDEX_SETTINGS, mappings=INDEX_MAPPINGS
            )
            BusinessLog(
                message=f"kb ES 索引已创建（index={name}）",
                action="kb_es_ensure_index",
                entity="kb_index",
                entity_id=name,
            ).info()

    async def index_chunks(
        self,
        kb_doc_id: Any,
        *,
        user_id: Any,
        chunks: list[dict[str, Any]],
        doc_meta: dict[str, Any],
    ) -> None:
        """先删后 bulk（重切分 chunk 数变少不留高序号残影）→ 逐批 errors 判定。

        refresh=false 不等可见——ack 即确认（B19 读路径容忍 ≤1s refresh 滞后）。
        任一 item 失败 → EsIndexError（带失败计数；pipeline 转 failed 不静默 ready）。
        """
        await self.delete_document(kb_doc_id=kb_doc_id, user_id=user_id)
        actions = build_bulk_actions(
            chunks, kb_doc_id=kb_doc_id, user_id=user_id, doc_meta=doc_meta
        )
        if not actions:
            return  # 零 chunk（异常态防御）：清理残留后直接成功
        client = self._client()
        total = len(actions)
        for batch in split_bulk_batches(actions):
            successes, errors = await async_bulk(
                client, batch, raise_on_error=False, refresh=False
            )
        # async_bulk 双形态返回（stats_only False/True 重载）——list 形态才有
        # 逐 item 明细；非 list 视为零失败（stats 形态本通道不使用，防御性兜底）
        error_items = errors if isinstance(errors, list) else []
        if error_items:
            item_statuses = _bulk_item_statuses(error_items)
            if item_statuses and all(
                status in _RETRYABLE_BULK_ITEM_STATUSES for status in item_statuses
            ):
                # B19 必答项③ item 级分型：全部失败 item ∈ 瞬态集合 → 携带
                # item_statuses（pipeline 既有退避预算复用；整文档重发幂等——
                # 先删后 bulk + 确定性 _id，不逐 item 重投）
                statuses_text = "/".join(str(s) for s in sorted(set(item_statuses)))
                failure = EsIndexError(
                    f"ES 写入失败: bulk 失败 {len(error_items)}/{total} item"
                    f"（成功 {successes}）status={statuses_text}（瞬态 item 分型）"
                )
                failure.item_statuses = item_statuses
                raise failure
            raise EsIndexError(
                f"ES 写入失败: bulk 失败 {len(error_items)}/{total} item"
                f"（成功 {successes}）"
            )

    async def delete_document(self, kb_doc_id: Any, *, user_id: Any) -> None:
        """按 kb_doc_id 删（user_id 留缝签名对齐——kb_doc_id 为 UUID 全局唯一寻址）；
        failures 非空 → EsIndexError（部分分片失败不静默）。

        404 index_not_found 短路（P3#2，b4 §三 #4）：删除幂等语义——索引不存在
        即无残留可删，静默返回（灰度期/重建窗口的 WARN 噪音消除）；其余 ApiError
        原样上抛（异常级重试判据照常消费）。
        """
        client = self._client()
        try:
            response = await client.delete_by_query(
                index=index_name(),
                query={"term": {"kb_doc_id": str(kb_doc_id)}},
            )
        except EsNotFoundError:
            return
        # ObjectApiResponse 逐键访问 pyright 不按 key 收窄（联合类型）——物化为
        # dict 后 .get（__iter__ 产出键序，微小体量拷贝可忽略）
        body: dict[str, Any] = dict(response)
        failures = body.get("failures") or []
        if failures:
            raise EsIndexError(f"ES 写入失败: delete_by_query failures={len(failures)}")

    async def count_by_doc(self, kb_doc_id: Any) -> int:
        """_count term 查询（回填 skip 判定 + audit 对账）。"""
        client = self._client()
        response = await client.count(
            index=index_name(), query={"term": {"kb_doc_id": str(kb_doc_id)}}
        )
        return int(response["count"])


def get_kb_es_indexer() -> KbEsIndexer:
    """进程级单例（pipeline / KbService / import_worker 三面装配缝）。"""
    global _kb_es_indexer_instance
    if _kb_es_indexer_instance is None:
        _kb_es_indexer_instance = KbEsIndexer(client=get_es_client)
    return _kb_es_indexer_instance
