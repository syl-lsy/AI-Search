"""integration：deep planner 节点真 LLM 结构冒烟（requires_llm——chat-standard 经
LiteLLM 网关；M2 批次 2 planner 节点五步闭环 DoD 2）。

口径（DEV §4 integration / §5.4 DoD 2，同 test_rewrite_node.py 先例）：
- **断言结构不断言文案**——subtopics 数量 ∈ [1, spec.deep.max_subagents]、outline 非空
  且每节含非空 section/intent、query_type ∈ 7 类枚举、time_range ∈ 枚举∪{None}；
  拆分/大纲措辞质量归 M4 评测线；
- 走**真 deep 子图 astream**（经主图壳同款调用面）：节点内 get_stream_writer 在
  runnable context 内，stage(plan, done, extra.subtopics) 信号一并取证（api.md §7.4）；
- 提示词消费真资产中心快照（L1/L4 均可；快照不可用回落内置默认——deep.planner 占位符
  契约集含 query/history，节点渲染变量全供给，两态均可跑）；
- 网关/chat-standard 别名不可达时 skip（记录到 skipped，不算失败）。
"""

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.requires_llm

#: deep.planner 输出 7 类枚举（nacos-config.md §1 deep.planner 行；无 navigational）
_PLANNER_QUERY_TYPES = {"factoid", "list", "comparison", "multi_facet", "temporal", "how_to", "other"}


def _chat_alias_ready() -> str | None:
    """网关/chat-standard 别名探活；不可达原因字符串（None=就绪）。不打印任何凭据。"""
    from app.core.config import litellm_settings

    try:
        resp = httpx.get(
            str(litellm_settings.base_url).rstrip("/") + "/v1/models",
            headers={
                "Authorization": "Bearer " + litellm_settings.api_key.get_secret_value()
            },
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 —— 探活失败统一 skip
        return f"LiteLLM 网关不可达（{type(exc).__name__}）"
    ids = {m.get("id") for m in resp.json().get("data", [])}
    if "chat-standard" not in ids:
        return "chat 别名未装载（chat-standard 不在网关模型清单）"
    return None


async def test_planner_real_llm_structure_via_deep_graph() -> None:
    """真 LLM 冒烟（经 deep 子图直跑）：stage(plan,done,extra.subtopics) + DeepState
    产出结构齐全（subtopics ≤ spec.deep.max_subagents / outline 必填 / 7 类枚举）。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)

    from app.agent.graphs.deep import graph as deep_graph
    from app.agent.state import DeepState
    from app.config_center.defaults import SPEC_CONFIG

    state: dict[str, Any] = {
        "query": "LangGraph 和 deepagents 是什么关系？各自适合什么场景？",
        "query_lang": "zh",
        "history": [
            {"q": "LangGraph 是什么", "a_head": "LangGraph 是 LangChain 的图编排框架…"},
        ],
        "spec": SPEC_CONFIG,  # 真实内置默认 spec（deep.max_subagents=3 / llm_timeout_s=60 全套）
        "chat_model_key": "qwen",
    }
    config = {
        "configurable": {
            "thread_id": "it-planner-th",
            "turn_id": "it-planner-turn",
            "model_mapping": {"chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}},
        }
    }

    plan_signals: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    # 子图直跑（无 subgraphs=True）：顶层流为 (stream_mode, chunk) 二元组
    async for stream_mode, chunk in deep_graph.astream(
        state, config=config, stream_mode=["custom", "updates"]
    ):
        if stream_mode == "custom" and chunk.get("type") == "stage":
            if chunk.get("stage_key") == "plan":
                plan_signals.append(chunk)
        elif stream_mode == "updates":
            for node_update in chunk.values():
                final.update(node_update)

    # stage 信号：done 帧恒带 extra.subtopics（running 已由 TurnRunner 图前发布——§4.3）
    assert [(s["status"]) for s in plan_signals] == ["done"]
    extra = plan_signals[-1].get("extra") or {}
    assert isinstance(extra.get("subtopics"), int)

    # DeepState 产出结构（断言结构不断言文案）
    max_subagents = int(SPEC_CONFIG["deep"]["max_subagents"])
    assert isinstance(final["subtopics"], list)
    assert 1 <= len(final["subtopics"]) <= max_subagents  # 夹取域（nacos-config §3 deep 节）
    assert all(isinstance(s, str) and s.strip() for s in final["subtopics"])
    assert isinstance(final["outline"], list) and final["outline"]  # outline 必填非空
    for section in final["outline"]:
        assert isinstance(section, dict)
        assert isinstance(section.get("section"), str) and section["section"].strip()
        assert isinstance(section.get("intent"), str) and section["intent"].strip()
    assert final["query_type"] in _PLANNER_QUERY_TYPES  # 7 类枚举（无 navigational）
    assert final["time_range"] in {"day", "week", "month", "year", None}
    assert "degraded" not in final  # 成功路径零 degraded（兜底不置 degraded 契约）
    assert set(final) <= set(DeepState.__annotations__)  # 无未声明字段
    # done.extra.subtopics 与 state.subtopics 一致（§7.4 extra 语义）
    assert extra["subtopics"] == len(final["subtopics"])
