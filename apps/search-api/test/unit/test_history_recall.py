"""unit 层：历史装配第二通道 `history_recall` 的骨架截断纯函数（ARCH §6.5 v1.51）。

钉住的语义（契约 D5 / nacos-config.md §1 v1.27 `{{history_block}}` 渲染内容契约）：
- 每轮装配形态 = **问题全文 + 答案首段 + markdown 标题骨架**（H2/H3 标题行 + 每节首句）；
- 最近 1~2 轮**放宽为更大预算**（近轮近全文、远轮骨架的简单动态分配）；
- **总软顶 6000 字符**，溢出**丢最旧**；窗口 6 轮（不扩窗）；
- 纯函数边界：输入 (question, answer_md) 元组序列（时间正序），零 DB / 零 LLM。

如实注记（R18）：骨架截断为工程创新、业界无对话历史截断先例——本文件 fixture 锁配方，
还原度不引外部背书，M4 评测探针（回顾类复述完整度）另立基线。
"""

import pytest

from app.utils.history_recall import (
    HISTORY_RECALL_FAR_ANSWER_CHARS,
    HISTORY_RECALL_MAX_CHARS,
    HISTORY_RECALL_RECENT_ANSWER_CHARS,
    HISTORY_RECALL_RECENT_TURNS,
    HISTORY_RECALL_WINDOW_TURNS,
    RECALL_QUERY_MAX_CHARS,
    assemble_history_recall,
    extract_answer_skeleton,
    render_history_recall,
)

_MULTI_SECTION_ANSWER = """LangGraph 是本项目的主编排框架，采用 StateGraph 组织节点与边。

## 架构设计

节点即函数，边决定流转顺序，条件边用于按状态分支。

### 状态投影

主图壳为投影薄壳，输入投影与产出回填同名。

## 选型对比

pgvector 与 Milvus 各有取舍，本地自用优先 pgvector。
"""


# ---- 骨架解析（各形态 markdown）----


def test_skeleton_keeps_first_paragraph_and_headings_with_section_first_sentences() -> None:
    """多节 markdown：首段 + H2/H3 标题行 + 每节首句（不整段搬运）。"""
    skeleton = extract_answer_skeleton(_MULTI_SECTION_ANSWER, max_chars=2000)

    assert skeleton.startswith("LangGraph 是本项目的主编排框架")  # 首段（结论先行）
    assert "## 架构设计" in skeleton
    assert "### 状态投影" in skeleton
    assert "## 选型对比" in skeleton
    assert "节点即函数，边决定流转顺序，条件边用于按状态分支。" in skeleton  # 每节首句
    assert "主图壳为投影薄壳，输入投影与产出回填同名。" in skeleton
    assert "pgvector 与 Milvus 各有取舍，本地自用优先 pgvector。" in skeleton
    # 非首句细节不搬运（骨架语义：知道丢了什么，不重复全文）
    assert skeleton.count("LangGraph 是本项目的主编排框架") == 1


def test_skeleton_without_headings_degrades_to_first_paragraph_plus_first_sentence() -> None:
    """无标题 markdown：首段 + 全文首句退化形态（首句不重复搬运）。"""
    answer = "这是答案首段，包含结论先行的一句话。后续还有第二句细节。"

    skeleton = extract_answer_skeleton(answer, max_chars=2000)

    assert skeleton.startswith("这是答案首段，包含结论先行的一句话。")
    assert skeleton.count("这是答案首段，包含结论先行的一句话。") == 1


def test_skeleton_ignores_hash_lines_inside_code_fences() -> None:
    """代码块内的 `## 标题` 不误判为 markdown 标题（fence 状态机）。

    【测试质量修复（评审 P2-4）】原 fixture 用单 `#` 行——`_HEADING_RE` 本就只匹配
    `#{2,6}`，单 `#` 在 fence 外也不会命中，删掉 fence 状态机测试照样绿（恒真）。
    改 fence 内双 `##` 后：只有 fence 状态机生效才通过，恒假可被检出。
    """
    answer = """先给结论：用 StateGraph 编排。

```python
## 这不是标题，是注释
def build():
    pass
```

## 真实标题

这一节说明实现要点。第一句是这句。
"""

    skeleton = extract_answer_skeleton(answer, max_chars=2000)

    assert "## 真实标题" in skeleton
    assert "## 这不是标题" not in skeleton  # fence 内伪标题被状态机挡下


def test_skeleton_single_section_keeps_heading_and_section_first_sentence() -> None:
    """单节 markdown（仅一个 H2）：首段 + 该节标题行 + 该节首句；节内后续细节不搬运。

    多节形态已覆盖（上一条），本节锁**单节**边界：标题只出现一次、首句只取该节首个
    句末标点前的片段、同节后续正文（第二句/下一行）不进骨架。
    """
    answer = """先说结论：用 StateGraph 编排最合适。

## 实现要点

节点即函数，边决定流转顺序。后续还有第二句细节不应搬运。
再下一行也不搬运。
"""

    skeleton = extract_answer_skeleton(answer, max_chars=2000)

    assert skeleton.startswith("先说结论：用 StateGraph 编排最合适。")  # 首段（结论先行）
    assert skeleton.count("## 实现要点") == 1
    assert "节点即函数，边决定流转顺序。" in skeleton  # 该节首句
    assert "第二句细节" not in skeleton  # 同节后续句子不搬运（骨架语义）
    assert "再下一行" not in skeleton


def test_skeleton_empty_answer_yields_empty() -> None:
    """空答案 → 空骨架（不产条目）。"""
    assert extract_answer_skeleton("", max_chars=2000) == ""
    assert extract_answer_skeleton("   \n\n ", max_chars=2000) == ""


def test_skeleton_respects_char_budget() -> None:
    """单轮骨架超预算时按字符截断（预算由装配层按近/远轮分配）。"""
    answer = "结论段。" + "\n\n" + "\n\n".join(
        f"## 章节{i}\n\n{'内容' * 40}。" for i in range(30)
    )

    skeleton = extract_answer_skeleton(answer, max_chars=200)

    assert len(skeleton) <= 200


# ---- 装配层（近轮放宽 / 总软顶 / 窗口）----


def _turns(count: int, *, answer_body: str = "结论在这。") -> list[tuple[str, str]]:
    return [(f"问题{i}", f"{answer_body}{i}") for i in range(1, count + 1)]


def test_assemble_keeps_question_full_and_renders_qa_blocks() -> None:
    """装配条目 = 问题全文 + 骨架；渲染形态 `Q:…\\nA:…`（与 rewrite 历史块同族）。"""
    items = assemble_history_recall([("这是完整的问题原文吗？", "答案正文。")])

    assert len(items) == 1
    assert items[0]["q"] == "这是完整的问题原文吗？"
    assert "答案正文。" in items[0]["a"]
    text = render_history_recall(items)
    assert text.startswith("Q: 这是完整的问题原文吗？")
    assert "\nA: 答案正文。" in text


def test_assemble_recent_turns_get_larger_budget_than_far_turns() -> None:
    """近轮放宽：最近 `HISTORY_RECALL_RECENT_TURNS` 轮用近轮预算、其余用远轮预算。

    fixture 构造成**两档预算都真正触底**的形态（骨架原始长 ~2.6K > 近轮预算 2000）——
    否则「近轮更长」可能只是远轮被裁的副产物，断言不承担语义（首版 fixture 即此缺陷）。
    """
    head = "结论先行" * 240  # 单行首段 ~960 字符（必被预算裁）
    sections = "\n\n".join(
        f"## 章节{i}\n\n{'要点' + str(i)}{'细节' * 100}。" for i in range(8)  # 每节首句各异（不触发去重）
    )
    long_answer = head + "\n\n" + sections
    turns = [(f"问题{i}", long_answer) for i in range(1, 5)]

    items = assemble_history_recall(turns)

    recent = [len(item["a"]) for item in items[-HISTORY_RECALL_RECENT_TURNS:]]
    far = [len(item["a"]) for item in items[: -HISTORY_RECALL_RECENT_TURNS]]
    assert far and recent  # 4 轮装配：近 2 轮 + 远 2 轮
    for size in recent:
        assert HISTORY_RECALL_FAR_ANSWER_CHARS < size <= HISTORY_RECALL_RECENT_ANSWER_CHARS
    for size in far:
        assert size <= HISTORY_RECALL_FAR_ANSWER_CHARS
    assert HISTORY_RECALL_RECENT_TURNS >= 1


def test_assemble_drops_oldest_when_over_soft_cap() -> None:
    """总软顶 6000 字符：溢出**确发生**时丢最旧（最新轮恒保留，最旧轮被裁出装配）。"""
    turn_answer = "结论。" + ("内容" * 1200)  # 每轮约 2400 字符
    turns = [(f"问题{i}", turn_answer) for i in range(1, 6)]

    items = assemble_history_recall(turns)

    total = len(render_history_recall(items))
    assert total <= HISTORY_RECALL_MAX_CHARS
    assert items[-1]["q"] == "问题5"  # 最新轮恒保留
    # 软顶生效（非「刚好装下」的弱断言）：本 fixture 5 轮必然溢出 → 确丢弃且丢最旧一侧
    assert len(items) < len(turns)
    assert "问题1" not in [item["q"] for item in items]
    assert [item["q"] for item in items] == sorted(
        [item["q"] for item in items], key=lambda q: int(q.removeprefix("问题"))
    )  # 时间正序


def test_assemble_caps_turn_window() -> None:
    """窗口 6 轮（不扩窗）：第 7 轮不进装配（SQL 已 limit，装配层再夹一次防漂移）。"""
    turns = _turns(9)

    items = assemble_history_recall(turns)

    assert len(items) <= HISTORY_RECALL_WINDOW_TURNS
    assert items[-1]["q"] == "问题9"  # 取最近窗口（丢最旧）


def test_assemble_skips_empty_answers() -> None:
    """空答案轮不产条目（不占预算、不留空段）。"""
    items = assemble_history_recall([("问题1", ""), ("问题2", "有答案。")])

    assert [item["q"] for item in items] == ["问题2"]


def test_render_empty_items_yields_empty_string() -> None:
    """空装配 → 空串（synthesis 非 recall 变体与空历史均渲染空串）。"""
    assert render_history_recall([]) == ""


@pytest.mark.parametrize("bad", [None, "not-a-list", 123])
def test_assemble_tolerates_bad_input(bad: object) -> None:
    """防御口径：非序列输入 → 空装配（提示词通道零崩溃）。"""
    assert assemble_history_recall(bad) == []  # type: ignore[arg-type]


# ---- 评审修复轮补强（P2-3 多行首段 / P3-7 超长问题预算）----


def test_skeleton_keeps_multi_line_first_paragraph_intact() -> None:
    """多行首段（中文按句换行形态）整段保留：第 2/3 行不得丢失（评审 P2-3）。

    修复前 `_first_paragraph` 收集多行但骨架只取 `paragraph[0]`——与契约「答案首段」
    及本模块 docstring 两头不符；整段保留后 `_clip` 仍兜总预算。
    """
    answer = (
        "第一行给出结论。\n"
        "第二行是前提条件与约束。\n"
        "第三行补充口径边界。\n"
        "\n"
        "## 明细\n"
        "\n"
        "这一节说明实现细节。第一句是这句。\n"
    )

    skeleton = extract_answer_skeleton(answer, max_chars=2000)

    assert "第一行给出结论。" in skeleton
    assert "第二行是前提条件与约束。" in skeleton  # 修复前丢失
    assert "第三行补充口径边界。" in skeleton  # 修复前丢失
    assert "## 明细" in skeleton
    assert skeleton.index("第三行补充口径边界。") < skeleton.index("## 明细")  # 首段在标题前


def test_assemble_clips_overlong_question_to_budget() -> None:
    """单轮超长问题受预算裁剪：总渲染不超过 6000 软顶（评审 P3-7）。

    修复前问题全文无预算——9000 字问题渲染 ~9010 字符直接击穿软顶（历史通道失效）。
    """
    huge_question = "问" * 9000
    turns = [(huge_question, "答案骨架。")]

    items = assemble_history_recall(turns)

    assert items, "超长问题轮仍应装配（裁剪而非丢弃）"
    assert len(items[0]["q"]) <= RECALL_QUERY_MAX_CHARS
    assert len(render_history_recall(items)) <= HISTORY_RECALL_MAX_CHARS
