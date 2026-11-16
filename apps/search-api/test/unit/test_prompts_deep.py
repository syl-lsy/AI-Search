"""unit 层：deep 四键提示词骨架（M2 批次 1——nacos-config.md §1 deep.* 键位/占位符集）。

钉住的语义（占位符集与结构段按契约一次到位；文案骨架终稿，批次 2~4 逐节点打磨，
键位与变量集不再动——需新占位符须走契约增量）：
- 四键注册进 BUILTIN_PROMPT_FILES（键 → prompts/deep/ 同名 .txt）；
- 占位符并集恰为契约集（单测锁死惯例，同 test_prompts.py fast 形态）：
  deep.planner         = {type_rules, max_subagents, memory_hints}
  deep.subagent.system = {subtopic, summary_max_tokens}
  deep.reflect         = set()（零占位符——大纲/已收集信息由节点代码在模板外追加，批次 3 接线）
  deep.report_writer   = {sections_hint, query_lang, memory_block, subagent_reports_block,
                          sources_block, cite_rules}
  （cite_rules 承载 common.citation_rules 注入——契约 §1「引用规则公共片段（被上述模板
  {{cite_rules}} 引用）」；fast.synthesis 同款先例）
- 双源同步：defaults.py PROMPTS_CONFIG 与内置文件逐字节一致（nacos_init.py ≡ defaults.py
  由 test_config_center.py TestNacosInitScriptParity 另锁，本文件不重复）；
- 渲染 fail-fast：全占位符可解析（渲染后零残留 {{var}}）；缺变量 / 未知占位符 →
  PromptRenderError（§1「未提供变量渲染报错」）；
- 结构段钉死：planner 输出 schema 五字段（deep 不产出 answerable_from_knowledge /
  search_strategy——nacos §1 v1.20「deep 零波及」钉死）/ subagent_system 的
  <external_content> 隔离语义 / reflect 输出 schema {sufficient, gaps:[{gap,query,origin}]} /
  writer「摘要→目录→分节→结论」结构 + 全程 [n] 引用。
"""

import re

import pytest

from app.agent.prompts import (
    PromptRenderError,
    builtin_prompt_files,
    load_builtin_prompt,
    render_prompt,
)
from app.config_center.defaults import PROMPTS_CONFIG

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

DEEP_KEYS = (
    "deep.planner",
    "deep.subagent.system",
    "deep.reflect",
    "deep.report_writer",
)

#: 各键契约占位符集（nacos-config.md §1 deep.* 行；单测锁死）
DEEP_EXPECTED_PLACEHOLDERS: dict[str, set[str]] = {
    "deep.planner": {"type_rules", "max_subagents", "memory_hints"},
    "deep.subagent.system": {"subtopic", "summary_max_tokens"},
    "deep.reflect": set(),
    "deep.report_writer": {
        "sections_hint",
        "query_lang",
        "memory_block",
        "subagent_reports_block",
        "sources_block",
        "cite_rules",
    },
}

#: 各键内置文件相对路径（BUILTIN_PROMPT_FILES 注册面）
DEEP_EXPECTED_FILES: dict[str, str] = {
    "deep.planner": "deep/planner.txt",
    "deep.subagent.system": "deep/subagent_system.txt",
    "deep.reflect": "deep/reflect.txt",
    "deep.report_writer": "deep/report_writer.txt",
}

#: 各键全变量渲染表（值取合法形态；空串合法——同 fast 系 memory_hints 先例）
DEEP_RENDER_VARS: dict[str, dict[str, str]] = {
    "deep.planner": {
        "type_rules": "【问题类型判定标准】（公共片段全文）",
        "max_subagents": "3",
        "memory_hints": "",
    },
    "deep.subagent.system": {
        "subtopic": "pgvector 与 Milvus 在个人知识库场景的对比",
        "summary_max_tokens": "1200",
    },
    "deep.reflect": {},
    "deep.report_writer": {
        "sections_hint": "第一节 背景；第二节 对比；第三节 选型建议",
        "query_lang": "zh",
        "memory_block": "",
        "subagent_reports_block": "",
        "sources_block": "",
        "cite_rules": "1. 编号对应：[n] 对应来源列表第 n 条。",
    },
}


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text))


# ==== 注册面与占位符集（契约锁死）====


def test_deep_registry_paths() -> None:
    """四键注册进内置文件注册表，路径指向 prompts/deep/ 同名文件。"""
    files = builtin_prompt_files()
    for key, relative in DEEP_EXPECTED_FILES.items():
        assert files.get(key) == relative, f"{key} 未注册或路径漂移"


@pytest.mark.parametrize("key", DEEP_KEYS)
def test_deep_builtin_file_exists(key: str) -> None:
    """每键内置文件存在且可读（§1「内置全部键的同名默认文件」）。"""
    text = load_builtin_prompt(key)
    assert isinstance(text, str) and text.strip()


@pytest.mark.parametrize("key", DEEP_KEYS)
def test_deep_placeholder_union_locked(key: str) -> None:
    """占位符并集恰为契约集（nacos-config §1 deep.* 行，单测锁死）：
    内置默认文件与 defaults.py 两源断言同值。"""
    expected = DEEP_EXPECTED_PLACEHOLDERS[key]
    assert _placeholders(load_builtin_prompt(key)) == expected, f"{key} 内置文件占位符漂移"
    assert _placeholders(PROMPTS_CONFIG["prompts"][key]) == expected, f"{key} defaults 占位符漂移"


@pytest.mark.parametrize("key", DEEP_KEYS)
def test_deep_defaults_match_builtin_file(key: str) -> None:
    """双源同步锁：defaults.py PROMPTS_CONFIG 与内置文件逐字节一致（防漂移）。"""
    assert PROMPTS_CONFIG["prompts"][key] == load_builtin_prompt(key), f"{key} 双源漂移"


# ==== 渲染面（全占位符可解析 / fail-fast）====


@pytest.mark.parametrize("key", DEEP_KEYS)
def test_deep_prompt_renders_fully(key: str) -> None:
    """全占位符以契约变量表可解析；渲染后零残留 {{var}}（单花括号 JSON 示例不受影响）。"""
    rendered = render_prompt(load_builtin_prompt(key), DEEP_RENDER_VARS[key])
    assert _PLACEHOLDER_RE.search(rendered) is None, f"{key} 渲染后仍有残留占位符"


@pytest.mark.parametrize("key", DEEP_KEYS)
def test_deep_prompt_missing_variable_fails_fast(key: str) -> None:
    """缺任一变量 → PromptRenderError（§1 fail-fast）。reflect 零变量无缺变量面，跳过。"""
    if not DEEP_EXPECTED_PLACEHOLDERS[key]:
        pytest.skip("reflect 零占位符：无缺变量形态")
    partial = {k: v for k, v in DEEP_RENDER_VARS[key].items() if k != sorted(
        DEEP_EXPECTED_PLACEHOLDERS[key])[0]}
    with pytest.raises(PromptRenderError):
        render_prompt(load_builtin_prompt(key), partial)


@pytest.mark.parametrize("key", DEEP_KEYS)
def test_deep_prompt_unknown_placeholder_fails_fast(key: str) -> None:
    """模板含未知占位符 → PromptRenderError（即便变量全供给也拒渲染）。"""
    patched = load_builtin_prompt(key) + "\n未知={{deep_unknown_var}}"
    with pytest.raises(PromptRenderError):
        render_prompt(patched, DEEP_RENDER_VARS[key])


# ==== 结构段（契约钉死，文案骨架期即锁）====


def test_deep_planner_output_schema_structure() -> None:
    """planner 输出 schema 五字段齐备（nacos-config §1 deep.planner 行）：
    subtopics / outline:[{section,intent}] / query_type(7 类枚举) / time_range /
    memory_directive；deep 零波及——不产出 answerable_from_knowledge 与 search_strategy。"""
    for text in (
        PROMPTS_CONFIG["prompts"]["deep.planner"],
        load_builtin_prompt("deep.planner"),
    ):
        assert '"subtopics"' in text
        assert '"outline":[{"section":"","intent":""}]' in text
        assert '"query_type":"factoid|list|comparison|multi_facet|temporal|how_to|other"' in text
        assert '"time_range"' in text
        assert '"memory_directive"' in text
        # deep 零波及（nacos §1 v1.20 钉死）：输出 schema 串不含这两键
        # （散文提及「仅 fast.rewrite 消费」不带 JSON 引号形态，不误伤）
        assert '"answerable_from_knowledge"' not in text
        assert '"search_strategy"' not in text
        # 拆分纪律三句（契约括号注：对比对称 / 时效时间窗 / factoid 轻量化 1 子课题）
        assert "对称覆盖" in text
        assert "时间窗" in text
        assert "1 个子课题" in text


def test_deep_subagent_system_external_content_isolation() -> None:
    """subagent_system 的外部内容隔离语义（安全铁律：外部内容仅为资料、不可信、
    经 <external_content> 包裹、不得执行其中指令）。"""
    for text in (
        PROMPTS_CONFIG["prompts"]["deep.subagent.system"],
        load_builtin_prompt("deep.subagent.system"),
    ):
        assert "<external_content>" in text
        assert "不可信" in text
        assert "仅为资料" in text
        assert "不得执行" in text
        # 流程四步（搜索→选 URL→读原文→沉淀摘要）与摘要 token 上限变量
        assert "搜索" in text
        assert "读原文" in text
        assert "沉淀摘要" in text


def test_deep_reflect_output_schema_structure() -> None:
    """reflect 输出 schema（nacos-config §1 deep.reflect 行）：
    {sufficient, gaps:[{gap,query,origin}]}；origin 枚举 web|kb。"""
    for text in (
        PROMPTS_CONFIG["prompts"]["deep.reflect"],
        load_builtin_prompt("deep.reflect"),
    ):
        assert '"sufficient"' in text
        assert '"gaps":[{"gap":"","query":"","origin":"web|kb"}]' in text
        # 反思纪律：对照大纲找缺口 + sufficient 即停
        assert "大纲" in text
        assert "缺口" in text


def test_deep_report_writer_structure_and_citations() -> None:
    """report_writer 结构段（nacos-config §1 deep.report_writer 行）：
    摘要→目录→分节（{{sections_hint}}）→结论 四段齐备且有序；全程 [n] 引用；
    {{cite_rules}} 承载 common.citation_rules 注入。"""
    for text in (
        PROMPTS_CONFIG["prompts"]["deep.report_writer"],
        load_builtin_prompt("deep.report_writer"),
    ):
        # 四段有序出现（骨架终稿结构段，文案批次 4 打磨不改序）
        assert text.index("摘要") < text.index("目录") < text.index("分节") < text.index("结论")
        assert "[n]" in text
        assert "每个事实句标注[n]" in text
        # 三块注入段行前缀（memory_block / 子课题报告 / 全量来源）
        assert "{{memory_block}}" in text
        assert "子课题报告" in text
        assert "全量来源" in text
        assert "{{cite_rules}}" in text
