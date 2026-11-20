"""integration：deep 整图收口真跑验证（plan 批次 4 闭环条件 :108 核销——「整图真轮
事件序列与 api §7.4 深度序列逐项比对记录 / 导出一致 / 注入超时后降级报告」的
integration 承载面）。

真依赖面（.env.test 通道）：
- **真 LLM 全链**：planner/research(deepagents)/reflect/supplement/writer 全部真模型
  （chat-standard 经 LiteLLM 网关，requires_llm 探活 skip 形态）——零 build_chat_model
  monkeypatch；
- **确定性源面**（LLM 行为是唯一真依赖——离线可复现，test_deep_research_node 同款
  纪律）：registry 搜索候选/读取正文确定性假体（搜索源真网面归 requires_mcp 层）；
- **快照链真路径**：save_snapshot 默认工厂（真 Postgres + 真 MinIO——缺凭据回落
  假体+真库播种快照行保 FK，两形态均验 backfill）；
- **真库**：migrated_db（回填断言面 + snapshots FK 宿主行）。

断言（api.md §7.4:863-868 深度主干逐帧相对序 + §7.2:802 done 契约）：
- 事件序列：plan done(extra.subtopics) → search running(extra.subagents) → sources
  批次 → read 帧 → reflect⇄search（真判定）→ reflect done → write running(撰写第 1 节)
  → token（writer messages 流）→ write done(extra.sections) → 零 error；
- answer_md 真报告（非桩文本、含摘要/结论结构、writer token 拼接==answer_md）；
- 快照回填链：snapshots 四键（snapshot_id 非空）→ backfill → sources.snapshot_id
  非空（有快照轮）；
- TurnRunner 级（真调度器）：done.usage 真值（total_tokens>0——批次 1 方案 A 真跑
  闭合）、token 事件、answer 持久化；
- 降级真轮（注入超时触发 degraded_writer）：done(finish=degraded, degraded_reason=
  timeout, degraded=True) + answer 含「研究未完成」+ 零 error（§7.2:803/:845）。

Langfuse trace：两用例 config 挂 build_turn_callbacks（trace_id=turn_id.hex、
session=thread_id）——trace 层级核验（root→deep→子代理）经 Langfuse API 于活体探针
步骤执行（batched ingestion 的 in-test 断言有时序脆弱面），本文件只锁 callbacks 在场。
"""

from typing import Any

import pytest
from sqlalchemy import select

import uuid  # noqa: E402 —— 模块级（seed 宿主行用）

from app.agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.requires_llm

_READ_CONTENT = (
    "# LangGraph 文档正文\n\nLangGraph 是低级编排框架，提供 StateGraph 与条件边等"
    "原语，适合需要精确控制多代理流程的场景。deepagents 在其上提供子代理中间件装配面。"
) * 5  # >2000 字符——快照摘录截断断言面


def _chat_alias_ready() -> str | None:
    """网关/chat-standard 别名探活；不可达原因字符串（None=就绪）。不打印任何凭据。"""
    import httpx

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


class _FullChainRegistry(ToolRegistry):
    """确定性搜索/读取假体（ToolRegistry 子类——LLM 行为唯一真依赖；继承 bind/
    resolve 家族供 TurnRunner 轮启动面消费——tools_registry.bind(:2089)）。"""

    def __init__(self) -> None:
        super().__init__()
        self.search_calls = 0
        self.read_calls: list[str] = []
        self._read_repeat: dict[str, int] = {}

    def resolve(self, mcp_payload: Any, query_lang: str) -> Any:
        from app.agent.tools.registry import SearchChain

        return SearchChain(sources=["tavily"], backup=None, degraded=False, detail="tavily")

    def resolve_read(self, mcp_payload: Any) -> Any:
        from app.agent.tools.registry import ReadChain

        return ReadChain(sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl")

    def credential_ready(self, server: str) -> bool:
        return True

    async def search(
        self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5,
        retry_sink: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls += 1
        return [
            {
                "url": "https://langchain-ai.github.io/langgraph/",
                "title": "LangGraph Overview",
                "snippet": "LangGraph 是低级编排框架，适合可控的多代理流程。",
                "score": 0.9,
                "origin": source,
            },
            {
                "url": "https://github.com/langchain-ai/deepagents",
                "title": "deepagents",
                "snippet": "deepagents 提供深度研究代理的中间件装配面。",
                "score": 0.8,
                "origin": source,
            },
        ]

    async def read(self, url: str, source: str, **kw: Any) -> str:
        self.read_calls.append(url)
        # 同 URL 重复读取返回「已至末尾」短文（防子代理对确定性正文反复读取烧循环
        # ——活体探针实证：同文重复 read → 子代理迭代至 recursion_limit → 慢轮）。
        self._read_repeat[url] = self._read_repeat.get(url, 0) + 1
        if self._read_repeat[url] > 1:
            return f"# {url}\n\n（该文档已在先前读取中完整呈现，已读至末尾，无新增内容。）"
        return f"# 正文 {url}\n\n{_READ_CONTENT}"


def _fullchain_config(turn_id: str, thread_id: str) -> dict[str, Any]:
    """图直跑 config（LLM 全真零 mock；观测 callbacks 在场——trace_id=turn_id.hex；
    usage_collector 注入=批次 1 方案 A 图直跑消费面——guards/finalize usage_total
    真值断言载体）。"""
    from app.core.observability import build_turn_callbacks, UsageCollector
    from app.agent.tools.registry import ReadChain, SearchChain

    registry = _FullChainRegistry()
    collector = UsageCollector()
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "model_mapping": {
                "chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}
            },
            "search_chain": SearchChain(
                sources=["tavily"], backup=None, degraded=False, detail="tavily"
            ),
            "read_chain": ReadChain(
                sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl"
            ),
            "tools_registry": registry,
            "usage_collector": collector,
        },
        "callbacks": [
            *build_turn_callbacks(
                thread_id=thread_id, turn_id=turn_id, user_id="u-1", mode="deep"
            ),
            # 生产同构（turn_service.py:1127-1134/:2190-2197）：usage collector 随
            # callbacks 并列注入（on_llm_end 累计面）——仅入 configurable 不入
            # callbacks 则 total() 恒 0（首轮真跑实测：usage_total=0 断言红根因）
            collector,
        ],
        "metadata": {
            "langfuse_session_id": thread_id,
            "langfuse_user_id": "u-1",
            "langfuse_tags": ["mode:deep"],
        },
    }
    return config


async def _seed_host_rows(thread_id: str, turn_id: str) -> None:
    """快照链宿主行（snapshots.turn_id/thread_id FK → 真库；user 经 migrated_db 补种）。"""
    from datetime import UTC, datetime

    from app.core.config import common_settings
    from app.db.models.thread import Thread
    from app.db.models.turn import Turn
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        session.add(
            Thread(
                id=uuid.UUID(thread_id),
                user_id=uuid.UUID(common_settings.default_user_id),
                title="deep 整图收口真跑",
            )
        )
        await session.flush()
        session.add(
            Turn(
                id=uuid.UUID(turn_id),
                thread_id=uuid.UUID(thread_id),
                user_id=uuid.UUID(common_settings.default_user_id),
                mode="deep",
                status="running",
                query="LangGraph 与 deepagents 的定位差异与各自适用场景",
                chat_model_key="qwen",
                model_name="chat-standard",
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()




async def test_full_chain_real_llm_event_sequence(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Any,
) -> None:
    """全链真 LLM（图直跑）：事件序列逐帧相对序对齐 api §7.4:863-868 + writer token
    拼接==answer_md + 快照回填链（snapshots snapshot_id→backfill→sources 行非空）。
    快照活体形态（真 MinIO）缺凭据时回落假体+真库播种快照行（FK 面）——两形态均验
    backfill。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)
    assert migrated_db is not None and migrated_db.name == "ai_search_test_db"

    from app.agent.graphs.deep import graph as deep_graph
    from app.config_center.defaults import SPEC_CONFIG

    thread_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
    await _seed_host_rows(thread_id, turn_id)

    # 快照链：MinIO 凭据在场 → save_snapshot 默认工厂真路径；缺 → 假体+真库播种行
    from app.core.config import minio_settings

    minio_live = bool(
        minio_settings.access_key.get_secret_value()
        and minio_settings.secret_key.get_secret_value()
    )
    if not minio_live:
        from app.agent.nodes import deep_research as deep_research_module
        from app.db.models.snapshot import Snapshot
        from app.db.session import get_session_factory

        async def fake_snapshot(**kwargs: Any) -> dict[str, Any]:
            return {
                "snapshot_id": str(uuid.uuid5(uuid.NAMESPACE_URL, kwargs["url"])),
                "url": kwargs["url"],
                "url_hash": "h" * 64,
                "object_key": f"snapshots/it/{kwargs['url']}",
                "size_bytes": 2048,
                "reused": False,
                "oversize": False,
            }

        monkeypatch.setattr(deep_research_module, "save_snapshot", fake_snapshot)
        async with get_session_factory()() as session:
            session.add_all(
                [
                    Snapshot(
                        id=uuid.uuid5(uuid.NAMESPACE_URL, url),
                        user_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
                        thread_id=uuid.UUID(thread_id),
                        turn_id=uuid.UUID(turn_id),
                        url=url,
                        url_hash="h" * 64,
                        object_key=f"snapshots/it/{url}",
                        content_type="text/markdown",
                        size_bytes=2048,
                        fetched_at=datetime.now(UTC),
                    )
                    for url in (
                        "https://langchain-ai.github.io/langgraph/",
                        "https://github.com/langchain-ai/deepagents",
                    )
                ]
            )
            await session.commit()

    state: dict[str, Any] = {
        "turn_id": turn_id,
        "thread_id": thread_id,
        "user_id": "00000000-0000-4000-8000-000000000001",
        "query": "LangGraph 与 deepagents 的定位差异与各自适用场景",
        "chat_model_key": "qwen",
        "history": [],
        "query_lang": "zh",
        # Agent Spec 热调值（合法配置面——Nacos §3 spec.deep 键域；非契约改动）：
        # - llm_timeout_s 150：单调用包络对「全报告流式 × thinking on」60s 初值实证
        #   贴边（活体探针：writer 流超 60s → TimeoutError → 兜底收束——真实发现，
        #   调优归 M4/评测线，测试轮取放宽形态验全报告路径）；
        # - recursion_limit 50：子代理循环上限 25 实证被高密度读取消耗
        #   （活体探针：22 次 read 帧 → GraphRecursionError → failed:true 隔离生效）
        #   ——放宽后验子代理正常收敛路径。
        "spec": {
            **SPEC_CONFIG,
            "deep": {
                **SPEC_CONFIG["deep"],
                "llm_timeout_s": 150,
                "recursion_limit": 50,
            },
        },
        "images": [],
    }
    stages: list[dict[str, Any]] = []
    sources_batches: list[dict[str, Any]] = []
    writer_tokens: list[str] = []
    errors: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for namespace, stream_mode, chunk in deep_graph.astream(
        state,
        config=_fullchain_config(turn_id, thread_id),
        stream_mode=["messages", "custom", "updates"],
        subgraphs=True,
    ):
        if stream_mode == "custom" and isinstance(chunk, dict):
            if chunk.get("type") == "stage":
                stages.append(chunk)
            elif chunk.get("type") == "sources":
                sources_batches.append(chunk)
            elif chunk.get("type") == "error":
                errors.append(chunk)
        elif stream_mode == "messages":
            meta = chunk[1] if isinstance(chunk, tuple) and len(chunk) > 1 else None
            if isinstance(meta, dict) and meta.get("langgraph_node") == "writer":
                message = chunk[0]
                content = getattr(message, "content", None)
                if isinstance(content, str) and content:
                    writer_tokens.append(content)
                elif isinstance(content, list):
                    writer_tokens.extend(
                        str(b.get("text") or "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
        elif stream_mode == "updates" and namespace == ():
            for node_update in chunk.values():
                if isinstance(node_update, dict):
                    final.update(node_update)

    # ---- 事件序列逐帧相对序（api.md §7.4:863-868 深度主干）----
    full_sequence = [(s["stage_key"], s["status"]) for s in stages]
    assert full_sequence[0] == ("plan", "done")  # running 归 TurnRunner 图前（§4.3）
    assert (stages[0].get("extra") or {}).get("subtopics", 0) >= 1
    keys = [k for k, _ in full_sequence]
    assert "search" in keys and "write" in keys and "reflect" in keys
    search_first = next(i for i, (k, _) in enumerate(full_sequence) if k == "search")
    search_running = full_sequence[search_first]
    assert search_running[1] == "running"
    assert (stages[search_first].get("extra") or {}).get("subagents"), "search running 带 extra.subagents"
    read_idx = [i for i, (k, _) in enumerate(full_sequence) if k == "read"]
    # 读取面为真 LLM 行为变量（Standard 档子代理对确定性候选自主决策是否调
    # firecrawl_read——批次 2 探针实证两档模型均不自发快照、read 与否属模型裁量）：
    # read 帧在场时断言 label 形态；缺席时不作硬断言（读原文链路语义由
    # test_deep_research.py 脚本化 read 面锁死、快照真路径由活体 MinIO 用例承载）。
    if read_idx:
        assert any(
            stages[i].get("label", "").startswith("阅读第") for i in read_idx
        )
    write_idx = [i for i, (k, _) in enumerate(full_sequence) if k == "write"]
    write_frames = [stages[i] for i in write_idx]
    assert write_frames[0]["status"] == "running" and write_frames[0]["label"] == "撰写第 1 节"
    assert write_frames[-1]["status"] == "done"
    assert int((write_frames[-1].get("extra") or {}).get("sections", 0)) >= 1
    assert full_sequence[-1] == ("write", "done")
    reflect_idx = [i for i, (k, _) in enumerate(full_sequence) if k == "reflect"]
    assert reflect_idx and full_sequence[reflect_idx[-1]][1] == "done"  # 回路退出帧
    assert reflect_idx[-1] < write_idx[0]  # reflect 收束先于 write
    assert search_first < reflect_idx[0]  # search 先于首轮 reflect（§7.4 相对序）
    # supplement 回路帧（真判定产 gaps 时）：相对序 search 段夹于 reflect running/done 间
    # （真 LLM 判定面——gaps 非空才进补搜，零 gaps 轮无 supplement search 帧，两形态均合法）
    assert errors == []  # 全链零 error（api.md §7.2）

    # ---- sources 批次（ref_no 首现冻结递增）----
    assert sources_batches, "真 research 搜索面产生 sources 批次"
    refnos = [item["ref_no"] for batch in sources_batches for item in batch["batch"]]
    assert refnos == sorted(refnos) and refnos[0] == 1

    # ---- writer token 拼接 == answer_md（messages 流域唯一 token 面）----
    answer_md = final.get("answer_md") or ""
    assert answer_md, "报告正文落 state"
    assert "桩" not in answer_md  # 非桩文本
    assert "".join(writer_tokens) == answer_md  # writer 分节流式累积=messages 增量
    # 结构段为模型软面（标题级形态漂移——活体实测「摘要」可无 ## 前缀）：断言内容级
    # 摘要/结论段在 + 全程 [n] 引用存在（§2.5 深度报告四段语义）
    assert "摘要" in answer_md and "结论" in answer_md
    assert "[1]" in answer_md

    # ---- 快照回填链（批次 2 注⑦闭合——真库断言；「有快照轮」条件语义）----
    # 图直跑无 TurnRunner 持久化面——sources 行按发射批次播种（模拟 _translate_custom
    # 落库面），再走 backfill 真路径断言。读（→snapshot）为真 LLM 行为变量：本轮若有
    # 快照账本则断言四键+回填翻真；无快照轮断言 snapshot_id 空的一致性（回填幂等零
    # 误填）——快照正例由 test_deep_research 脚本化 read 面 + export_chain 回填用例 +
    # 活体 MinIO 用例三层承载。
    snapshots = final.get("snapshots") or []
    from app.db.models.source import Source
    from app.db.session import get_session_factory
    from app.services.turn_service import backfill_source_snapshot_ids

    emitted_urls = [item["url"] for batch in sources_batches for item in batch["batch"]]
    factory = get_session_factory()
    async with factory() as session:
        for ref_no, url in enumerate(emitted_urls, start=1):
            session.add(
                Source(
                    id=uuid.uuid4(),
                    turn_id=uuid.UUID(turn_id),
                    user_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
                    ref_no=ref_no,
                    source_type="web",
                    title=f"来源{ref_no}",
                    url=url,
                    snippet="片段",
                    snapshot_id=None,
                )
            )
        await session.commit()

    if snapshots:
        for entry in snapshots:
            assert entry.get("snapshot_id"), "snapshot_id 入账（save_snapshot._meta 返回面）"
        filled = await backfill_source_snapshot_ids(
            get_session_factory(), turn_id, snapshots
        )
        assert filled >= 1
    async with factory() as session:
        rows = (
            await session.execute(
                select(Source).where(Source.turn_id == uuid.UUID(turn_id))
            )
        ).scalars().all()
    assert rows, "sources 行在库"
    if snapshots:
        assert all(row.snapshot_id is not None for row in rows), "有快照轮 sources.snapshot_id 非空"
    else:
        assert all(row.snapshot_id is None for row in rows), "无快照轮零误填（回填幂等）"

    # ---- Langfuse callbacks 在场（trace 层级核验归活体探针 API 步）----
    assert _fullchain_config(turn_id, thread_id)["callbacks"], "观测 callbacks 面"

    # ---- usage 真值（usage_collector 注入 → guards/finalize usage_total 累计）----
    assert int(final.get("usage_total") or 0) > 0, "done.usage 数据源（collector）真值累计"


async def test_full_chain_turnrunner_done_usage_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TurnRunner 级真轮（真调度器+真图装配+全模型假体）：done(completed) +
    done.usage 合规形态 + token 事件（writer messages 门控放行）+ answer 持久化非桩
    + Langfuse callbacks 在场。调度器分流链路是主对象（collector 注入→done.usage
    消费、_messages_gate writer 帧放行、_finish_completed completed 分流零 error）。

    planner/research/reflect/writer 四模型全假体控规模（批次 2/3 先例同款：
    test_deep_research_node「planner 打桩控规模」+ test_deep_reflection_loop
    「research 失败报告形态桩」+ 本文件 reflect 桩 sufficient 形态 + conftest
    _WriterStreamModel writer 流式假体同款）。usage 真值（>0）断言面由 test A
    （图直跑全链真 LLM）承载——本用例 writer 真跑在批次 4 复跑实证不可控收敛
    （150s/240s 包络两轮均撞满 → on_llm_end 不达 → collector 零累计 → done.usage
    ={}；活体探针「慢调用进重试窗吃满预算」同形态、网关延迟变异环境性），假体化
    后用例毫秒级确定绿；usage={} 与 {"total_tokens": N} 均为 §2.5 合规形态（>0 键
    才出现——UsageBrief 可空）。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)

    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_reflect as deep_reflect_module
    from app.agent.nodes import deep_research as deep_research_module
    from app.agent.nodes import deep_writer as deep_writer_module
    from app.config_center.defaults import SPEC_CONFIG
    from app.services.turn_service import ThreadScheduler
    from test.integration.conftest import DEEP_WRITER_FAKE_PIECES
    from test.unit.turn_fakes import (
        _MAPPING_PAYLOAD,
        _FakeSession,
        _FakeSessionFactory,
        _TurnRow,
        _bus,
        _drain_all,
        _snapshot,
    )
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _WriterStreamModel(GenericFakeChatModel):
        """writer 流式假体（BaseChatModel 子类——langgraph messages 模式经
        on_llm_new_token callback 捕获，裸 astream 生成器不冒泡 callback → token
        事件零帧，本用例首轮实测）：正文经 GenericFakeChatModel 按行拆 chunk——
        分节标题行独立成帧（label 推进面确定）。"""

        def bind(self, *args: Any, **kw: Any) -> "_WriterStreamModel":  # noqa: ANN201
            return self

    class _ReflectChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {"sufficient": True, "gaps": []}  # 首判 sufficient → 回路退出

    class _ReflectModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _ReflectChain:
            return _ReflectChain()

    class _PlanChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "subtopics": ["LangGraph 与 deepagents 的定位差异与各自适用场景"],
                "outline": [
                    {"section": "定位差异", "intent": "两者的层次关系与分工"},
                    {"section": "适用场景", "intent": "各自适合的落地形态"},
                ],
                "query_type": "comparison",
                "time_range": None,
                "memory_directive": None,
            }

    class _PlannerModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _PlanChain:
            return _PlanChain()

    async def _failed_research_stub(state: Any, config: Any = None) -> dict[str, Any]:
        """research 瞬时化桩（失败报告形态——test_deep_reflection_loop 同款；本用例
        真依赖面=reflect/supplement/writer 真轮 + done.usage 真值收束面）。"""
        return {
            "subagent_reports": [
                {
                    "subtopic": "LangGraph 与 deepagents 的定位差异与各自适用场景",
                    "summary": "",
                    "hard_facts": [],
                    "quotes": [],
                    "sources": [],
                    "failed": True,
                }
            ],
            "visited_urls": [],
            "searched_queries": [],
            "snapshots": [],
            "collected_sources": [],
            "errors": [
                {"stage": "research", "reason": "research_agent_failed", "error": "NotImplementedError"}
            ],
        }

    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(deep_reflect_module, "build_chat_model", lambda alias, **kw: _ReflectModel())
    monkeypatch.setattr(
        deep_writer_module,
        "build_chat_model",
        lambda alias, **kw: _WriterStreamModel(
            messages=iter([AIMessage(content="".join(DEEP_WRITER_FAKE_PIECES))])
        ),
    )
    # research 整节点替换（失败报告形态——零材料零来源）：编译图固化节点函数引用
    # （deep.py:63 from-import + add_node），patch nodes 模块属性无效——批次 3 先例
    # 姿势（test_deep_reflection_loop.py:149-150）：patch graphs/deep 模块引用 + 重建
    # deep 图 + 替换 GRAPH_REGISTRY 项 + 重建 main（route_by_mode 与 build_main_graph
    # 经 from-import 共享同一 dict 对象——setitem 全链可见）。
    from app.agent.graphs import deep as deep_mod
    from app.agent.graphs import main as main_mod
    from app.agent.graphs import registry as graphs_registry_mod

    monkeypatch.setattr(deep_mod, "research", _failed_research_stub)
    monkeypatch.setattr(deep_mod, "graph", deep_mod.build_deep_graph())
    monkeypatch.setitem(graphs_registry_mod.GRAPH_REGISTRY, "deep", deep_mod.graph)
    rebuilt_main = main_mod.build_main_graph()

    bus = _bus()
    turn = _TurnRow(status="running", mode="deep")
    session = _FakeSession(turn=turn)
    # Spec 全默认（假体毫秒级——60s 单调用包络/300s 轮预算零压力，无放宽需求）
    spec = dict(SPEC_CONFIG)
    scheduler = ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=rebuilt_main,
        spec_snapshot=_snapshot(spec),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=_FullChainRegistry(),
    )
    await scheduler._execute_turn(turn)
    published = await _drain_all(bus, str(turn.id))

    done = published[-1]
    assert done.event_type == "done"
    assert done.data["finish"] == "completed"
    # done.usage 合规形态（假体零 token 累计 → {} 合规——>0 键才出现，UsageBrief
    # 可空；usage 真值面由 test A 图直跑真 LLM 承载——collector 注入→_finish_completed
    # 消费接线由本断言的「done.usage 键在场且为 dict」结构面覆盖）
    usage = done.data.get("usage")
    assert isinstance(usage, dict), f"done.usage 合规形态（dict）——实测 {usage!r}"
    # research 桩（失败报告形态）→ 零检索零来源（source_count=0 契约合法——§7.4
    # 反思轮形态；真检索面由 test A 图直跑全链承担）
    assert done.data.get("degraded") is False and done.data.get("degraded_reason") is None
    assert [e.event_type for e in published if e.event_type == "error"] == []
    tokens = [e for e in published if e.event_type == "token"]
    assert tokens, "writer token 事件（messages 流域）"
    assert turn.answer_md and "桩" not in turn.answer_md  # 非桩文本持久化
    assert turn.status == "completed"
    stages = [e.data.get("stage_key") for e in published if e.event_type == "stage"]
    assert "plan" in stages and "write" in stages


async def test_full_chain_degraded_real_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """降级真轮（注入超时触发 degraded_writer）：done(finish=degraded,
    degraded_reason=timeout, degraded=True) + answer 含「研究未完成」+ 零 error——
    api.md §7.2:803/:845 图内熔断形态真轮闭合（plan :108 降级报告真轮实测条件）。

    超时注入构造（剩余预算 ≤30s 包络 trip——turn_timeout_s=30）：trip 前各阶段
    瞬时化（planner/research/reflect 三桩——判据注入面与批次 3 guards integration
    同款），reflect 首判定前 `_pre_trip` 即命中（remaining 29s ≤ 30s 包络）→ 零
    LLM 收敛 guards 出口 → 条件边进 degraded_writer → done(finish=degraded)。

    degraded_writer 假体化（批次 4 复跑裁决）：trip 判据 remaining≤30s 与轮级包络
    同窗——降级真 LLM 单调用在当前网关延迟变异下 >29s（复跑实证：真调用撞轮级
    包络 → 500301 兜底 error 事件 vs 期望零 error；健康网关 short 窗亦属贴边赌博），
    桩化控规模与 planner/research/reflect 同纪律；**真 LLM 降级报告面归阶段 6 live
    探针**（plan Verification 5「注入超时后降级报告」真轮实测条件）。本用例主对象=
    TurnRunner 分流真链路（trip→route_guards 条件边→degraded_writer 节点→
    _finish_completed done(finish=degraded) 分流零 error——api.md §7.2:845 v1.21 注
    的结构面），断言面不受假体影响：机械前置「⚠️ 研究未完成」为代码常量拼接
    （deep_degraded_writer._DEGRADED_PREAMBLE——零 LLM 不可被吞）、degraded_reason
    贯通为 guards 置位消费（D2 单一事实源）。reflect 真判定语义归
    test_deep_reflect_node 冒烟。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)

    from app.agent.graphs.main import graph as main_graph
    from app.agent.nodes import deep_degraded_writer as deep_degraded_writer_module
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_reflect as deep_reflect_module
    from app.agent.nodes import deep_research as deep_research_module
    from app.config_center.defaults import SPEC_CONFIG
    from app.services.turn_service import ThreadScheduler
    from test.unit.turn_fakes import (
        _MAPPING_PAYLOAD,
        _FakeSession,
        _FakeSessionFactory,
        _TurnRow,
        _bus,
        _drain_all,
        _snapshot,
    )

    class _DegradedChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> Any:
            from langchain_core.messages import AIMessage

            return AIMessage(content="阶段性报告正文（假体——材料面已收集内容如实转述）。")

    class _DegradedModel:
        def bind(self, *args: Any, **kw: Any) -> "_DegradedModel":
            return self

        async def ainvoke(self, prompt: Any, config: Any = None) -> Any:
            return await _DegradedChain().ainvoke(prompt, config)

    class _PlanChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "subtopics": ["LangGraph 与 deepagents 的定位差异"],
                "outline": [{"section": "定位差异", "intent": "层次关系与分工"}],
                "query_type": "comparison",
                "time_range": None,
                "memory_directive": None,
            }

    class _PlannerModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _PlanChain:
            return _PlanChain()

    class _ReflectChain:
        """reflect 假体（pre-trip 先于判定收敛——本假体不应被调用；trip 后 gaps=[]
        直通 guards）。"""

        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            raise AssertionError("reflect 不应在 pre-trip 收敛路径被调用")

    class _ReflectModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _ReflectChain:
            return _ReflectChain()

    async def _failed_research_stub(state: Any, config: Any = None) -> dict[str, Any]:
        """失败报告形态（研究面瞬时化——test_deep_reflection_loop 同款构造）。"""
        return {
            "subagent_reports": [
                {
                    "subtopic": "LangGraph 与 deepagents 的定位差异",
                    "summary": "",
                    "hard_facts": [],
                    "quotes": [],
                    "sources": [],
                    "failed": True,
                }
            ],
            "visited_urls": [],
            "searched_queries": [],
            "snapshots": [],
            "collected_sources": [],
            "errors": [
                {"stage": "research", "reason": "research_agent_failed", "error": "NotImplementedError"}
            ],
        }

    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(deep_reflect_module, "build_chat_model", lambda alias, **kw: _ReflectModel())
    monkeypatch.setattr(
        deep_degraded_writer_module, "build_chat_model", lambda alias, **kw: _DegradedModel()
    )
    # research 整节点替换（编译图固化引用——done_usage 用例同款重建链，批次 3 先例）
    from app.agent.graphs import deep as deep_mod
    from app.agent.graphs import main as main_mod
    from app.agent.graphs import registry as graphs_registry_mod

    monkeypatch.setattr(deep_mod, "research", _failed_research_stub)
    monkeypatch.setattr(deep_mod, "graph", deep_mod.build_deep_graph())
    monkeypatch.setitem(graphs_registry_mod.GRAPH_REGISTRY, "deep", deep_mod.graph)
    rebuilt_main = main_mod.build_main_graph()

    bus = _bus()
    turn = _TurnRow(status="running", mode="deep")
    session = _FakeSession(turn=turn)
    scheduler = ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=rebuilt_main,
        spec_snapshot=_snapshot({**SPEC_CONFIG, "deep": {**SPEC_CONFIG["deep"], "turn_timeout_s": 30}}),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=_FullChainRegistry(),
    )
    await scheduler._execute_turn(turn)
    published = await _drain_all(bus, str(turn.id))

    error_events = [e.data for e in published if e.event_type == "error"]
    assert error_events == [], f"零 error（api.md §7.2:845 图内熔断形态）——实测：{error_events}"
    done = published[-1]
    assert done.event_type == "done"
    assert done.data["finish"] == "degraded"
    assert done.data["degraded"] is True
    assert done.data["degraded_reason"] == "timeout"
    assert turn.status == "degraded" and turn.degraded_reason == "timeout"
    assert turn.answer_md and turn.answer_md.startswith("⚠️ 研究未完成")
