"""KB ES 索引器包（M3 批次 4 / B18——data-model §1.19 事实源分层条款指定落点）。

mapping 细节的唯一事实源：索引名/settings/mapping/bulk action 构造在 es_mapping
（纯函数 unit 直驱）；客户端工厂在 es_client；操作面（ensure_index / index_chunks /
delete_document / count_by_doc）在 es_indexer。B19 混合检索消费侧的 BM25 查询构造
不在本包（写入通道只管写入）。
"""
