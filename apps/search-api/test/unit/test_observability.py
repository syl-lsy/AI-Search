"""unit 层：轮级可观测 callbacks（app/core/observability.py，ARCH §6.6）。

零网络零 SDK 实调：CallbackHandler 构造缝用 monkeypatch 替换/抛异常验证回落链，
Langfuse 配置缝 monkeypatch langfuse_settings 单例属性（test_trace_cleanup 同款做法）。
覆盖点：
- trace_id_for：UUID（带杠）→ 32-hex（trace_context 要求 W3C 32-hex，带杠形态会被
  Langfuse 静默换 id）；非 UUID 原样回落（对账键语义：非 UUID 的 turn_id 不失真）；
- build_turn_callbacks：LANGFUSE_PUBLIC_KEY/SECRET_KEY 任一缺失 → 空表零开销
  （单测/未接观测部署形态不建 handler）；SDK 构造抛异常 → AgentLog warning 告警 +
  空表回落（观测构建失败不阻塞主链路）；
- LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST 三键 setdefault 回填：缺失才回填、已有值不覆盖
  （SDK 客户端走环境变量，pydantic-settings 不导出 os.environ，此处回填供全局客户端读）；
- R8 版本护栏：langfuse==4.18.0 锁定（升级前必须先跑护栏；回退/升级后只改这一行断言）；
- API 面护栏：from langfuse.langchain import CallbackHandler 可导入（观测接入面）。
参考既有护栏写法：test_config_center_client.py R15 SDK 护栏段（R8 同口径）。
"""

import importlib.metadata
import os
import uuid

import pytest
from pydantic import SecretStr

from app.core import observability
from app.core.observability import build_turn_callbacks, trace_id_for

# 配置缝缺省配置（pk/sk 齐全形态）：键值任意——SDK 构造已被 monkeypatch 替换，零真实读用
_PK = "pk-lf-test"
_SK = "sk-lf-test"
_HOST = "http://127.0.0.1:3100"
# LANGFUSE_ 三环境变量（SDK 全局客户端读取面，与 core/config LANGFUSE_ 组同名）
_LANGFUSE_ENV_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


def _configure_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 langfuse_settings 单例拨到「配置齐全」形态（monkeypatch 遥测后自动还原）。"""
    monkeypatch.setattr(observability.langfuse_settings, "public_key", _PK)
    monkeypatch.setattr(observability.langfuse_settings, "secret_key", SecretStr(_SK))
    monkeypatch.setattr(observability.langfuse_settings, "host", _HOST)


# ---- trace_id_for：UUID → 32-hex / 非 UUID 原样回落 ----


def test_trace_id_for_uuid_becomes_32hex():
    """UUID 带杠形态 hex 化为 32 位无杠小写（trace_context W3C 形态；对账键不漂移）。"""
    turn_id = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    assert trace_id_for(turn_id) == "3f2504e04f8911d39a0c0305e82c3301"
    result = trace_id_for(turn_id)
    assert len(result) == 32
    assert "-" not in result


def test_trace_id_for_uuid_roundtrip_matches_stdlib_hex():
    """任意 UUID 的 hex 化结果与 stdlib uuid.UUID.hex 一致（对账：turn 行 id 互查不漂移）。"""
    for _ in range(5):
        raw = str(uuid.uuid4())
        assert trace_id_for(raw) == uuid.UUID(raw).hex


@pytest.mark.parametrize(
    "raw",
    [
        "turn-not-a-uuid",  # 普通非 UUID 串
        "",  # 空串
        "3f2504e0_4f89_11d3_9a0c_0305e82c3301",  # 杠换下划线（非合法 UUID）
        "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",  # 形似 UUID 但非 hex
    ],
)
def test_trace_id_for_non_uuid_passthrough(raw: str):
    """非 UUID 输入原样返回（ValueError 回落；不做二次加工避免对账键失真）。"""
    assert trace_id_for(raw) == raw


# ---- build_turn_callbacks：缺配置 → 空表零开销 ----


@pytest.mark.parametrize(
    ("public_key", "secret_key"),
    [
        ("", ""),  # 全缺（单测/未接观测部署形态）
        ("", "sk-lf-only"),  # 仅缺 pk
        ("pk-lf-only", ""),  # 仅缺 sk
    ],
)
def test_build_turn_callbacks_empty_without_langfuse_keys(
    monkeypatch: pytest.MonkeyPatch, public_key: str, secret_key: str
):
    """LANGFUSE_PUBLIC_KEY/SECRET_KEY 任一缺失 → 空表（零 SDK 触达，观测零开销）。"""
    monkeypatch.setattr(observability.langfuse_settings, "public_key", public_key)
    monkeypatch.setattr(
        observability.langfuse_settings, "secret_key", SecretStr(secret_key)
    )
    result = build_turn_callbacks(
        thread_id="thread-1", turn_id=str(uuid.uuid4()), user_id="u1", mode="fast"
    )
    assert result == []


# ---- build_turn_callbacks：SDK 构造抛异常 → warning 告警 + 空表回落 ----


def test_build_turn_callbacks_sdk_failure_warns_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch, log_records
):
    """SDK 构造抛异常 → 空表回落（观测不阻塞主链路）+ AgentLog warning 告警留痕。

    缝：langfuse.langchain.CallbackHandler 替换为抛异常假体——覆盖「import 成功、
    构造炸」的形态（import 面另有独立护栏用例）。
    """
    _configure_langfuse(monkeypatch)
    # env 预清理：构造前的 setdefault 回填会写 os.environ，delenv 保证测试后还原
    for key in _LANGFUSE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("sdk constructor exploded")

    monkeypatch.setattr("langfuse.langchain.CallbackHandler", _boom)
    result = build_turn_callbacks(
        thread_id="thread-1", turn_id=str(uuid.uuid4()), user_id="u1", mode="fast"
    )
    assert result == []
    assert any(
        record["level"].name == "WARNING"
        and "Langfuse callbacks 构建失败" in record["message"]
        and record["extra"].get("graph") == "observability"
        for record in log_records
    )


# ---- LANGFUSE_ 三环境变量 setdefault 回填 ----


def test_build_turn_callbacks_backfills_missing_langfuse_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """三键缺失时 setdefault 回填 settings 值（SDK 全局客户端读 os.environ 的前提）。"""
    _configure_langfuse(monkeypatch)
    for key in _LANGFUSE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "langfuse.langchain.CallbackHandler", lambda *a, **k: "fake-handler"
    )

    build_turn_callbacks(
        thread_id="thread-1", turn_id=str(uuid.uuid4()), user_id="u1", mode="fast"
    )
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == _PK
    assert os.environ["LANGFUSE_SECRET_KEY"] == _SK
    assert os.environ["LANGFUSE_HOST"] == _HOST


def test_build_turn_callbacks_setdefault_keeps_existing_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """setdefault 语义：os.environ 已有值不覆盖（外部显式配置优先于 settings 回填）。"""
    _configure_langfuse(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-preexisting")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setattr(
        "langfuse.langchain.CallbackHandler", lambda *a, **k: "fake-handler"
    )

    build_turn_callbacks(
        thread_id="thread-1", turn_id=str(uuid.uuid4()), user_id="u1", mode="fast"
    )
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-lf-preexisting"  # 已有值不动
    assert os.environ["LANGFUSE_SECRET_KEY"] == _SK  # 缺失键照常回填
    assert os.environ["LANGFUSE_HOST"] == _HOST


# ---- R8 版本护栏 + API 面护栏（锁版本 + 导入面，R15 同口径）----


def test_r8_guard_langfuse_version_locked():
    """锁版本护栏：langfuse==4.18.0（R8 口径：升级/回退前必须先跑护栏，只改这一行断言）。

    与 langchain-openai 私有面联动升级——版本被升级行 → 此处红，提示先复核
    build_turn_callbacks 的 trace_context 形态与 langfuse_* metadata 契约键。
    """
    # 2026-11-13 自 3.15.0 升 4.18.0：v5 首选路径被服务端版本卡死（Langfuse server
    # 4.56.0 < SDK 5.x 硬要求 4.59.0，大版本锁 pull 未滚动），按用户阶梯裁决回退 4.x
    # 线最新的 4.18.0；服务端升到 ≥4.59 后可重探 v5。探针确认 4.18.0 CallbackHandler
    # 仍收 trace_context、langfuse_* 契约键保留（见 observability.py 模块 docstring）
    assert importlib.metadata.version("langfuse") == "4.18.0"


def test_guard_langchain_callback_handler_importable():
    """API 面护栏：from langfuse.langchain import CallbackHandler 可导入（观测接入面）——
    上游改集成包布局（如拆回独立 langfuse-langchain 包）即此处红，提示改观测模块导入。"""
    from langfuse.langchain import CallbackHandler  # noqa: F401 —— 可导入即通过
