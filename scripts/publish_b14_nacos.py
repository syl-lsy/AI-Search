"""B14 runbook 第 2 步：Nacos v1.32 **合并式**发布（prompts 四键 + spec 九键）。

## 为什么不用 `make nacos-init`（NACOS_INIT_FORCE=1）

FORCE 会**全量重发五个 dataId 的默认初值**，把运行态热更值（`fast.synthesis` 篇幅段、
`common.type_rules` recall 判据、模型映射等历史热更）静默回滚。本脚本反过来：**以运行态
payload 为基底**，只覆盖本批目标键，其余键（含全部历史热更）原样保留；序列化口径复用
`nacos_init.serialize_payload`（与 `config_center/cache.py` 的 canonical_text 同源，
indent=2 —— 发布端与消费端字节一致才能命中 md5 防抖）。

## 目标键（恰好下面这些，多一个都不动）

- `ai_search.agent.prompts.json`：`fast.rewrite` / `deep.planner` / `common.safety_check` /
  `common.safety_reply`（五值 safety 枚举 + 涉政类别四段话术；值取自 defaults.PROMPTS_CONFIG）
- `ai_search.agent.spec.json`：
  - `fast.llm_timeout_s=60`、`fast.retry.connection.total_window_s=60.0`（增量 A 裁决①）
  - `fast.thinking="off"`、`deep.thinking="on"`（增量 C 裁决值）
  - `safety.enabled=true`、`safety.politics_terms=[]`（增量 B 全类别启用 + 词表用户自填）
  - `safety.danger_terms` / `profanity_terms` / `grey_rules`（第⑩件：逐表对齐契约 §3 示例值）

## 次序语义（务必按此执行）

1. **本脚本先发 prompts**（新 validator 硬校验 `common.safety_reply` 四段前缀；旧三段
   prompts 会被整版拒收 → 开发态回落 L4 内置默认、**production 拒绝启动 503001**）。
2. 再发 spec（`thinking` 键缺失合法=裁决值，spec 面无拒收风险；但增量 A 的 60s 值需发布
   才生效）。
3. **两个 dataId 都发布完成后才重启 API**；`politics_terms` 词表内容由用户在 Nacos 控制台
   自填（AI 零生成，允许为空数组）。
4. LiteLLM 无需变更（B13 钩子不变，`reasoning_effort` / 嵌套 `thinking` 原样透传）。

## 用法（仓库根）

    python3 scripts/publish_b14_nacos.py            # 默认干跑：拉运行态 → 键级 diff
    python3 scripts/publish_b14_nacos.py --yes      # 真发布（管理员凭据交互输入）

- 读取运行态：`.env` 只读账号（`NACOS_USERNAME` / `NACOS_PASSWORD`，同 nacos_init 读法）；
- 发布：管理员凭据 **getpass 交互输入**，或 `NACOS_INIT_USER=... NACOS_INIT_PASS=...` 直传；
  凭据值（含账号名与 token）零打印零日志；
- 发布后逐 dataId **GET 回读逐字节比对** `serialize_payload(merged)`；不一致 = 非零退出。
- 退出码：0 干跑/发布+回读通过；1 发布或回读失败；2 凭据/运行态基底不可用（未发布）。

依赖：namespace/group/登录与配置端点/序列化全部复用 `scripts/nacos_init.py` 常量与函数；
初值优先 import `app.config_center.defaults`（项目环境），缺环境时回落
`nacos_init.PROMPTS_CONFIG/SPEC_CONFIG`（两者由 `test_config_center.py::TestNacosInitScriptParity`
逐字节锁定，语义等价）。
"""

from __future__ import annotations

import copy
import getpass
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# 同目录导入（`python3 scripts/publish_b14_nacos.py` 时 sys.path[0] 即 scripts/；
# 显式补插兼容被其它入口/其它 cwd 调用的形态）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import nacos_init  # noqa: E402 —— 复用 SERVER/NAMESPACE/GROUP/login/fetch/publish/serialize

PROMPTS_DATA_ID = "ai_search.agent.prompts.json"
SPEC_DATA_ID = "ai_search.agent.spec.json"

#: prompts 目标键（值整体取自 defaults；B14 增量 B/C 的契约面）
PROMPTS_TARGET_KEYS: tuple[str, ...] = (
    "fast.rewrite",
    "deep.planner",
    "common.safety_check",
    "common.safety_reply",
)

#: spec 目标路径（嵌套路径逐键写入；其余 spec 键原样保留）
SPEC_TARGET_PATHS: tuple[tuple[str, ...], ...] = (
    ("fast", "llm_timeout_s"),  # 30 → 60（增量 A 裁决①，fast 全节点共享键）
    ("fast", "retry", "connection", "total_window_s"),  # 20.0 → 60.0（窗-包络对齐）
    ("fast", "thinking"),  # 新增 "off"（增量 C）
    ("deep", "thinking"),  # 新增 "on"（增量 C）
    ("safety", "enabled"),  # false → true（裁决④全类别生效）
    ("safety", "politics_terms"),  # 新增 []（词表用户自填）
    ("safety", "danger_terms"),  # 第⑩件：逐表对齐契约 §3 示例值
    ("safety", "profanity_terms"),
    ("safety", "grey_rules"),
)

#: 干跑摘要截断（长文本/词表只打结构与示例，不全文打印）
_SUMMARY_EXCERPT_CHARS = 40
_TERM_EXAMPLE_CHARS = 40

#: 运行态缺键哨兵（区分「键不存在」与「键值为 None」）
_MISSING = object()


class _SetupError(RuntimeError):
    """凭据/运行态基底不可用（尚未发生任何发布）。"""


# ── 初值来源（defaults 优先，nacos_init 投影兜底）──


def _load_defaults() -> tuple[dict[str, Any], dict[str, Any], str]:
    """返回 (PROMPTS_CONFIG, SPEC_CONFIG, 来源说明)。

    优先 import 项目包（`app.config_center.defaults`）；缺 venv/依赖时回落
    `nacos_init` 同名常量——两者由 parity 测试逐字节锁定，值语义等价。
    """
    try:
        from app.config_center.defaults import (  # noqa: PLC0415 —— 可选依赖，失败即回落
            PROMPTS_CONFIG,
            SPEC_CONFIG,
        )

        return PROMPTS_CONFIG, SPEC_CONFIG, "app.config_center.defaults"
    except Exception:  # noqa: BLE001 —— 仅探测导入面（缺 venv 属预期）
        return (
            nacos_init.PROMPTS_CONFIG,
            nacos_init.SPEC_CONFIG,
            "scripts/nacos_init.py（与 defaults 逐字节锁定投影）",
        )


# ── 凭据（值零打印）──


def _readonly_credentials() -> tuple[str, str]:
    """只读账号：仓库根 .env 的 NACOS_USERNAME / NACOS_PASSWORD（须成对）。"""
    parsed = nacos_init._parse_env_file(nacos_init._default_env_path())
    user = parsed.get("NACOS_USERNAME", "").strip()
    password = parsed.get("NACOS_PASSWORD", "").strip()
    if not user or not password:
        raise _SetupError(
            "缺只读账号凭据：仓库根 .env 需成对提供 NACOS_USERNAME / NACOS_PASSWORD"
            "（双账号口径，nacos-config.md §8）"
        )
    return user, password


def _admin_credentials() -> tuple[str, str, str]:
    """管理员凭据：`NACOS_INIT_USER/PASS` 环境变量优先，否则 getpass 交互输入。

    返回 (user, password, 来源说明)——说明仅记「env / 交互输入」，凭据值绝不打印。
    非交互环境（无 tty）读不到输入时抛 _SetupError 并提示 env 直传形态。
    """
    env_user = os.environ.get("NACOS_INIT_USER", "").strip()
    env_pass = os.environ.get("NACOS_INIT_PASS", "").strip()
    if env_user and env_pass:
        return env_user, env_pass, "NACOS_INIT_USER/NACOS_INIT_PASS 环境变量"
    if env_user or env_pass:
        raise _SetupError(
            "管理员凭据半对：NACOS_INIT_USER 与 NACOS_INIT_PASS 须同时提供（不混拼来源）"
        )
    try:
        user = input("Nacos 管理员用户名（不回显于日志）: ").strip()
        password = getpass.getpass("Nacos 管理员密码（不回显）: ")
    except EOFError as exc:
        raise _SetupError(
            "非交互环境无法输入管理员凭据——改用 NACOS_INIT_USER=... "
            "NACOS_INIT_PASS=... python3 scripts/publish_b14_nacos.py --yes"
        ) from exc
    if not user or not password:
        raise _SetupError("管理员凭据为空（用户名/密码均必填）")
    return user, password, "getpass 交互输入"


# ── 运行态读取 ──


def _read_runtime(token: str, data_id: str) -> dict[str, Any]:
    """GET 运行态 payload（合并基底）。缺失/非法即中止——没有基底不做「新建式」发布。"""
    content, status = nacos_init.fetch_current(token, data_id)
    if content is None:
        raise _SetupError(
            f"运行态 {data_id} 读取失败（HTTP {status}）——合并式发布需要运行态基底，"
            "请先确认该 dataId 已存在（首次部署请走 make nacos-init）"
        )
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise _SetupError(f"运行态 {data_id} 非合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise _SetupError(f"运行态 {data_id} 顶层不是 JSON 对象")
    return payload


# ── 合并（只动目标键）──


def _get_path(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    """按嵌套路径取叶子；任一环节缺失返回 _MISSING（不抛）。"""
    node: Any = payload
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _apply_path(
    runtime: dict[str, Any], defaults: Mapping[str, Any], path: tuple[str, ...]
) -> tuple[bool, Any, Any]:
    """把 defaults 在 path 上的值写进 runtime（就地），返回 (是否变化, 旧值, 新值)。

    父节点缺失/非对象时按 defaults 同前缀子树物化——**运行态本无该结构即无值可保**，
    物化后可保证目标节点自身结构合法（如 `retry.connection` 五键整节）。defaults 缺该
    目标键即中止（不猜值）。
    """
    new_value = _get_path(defaults, path)
    if new_value is _MISSING:
        raise _SetupError(f"defaults 缺目标键 {'.'.join(path)}——初值来源不完整")
    old_value = _get_path(runtime, path)

    node = runtime
    for depth, key in enumerate(path[:-1]):
        child = node.get(key)
        if not isinstance(child, dict):
            fallback = _get_path(defaults, path[: depth + 1])
            if not isinstance(fallback, dict):
                raise _SetupError(
                    f"目标 {'.'.join(path)} 的父节点缺失且 defaults 无对应结构"
                )
            child = copy.deepcopy(fallback)
            node[key] = child
        node = child
    node[path[-1]] = copy.deepcopy(new_value)

    changed = old_value is _MISSING or old_value != new_value
    return changed, old_value, new_value


def _count_leaves(node: Any) -> int:
    """叶子计数：dict 逐子键递归，非 dict（含数组整体）计 1。"""
    if isinstance(node, dict):
        return sum(_count_leaves(value) for value in node.values())
    return 1


# ── diff 展示（结构 + 示例，不全文打印）──


def _summarize(value: Any) -> str:
    """单值摘要：词表只打条数 + 首条示例；长文本只打长度 + 首行片段。"""
    if value is _MISSING:
        return "（运行态无此键）"
    if isinstance(value, list):
        example = str(value[0])[:_TERM_EXAMPLE_CHARS] if value else ""
        suffix = f"首条示例：{example!r}" if example else "空表"
        return f"{len(value)} 条（{suffix}）"
    if isinstance(value, str):
        if "\n" in value or len(value) > 200:
            first_line = value.splitlines()[0] if value else ""
            return f"{len(value)} 字符（首行：{first_line[:_SUMMARY_EXCERPT_CHARS]!r}…）"
        return repr(value)
    return repr(value)


def _print_diff(
    label: str, path: tuple[str, ...], changed: bool, old: Any, new: Any
) -> None:
    status = "改动" if changed else "无变化"
    print(f"  [{label}] {'.'.join(path)}  —— {status}")
    if not changed:
        return
    print(f"        旧：{_summarize(old)}")
    print(f"        新：{_summarize(new)}")
    if isinstance(old, list) and isinstance(new, list) and len(new) < len(old):
        # 词表收缩提示（历史热更条目将被契约对齐值覆盖——如 grey_rules/danger_terms）
        print(
            f"        注意：运行态多出的 {len(old) - len(new)} 条将被覆盖"
            "（历史热更条目；如需保留请发布后在控制台补回）"
        )


def _build_merged(
    runtime_prompts: dict[str, Any],
    runtime_spec: dict[str, Any],
    defaults_prompts: Mapping[str, Any],
    defaults_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[tuple[str, tuple[str, ...], bool, Any, Any]]]:
    """运行态为基底逐键覆盖 → (merged_prompts, merged_spec, 逐键 diff 记录)。"""
    merged_prompts = copy.deepcopy(runtime_prompts)
    merged_spec = copy.deepcopy(runtime_spec)
    diffs: list[tuple[str, tuple[str, ...], bool, Any, Any]] = []
    for key in PROMPTS_TARGET_KEYS:
        path = ("prompts", key)
        changed, old, new = _apply_path(merged_prompts, defaults_prompts, path)
        diffs.append(("prompts", path, changed, old, new))
    for path in SPEC_TARGET_PATHS:
        changed, old, new = _apply_path(merged_spec, defaults_spec, path)
        diffs.append(("spec", path, changed, old, new))
    return merged_prompts, merged_spec, diffs


# ── 发布与回读校验 ──


def _publish_and_verify(admin_token: str, data_id: str, merged: dict[str, Any]) -> bool:
    """发布单 dataId → GET 回读 → 与 serialize_payload 逐字节比对。"""
    expected = nacos_init.serialize_payload(merged)
    ok, status = nacos_init.publish(admin_token, data_id, merged)
    if not ok:
        print(f"[publish] FAIL {data_id}（HTTP {status}）")
        return False
    content, status = nacos_init.fetch_current(admin_token, data_id)
    if content is None:
        print(f"[verify] FAIL {data_id} 回读失败（HTTP {status}）")
        return False
    if content != expected:
        print(
            f"[verify] FAIL {data_id} 回读与发布内容不一致"
            f"（期望 {len(expected)} 字符 / 实际 {len(content)} 字符）"
        )
        for index, (want, got) in enumerate(zip(expected, content)):
            if want != got:
                print(
                    f"  首处差异 @ {index}：期望 "
                    f"{expected[max(0, index - 30): index + 30]!r} / 实际 "
                    f"{content[max(0, index - 30): index + 30]!r}"
                )
                break
        return False
    print(f"[verify] OK   {data_id}（回读 {len(content)} 字符逐字节一致）")
    return True


# ── 主流程 ──


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [arg for arg in args if arg != "--yes"]
    if unknown:
        sys.stderr.write("用法: python3 scripts/publish_b14_nacos.py [--yes]\n")
        return 2
    should_publish = "--yes" in args

    defaults_prompts, defaults_spec, source = _load_defaults()
    print(f"[b14] 初值来源：{source}")
    print(
        f"[b14] 目标 Nacos：{nacos_init.SERVER} namespace={nacos_init.NAMESPACE_ID} "
        f"group={nacos_init.GROUP_NAME}"
    )

    try:
        readonly_user, readonly_password = _readonly_credentials()
        print("[b14] 登录（.env 只读账号，凭据不回显）…")
        readonly_token = nacos_init.login(readonly_user, readonly_password)
        runtime_prompts = _read_runtime(readonly_token, PROMPTS_DATA_ID)
        runtime_spec = _read_runtime(readonly_token, SPEC_DATA_ID)
        merged_prompts, merged_spec, diffs = _build_merged(
            runtime_prompts, runtime_spec, defaults_prompts, defaults_spec
        )
    except _SetupError as exc:
        sys.stderr.write(f"[b14] 中止（未发布）：{exc}\n")
        return 2
    except SystemExit:  # nacos_init.login 失败即 SystemExit(1)，stderr 已提示（凭据面）
        return 2

    print("\n[b14] 键级 diff（运行态基底 → 合并发布值）")
    for label, path, changed, old, new in diffs:
        _print_diff(label, path, changed, old, new)

    merged_leaves = _count_leaves(merged_prompts) + _count_leaves(merged_spec)
    runtime_leaves = _count_leaves(runtime_prompts) + _count_leaves(runtime_spec)
    runtime_prompt_keys = len(runtime_prompts.get("prompts", {}))
    merged_prompt_keys = len(merged_prompts.get("prompts", {}))
    changed_existing = sum(
        1 for _, _, changed, old, _ in diffs if changed and old is not _MISSING
    )
    added = sum(1 for _, _, _, old, _ in diffs if old is _MISSING)
    unchanged = sum(1 for _, _, changed, _, _ in diffs if not changed)
    print(
        f"\n[b14] prompts 键数 {runtime_prompt_keys} → {merged_prompt_keys}"
        "（仅替换 4 键，其余原样保留）"
    )
    print(
        f"[b14] 叶子键合计：运行态 {runtime_leaves} → 合并后 {merged_leaves}"
        "（dict 末级键 / 数组整体计 1）"
    )
    print(
        f"[b14] 本批改写已存在键 {changed_existing}、新增键 {added}、目标键无变化 {unchanged}；"
        f"**保留不动的运行态键 {runtime_leaves - changed_existing}**"
    )

    if not should_publish:
        print(
            "\n[b14] 干跑结束（未发布）。确认 diff 后执行：\n"
            "      python3 scripts/publish_b14_nacos.py --yes\n"
            "[b14] 次序提醒：本脚本先 prompts 后 spec；两 dataId 都发布完成后才重启 API；\n"
            "      safety.politics_terms 由用户在 Nacos 控制台自填（允许空数组）。"
        )
        return 0

    try:
        admin_user, admin_password, admin_source = _admin_credentials()
    except _SetupError as exc:
        sys.stderr.write(f"[b14] 中止（未发布）：{exc}\n")
        return 2
    print(f"\n[b14] 管理员凭据来源：{admin_source}（凭据值/账号名不回显）")
    try:
        admin_token = nacos_init.login(admin_user, admin_password)
    except SystemExit:  # 管理员凭据/登录失败（stderr 已提示，值不回显）
        return 2

    if not _publish_and_verify(admin_token, PROMPTS_DATA_ID, merged_prompts):
        return 1
    if not _publish_and_verify(admin_token, SPEC_DATA_ID, merged_spec):
        return 1

    print(
        "\n[b14] 完成：两 dataId 已合并发布并回读逐字节一致。\n"
        "[b14] 下一步：重启 API（prompts 四段已就位，production 不再因校验拒收拒绝启动）；\n"
        "      用户在 Nacos 控制台自填 safety.politics_terms 内容（允许留空数组）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
