"""unit 层：Studio 图运行接入自托管 Langfuse（app/core/studio_observability.py；
ARCH §6.6 v1.67 边界反转工单，2026-11-20）。

机制被测物是 langchain-core configure-hook（与 LangSmith tracer 同机制，manager.py
_configure 消费）：用 RunnableLambda（func 接受 config kwarg）真实 invoke，检查
config["callbacks"].handlers 里有无 handler 实例——零网络零 LLM 零真实
CallbackHandler 构造（handler 构造缝全局换 FakeStudioHandler 假体）。

覆盖点：
- 激活门控（truthiness 语义钉死）：STUDIO_LANGFUSE 未设 / "0" / "false" → 不挂；
  设 "1"（假 LANGFUSE_ key）→ 根 run 挂上恰 1 个实例，嵌套子 run 去重仍恰 1 个、
  同实例继承；
- 幂等：二次 register 不重复注册（返回 False、零追加）；
- 生产接线面：register_configure_hook 收到 context_var=studio_handler_context、
  inheritable=True、env_var=STUDIO_LANGFUSE、handle_class=真实 CallbackHandler 类
  （只捕获参数不消费，零构造）；
- key 缺失告警：STUDIO_LANGFUSE 已设而 LANGFUSE_PUBLIC_KEY/SECRET_KEY 缺失 →
  AgentLog warning 留痕，注册继续（注册无条件）；
- 注册失败防御：注册器抛异常 → 返回 False + warning、flag 仍未注册（观测失败
  不炸进程，同 core/observability.build_turn_callbacks 防御口径）。

测试间隔离论证（hook 注册是 langchain 模块级全局副作用，上游无 unregister API）：
- 激活由 STUDIO_LANGFUSE env 在每根 run 的 _configure 实时门控；本 suite 无任何
  用例在该门控路径外设它 → hook 残留但休眠，对其他测试模块零行为（LangSmith
  tracer hook 永驻进程同先例）；
- 本模块 module 级 fixture 把全局 _configure_hooks 中指向本模块 context_var 的
  hook（含仓内其他测试模块 import graphs 包时真实注册的休眠 hook——收集期先于
  本 fixture 执行，故必在其后）surgical 替换为假体：后续 env 拨 1 的用例只构造
  假体，零网络（unit 零网络纪律；真实 CallbackHandler 构造含后台 auth 线程）；
- 模块 _registered flag：fixture 直接拨 True（= 生产稳态，等价一次成功注册）；
  需要「未注册」形态的用例用 monkeypatch.setattr 拨 False（测试退出自动还原）；
- env 一律 monkeypatch，退出自动还原。
"""

from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableLambda
from langchain_core.tracers import context as _tracer_context
from langfuse.langchain import CallbackHandler

from app.core import studio_observability

# ---- 假体与计数缝 -----------------------------------------------------------


class FakeStudioHandler(BaseCallbackHandler):
    """零网络假体：与 CallbackHandler 同构（BaseCallbackHandler 子类），构造零副作用。

    module 级 fixture 用它替换全局 hook 的 handle_class——机制（门控/挂载/去重/
    继承）与真体同判据（isinstance 检查的类对象替换前后同族），构造面零网络。
    """


def _run_probe() -> dict[str, Any]:
    """根 run 真实 invoke（零网络零 LLM），返回 func 侧收集到的事实。"""
    seen: dict[str, Any] = {}

    def _collect(x: Any, config: Any = None) -> list[BaseCallbackHandler]:
        handlers = [
            h
            for h in config["callbacks"].handlers
            if isinstance(h, FakeStudioHandler)
        ]
        seen["root"] = handlers
        return handlers

    RunnableLambda(_collect).invoke("x", config={"tags": ["probe"]})
    return seen


def _nested_probe() -> dict[str, Any]:
    """嵌套 invoke 探针：根 run（outer）内再 invoke 子 run（inner），双向收集。"""
    seen: dict[str, Any] = {}

    def inner(x: Any, config: Any = None) -> Any:
        seen["inner"] = [
            h
            for h in config["callbacks"].handlers
            if isinstance(h, FakeStudioHandler)
        ]
        return x

    def outer(x: Any, config: Any = None) -> Any:
        seen["root"] = [
            h
            for h in config["callbacks"].handlers
            if isinstance(h, FakeStudioHandler)
        ]
        return RunnableLambda(inner).invoke(x, config=config)

    RunnableLambda(outer).invoke("x", config={"tags": ["probe"]})
    return seen


# ---- 全局 hook 换假体（module 级，先于本模块所有用例） ------------------------


@pytest.fixture(scope="module", autouse=True)
def _fake_global_hook():
    """全局 hook surgical 换假体 + flag 拨稳态（隔离论证见模块 docstring）。

    - 删掉全局列表中所有指向本模块 context_var 的条目（含其他测试模块 import
      graphs 包时真实注册的休眠 hook）——列表原地改写（manager._configure 每次
      deferred import 读活列表，原地改写即生效；禁用 setattr 换名）；
    - 追加假体 hook（同 env_var 同 context_var，机制判据同族）；
    - _registered 拨 True（生产稳态；wiring 用例自行 monkeypatch 拨 False）。
    """
    hooks = _tracer_context._configure_hooks
    hooks[:] = [
        t
        for t in hooks
        if t[0] is not studio_observability.studio_handler_context
    ]
    _tracer_context.register_configure_hook(
        studio_observability.studio_handler_context,
        inheritable=True,
        handle_class=FakeStudioHandler,
        env_var=studio_observability.STUDIO_ENV_VAR,
    )
    studio_observability._registered = True
    yield


# ---- 激活门控（truthiness 语义钉死） -----------------------------------------


def test_env_unset_hook_dormant_no_handler(monkeypatch: pytest.MonkeyPatch):
    """STUDIO_LANGFUSE 未设 → 不挂（app 进程休眠零行为的机制同源验证）。"""
    monkeypatch.delenv(studio_observability.STUDIO_ENV_VAR, raising=False)
    seen = _run_probe()
    assert seen["root"] == []


def test_env_set_attaches_handler_and_nested_dedupe(
    monkeypatch: pytest.MonkeyPatch,
):
    """STUDIO_LANGFUSE=1（假 LANGFUSE_ key）→ 根 run 恰挂 1 个，嵌套子 run 去重
    仍恰 1 个且同实例继承（inheritable 传播 + isinstance 去重）。"""
    monkeypatch.setenv(studio_observability.STUDIO_ENV_VAR, "1")
    # 假 key：镜像 studio 进程形态（凭据来自 langgraph.json env 注入的仓库根 .env）
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake")
    seen = _nested_probe()
    assert len(seen["root"]) == 1
    assert len(seen["inner"]) == 1
    assert seen["root"][0] is seen["inner"][0]


def test_env_zero_string_is_off(monkeypatch: pytest.MonkeyPatch):
    """STUDIO_LANGFUSE="0" → 不挂（langchain env_var_is_set truthiness：算未设）。"""
    monkeypatch.setenv(studio_observability.STUDIO_ENV_VAR, "0")
    seen = _run_probe()
    assert seen["root"] == []


def test_env_false_string_is_off(monkeypatch: pytest.MonkeyPatch):
    """STUDIO_LANGFUSE="false" → 不挂（truthiness 语义另一半，""/"False" 同族）。"""
    monkeypatch.setenv(studio_observability.STUDIO_ENV_VAR, "false")
    seen = _run_probe()
    assert seen["root"] == []


# ---- 幂等 --------------------------------------------------------------------


def test_register_is_idempotent_second_call_noop(monkeypatch: pytest.MonkeyPatch):
    """已注册后再调 register → 返回 False 且零追加（模块级 flag 幂等）。"""
    assert studio_observability._registered is True  # fixture 已拨稳态

    calls: list[tuple[Any, ...]] = []

    def _recorder(*args: Any, **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(
        _tracer_context, "register_configure_hook", _recorder
    )
    assert studio_observability.register_studio_langfuse_hook() is False
    assert studio_observability.register_studio_langfuse_hook() is False
    assert calls == []  # 二次调用零追加
    hooks = _tracer_context._configure_hooks
    assert (
        sum(
            1
            for t in hooks
            if t[0] is studio_observability.studio_handler_context
        )
        == 1
    )


# ---- 生产接线面（只捕获参数，零构造零消费） -----------------------------------


def test_register_records_real_wiring_args(monkeypatch: pytest.MonkeyPatch):
    """首次注册的接线面：context_var / inheritable=True / env_var / 真实
    CallbackHandler 类，逐参钉死（升级 langchain-core 时此处先红）。"""
    calls: list[dict[str, Any]] = []

    def _recorder(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(
        _tracer_context, "register_configure_hook", _recorder
    )
    monkeypatch.setattr(studio_observability, "_registered", False)
    monkeypatch.setenv(studio_observability.STUDIO_ENV_VAR, "1")
    # 假 key：拨掉「key 缺失告警」路径（该路径另有独立用例），本用例只看接线参数
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake")
    assert studio_observability.register_studio_langfuse_hook() is True
    assert len(calls) == 1
    call = calls[0]
    assert call["args"][0] is studio_observability.studio_handler_context  # 位置参
    assert call["kwargs"]["inheritable"] is True
    assert call["kwargs"]["env_var"] == "STUDIO_LANGFUSE"
    assert call["kwargs"]["handle_class"] is CallbackHandler  # 真实类（非假体）


# ---- key 缺失告警（注册无条件） -----------------------------------------------


def test_register_missing_keys_warns_but_still_registers(
    monkeypatch: pytest.MonkeyPatch, log_records
):
    """STUDIO_LANGFUSE=1 而 LANGFUSE_PUBLIC_KEY/SECRET_KEY 缺失 → warning 留痕
    （handler 将自禁用、观测不生效），注册继续（注册无条件）。"""
    calls: list[Any] = []

    def _recorder(*args: Any, **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(
        _tracer_context, "register_configure_hook", _recorder
    )
    monkeypatch.setattr(studio_observability, "_registered", False)
    monkeypatch.setenv(studio_observability.STUDIO_ENV_VAR, "1")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert studio_observability.register_studio_langfuse_hook() is True
    assert len(calls) == 1  # 注册无条件：告警不拦截
    assert any(
        record["level"].name == "WARNING"
        and "STUDIO_LANGFUSE" in record["message"]
        and record["extra"].get("graph") == "observability"
        for record in log_records
    )


# ---- 注册失败防御（告警零阻塞） -----------------------------------------------


def test_register_failure_warns_and_stays_unregistered(
    monkeypatch: pytest.MonkeyPatch, log_records
):
    """注册器抛异常 → 返回 False + warning、flag 仍未注册（观测失败不炸进程）。"""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("register exploded")

    monkeypatch.setattr(_tracer_context, "register_configure_hook", _boom)
    monkeypatch.setattr(studio_observability, "_registered", False)
    monkeypatch.delenv(studio_observability.STUDIO_ENV_VAR, raising=False)
    assert studio_observability.register_studio_langfuse_hook() is False
    assert studio_observability._registered is False  # 失败不置稳态，可重试
    assert any(
        record["level"].name == "WARNING"
        and "注册失败" in record["message"]
        and record["extra"].get("graph") == "observability"
        for record in log_records
    )
