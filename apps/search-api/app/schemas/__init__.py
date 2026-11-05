"""Pydantic DTO 层（请求/响应模型）——与 db/models/（ORM）严格分离，互不混用。

基座口径本目录为 app/schema/，按 AI Agent 模板口径改名 app/schemas/。
规划模型族（ARCH §3.1，B2 起按 contract/api.md 落地）：Thread* / Turn* / Source* /
Kb* / Artifact* / Sse*；Suggestion*（v1.17）。
"""
