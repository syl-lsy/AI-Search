"""轮级可观测 callbacks（Langfuse trace root=轮，session=thread_id——ARCH §6.6）。

LANGFUSE_ 未配置（单测/未接观测部署形态）→ 空表零开销；SDK 异常 → 告警回落空表
（观测不阻塞主链路）。SDK 客户端走环境变量（LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST，
与 core/config LANGFUSE_ 组同名——pydantic-settings 不导出 os.environ，此处把 settings
值回填 env 供 langfuse v3 全局客户端读取）；trace id = turn_id（对账键：Langfuse
trace 与 turns 行同 id 可查）；session=thread_id 由 RunnableConfig metadata 的
langfuse_* 键承载（langfuse-langchain v3 契约键，TurnRunner 注入）。
"""

import os
from typing import Any

from app.core.config import langfuse_settings
from app.core.logger import AgentLog

__all__ = ["build_turn_callbacks"]


def build_turn_callbacks(
    *, thread_id: str, turn_id: str, user_id: str, mode: str
) -> list[Any]:
    """构建轮级 callbacks：LANGFUSE_ 配置齐全 → [CallbackHandler(trace_id=turn_id)]。"""
    secret = langfuse_settings.secret_key.get_secret_value()
    if not (langfuse_settings.public_key and secret):
        return []
    try:
        from langfuse.langchain import CallbackHandler

        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", langfuse_settings.public_key)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", secret)
        os.environ.setdefault("LANGFUSE_HOST", str(langfuse_settings.host))
        return [CallbackHandler(trace_context={"trace_id": trace_id_for(turn_id)})]
    except Exception as exc:  # noqa: BLE001 —— 观测构建失败不阻塞主链路
        AgentLog(
            message=f"Langfuse callbacks 构建失败，本轮零观测继续：{type(exc).__name__}",
            graph="observability",
        ).warning()
        return []


def trace_id_for(turn_id: str) -> str:
    """Langfuse trace id = turn_id 的 32-hex 形态（对账键；v3 trace_context 要求
    W3C 32-hex——UUID 带杠形态会被静默换 id，此处 hex 化保 id==turn 互查）。"""
    try:
        import uuid

        return uuid.UUID(turn_id).hex
    except ValueError:
        return turn_id
