"""ES 索引设计纯函数面（plan B18 §4 决策三——mapping 细节唯一事实源）。

契约锚点：data-model §1.19——索引名 `{prefix}_kb_chunks`；字段要点
chunk_id/kb_doc_id/user_id(keyword, term 过滤恒携带)/content(text, jieba)/
heading_path(text 独立字段+keyword 副本)/kind·raw_type(keyword)/
chunk_index·char_start·char_end·created_at；pg 事实源、ES 可整体重建（OQ-28）。

零自定义 analysis：`jieba_index`（索引时）/`jieba_search`（检索时）两 analyzer 由
jieba 插件注册（探针 m3-probe-es-jieba §5.1 实测名），mapping 直接引用——零自定义
analysis 即零 analyzer 配置漂移面。

`_id = {kb_doc_id}:{chunk_index}` 确定性复合键：重写幂等（同 id 覆盖不重复）；
pg 侧 `uq_kb_chunks_doc_idx`（(kb_doc_id, chunk_index) 唯一）保证 1:1 映射零碰撞。
"""

import json
from datetime import datetime
from typing import Any

from app.core.config import es_settings

#: bulk 尺寸窗切护栏（plan B18 §7.2——防御异常大文档；先到者切分）
BULK_MAX_ACTIONS = 500
BULK_MAX_BYTES = 8 * 1024 * 1024

#: bulk action NDJSON 构造的 op 形态（helpers.async_bulk 原生 dict 形态）
_BULK_OP_TYPE = "index"


def index_name(prefix: str | None = None) -> str:
    """索引名 `{prefix}_kb_chunks`（契约 §1.19 字面；缺省前缀 ai_search）。"""
    return f"{prefix or es_settings.index_prefix}_kb_chunks"


#: 索引 settings（plan B18 §4.2）：单分片/零副本（单节点 health 才能到 green——
#: healthcheck yellow 兜索引未创建中间态）/refresh 1s（缺省显式化，B19 读路径
#: 容忍亚秒可见性滞后）；analysis 段零自定义（jieba 两 analyzer 由插件注册）
INDEX_SETTINGS: dict[str, Any] = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "refresh_interval": "1s",
}

#: mapping（plan B18 §4.3，与 kb_chunks 行 + meta 七键 + 文档级两键对齐）：
#: loc/references 用 enabled:false（存 _source 不建索引——溯源 O(1) 契约走
#: kb_chunks.meta 物化字段，ES 副本仅为 _source 可读性）；doc_kind 为文档级
#: 辅助过滤字段（IR root 派生，不可达时 null 不阻断）；kind 为 chunk 首来源
#: 单元统一类型（IR node.type 派生——与 chunker 同一事实源，pg 读回经
#: node_kinds 映射注入）。
INDEX_MAPPINGS: dict[str, Any] = {
    "properties": {
        "chunk_id": {"type": "keyword"},  # = kb_chunks.id（str(UUID)）
        "kb_doc_id": {"type": "keyword"},  # 删除/重建按文档寻址键
        "user_id": {"type": "keyword"},  # term 过滤恒携带（契约 §1.19；REQ-011/050）
        "content": {
            "type": "text",
            "analyzer": "jieba_index",
            "search_analyzer": "jieba_search",
            # §11 英文词法加固（plan-m3-b19 §11，2026-11-26 用户裁决 multi-fields
            # 双查）：en 子字段走 ES 内置 english analyzer（standard tokenizer+
            # lowercase+porter 词干化）——词干形态差（limits/limit 类）词法可命中，
            # 零新依赖零自定义 analysis（settings 零 analysis 段不变）；multi_match
            # 逐字段用各自 search_analyzer 分析查询→同份 query 天然双路（plan
            # §11.2）；heading_path 不加（轨迹中文形态为主，slice 观察项不扩）
            "fields": {"en": {"type": "text", "analyzer": "english"}},
        },
        "heading_path": {  # 标题链（root→leaf join，分隔符 " > "）
            "type": "text",
            "analyzer": "jieba_index",
            "search_analyzer": "jieba_search",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
        },
        "catalog_path": {  # 文档级目录链（kb_documents.catalog_path 原值）
            "type": "keyword",
            "fields": {
                "txt": {
                    "type": "text",
                    "analyzer": "jieba_index",
                    "search_analyzer": "jieba_search",
                }
            },
        },
        "doc_kind": {"type": "keyword"},  # 文档级（IR root.doc_kind 六值；可空）
        "kind": {"type": "keyword"},  # chunk 首来源单元统一类型（IR node.type 派生）
        "raw_type": {"type": "keyword"},  # meta 七键（str() 归一后写入）
        "chunk_index": {"type": "integer"},
        "char_start": {"type": "integer"},  # meta 七键
        "char_end": {"type": "integer"},  # meta 七键
        "token_count": {"type": "integer"},  # kb_chunks 行列（B19 截断预判可用）
        "created_at": {"type": "date"},  # kb_chunks.created_at（契约 §1.19）
        "node_id": {"type": "keyword"},  # meta 七键（溯源寻址）
        "loc": {"type": "object", "enabled": False},  # meta 七键——只入 _source
        "references": {"type": "object", "enabled": False},  # meta 七键——同上
    }
}


def es_doc_id(kb_doc_id: Any, chunk_index: int) -> str:
    """确定性复合键 `{kb_doc_id}:{chunk_index}`（重写幂等/对账直查）。"""
    return f"{kb_doc_id}:{chunk_index}"


def build_chunk_doc(
    row: dict[str, Any], *, kb_doc_id: Any, user_id: Any, doc_meta: dict[str, Any]
) -> dict[str, Any]:
    """kb_chunks 行投影（list_chunks_by_doc 形态）→ ES doc 字段集。

    meta 七键逐键映射：heading_path（" > " join）/node_id/loc/references/
    raw_type（str() 归一）/char_start/char_end；doc 级三键从 doc_meta 注入：
    catalog_path（kb_documents.catalog_path）、doc_kind（IR root）、kind
    （node_kinds[node_id]——IR node.type 派生，与 chunker Chunk.kind 同一
    事实源 node.type.value）。缺键落 None 不伪造（keyword/text 可空）。
    """
    meta: dict[str, Any] = row.get("meta") or {}
    node_id = meta.get("node_id")
    node_kinds: dict[str, str] = doc_meta.get("node_kinds") or {}
    heading_path = meta.get("heading_path") or []
    created_at = row.get("created_at")
    raw_type = meta.get("raw_type")
    return {
        "chunk_id": str(row["chunk_id"]),
        "kb_doc_id": str(kb_doc_id),
        "user_id": str(user_id),
        "content": row.get("content"),
        "heading_path": " > ".join(str(part) for part in heading_path) if heading_path else None,
        "catalog_path": doc_meta.get("catalog_path"),
        "doc_kind": doc_meta.get("doc_kind"),
        "kind": node_kinds.get(str(node_id)) if node_id else None,
        "raw_type": str(raw_type) if raw_type is not None else None,
        "chunk_index": row.get("chunk_index"),
        "char_start": meta.get("char_start"),
        "char_end": meta.get("char_end"),
        "token_count": row.get("token_count"),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "node_id": node_id,
        "loc": meta.get("loc"),
        "references": meta.get("references"),
    }


def build_bulk_actions(
    rows: list[dict[str, Any]],
    *,
    kb_doc_id: Any,
    user_id: Any,
    doc_meta: dict[str, Any],
    index: str | None = None,
) -> list[dict[str, Any]]:
    """行集 → bulk actions（helpers.async_bulk 原生形态：_op_type/_index/_id + 源字段）。"""
    target = index or index_name()
    return [
        {
            "_op_type": _BULK_OP_TYPE,
            "_index": target,
            "_id": es_doc_id(kb_doc_id, row["chunk_index"]),
            **build_chunk_doc(row, kb_doc_id=kb_doc_id, user_id=user_id, doc_meta=doc_meta),
        }
        for row in rows
    ]


def split_bulk_batches(
    actions: list[dict[str, Any]],
    *,
    max_actions: int = BULK_MAX_ACTIONS,
    max_bytes: int = BULK_MAX_BYTES,
) -> list[list[dict[str, Any]]]:
    """bulk 尺寸窗切器（≤max_actions 条或 ≤max_bytes 序列化字节，先到者切分）。

    单条超限独立成批（不可再切——不丢数据）；空输入返回空列表；切分保持原序
    （拼接回全集=原序，零丢失零重排）。
    """
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for action in actions:
        size = len(json.dumps(action, ensure_ascii=False, default=str).encode("utf-8"))
        if current and (
            len(current) >= max_actions or current_bytes + size > max_bytes
        ):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(action)
        current_bytes += size
    if current:
        batches.append(current)
    return batches
