"""ORM 表模型包（占位，B1 起按 data-model.md DDL 契约逐表落地）。

规划表（ARCH §3.1）：user / thread / turn / turn_event / source / snapshot /
kb_document / kb_chunk / artifact / offload_chunk(M4) / eval_*。
模型落地后须在迁移序列（migrations/versions/）同步出对应版本。
"""
