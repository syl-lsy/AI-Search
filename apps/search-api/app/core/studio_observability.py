"""Studio 图运行接入自托管 Langfuse 观测（B15 后图外工单 2026-11-20——ARCH §6.6
v1.67 边界反转）。

历史边界（ARCH v1.42 原文）：「Studio 不作 trace 后端、不进 Langfuse」——langgraph
dev 进程内服务器不经 TurnRunner，core/observability.build_turn_callbacks 的轮级
callbacks 永远不会被挂上，Studio 运行零 trace。本工单反转该边界（仅自托管
Langfuse；LANGSMITH_TRACING=false 保持不动，LangSmith 云仍禁）：走 langchain-core
configure-hook 机制（LangSmith tracer 同款先例）注册全局 hook，每个 run 树装配
CallbackManager 时按 env 实时门控构造 langfuse CallbackHandler。

机制（langchain-core 1.7.6 实证，callbacks/manager.py _configure 消费点）：
- register_configure_hook(context_var, inheritable, handle_class, env_var) 把四元组
  追加进 tracers/context._configure_hooks（模块级列表，上游无 unregister API）；
- 每个 run 的 _configure 消费：env_var_is_set(env_var) 运行时实时判定（truthiness
  语义："" / "0" / "false" / "False" 算未设——天然 on/off 开关）→ handler_class()
  每根 run 实例化一次 → isinstance 去重防嵌套重复 → inheritable=True 随 config
  继承传播到子 run。

使用形态：
- 注册无条件（app 进程同样经 app/agent/graphs/__init__.py import 本模块执行注册）：
  激活由 STUDIO_LANGFUSE 在 _configure 时实时门控——app 进程不设该变量 = hook
  休眠零行为零成本（只是列表里的一个四元组；app 进程启动时一次性 import langfuse
  ~0.4s，激活态判定恒零开销）；
- make studio（Makefile 注入 STUDIO_LANGFUSE=1 + LANGSMITH_TRACING=false）激活；
  STUDIO_LANGFUSE 属 Makefile 级开发开关，不进 .env 不进 Nacos（语义见 Makefile
  studio target 注释）；
- LANGFUSE_ 凭据由 langfuse SDK 全局客户端从进程环境读：langgraph.json
  "env": ".env" 注入仓库根 .env（langgraph-cli inmem 的 DotEnv(dotenv_path=env)
  按进程 CWD 解析，Makefile 从仓库根跑）；key 缺失时 SDK 自禁用（auth warning）
  不炸运行，注册时另有一次性 warning 预检（见下）。

边界语义与局限（ARCH §6.6 v1.67）：
- Studio trace 为散点：无 session/thread 关联、无 turns 行对账键（trace id 随机），
  仅图开发调试观测用途；
- TurnRunner 生产链路不变（轮级 trace 仍由 core/observability.build_turn_callbacks
  承担：trace_id=turn_id、session=thread_id、UsageCollector 用量累计）。
"""

import os
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers import context as _tracer_context
from langchain_core.utils.env import env_var_is_set

from app.core.logger import AgentLog

__all__ = ["STUDIO_ENV_VAR", "register_studio_langfuse_hook", "studio_handler_context"]

# 激活开关变量名（truthiness 语义由 langchain env_var_is_set 承担）
STUDIO_ENV_VAR = "STUDIO_LANGFUSE"

# 与 LangSmith tracer hook 同形态：var 恒空（不手动塞实例），handler 由 handle_class
# 在 _configure 时构造；留 var 位只为机制对齐（未来显式注入缝）。
studio_handler_context: ContextVar[BaseCallbackHandler | None] = ContextVar(
    "studio_langfuse_handler", default=None
)

# 幂等 flag：模块级一次注册（进程内全局副作用只发生一次）
_registered = False


def register_studio_langfuse_hook() -> bool:
    """注册全局 configure hook（幂等；注册无条件、激活由 STUDIO_LANGFUSE 门控）。

    返回 True=本次完成注册 / False=已注册过或注册失败（失败 AgentLog warning
    告警零阻塞——同 core/observability.build_turn_callbacks 防御口径：观测失败
    不炸进程）。
    """
    global _registered
    if _registered:
        return False
    try:
        from langfuse.langchain import CallbackHandler  # noqa: PLC0415 lazy import

        # 前置体检（注册时一次性）：STUDIO_LANGFUSE 已设（truthiness）而 LANGFUSE_
        # 凭据缺失 → 告警一次（handler 将自禁用、观测不生效）；注册继续（无条件——
        # 注册与激活解耦，凭据可在进程生命周期内晚到）。
        if env_var_is_set(STUDIO_ENV_VAR) and not (
            os.environ.get("LANGFUSE_PUBLIC_KEY")
            and os.environ.get("LANGFUSE_SECRET_KEY")
        ):
            AgentLog(
                message=(
                    "STUDIO_LANGFUSE 已设但 LANGFUSE_PUBLIC_KEY/SECRET_KEY 缺失"
                    "——Langfuse handler 将 disabled，Studio 观测不生效"
                ),
                graph="observability",
            ).warning()
        _tracer_context.register_configure_hook(
            studio_handler_context,
            inheritable=True,
            handle_class=CallbackHandler,
            env_var=STUDIO_ENV_VAR,
        )
        _registered = True
        return True
    except Exception as exc:  # noqa: BLE001 —— 观测注册失败零阻塞
        AgentLog(
            message=(
                f"Studio Langfuse hook 注册失败（观测休眠继续）：{type(exc).__name__}"
            ),
            graph="observability",
        ).warning()
        return False
