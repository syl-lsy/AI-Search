"""轮级可观测 callbacks（Langfuse trace root=轮，session=thread_id——ARCH §6.6）。

LANGFUSE_ 未配置（单测/未接观测部署形态）→ 空表零开销；SDK 异常 → 告警回落空表
（观测不阻塞主链路）。SDK 客户端走环境变量（LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST，
与 core/config LANGFUSE_ 组同名——pydantic-settings 不导出 os.environ，此处把 settings
值回填 env 供 langfuse 全局客户端读取）；trace id = turn_id（对账键：Langfuse
trace 与 turns 行同 id 可查）；session=thread_id 由 RunnableConfig metadata 的
langfuse_* 键承载（CallbackHandler 契约键，4.18.0 探针确认保留，TurnRunner 注入；
session 落 trace 根由 handler 根链入口内部 propagate_attributes 承担）。

M2 批次 1 增 `UsageCollector`（usage 方案 A——docs/m2-usage-probe-batch0.md §3/§4）：
轮级 token 用量累计 callback，TurnRunner 随 build_turn_callbacks 并列注入 astream
config（fast/deep 通用），`_finish_completed` 消费累计值填 done.usage 与 turns.usage。
"""

import os
import threading
from collections.abc import Mapping
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from app.core.config import langfuse_settings
from app.core.logger import AgentLog

__all__ = ["UsageCollector", "build_turn_callbacks"]


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
    """Langfuse trace id = turn_id 的 32-hex 形态（对账键；trace_context 要求 W3C
    32-hex——UUID 带杠形态会被静默换 id，此处 hex 化保 id==turn 互查；4.18.0
    TraceContext 无运行时校验，形态正确性由本函数保证）。"""
    try:
        import uuid

        return uuid.UUID(turn_id).hex
    except ValueError:
        return turn_id


class UsageCollector(BaseCallbackHandler):
    """轮级 token 用量累计 callback（M2 批次 1，usage 方案 A——探针 §3 表 A 主链）。

    TurnRunner 每轮新建一例、随 build_turn_callbacks 并列注入 astream config
    （fast/deep 通用；随 RunnableConfig 继承传播——批次 2 deepagents 子代理的
    LLM 调用同面覆盖，探针 §4.2 验证点②），`_finish_completed` 消费 `total()`
    填 done.usage 与 turns.usage（零契约变更：UsageBrief 全可空，>0 键才出现）。

    事件面（如实注记，避免后人按探针 §3 字面挂错）：探针原文写「on_chat_model_end
    读 generations[*].message.usage_metadata」，但本仓 langchain-core 1.7.6 的
    BaseCallbackHandler 已无 on_chat_model_end——chat 模型运行的结束事件为
    `on_llm_end(response: LLMResult)`，流式调用下 LLMResult.generations[0][0].message
    为聚合 chunk，其 usage_metadata 经 chunk 合并逐键求和（本仓版本实测：两片
    10/3/13 + 5/2/7 → 15/5/20）；非流式调用同一事件携带响应体原生 usage_metadata。
    语义与探针 §3 方案 A 一致，仅事件名随上游 API 演进。

    cost_usd 方案 A 不可得（价格面在网关 OTel trace，与 app trace 零 id 交叠——
    探针 §2.3），恒不产出该键（UsageBrief 可空；成本对账通道留 M4 评测线再议）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Any = None,
        **kwargs: Any,
    ) -> None:
        """模型运行结束：读 generations[*].message.usage_metadata 累计（同步方法——
        异步运行经 executor 调度，锁保并发安全）。畸形 response/帧零贡献不抛
        （观测不阻塞主链路，同 build_turn_callbacks 回落口径）。"""
        try:
            for gen_group in getattr(response, "generations", None) or []:
                # LLMResult.generations 为 list[list[ChatGeneration]] 嵌套；扁平形态防御兼容
                items = gen_group if isinstance(gen_group, (list, tuple)) else [gen_group]
                for gen in items:
                    message = getattr(gen, "message", None)
                    usage = getattr(message, "usage_metadata", None)
                    if not isinstance(usage, Mapping):
                        continue  # 无 usage 帧（未开 stream_usage 的旧通道）：零贡献
                    prompt = self._read_tokens(usage, "input_tokens", "prompt_tokens")
                    completion = self._read_tokens(usage, "output_tokens", "completion_tokens")
                    total = self._read_tokens(usage, "total_tokens", None)
                    with self._lock:
                        self._prompt_tokens += prompt
                        self._completion_tokens += completion
                        self._total_tokens += total
        except Exception as exc:  # noqa: BLE001 —— 累计面防御：畸形响应不阻塞图运行
            AgentLog(
                message=f"usage collector 累计异常（零贡献继续）："
                f"{type(exc).__name__}: {str(exc)[:200]}",
                graph="observability",
            ).warning()

    @staticmethod
    def _read_tokens(usage: Mapping[str, Any], primary: str, secondary: str | None) -> int:
        """键位双形态读取：langchain-core 归一化键优先，provider 原生键兜底（同义
        取一不双计）；非法值按 0。"""
        value = usage.get(primary)
        if value is None and secondary is not None:
            value = usage.get(secondary)
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    def total(self) -> dict[str, int]:
        """累计快照：>0 键才出现（全零 → {}——缺键省略先例，与 done.usage 旧形态
        兼容；键位即 UsageBrief/契约 §2.5 usage 字段名）。"""
        with self._lock:
            out: dict[str, int] = {}
            if self._prompt_tokens > 0:
                out["prompt_tokens"] = self._prompt_tokens
            if self._completion_tokens > 0:
                out["completion_tokens"] = self._completion_tokens
            if self._total_tokens > 0:
                out["total_tokens"] = self._total_tokens
            return out
