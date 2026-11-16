"""后续提问建议（followups）图外钩子的判定与生成基元（REQ-046 / ADR-011 / api.md §7.2）。

**不是图节点**（ADR-011 / ARCH §6.1 图外钩子）：本模块供 TurnRunner 在 done 落库后调用，
不参与 fast/deep 任何图拓扑。分工：
- 判定纯函数（`resolve_followups_settings` / `should_generate_followups`）——条件矩阵
  全部来自该轮发起时捕获的 Agent Spec 快照（OQ-11 同源：热更仅对后续轮生效）；
- 生成（`generate_followups`）——light 模型（`model.mapping.light` 别名，与 rewrite 同
  通道 / 同 S15 结构化输出基元）+ Nacos `fast.followups` 提示词；修复回喂 1 次后放弃；
- 输出校验（`normalize_followups`）——**不截断**：空数组/空串/超 200 字符/非字符串一律
  视为校验违规（api.md §7.2 items 括号注：非空且 ≤200 字符，违规走修复回喂→静默放弃）。

**静默铁律**（api.md §0/§7.2、R13 实验功能四要件之「失败静默」）：本模块任何失败
（超时/解析失败/上游异常/提示词渲染失败）一律返回 None + WARN 日志留痕，**绝不抛出**
——增强层不为主链路让路，调用方据此不发事件、不落库、不报错（2.5 恒 null）。

**外部内容包裹**（ARCH §6.4.3 铁律）：来源清单逐条 `<external_content source=…>` 包裹、
答案整体 `<external_content source="answer">` 包裹（nacos-config.md §1 v1.23 安全加固，
安全审计 P3-2/P3-4 闭合：答案可能含来源原文逐字引用=有界二阶间接注入面），两者均
html 转义（内容中的闭标记/引号被实体化，隔离边界不可被击穿）；**问题不平铺包裹**
（用户本轮自身输入，非外部内容）。

时延口径：生成发生在 done 之后——不计入 P50/duration_ms（PRD「不计入 P50」实验要件）。
"""
import asyncio
import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ValidationError

from app.agent.nodes.fast_rewrite import build_light_model
from app.agent.prompts import resolve_prompt, render_prompt
from app.core.logger import AgentLog

__all__ = [
    "DEFAULT_FOLLOWUPS_SETTINGS",
    "FOLLOWUPS_ANSWER_MAX_CHARS",
    "FOLLOWUPS_ITEM_MAX_CHARS",
    "FOLLOWUPS_MAX_ITEMS",
    "FOLLOWUPS_SOURCES_MAX",
    "FollowupsOutput",
    "FollowupsSettings",
    "build_followups_prompt",
    "generate_followups",
    "normalize_followups",
    "resolve_followups_settings",
    "should_generate_followups",
]

#: items 单条最大字符数（api.md §7.2 items 括号注：非空字符串且 ≤200 字符——契约口径；
#: 提示词内的「≤30 字」是对模型内容的软引导，校验面按契约 200 硬界，违规不截断）
FOLLOWUPS_ITEM_MAX_CHARS = 200
#: items 条数上限（data-model.md §1.3 followups：长度 1~5，与 spec.followups.count 对齐）
FOLLOWUPS_MAX_ITEMS = 5
#: 答案入提示词的字数上限（api.md §7.2：synthesis 最终答案 ≤3000 字上限——控 token）。
#: 口径 = **内容字符数截断在先**：截断后才 html.escape(quote=True)，转义后上限 ×6
#: （最坏形态 `&quot;` 六字符/原字符；`&amp;` 5、`&lt;`/`&gt;` 4），即提示词内答案段落
#: 最宽约 18000 字符——token 预算按内容字符计，转义膨胀不计入本上限。
FOLLOWUPS_ANSWER_MAX_CHARS = 3000
#: 来源清单入提示词条数上限（api.md §7.2「标题/域名清单（控 token）」的代码口径）
FOLLOWUPS_SOURCES_MAX = 5

#: 提示词键（nacos-config.md §1 `fast.followups`；消费方 = TurnRunner 图外钩子）
_FOLLOWUPS_PROMPT_KEY = "fast.followups"


@dataclass(frozen=True, slots=True)
class FollowupsSettings:
    """Agent Spec `followups` 节的轮内快照（OQ-11：轮内不重读，热更只对后续轮生效）。"""

    enabled: bool
    count: int
    generate_timeout_ms: int
    sse_hold_window_ms: int


#: 节整节缺失/非法时的内置默认（nacos-config.md §3 v1.22：enabled 默认 true——
#: 与 config_center/defaults.py 及 scripts/nacos_init.py 双侧同步）
DEFAULT_FOLLOWUPS_SETTINGS = FollowupsSettings(
    enabled=True, count=5, generate_timeout_ms=8000, sse_hold_window_ms=10000
)


def resolve_followups_settings(spec_payload: Mapping[str, Any] | Any) -> FollowupsSettings:
    """解析 `followups` 节（轮内快照）：节缺失/结构非法/越界 → 内置默认 + WARN。

    校验面口径同 config_center validator（1 ≤ count ≤ 5、generate_timeout_ms ≥ 1000、
    `sse_hold_window_ms ≥ generate_timeout_ms + 2000` 强制裕量）——运行期再兜一层，
    防 Studio 直跑/快照残缺把钩子写崩主链路。
    """
    section = (
        spec_payload.get("followups") if isinstance(spec_payload, Mapping) else None
    )
    if not isinstance(section, Mapping):
        AgentLog(
            message="Agent Spec followups 节缺失/非法（回落内置默认 enabled=true）",
            graph="followups",
        ).warning()
        return DEFAULT_FOLLOWUPS_SETTINGS
    enabled = section.get("enabled")
    count = section.get("count")
    generate_timeout_ms = section.get("generate_timeout_ms")
    hold_window_ms = section.get("sse_hold_window_ms")
    valid = (
        isinstance(enabled, bool)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and 1 <= count <= FOLLOWUPS_MAX_ITEMS
        and isinstance(generate_timeout_ms, int)
        and not isinstance(generate_timeout_ms, bool)
        and generate_timeout_ms >= 1000
        and isinstance(hold_window_ms, int)
        and not isinstance(hold_window_ms, bool)
        and hold_window_ms >= generate_timeout_ms + 2000
    )
    if not valid:
        AgentLog(
            message=(
                "Agent Spec followups 节取值非法/越界（回落内置默认）："
                f"enabled={enabled!r} count={count!r} "
                f"generate_timeout_ms={generate_timeout_ms!r} "
                f"sse_hold_window_ms={hold_window_ms!r}"
            ),
            graph="followups",
        ).warning()
        return DEFAULT_FOLLOWUPS_SETTINGS
    return FollowupsSettings(
        enabled=cast(bool, enabled),
        count=cast(int, count),
        generate_timeout_ms=cast(int, generate_timeout_ms),
        sse_hold_window_ms=cast(int, hold_window_ms),
    )


def should_generate_followups(
    *,
    settings: FollowupsSettings,
    mode: str,
    status: str,
    answer_md: str | None,
) -> bool:
    """触发条件矩阵（api.md §7.2 followups 条目：四条件全部满足才生成）。

    `enabled=true` ∧ `mode=fast`（deep 永不触发）∧ `status=completed`（canceled/
    degraded/failed 不触发——取消/打断非故障、降级轮不产增强面）∧ `answer_md` 非空
    （strip 后非空；空白答案视同空）。
    """
    if not settings.enabled:
        return False
    if mode != "fast":
        return False
    if status != "completed":
        return False
    return bool(answer_md) and bool(answer_md.strip())


class FollowupsOutput(BaseModel):
    """结构化输出 schema（nacos-config.md §1 `fast.followups` 输出 JSON：{"followups": [...]}）。

    单条/条数校验不进 pydantic（避免校验错误信息里带长文本）——由 normalize_followups
    统一裁决，违规走修复回喂→静默放弃路径（§7.2）。
    """

    followups: list[str]


def normalize_followups(raw: Any, *, count: int) -> list[str] | None:
    """输出校验：strip + 丢弃空项 + 保序去重 + 截到 count 条；任何违规返回 None。

    违规（不截断，走修复回喂→静默放弃）：非列表 / 全空 / 单条非字符串或空白 /
    单条 > FOLLOWUPS_ITEM_MAX_CHARS。`count` 上限仍夹到 FOLLOWUPS_MAX_ITEMS。
    """
    items = (
        raw.get("followups")
        if isinstance(raw, Mapping)
        else getattr(raw, "followups", raw)
    )
    if not isinstance(items, list):
        return None
    cap = max(1, min(int(count), FOLLOWUPS_MAX_ITEMS))
    out: list[str] = []
    seen: set[str] = set()
    for entry in items:
        if not isinstance(entry, str):
            return None
        text = entry.strip()
        if not text or len(text) > FOLLOWUPS_ITEM_MAX_CHARS:
            return None
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    if not out:
        return None
    return out[:cap]


def _external_content(content: str, *, source: str, kind: str | None = None) -> str:
    """`<external_content>` 包裹体在**本模块**的唯一构造点（§6.4.3）：属性值转义，内容由调用方转义。

    来源块（`type="web"`）与答案块（`source="answer"`）完全同构——本模块两条装配路径
    共用这一处开闭标记实现，转义口径不会漂移。synthesis 图节点另有独立实现
    （`app/agent/nodes/synthesis.py::_sources_block`，多 snippet 段、图内消费），两处
    不共享本函数；改包裹格式须双侧同步（§6.4.3 契约面）。
    """
    attrs = f' source="{html.escape(source, quote=True)}"'
    if kind is not None:
        attrs += f' type="{html.escape(kind, quote=True)}"'
    return f"<external_content{attrs}>\n{content}\n</external_content>"


def _wrap_sources(sources: Sequence[Mapping[str, Any]]) -> str:
    """来源块装配（§6.4.3）：逐条 `<external_content source=url type=web>` 包裹 + 转义。

    仅标题/URL（控 token——api.md §7.2「fused sources 标题/域名清单」）；空来源返回
    空串（direct 轮零检索，模板既有支持）。
    """
    blocks: list[str] = []
    for index, item in enumerate(list(sources)[:FOLLOWUPS_SOURCES_MAX], start=1):
        title = html.escape(str(item.get("title") or ""), quote=True)
        blocks.append(
            _external_content(
                f"[{index}] {title}",
                source=str(item.get("url") or ""),
                kind="web",
            )
        )
    return "\n".join(blocks)


def _wrap_answer(answer: str) -> str:
    """答案块装配（§6.4.3 同构于来源块；nacos-config.md §1 v1.23 安全加固）。

    答案属模型自身转述，但可能逐字引用来源原文（有界二阶间接注入面，安全审计
    P3-2/P3-4）——整体 `<external_content source="answer">` 包裹 + quote=True 转义，
    答案内闭标记/引号被实体化。空答案仍产空包裹体（条件矩阵已保证 answer_md 非空，
    此处保持既有输入容错形态）。
    """
    return _external_content(
        html.escape(answer[:FOLLOWUPS_ANSWER_MAX_CHARS], quote=True), source="answer"
    )


def build_followups_prompt(
    *,
    query: str,
    answer: str,
    sources: Sequence[Mapping[str, Any]],
    count: int,
    query_lang: str,
) -> str:
    """提示词装配：Nacos `fast.followups` 模板 + 五占位符（count/query_lang/query/answer/
    sources_block）；答案截到 3000 字上限后整体 external_content 包裹（§7.2 / v1.23）。"""
    return render_prompt(
        resolve_prompt(_FOLLOWUPS_PROMPT_KEY),
        {
            "count": str(max(1, min(int(count), FOLLOWUPS_MAX_ITEMS))),
            "query_lang": str(query_lang or "en"),
            "query": str(query or ""),
            "answer": _wrap_answer(str(answer or "")),
            "sources_block": _wrap_sources(sources),
        },
    )


async def _invoke_validate(chain: Any, prompt: str, *, count: int) -> list[str] | None:
    """单次结构化调用 + 校验；校验类失败返回 None（交调用方修复回喂），传输类上抛。"""
    raw = await chain.ainvoke(prompt)
    return normalize_followups(raw, count=count)


async def generate_followups(
    *,
    query: str,
    answer: str,
    sources: Sequence[Mapping[str, Any]],
    settings: FollowupsSettings,
    query_lang: str,
    mapping: Mapping[str, Any] | None = None,
) -> list[str] | None:
    """生成后续提问建议（light 模型 + 结构化输出，修复回喂 1 次；失败静默返回 None）。

    返回 None 的语义 = api.md §7.2「静默放弃」：调用方不发 followups 事件、不写
    turns.followups、不产生任何 error。`generate_timeout_ms` 为**整段生成预算**
    （首轮 + 修复回喂共享同一 deadline——保「生成永不晚于保活窗口」）。
    """
    if not settings.enabled:  # 防御：调用方已按条件矩阵门控
        return None
    try:
        prompt = build_followups_prompt(
            query=query,
            answer=answer,
            sources=sources,
            count=settings.count,
            query_lang=query_lang,
        )
        chain = build_light_model(mapping).with_structured_output(
            FollowupsOutput, method="function_calling"
        )
        async with asyncio.timeout(settings.generate_timeout_ms / 1000):
            try:
                items = await _invoke_validate(chain, prompt, count=settings.count)
            except (OutputParserException, ValidationError) as exc:
                # 校验类失败（解析/schema）：进修复回喂（§7.2 同日 P2 修订路径）
                AgentLog(
                    message=(
                        "followups 结构化输出校验失败（进修复回喂）："
                        f"{type(exc).__name__}: {str(exc)[:800]}"
                    ),
                    graph="followups",
                ).warning()
                items = None
            if items is not None:
                return items
            # 修复回喂 1 次（S15 基元既有纪律：校验错误文本回喂）
            repaired_prompt = (
                f"{prompt}\n\n上一次输出未通过校验，错误信息："
                f"输出须为 {{'followups': [非空字符串…]}}，每条 ≤"
                f"{FOLLOWUPS_ITEM_MAX_CHARS} 字符。请调用 FollowupsOutput 工具重新提交结果，"
                "严格按 schema 填写；不要在文本回复里书写 JSON。"
            )
            repaired = await _invoke_validate(
                chain, repaired_prompt, count=settings.count
            )
            if repaired is None:
                # 修复回喂「返回了但校验违规」（非异常）：静默放弃前仍须 WARN 留痕
                # （本模块 docstring「任何失败一律 WARN」承诺；评审 P3-2 补口）
                AgentLog(
                    message=(
                        "followups 修复回喂仍失败（静默放弃）：输出校验违规"
                        "（条数/单条非空/≤"
                        f"{FOLLOWUPS_ITEM_MAX_CHARS} 字符）"
                    ),
                    graph="followups",
                ).warning()
            return repaired
    except (OutputParserException, ValidationError) as exc:
        AgentLog(
            message=(
                "followups 修复回喂仍失败（静默放弃）："
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="followups",
        ).warning()
        return None
    except TimeoutError:
        AgentLog(
            message=(
                "followups 生成超时（静默放弃）："
                f"timeout={settings.generate_timeout_ms}ms"
            ),
            graph="followups",
        ).warning()
        return None
    except Exception as exc:  # noqa: BLE001 —— 增强层静默：任何上游/渲染异常都不外泄
        AgentLog(
            message=(
                f"followups 生成异常（静默放弃）：{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="followups",
        ).warning()
        return None
