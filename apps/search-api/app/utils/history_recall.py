"""历史装配第二通道 `history_recall`（ARCH §6.5 v1.51 / nacos-config.md §1 v1.27）。

**职责**：为 recall 回顾整理轮装配「问题全文 + 答案首段 + markdown 标题骨架（H2/H3 标题行
+ 每节首句）」的历史块，经 synthesis 的 `{{history_block}}` 注入（仅 recall 变体非空）。

**结构感知截断配方**（v2 计划 §3.4 / 调研 `conversation-memory-recall-research-20261116.md`
§5/§7 案 A）：
- 首段保结论先行（模型答案的开篇即结论）；
- 标题骨架保「知道丢了什么」（章节目录在场，细节可由模型如实说明缺失）；
- 每节首句保小节主旨；
- 最近 `HISTORY_RECALL_RECENT_TURNS` 轮放宽为更大答案预算（近轮近全文、远轮骨架的简单
  动态分配）；总软顶 `HISTORY_RECALL_MAX_CHARS`（≈4K token），溢出丢最旧；窗口
  `HISTORY_RECALL_WINDOW_TURNS`（与 `HISTORY_WINDOW_TURNS` 同值，SQL 已 limit，此处再夹
  一次防漂移）。

**业界定位（R18 如实注记）**：骨架截断作为对话历史截断策略**无产品先例**（生产排序 = 纯
前缀截断 → 阈值滚动摘要；结构感知截断为工程创新，最近亲缘 = Claude Code `Referenced file`
/ Manus「丢内容保结构引用」族）——无外部量化数据可引，按 fixture 测试锁配方 + M4 评测探针
（回顾类复述完整度）自证。不足则触发「轮次滚动摘要」条件工单（PRD OQ-23 挂账）。

**纯函数边界**：输入为时间正序的 `(question, answer_md)` 序列，零 DB / 零 LLM / 零全局态；
SQL 与纳入口径（`_HISTORY_STATUSES` / rolled_back 排除）由调用方（TurnRunner）保证且两通道
共享（ARCH §6.5 双通道）。
"""

import re

__all__ = [
    "HISTORY_RECALL_FAR_ANSWER_CHARS",
    "HISTORY_RECALL_MAX_CHARS",
    "HISTORY_RECALL_RECENT_ANSWER_CHARS",
    "HISTORY_RECALL_RECENT_TURNS",
    "HISTORY_RECALL_WINDOW_TURNS",
    "RECALL_QUERY_MAX_CHARS",
    "assemble_history_recall",
    "extract_answer_skeleton",
    "render_history_recall",
]

#: 装配窗口（与 TurnRunner `HISTORY_WINDOW_TURNS` 对齐——不扩窗，SQL limit 之外的二次夹取）
HISTORY_RECALL_WINDOW_TURNS = 6
#: 总软顶字符数（≈4K token；溢出丢最旧轮）
HISTORY_RECALL_MAX_CHARS = 6000
#: 近轮放宽轮数（最近 N 轮用更大答案预算）
HISTORY_RECALL_RECENT_TURNS = 2
#: 近轮答案预算（近全文——单轮骨架字符上限）
HISTORY_RECALL_RECENT_ANSWER_CHARS = 2000
#: 远轮答案预算（骨架——标题 + 每节首句的字符上限）
HISTORY_RECALL_FAR_ANSWER_CHARS = 700
#: 单轮**问题**预算（评审 P3-7）：问题全文也可能超长（单轮 9000 字问题会击穿总软顶）——
#: 裁剪保留问句前半（主题与约束通常在前），超长问答轮仍可装配而非丢弃
RECALL_QUERY_MAX_CHARS = 2000
#: 每节首句的抽取上限（超出即按句末截断并补省略号）
_SECTION_SENTENCE_CHARS = 200

#: markdown 标题行（H2~H6；H1 视作首段标题不拆节——H2/H3 为契约主力）
_HEADING_RE = re.compile(r"^(#{2,6})\s+(\S.*)$")
#: 代码围栏开关行（``` 或 ~~~ 起始；fence 内的 `#` 不判标题）
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
#: 句末标点（中英混排口径）
_SENTENCE_ENDS = "。！？；.!?;"
#: 截断省略号
_ELLIPSIS = "…"

#: 空骨架的渲染占位（空答案轮不产条目——不占预算、不留空段）
_EMPTY = ""


def _first_sentence(text: str, *, max_chars: int = _SECTION_SENTENCE_CHARS) -> str:
    """取文本首个句子（到句末标点为止；无句末标点则按字符上限截断）。"""
    stripped = text.strip()
    if not stripped:
        return ""
    for index, char in enumerate(stripped):
        if char in _SENTENCE_ENDS:
            candidate = stripped[: index + 1]
            return candidate if len(candidate) <= max_chars else candidate[:max_chars] + _ELLIPSIS
    return stripped if len(stripped) <= max_chars else stripped[:max_chars] + _ELLIPSIS


def _clip(text: str, max_chars: int) -> str:
    """按字符预算截断（补省略号且总长不超预算；预算过小时返回空串）。"""
    if max_chars <= 0:
        return _EMPTY
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    if max_chars <= len(_ELLIPSIS):
        return _ELLIPSIS[:max_chars]
    return stripped[: max_chars - len(_ELLIPSIS)] + _ELLIPSIS


def _first_paragraph(lines: list[str]) -> tuple[list[str], int]:
    """抽取首个段落（到首个空行或首个标题为止）——返回 (段落行, 下一行索引)。"""
    paragraph: list[str] = []
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        heading = _HEADING_RE.match(line) if not in_fence else None
        if heading is not None:
            break
        if not line.strip():
            if paragraph:
                break  # 段落结束（首段只到第一个空行）
            index += 1
            continue
        paragraph.append(line.strip())
        index += 1
    return paragraph, index


def extract_answer_skeleton(answer_md: str, *, max_chars: int) -> str:
    """答案正文 → 骨架文本（首段 + H2/H3 标题行 + 每节首句），按 `max_chars` 截断。

    - 无标题形态：首段 + 全文首句（首段已含首句时不重复）；
    - 代码围栏内的 `#` 注释不误判为标题（fence 状态机）；
    - 空正文 → 空串（调用方据此跳过该轮，不产条目）。
    """
    if not isinstance(answer_md, str) or not answer_md.strip():
        return _EMPTY
    lines = answer_md.splitlines()
    chunks: list[str] = []
    seen: set[str] = set()

    paragraph, cursor = _first_paragraph(lines)
    paragraph_text = _EMPTY
    if paragraph:
        # **整段保留**（评审 P2-3）：中文答案常按句换行，只取 paragraph[0] 会丢中段；
        # 预算由 `_clip` 统一兜底——首段是结论先行段，价值密度高于骨架其余部分。
        paragraph_text = "\n".join(paragraph)
        chunks.append(paragraph_text)
        seen.add(paragraph[0])
        seen.add(paragraph_text)

    has_heading = False
    in_fence = False
    index = cursor
    while index < len(lines):
        line = lines[index]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            index += 1
            continue
        heading = _HEADING_RE.match(line) if not in_fence else None
        if heading is None:
            index += 1
            continue
        has_heading = True
        chunks.append(line.strip())
        # 本节首句：标题后第一个非空、非标题、非围栏行
        probe = index + 1
        probe_fence = False
        while probe < len(lines):
            candidate = lines[probe]
            if _FENCE_RE.match(candidate):
                probe_fence = not probe_fence
                probe += 1
                continue
            if (
                _HEADING_RE.match(candidate) and not probe_fence
            ):  # 空节（下一节紧邻）——本节无首句
                break
            sentence = _first_sentence(candidate) if not probe_fence else ""
            if sentence:
                if sentence not in seen:
                    chunks.append(sentence)
                    seen.add(sentence)
                break
            probe += 1
        index += 1

    if not has_heading and paragraph:
        # 退化形态：首段 + 全文首句（首段之外的首句；已覆盖时不重复）
        body_sentence = _first_sentence(" ".join(lines))
        if (
            body_sentence
            and body_sentence not in seen
            and body_sentence not in paragraph_text
        ):
            chunks.append(body_sentence)

    return _clip("\n".join(chunk for chunk in chunks if chunk), max_chars)


def _render_item(item: dict[str, str]) -> str:
    """单轮渲染形态（Q/A 两行——与 rewrite 历史块 `_format_history` 同族）。"""
    return f"Q: {item.get('q', '')}\nA: {item.get('a', '')}"


def render_history_recall(items: list[dict[str, str]]) -> str:
    """装配条目 → 提示词文本块（空装配空串——非 recall 变体与空历史同值）。"""
    if not items:
        return _EMPTY
    return "\n".join(_render_item(item) for item in items)


def assemble_history_recall(turns: object) -> list[dict[str, str]]:
    """(question, answer_md) 时间正序序列 → history_recall 条目（时间正序）。

    预算分配（v2 §3.4）：从最新轮往回装配——最近 `HISTORY_RECALL_RECENT_TURNS` 轮用
    `HISTORY_RECALL_RECENT_ANSWER_CHARS` 预算（近全文），其余用
    `HISTORY_RECALL_FAR_ANSWER_CHARS`（骨架）；累计渲染长度超
    `HISTORY_RECALL_MAX_CHARS` 即停止（**溢出丢最旧**，最新轮恒保留）；最多
    `HISTORY_RECALL_WINDOW_TURNS` 轮。空答案轮不产条目；畸形输入（非序列）回落空装配。
    """
    if not isinstance(turns, (list, tuple)):
        return []
    windowed = list(turns)[-HISTORY_RECALL_WINDOW_TURNS:]
    kept_reversed: list[dict[str, str]] = []
    total = 0
    for offset, entry in enumerate(reversed(windowed)):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        question, answer_md = entry
        budget = (
            HISTORY_RECALL_RECENT_ANSWER_CHARS
            if offset < HISTORY_RECALL_RECENT_TURNS
            else HISTORY_RECALL_FAR_ANSWER_CHARS
        )
        skeleton = extract_answer_skeleton(answer_md, max_chars=budget)
        if not skeleton:
            continue
        # 问题全文亦受预算（评审 P3-7）：超长问句裁剪而非丢弃该轮
        item = {"q": _clip(str(question), RECALL_QUERY_MAX_CHARS), "a": skeleton}
        rendered_length = len(_render_item(item)) + 1  # +1 为条目分隔符
        if kept_reversed and total + rendered_length > HISTORY_RECALL_MAX_CHARS:
            break  # 溢出丢最旧：更旧的轮不再装配
        kept_reversed.append(item)
        total += rendered_length
    return list(reversed(kept_reversed))
