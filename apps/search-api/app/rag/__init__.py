"""RAG 管线包：归一化 → 树 IR → 逐级切分 → 上传流水线 → 存储。

对应契约：data-model.md §1.18（统一完全树 IR）与 ARCHITECTURE ADR-018
（格式路由归一化管线）。本包承载 KB 入库管线（normalizers / ir_schema /
tree_assembler / chunker / pipeline / storage）及其 REST 业务层 service.py；
归属图外组件（ADR-011 图外组件再例），不入主图拓扑。
"""
