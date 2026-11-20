"""finalize 节点（M2 批次 4——ARCH §4.2 finalize 行 / DEV §5.4 五步闭环）。

引用统一编号去重 + [n]↔来源双向核对（规则面）+ sources 账本核验 + 报告持久化 +
usage_total 终值回填——**纯编排面零 LLM 零 IO**（核对/去重/回填全是确定性规则，
ARCH :477「finalize: 引用统一编号去重 + [n]↔来源双向核对(规则)·sources 全量落库·
报告持久化」；模块无模型工厂面——单测锁死）。

**[n]↔sources 双向核对（api.md §2.5「ref_no 与 [n] 一一对应」）**：
- **text→ledger 方向**：[n] 越界（n 不在账本 ref_no 集）标记剔除——不伪造编号
  （synthesis._strip_out_of_range_citations 同款语义）；**有效性=ref_no 集合**而非
  条目计数（账本空洞形态下 [n] 无卡可依即剔除）；ref_no 首现冻结只增不改
  （api.md §7.2:739）——合法编号零重编号（重编号会断 [n]↔来源卡映射）；
- **ledger→编号方向（引用统一编号核验）**：账本防御性复验——ref_no 重复 / URL 重复
  （不同 ref_no 同 URL）/ 编号缺失三项计数（research/supplement 去重纪律的收口面；
  落库面由 TurnRunner `_publish_and_persist` 同事务结构性保证——事件与行同短事务，
  图节点零 DB，finalize 承载账本侧核验，违例 errors[] 留痕不静默改账）。

**warning 时间线事件（盘点钉点 decisions[2]——载体钉死）**：越界剔除计数挂
stage(write,done) extra 可选键 `citation_dropped`（仅 >0 携带——键可省先例
api.md v1.12 images；data-model.md §1.4:169 event_type 枚举 stage|sources|images|
done|error|memory 冻结零扩容，stage extra 通用 dict 扩键先例链 v1.12/v1.14/v1.19/
v1.25/v1.26）+ state.errors[] 聚合诊断面 {stage:"finalize", reason:
"citation_out_of_range", dropped: k} + AgentLog WARN——fuse_rerank.py:304-307
rerank 兜底同款三通道先例。键名钉文字归收官环节（批次 3 gaps 上限先例）。

**write done 帧唯一归属本节点（盘点 decisions[3]）**：writer 只发 running 推进帧
（入口 撰写第 1 节 + 分节推进），本节点发唯一 stage(write,done) 收束时间线（规避双
done 帧观感）；extra.sections = 大纲节数（_clean_outline 与 writer 同源单一事实源），
writer 失败兜底轮=0（经 state.errors 的 {stage:"write", reason:"llm_call_failed"}
标记判定——writer 循环钉语义，机械速览/partial 非分节正文诚实留痕）。

**报告持久化**：规范化正文写 answer_md（TurnRunner 终态落库键）+ report_md 镜像
（DeepState「writer/finalize 产出」声明键——两键终值等值）；空正文防御零键（不伪造
持久化——Studio 直跑残缺形态）。

**usage_total 终值回填**：configurable 注入 usage_collector（TurnRunner
turn_service.py:2121 注入面，guards 先例同款——注入面可选，无注入面零键）→
state.usage_total = collector.total()["total_tokens"]（终值覆盖 guards 检查点快照
——state.py:185 注「终值回填归批次 4 finalize」）；**done.usage 数据源仍=collector**
（turn_service.py:1467 不变——本节点只回填 state 镜像）；collector 异常视同零用量
（guards 同款口径，观测面不阻塞主链路）。

**引用核对边界（与 fast synthesis 同款）**：_CITATION_RE 仅匹配纯数字标记——
markdown 链接 [text](url)、脚注 [^1] 不受影响；代码块内 [n] 同样参与核对（fast
synthesis 既有边界，如实继承）。相邻重复标记折叠 [1][1]→[1]（去重）；并列引用
[1][3] 契约合法原样保留（citation_rules 第 2 条）。

取消感知：ensure_not_canceled 入口接线（R14②，同 planner/research/reflect/
supplement/writer 家族先例）。update 键 ⊆ DeepState.__annotations__（R14①）。
"""

import re
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.guards import ensure_not_canceled
from app.agent.graphs.signals import emit_stage
from app.agent.nodes.deep_writer import _clean_outline
from app.core.logger import AgentLog

__all__ = ["finalize"]

#: 引用标记（synthesis._CITATION_RE 同款——仅纯数字，markdown 链接/脚注不受影响）
_CITATION_RE = re.compile(r"\[(\d+)\]")

#: 相邻重复标记折叠（引用统一编号去重 text 方向——[1][1]→[1]；并列 [1][2] 不受影响）
_ADJACENT_DUP_RE = re.compile(r"(\[\d+\])\1+")


def _ledger_refnos(sources: Any) -> set[int]:
    """账本有效 ref_no 集合（[n] 有效性判据——集合语义非计数语义）。"""
    if not isinstance(sources, list):
        return set()
    return {
        int(item["ref_no"])
        for item in sources
        if isinstance(item, Mapping) and isinstance(item.get("ref_no"), (int, float))
    }


def _ledger_issues(sources: Any) -> dict[str, int]:
    """账本统一编号核验（ledger→编号方向）：ref_no 重复 / URL 重复 / 编号缺失计数。

    落库面由 TurnRunner 同事务结构性保证（_publish_and_persist——事件与 sources 行
    同短事务），本核验为账本侧防御性复验（reducer 合流/收集面漂移的收口）；违例
    errors[] 留痕，不静默改账（ref_no 冻结——改账即断 [n]↔卡映射）。
    """
    issues = {"duplicate_refno": 0, "duplicate_url": 0, "missing_refno": 0}
    if not isinstance(sources, list):
        return issues
    seen_refnos: set[int] = set()
    seen_urls: set[str] = set()
    for item in sources:
        if not isinstance(item, Mapping):
            continue
        ref = item.get("ref_no")
        if isinstance(ref, (int, float)):
            n = int(ref)
            if n in seen_refnos:
                issues["duplicate_refno"] += 1
            else:
                seen_refnos.add(n)
        else:
            issues["missing_refno"] += 1
        url = item.get("url")
        if isinstance(url, str) and url:
            if url in seen_urls:
                issues["duplicate_url"] += 1
            else:
                seen_urls.add(url)
    return issues


def _normalize_citations(text: str, valid: set[int]) -> tuple[str, int]:
    """引用归一（text→ledger 方向）：相邻重复标记折叠 → 越界标记剔除。

    返回 (规范化正文, 剔除计数)；先折叠后剔除（[9][9] 计 1 次剔除——重复标记
    是同一引用的重复表述非多卡引用）。
    """
    deduped = _ADJACENT_DUP_RE.sub(r"\1", text)
    dropped = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal dropped
        if int(match.group(1)) in valid:
            return match.group(0)
        dropped += 1
        return ""

    return _CITATION_RE.sub(_replace, deduped), dropped


async def finalize(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """finalize 节点（deep 子图）：引用核对归一 + 账本核验 + 持久化投影 +
    usage_total 终值回填 + write done 帧收束；零 LLM 零 IO。"""
    ensure_not_canceled(config)  # 取消非故障——穿透重抛（TurnRunner _finish_canceled 收尾）

    state_map = state if isinstance(state, Mapping) else {}
    sources = state_map.get("collected_sources")
    text = state_map.get("answer_md")
    text = text if isinstance(text, str) else ""

    # 1) 账本核验（ledger→编号方向）+ 引用归一（text→ledger 方向）
    issues = _ledger_issues(sources)
    valid = _ledger_refnos(sources)
    normalized, dropped = _normalize_citations(text, valid)

    # 2) 持久化投影（answer_md 终态落库键 + report_md 镜像；空正文零键防御）
    update: dict[str, Any] = {}
    if text:
        update["answer_md"] = normalized
        update["report_md"] = normalized

    # 3) 诊断留痕（errors Annotated[add] 增量——聚合条目不逐标记展开）
    new_errors: list[dict[str, Any]] = []
    if dropped:
        new_errors.append(
            {"stage": "finalize", "reason": "citation_out_of_range", "dropped": dropped}
        )
        AgentLog(
            message=f"finalize 引用越界剔除 {dropped} 处（[n]↔sources 双向核对）",
            graph="deep",
            node="finalize",
        ).warning()
    if any(issues.values()):
        new_errors.append(
            {"stage": "finalize", "reason": "ledger_inconsistent", **issues}
        )
        AgentLog(
            message=f"finalize 账本核验违例：{issues}",
            graph="deep",
            node="finalize",
        ).warning()
    if new_errors:
        update["errors"] = new_errors

    # 4) usage_total 终值回填（注入面可选——guards 先例；done.usage 仍走 collector）
    configurable = (config or {}).get("configurable") or {}
    collector = configurable.get("usage_collector")
    if collector is not None:
        try:
            total = collector.total()
        except Exception:  # noqa: BLE001 —— collector 异常视同零用量（guards 同款）
            total = {}
        update["usage_total"] = int(total.get("total_tokens") or 0)

    # 5) write done 帧唯一收束（盘点 decisions[3]——writer 只发 running 推进帧）：
    #    sections=大纲节数；writer 失败兜底轮=0（errors 标记判定——writer 循环钉语义）
    errors_in = state_map.get("errors")
    writer_failed = any(
        isinstance(e, Mapping)
        and e.get("stage") == "write"
        and e.get("reason") == "llm_call_failed"
        for e in (errors_in if isinstance(errors_in, list) else [])
    )
    outline = _clean_outline(state_map.get("outline"))
    extra: dict[str, Any] = {"sections": 0 if writer_failed else len(outline)}
    if dropped:
        extra["citation_dropped"] = dropped  # warning 载体（盘点 decisions[2]——仅 >0 携带）
    emit_stage("write", "done", extra=extra)
    return update
