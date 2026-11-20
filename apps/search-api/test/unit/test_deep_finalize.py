"""unit 层：finalize 节点（M2 批次 4——ARCH §4.2 finalize 行 / DEV §5.4 五步闭环）。

钉住的语义（冻结契约 + 盘点钉点，逐条对应测试）：
- **纯编排面零 LLM 零 IO**：核对/去重/回填全是确定性规则（模块无 build_chat_model
  ——ARCH :477「finalize: 引用统一编号去重 + [n]↔来源双向核对(规则)」）；
- **[n]↔sources 双向核对**（api.md §2.5「ref_no 与 [n] 一一对应」）：
  - text→ledger 方向：[n] 越界（n 不在账本 ref_no 集）标记剔除——不伪造编号
    （synthesis._strip_out_of_range_citations 同款语义；**有效性=ref_no 集合**
    而非条目计数——账本空洞形态 [n] 无卡可依即剔除）；
  - ledger→编号方向：账本统一编号核验（ref_no 重复 / URL 重复 / 编号缺失计数
    ——research/supplement 去重纪律的防御性复验；落库面由 TurnRunner
    _publish_and_persist 同事务结构性保证，finalize 承载账本侧核验）；
- **warning 时间线事件（盘点钉死载体——decisions[2]）**：越界剔除计数挂
  stage(write,done) extra 可选键 `citation_dropped`（仅 >0 携带——键可省先例
  v1.12 images；data-model §1.4:169 event_type 枚举冻结零扩容）+ state.errors[]
  诊断面 + AgentLog WARN 三通道（fuse_rerank.py:304-307 同款先例）；
- **write done 帧唯一归属 finalize**（盘点 decisions[3]——writer 只发 running 推进
  帧，规避双 done 帧观感）：extra.sections = 大纲节数（writer 失败兜底轮=0——
  经 state.errors write/llm_call_failed 标记判定）；
- **引用统一编号去重**：相邻重复标记折叠（[1][1]→[1]；并列引用 [1][3] 契约合法
  ——citation_rules 第 2 条，不动）；
- **报告持久化**：规范化正文写 answer_md（TurnRunner 终态落库键）+ report_md 镜像
  （DeepState:182 writer/finalize 产出键——两键终值等值）；
- **usage_total 终值回填**：configurable 注入 usage_collector（guards 先例——
  注入面可选）→ state.usage_total = collector.total()["total_tokens"]（终值覆盖
  guards 检查点快照；done.usage 数据源仍=collector——turn_service.py:1467 不变）；
  collector 异常视同零用量（guards 同款）；无注入面零键；
- markdown 链接 [text](url) 不受引用核对影响（_CITATION_RE 仅纯数字标记——
  synthesis 同款边界）；空正文防御（零键不伪造持久化）；
- 节点纯函数：零 DB/EventBus；取消感知 ensure_not_canceled 入口接线（R14② 家族
  先例）；update 键 ⊆ DeepState.__annotations__（R14①）。
"""

import pytest

from app.agent.guards import TurnCanceledSignal
from app.agent.nodes import deep_finalize
from app.agent.nodes.deep_finalize import finalize
from app.agent.state import DeepState


_OUTLINE: list[dict[str, str]] = [
    {"section": "背景", "intent": "问题背景"},
    {"section": "分析", "intent": "证据综合"},
]


def _sources(*refnos: int) -> list[dict[str, object]]:
    return [
        {"url": f"https://s.example/{n}", "title": f"来源{n}", "snippet": "片段", "ref_no": n}
        for n in refnos
    ]


_STATE: dict[str, object] = {
    "outline": list(_OUTLINE),
    "collected_sources": _sources(1, 2),
    "answer_md": "# 报告\n\n## 摘要\n\n要点[1]。补充[2]。\n\n## 结论\n\n收束[1]。",
}


class _FakeCollector:
    """UsageCollector 假体（guards 注入面同款形态）。"""

    def __init__(self, total: dict[str, int] | Exception) -> None:
        self._total = total

    def total(self) -> dict[str, int]:
        if isinstance(self._total, Exception):
            raise self._total
        return dict(self._total)


def _install(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, dict[str, object]]]:
    """stage 信号记录器（finalize 零 LLM——仅事件面注入）。"""
    frames: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(
        deep_finalize,
        "emit_stage",
        lambda key, status, **kw: frames.append((key, status, kw)),
    )
    return frames


def _state_with(**overrides: object) -> dict[str, object]:
    state = {**_STATE}
    state.update(overrides)
    return state


def _config(**extra: object) -> dict[str, object]:
    configurable: dict[str, object] = {"thread_id": "th-1", "turn_id": "t-1"}
    configurable.update(extra)
    return {"configurable": configurable}


# ==== [n]↔sources 双向核对：text→ledger 方向（越界剔除）====


async def test_out_of_range_citations_removed_with_warning_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """越界引用剔除（[99]/[0] 不在账本）：正文标记移除不伪造编号；warning 三通道
    ——done extra.citation_dropped=2（>0 携带）+ errors[] 聚合留痕；合法 [1]/[2]
    原样保留（ref_no 首现冻结只增不改——重编号不可发生）。"""
    frames = _install(monkeypatch)
    text = "# 报告\n\n要点[1]。越界[99]。零[0]。补充[2]。\n\n收束[1]。"

    update = await finalize(_state_with(answer_md=text), _config())

    assert update["answer_md"] == "# 报告\n\n要点[1]。越界。零。补充[2]。\n\n收束[1]。"
    assert update["errors"] == [
        {"stage": "finalize", "reason": "citation_out_of_range", "dropped": 2}
    ]
    assert frames == [("write", "done", {"extra": {"sections": 2, "citation_dropped": 2}})]


async def test_valid_citations_untouched_no_warning_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部引用合法：正文零改动；done extra 零 citation_dropped 键（键可省——
    v1.12 images 先例）；零 errors 留痕。"""
    frames = _install(monkeypatch)

    update = await finalize(_state_with(), _config())

    assert update["answer_md"] == _STATE["answer_md"]
    assert "errors" not in update
    assert frames == [("write", "done", {"extra": {"sections": 2}})]


async def test_citation_validity_uses_refno_set_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有效性=账本 ref_no 集合而非条目计数：账本 {1,3}（空洞形态）→ [2] 无卡
    剔除、[3] 保留（len(sources)==2 若按计数判 [3] 即误杀）。"""
    frames = _install(monkeypatch)
    text = "甲[1]乙[2]丙[3]。"

    update = await finalize(
        _state_with(collected_sources=_sources(1, 3), answer_md=text), _config()
    )

    assert update["answer_md"] == "甲[1]乙丙[3]。"
    assert frames[0][2]["extra"]["citation_dropped"] == 1


async def test_adjacent_duplicate_markers_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引用统一编号去重（text 方向）：相邻重复标记 [1][1]→[1]（去重后合法零剔除
    零 warning）；并列引用 [1][2] 契约合法原样保留（citation_rules 第 2 条）。"""
    frames = _install(monkeypatch)

    update = await finalize(
        _state_with(answer_md="甲[1][1]乙[1][2]丙。"), _config()
    )

    assert update["answer_md"] == "甲[1]乙[1][2]丙。"
    assert "errors" not in update
    assert frames == [("write", "done", {"extra": {"sections": 2}})]


async def test_markdown_links_not_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """markdown 链接 [text](url) 与脚注形态不受引用核对影响（_CITATION_RE 仅纯
    数字标记——synthesis 同款边界）。"""
    _install(monkeypatch)
    text = "见[文档](https://x.example/a)与[^1]注。事实[1]。"

    update = await finalize(_state_with(answer_md=text), _config())

    assert update["answer_md"] == text


# ==== [n]↔sources 双向核对：ledger→编号方向（统一编号核验）====


async def test_ledger_duplicate_refno_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账本 ref_no 重复（防御性复验——research/supplement 去重纪律）：errors[]
    ledger_inconsistent 留痕（计数面）；有效性集合去重后 [2] 无卡剔除。"""
    _install(monkeypatch)
    dup = [{"url": "https://a.dev/1", "title": "A", "ref_no": 1}]

    update = await finalize(
        _state_with(
            # 同 ref_no 不同 URL（隔离 duplicate_refno 维度——同 URL 形态归下用例）
            collected_sources=dup + [{"url": "https://b.dev/2", "title": "B", "ref_no": 1}],
            answer_md="甲[1]乙[2]。",
        ),
        _config(),
    )

    ledger_entry = next(
        e for e in update["errors"] if e["reason"] == "ledger_inconsistent"
    )
    assert ledger_entry["stage"] == "finalize"
    assert ledger_entry["duplicate_refno"] == 1
    assert ledger_entry["duplicate_url"] == 0
    assert ledger_entry["missing_refno"] == 0
    assert update["answer_md"] == "甲[1]乙。"  # 有效性集={1}


async def test_ledger_duplicate_url_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账本 URL 重复（不同 ref_no 同 URL——账本卫生面）：errors[] 计数留痕；
    引用核对不受影响（两卡均存在）。"""
    _install(monkeypatch)
    ledger = [
        {"url": "https://a.dev/1", "title": "A", "ref_no": 1},
        {"url": "https://a.dev/1", "title": "A-dup", "ref_no": 2},
    ]

    update = await finalize(
        _state_with(collected_sources=ledger, answer_md="甲[1]乙[2]。"), _config()
    )

    ledger_entry = next(
        e for e in update["errors"] if e["reason"] == "ledger_inconsistent"
    )
    assert ledger_entry["duplicate_url"] == 1
    assert ledger_entry["duplicate_refno"] == 0
    assert update["answer_md"] == "甲[1]乙[2]。"  # 双卡在账——引用合法


async def test_ledger_missing_refno_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账本条目缺 ref_no（残缺形态）：errors[] 计数留痕；该条不进有效集。"""
    _install(monkeypatch)
    ledger = [
        {"url": "https://a.dev/1", "title": "A", "ref_no": 1},
        {"url": "https://b.dev/2", "title": "B"},  # 缺 ref_no
    ]

    update = await finalize(
        _state_with(collected_sources=ledger, answer_md="甲[1]乙[2]。"), _config()
    )

    ledger_entry = next(
        e for e in update["errors"] if e["reason"] == "ledger_inconsistent"
    )
    assert ledger_entry["missing_refno"] == 1
    assert update["answer_md"] == "甲[1]乙。"  # [2] 无卡剔除


# ==== 报告持久化 + usage_total 终值回填 ====


async def test_answer_md_and_report_md_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规范化正文写 answer_md（TurnRunner 终态落库键）+ report_md 镜像（DeepState
    writer/finalize 产出键——两键终值等值）。"""
    _install(monkeypatch)
    text = "要点[1]。越界[9]。"

    update = await finalize(
        _state_with(answer_md=text), _config()
    )

    assert update["answer_md"] == "要点[1]。越界。"
    assert update["report_md"] == update["answer_md"]
    assert set(update) <= set(DeepState.__annotations__)  # R14①


async def test_usage_total_final_backfill_from_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """usage_total 终值回填：configurable 注入 usage_collector → state.usage_total=
    collector.total()["total_tokens"]（终值覆盖 guards 检查点快照语义）；无注入面
    零键（guards 先例——注入面可选）。done.usage 数据源仍=collector
    （turn_service.py:1467——本节点只回填 state 镜像）。"""
    frames = _install(monkeypatch)

    update = await finalize(
        _state_with(), _config(usage_collector=_FakeCollector({"total_tokens": 12345}))
    )
    assert update["usage_total"] == 12345

    update = await finalize(_state_with(), _config())
    assert "usage_total" not in update


async def test_usage_total_collector_exception_counts_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """collector 异常视同零用量（guards 同款口径——观测面不阻塞主链路）。"""
    _install(monkeypatch)

    update = await finalize(
        _state_with(), _config(usage_collector=_FakeCollector(RuntimeError("boom")))
    )
    assert update["usage_total"] == 0


# ==== write done 帧唯一归属（盘点 decisions[3]）====


async def test_done_frame_sections_from_outline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """done 帧由 finalize 唯一发射（writer 只发 running 推进帧——规避双 done）：
    extra.sections=大纲节数。"""
    frames = _install(monkeypatch)

    await finalize(_state_with(), _config())

    assert frames == [("write", "done", {"extra": {"sections": 2}})]


async def test_done_frame_sections_zero_when_writer_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """writer 失败兜底轮（state.errors 含 write/llm_call_failed 标记）→
    sections=0（诚实留痕——机械速览/partial 非分节正文；writer 循环钉语义）。"""
    frames = _install(monkeypatch)

    await finalize(
        _state_with(errors=[{"stage": "write", "reason": "llm_call_failed"}]),
        _config(),
    )

    assert frames == [("write", "done", {"extra": {"sections": 0}})]


async def test_empty_answer_defensive_no_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空正文防御（Studio 直跑残缺形态）：零 answer_md/report_md 键（不伪造持久化）；
    done 帧照常收束。"""
    frames = _install(monkeypatch)

    update = await finalize(_state_with(answer_md=None), _config())

    assert "answer_md" not in update and "report_md" not in update
    assert frames == [("write", "done", {"extra": {"sections": 2}})]


# ==== 纯编排面 + 家族纪律 ====


def test_finalize_module_has_no_llm_machinery() -> None:
    """纯编排面零 LLM（ARCH :477「规则」核对——模块无模型工厂面）。"""
    assert not hasattr(deep_finalize, "build_chat_model")
    assert not hasattr(deep_finalize, "build_light_model")


async def test_canceled_at_entry_raises_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入口取消感知（R14②，同 planner/research/reflect/supplement/writer 家族先例）。"""
    _install(monkeypatch)
    config = _config(cancel_flags={"t-1": "user"})

    with pytest.raises(TurnCanceledSignal) as exc_info:
        await finalize(_state_with(), config)

    assert exc_info.value.reason == "user"


async def test_writes_only_deepstate_declared_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update 键 ⊆ DeepState.__annotations__（R14①）——finalize 触达
    answer_md/report_md/usage_total/errors 四键域。"""
    frames = _install(monkeypatch)

    update = await finalize(
        _state_with(), _config(usage_collector=_FakeCollector({"total_tokens": 1}))
    )

    assert set(update) <= set(DeepState.__annotations__)
