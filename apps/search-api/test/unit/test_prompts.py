"""unit 层：提示词内置默认与解析（B3 rewrite——nacos-config.md §1 键位/占位符集/回落口径）。

钉住的语义（冻结契约，逐条对应测试）：
- `fast.rewrite` 占位符并集恰为 {type_rules, query_lang, memory_hints, history, query}
  （§1 v1.2 变量集，单测锁死惯例——内置默认文件与 defaults.py 两源一致）；
- `common.type_rules` 零占位符（图中立公共片段，fast.rewrite/deep.planner 共用）；
- 内置默认文件（app/agent/prompts/ 同名 .txt）与 config_center L4（defaults.py
  PROMPTS_CONFIG）内容一致（双源同步锁，防漂移）；
- 解析优先级：Nacos prompts 快照（轮内语义由调用方保证）→ 键缺失/快照不可用回落
  内置默认（§1「Nacos 键缺失时回落默认」）；
- 渲染 fail-fast：未提供变量 / 模板含未知占位符 → PromptRenderError（§1「未提供变量
  渲染报错」）；Jinja2 子集仅 {{var}} 替换，模板中单花括号 JSON 示例不受影响。
"""

import re
from typing import Any

import pytest

from app.agent.prompts import (
    PROMPTS_DATA_ID,
    PromptRenderError,
    builtin_prompt_files,
    load_builtin_prompt,
    render_prompt,
    resolve_prompt,
)
from app.config_center.defaults import PROMPTS_CONFIG

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text))


def test_rewrite_placeholder_union_locked() -> None:
    """fast.rewrite 占位符并集恰为五变量（nacos-config §1 fast.rewrite 行，单测锁死）。"""
    expected = {"type_rules", "query_lang", "memory_hints", "history", "query"}
    assert _placeholders(PROMPTS_CONFIG["prompts"]["fast.rewrite"]) == expected
    assert _placeholders(load_builtin_prompt("fast.rewrite")) == expected


def test_type_rules_placeholders_empty() -> None:
    """common.type_rules 零占位符：图中立公共片段（deep.planner 复用同文）。"""
    assert _placeholders(load_builtin_prompt("common.type_rules")) == set()
    assert _placeholders(PROMPTS_CONFIG["prompts"]["common.type_rules"]) == set()


def test_synthesis_placeholder_union_locked() -> None:
    """fast.synthesis 占位符并集恰为六变量（nacos-config §1 fast.synthesis 行，单测锁死）。"""
    expected = {"cite_rules", "max_answer_chars", "query_lang", "memory_block", "sources_block", "query"}
    assert _placeholders(PROMPTS_CONFIG["prompts"]["fast.synthesis"]) == expected
    assert _placeholders(load_builtin_prompt("fast.synthesis")) == expected


def test_citation_rules_placeholders_empty() -> None:
    """common.citation_rules 零占位符：图中立公共片段（经 {{cite_rules}} 注入）。"""
    assert _placeholders(load_builtin_prompt("common.citation_rules")) == set()
    assert _placeholders(PROMPTS_CONFIG["prompts"]["common.citation_rules"]) == set()


def test_defaults_prompts_match_builtin_files() -> None:
    """双源同步锁：defaults.py PROMPTS_CONFIG 与 app/agent/prompts/ 内置文件逐字节一致。"""
    for key in builtin_prompt_files():
        assert PROMPTS_CONFIG["prompts"][key] == load_builtin_prompt(key), f"{key} 双源漂移"


def test_builtin_registry_covers_rewrite_keys() -> None:
    """内置文件注册表覆盖 rewrite 所需两键（§1 同名默认文件口径）。"""
    files = builtin_prompt_files()
    assert files["fast.rewrite"] == "fast/rewrite.txt"
    assert files["common.type_rules"] == "common/type_rules.txt"


class _FakeSnapshot:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class _FakeCenter:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload

    def snapshot(self, data_id: str) -> _FakeSnapshot | None:
        if self._payload is None:
            return None
        return _FakeSnapshot(self._payload)


def test_resolve_prompt_prefers_nacos_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nacos prompts 快照存在且键可用 → 返回快照值（内置默认仅回落用）。"""
    from app.agent import prompts as prompts_module

    center = _FakeCenter({"prompts": {"fast.rewrite": "NACOS 版模板"}})
    monkeypatch.setattr(prompts_module, "get_asset_center", lambda: center)
    assert resolve_prompt("fast.rewrite") == "NACOS 版模板"


def test_resolve_prompt_falls_back_on_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照存在但键缺失 → 回落内置默认（§1「Nacos 键缺失时回落默认」）。"""
    from app.agent import prompts as prompts_module

    center = _FakeCenter({"prompts": {}})
    monkeypatch.setattr(prompts_module, "get_asset_center", lambda: center)
    assert resolve_prompt("fast.rewrite") == load_builtin_prompt("fast.rewrite")


def test_resolve_prompt_falls_back_when_center_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """资产中心未启动（Studio 直跑/单测环境）→ 回落内置默认，不向节点泄漏异常。"""
    from app.agent import prompts as prompts_module

    def _boom() -> Any:
        raise RuntimeError("AssetCenter 尚未启动")

    monkeypatch.setattr(prompts_module, "get_asset_center", _boom)
    assert resolve_prompt("fast.rewrite") == load_builtin_prompt("fast.rewrite")
    assert resolve_prompt("common.type_rules") == load_builtin_prompt("common.type_rules")
    assert PROMPTS_DATA_ID == "ai_search.agent.prompts.json"


def test_render_prompt_substitutes_values() -> None:
    """Jinja2 子集渲染：{{var}} 全量替换；单花括号 JSON 示例不受影响。"""
    rendered = render_prompt(
        "语言={{query_lang}}；规则={{type_rules}}；输出 {\"a\": 1}；历史={{history}}；提示={{memory_hints}}；问题={{query}}",
        {
            "query_lang": "zh",
            "type_rules": "七类规则",
            "history": "Q: 上一问\nA: 上一答",
            "memory_hints": "",
            "query": "它是什么？",
        },
    )
    assert rendered == "语言=zh；规则=七类规则；输出 {\"a\": 1}；历史=Q: 上一问\nA: 上一答；提示=；问题=它是什么？"


def test_render_prompt_fails_fast_on_missing_and_unknown() -> None:
    """渲染 fail-fast（§1）：变量缺失 / 模板含未知占位符 → PromptRenderError。"""
    with pytest.raises(PromptRenderError):
        render_prompt("问题={{query}}", {})  # 未提供 query
    with pytest.raises(PromptRenderError):
        render_prompt("问题={{query}} 未知={{nope}}", {"query": "x"})  # 未知占位符
