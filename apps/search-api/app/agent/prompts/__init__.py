"""提示词解析与渲染（nacos-config.md §1 契约实现——B3 rewrite 起逐键启用）。

三层分工：
- **内置默认**：本目录同名 .txt 文件（键 → 相对路径注册表 BUILTIN_PROMPT_FILES）——
  Nacos 键缺失/资产中心未启动（Studio 直跑、单测环境）时的回落值，不含任何密钥；
- **运行时值**：Nacos prompts dataId 快照（`resolve_prompt` 读 AssetCenter 内存快照，
  非网络 IO；轮内冻结语义由调用方保证——节点每次解析同值，快照热更仅对新轮生效）；
  键缺失时回落内置默认并告警（§1「Nacos 键缺失时回落默认」）；
- **渲染**：Jinja2 子集——仅 `{{var}}` 整体替换；未提供变量 / 模板含未知占位符
  fail-fast（§1「未提供变量渲染报错」）；模板中单花括号 JSON 示例不受影响。

变更纪律（§1 提示词纪律）：改提示词先补 fixture 测试与评测对比再热更上线；内置默认
与 `config_center/defaults.py` PROMPTS_CONFIG 双源同步由 test/unit/test_prompts.py 锁死。
"""

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config_center import get_asset_center
from app.core.logger import AgentLog

__all__ = [
    "PROMPTS_DATA_ID",
    "PromptRenderError",
    "builtin_prompt_files",
    "load_builtin_prompt",
    "render_prompt",
    "resolve_prompt",
]

PROMPTS_DATA_ID = "ai_search.agent.prompts.json"

#: 内置默认文件注册表（键 = nacos-config §1 契约键；值 = 本目录相对路径）
BUILTIN_PROMPT_FILES: dict[str, str] = {
    "fast.rewrite": "fast/rewrite.txt",
    "fast.synthesis": "fast/synthesis.txt",
    "common.type_rules": "common/type_rules.txt",
    "common.citation_rules": "common/citation_rules.txt",
}

_BUILTIN_DIR = Path(__file__).resolve().parent

#: Jinja2 子集占位符（{{var}}；允许两侧空白）
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class PromptRenderError(ValueError):
    """提示词渲染失败（变量缺失/未知占位符）——配置错误 fail-fast（§1）。"""


def builtin_prompt_files() -> dict[str, str]:
    """内置文件注册表副本（单测/审计用）。"""
    return dict(BUILTIN_PROMPT_FILES)


@lru_cache(maxsize=None)
def load_builtin_prompt(key: str) -> str:
    """读内置默认文件（进程内缓存；文件为打包资产、运行期只读）。"""
    relative = BUILTIN_PROMPT_FILES.get(key)
    if relative is None:
        raise PromptRenderError(f"提示词键无内置默认文件：{key!r}")
    return (_BUILTIN_DIR / relative).read_text(encoding="utf-8")


def resolve_prompt(key: str) -> str:
    """解析提示词：Nacos prompts 快照 → 键缺失/快照不可用回落内置默认。

    资产中心未启动（Studio 直跑/单测环境）属回落预期，静默走内置默认不刷告警；
    快照在但键缺失按 §1 告警（配置面缺键应当被看见）。
    """
    try:
        snapshot = get_asset_center().snapshot(PROMPTS_DATA_ID)
    except Exception:  # noqa: BLE001 —— 回落属预期运行形态（组装层未启动）
        return load_builtin_prompt(key)
    if snapshot is not None:
        prompts = snapshot.payload.get("prompts")
        value = prompts.get(key) if isinstance(prompts, Mapping) else None
        if isinstance(value, str) and value:
            return value
    AgentLog(
        message=f"提示词键缺失（快照不可用或无该键），回落内置默认：{key}",
        graph="prompts",
    ).warning()
    return load_builtin_prompt(key)


def render_prompt(template: str, variables: Mapping[str, str]) -> str:
    """Jinja2 子集渲染：{{var}} 全量替换；缺变量/未知占位符 fail-fast。"""
    found = set(_PLACEHOLDER_RE.findall(template))
    missing = found - set(variables)
    if missing:
        raise PromptRenderError(f"提示词渲染缺少变量：{sorted(missing)}")
    return _PLACEHOLDER_RE.sub(lambda m: str(variables[m.group(1)]), template)


def _unused(value: Any) -> None:  # pragma: no cover —— Any 导入占位（模板渲染边界类型宽松）
    return None

