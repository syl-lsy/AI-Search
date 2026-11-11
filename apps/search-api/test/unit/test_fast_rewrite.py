"""unit 层：rewrite 节点（B3 节点 1/5——ARCH §4.1 rewrite 行 / §6.2 P1 修复回喂）。

钉住的语义（冻结契约，逐条对应测试）：
- light 模型经 with_structured_output(Schema, method="function_calling")（R7 结论：
  本地 qwen3.5:4b 合法率 95%，ARCH §10 R7 行 S0 PASS）；模型别名 = 轮内模型映射快照
  `model.mapping.light`（configurable 注入，OQ-11 同源）；
- 7 类 query_type 合法输出经 type_query_map 夹取（上限截断、同义重复先去重——
  nacos-config §3「超出的会被丢弃」；下限不足不伪造 query 内容）；
- answerable_from_knowledge/time_range 透传；memory_directive 为 M5 记忆通道——
  不写 state（FastState 无该字段），仅经 stage done.extra 留档；
- 校验失败 → 修复回喂 1 次（§6.2 P1：校验错误文本回喂）→ 仍失败按 factoid 档 /
  3 query 机制兜底（显式常量，不经 type_query_map 夹取——ARCH §4.1 v1.4 澄清）；
- LLM 调用重试耗尽（传输类，default_retry_on 分类）→ 降级原始 query 单发（不终止）；
  退避 = initial_interval_s × backoff_factor^attempt + jitter（spec.retry）；重试尝试
  前查轮级剩余预算（deadline-aware，§6.2 P0——预算不足直接放弃重试走降级）；
- stage(rewrite, running→done) 夹住全部内部动作（api.md §7.2；兜底路径 done 带
  extra.fallback 标注）；节点纯函数（无 DB/EventBus；LLM 工厂可注入可 mock）。
"""

from typing import Any

import httpx
import pytest

from app.agent.nodes import fast_rewrite
from app.agent.nodes.fast_rewrite import rewrite

#: 契约默认 type_query_map（nacos-config §3 初值）
_TYPE_QUERY_MAP: dict[str, list[int]] = {
    "factoid": [1, 2],
    "list": [2, 3],
    "comparison": [2, 4],
    "multi_facet": [3, 4],
    "temporal": [2, 3],
    "how_to": [1, 2],
    "other": [2, 3],
}

_SPEC: dict[str, Any] = {
    "fast": {
        "type_query_map": _TYPE_QUERY_MAP,
        "llm_timeout_s": 30,
        "tool_timeout_s": 10,
        "turn_timeout_s": 60,
        "retry": {
            "max_attempts": 2,
            "initial_interval_s": 0.01,
            "backoff_factor": 2.0,
            "jitter": False,
        },
    }
}

_STATE: dict[str, Any] = {
    "query": "pgvector 和 Milvus 个人知识库场景怎么选？",
    "query_lang": "zh",
    "history": [{"q": "什么是向量数据库", "a_head": "向量数据库是…"}],
    "spec": _SPEC,
}

#: 成功 fixture（结构化输出，S15；unit 全 mock）
_OK_OUTPUT: dict[str, Any] = {
    "queries": ["pgvector Milvus 个人知识库选型对比", "pgvector vs Milvus 性能成本"],
    "query_type": "comparison",
    "answerable_from_knowledge": False,
    "time_range": None,
    "memory_directive": None,
}


class _FakeChain:
    """结构化输出链假体：ainvoke 按脚本逐次弹出 dict/异常；记录收到的 prompt。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: Any, config: Any = None) -> Any:
        self.prompts.append(str(prompt))
        if not self.script:  # 脚本耗尽即失败（防被测代码静默多调）
            raise AssertionError("FakeChain 脚本耗尽：被测代码调用次数超出预期")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeModel:
    """模型假体：with_structured_output 返回注入的链（记录 schema 与 method）。"""

    def __init__(self, chain: _FakeChain) -> None:
        self._chain = chain
        self.schema_seen: Any = None
        self.method_seen: str | None = None

    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kw: Any
    ) -> _FakeChain:
        self.schema_seen = schema
        self.method_seen = method
        return self._chain


def _install(
    monkeypatch: pytest.MonkeyPatch, chain: _FakeChain, *, mapping: dict[str, Any] | None = None
) -> tuple[_FakeModel, list[tuple[str, str, dict[str, Any]]]]:
    """注入模型假体 + stage 发射记录器；返回 (假模型, stage 记录) 供断言。"""
    model = _FakeModel(chain)
    monkeypatch.setattr(fast_rewrite, "build_light_model", lambda m: model)
    emitted: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        fast_rewrite, "emit_stage", lambda key, status, **kw: emitted.append((key, status, kw))
    )
    return model, emitted


def _config(mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    """TurnRunner 注入形态的 RunnableConfig（configurable 三键）。"""
    return {
        "configurable": {
            "thread_id": "th-1",
            "turn_id": "t-1",
            "model_mapping": mapping if mapping is not None else {"light": "qwen3.5-4b-ollama"},
        }
    }


# ---- 成功路径：7 类透传 + type_query_map 夹取 ----


@pytest.mark.parametrize(
    ("query_type", "llm_queries", "expected_count"),
    [
        ("factoid", ["q1", "q2", "q3", "q4", "q5"], 2),  # [1,2] 上限截断（超出的会被丢弃）
        ("list", ["q1", "q2", "q3"], 3),  # [2,3] 区间内原样
        ("comparison", ["q1", "q2"], 2),  # [2,4] 下限不足不伪造（LLM 给几条用几条）
        ("multi_facet", ["q1", "q2", "q3", "q4"], 4),  # [3,4] 上限内
        ("temporal", ["q1"], 1),  # [2,3] 下限不足不伪造
        ("how_to", ["q1", "q2"], 2),  # [1,2] 区间内
        ("other", ["q1", "q2", "q3", "q4"], 3),  # [2,3] 上限截断
    ],
)
async def test_rewrite_seven_types_with_type_query_map_clamp(
    monkeypatch: pytest.MonkeyPatch,
    query_type: str,
    llm_queries: list[str],
    expected_count: int,
) -> None:
    """7 类 query_type 合法输出：类型透传 + queries 经 type_query_map 夹取（nacos §3）。"""
    chain = _FakeChain([{**_OK_OUTPUT, "queries": llm_queries, "query_type": query_type}])
    _install(monkeypatch, chain)

    update = await rewrite({**_STATE}, _config())

    assert update["query_type"] == query_type
    assert update["rewritten_queries"] == llm_queries[:expected_count]


async def test_rewrite_clamp_uses_spec_snapshot_not_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """夹取表读轮内 spec 快照（OQ-11）：热改 spec 的 factoid 上限立即对当轮生效。"""
    spec = {
        **_SPEC,
        "fast": {**_SPEC["fast"], "type_query_map": {**_TYPE_QUERY_MAP, "factoid": [1, 1]}},
    }
    chain = _FakeChain([{**_OK_OUTPUT, "queries": ["q1", "q2", "q3"], "query_type": "factoid"}])
    _install(monkeypatch, chain)

    update = await rewrite({**_STATE, "spec": spec}, _config())

    assert update["rewritten_queries"] == ["q1"]  # 快照上限 1（非内置 [1,2] 的 2）


async def test_rewrite_dedupes_before_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """同义重复先去重再夹取（保序）——「禁止同义重复凑数（超出的会被丢弃）」。"""
    chain = _FakeChain([
        {**_OK_OUTPUT, "queries": ["pgvector 选型", "pgvector 选型", "Milvus 选型", "pgvector 选型"]},
    ])
    _install(monkeypatch, chain)

    update = await rewrite({**_STATE}, _config())

    assert update["rewritten_queries"] == ["pgvector 选型", "Milvus 选型"]


async def test_rewrite_passthrough_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """answerable/time_range 透传；memory_directive（M5 通道）不写 state。"""
    chain = _FakeChain([
        {
            **_OK_OUTPUT,
            "answerable_from_knowledge": True,
            "time_range": "month",
            "memory_directive": "记住：偏好官方文档源",
        },
    ])
    _install(monkeypatch, chain)

    update = await rewrite({**_STATE}, _config())

    # 最小检索语义：answerable=true 由 synthesis 分支消费，本节点锁 state 产出
    assert update["answerable_from_knowledge"] is True
    assert update["time_range"] == "month"
    assert "memory_directive" not in update  # FastState 无该字段——state 写入与声明一致
    assert update["degraded"] is False
    assert "errors" not in update


async def test_rewrite_stage_events_and_extra_for_memory_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stage(rewrite, running→done) 夹住节点全部动作（api.md §7.2）；memory_directive
    非空时经 done.extra 留档（M5 审计入口），null 时 extra 零携带。"""
    chain = _FakeChain([_OK_OUTPUT])
    _, emitted = _install(monkeypatch, chain)

    await rewrite({**_STATE}, _config())

    assert [(key, status) for key, status, _ in emitted] == [
        ("rewrite", "done"),
    ]  # running 已由 TurnRunner 图前发布（§4.3）——节点只发 done
    assert emitted[-1][2] == {}  # 成功且 memory_directive 为 null：done 无 extra

    chain2 = _FakeChain([{**_OK_OUTPUT, "memory_directive": "记住：偏好官方文档源"}])
    _, emitted2 = _install(monkeypatch, chain2)
    await rewrite({**_STATE}, _config())
    assert emitted2[-1][2] == {"extra": {"memory_directive": "记住：偏好官方文档源"}}


async def test_rewrite_prompt_renders_contract_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """渲染锁死：prompt 含 query_lang/type_rules 内置全文/历史 Q/A/query；memory_hints 空串（M5 前）。"""
    chain = _FakeChain([_OK_OUTPUT])
    _install(monkeypatch, chain)

    await rewrite({**_STATE}, _config())

    prompt = chain.prompts[0]
    assert _STATE["query"] in prompt
    assert "zh" in prompt  # query_lang（前置纯代码判定，改写不改判）
    assert "参数可答判定" in prompt  # type_rules 内置全文注入
    assert "Q: 什么是向量数据库" in prompt and "A: 向量数据库是…" in prompt


async def test_rewrite_receives_mapping_snapshot_via_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型映射 = configurable 注入的轮内快照（OQ-11 同源），工厂按快照构建。"""
    seen: dict[str, Any] = {}
    chain = _FakeChain([_OK_OUTPUT])

    def _spy(mapping: dict[str, Any]) -> _FakeModel:
        seen["mapping"] = dict(mapping)
        return _FakeModel(chain)

    monkeypatch.setattr(fast_rewrite, "build_light_model", _spy)
    monkeypatch.setattr(
        fast_rewrite, "emit_stage", lambda key, status, **kw: None
    )

    await rewrite(
        {**_STATE},
        _config({"light": "MY-ALIAS"}),
    )

    assert seen["mapping"]["light"] == "MY-ALIAS"


async def test_rewrite_schema_method_is_function_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S15 基元：with_structured_output(Schema, method="function_calling")（R7 验证形态）。"""
    chain = _FakeChain([_OK_OUTPUT])
    model, _ = _install(monkeypatch, chain)

    await rewrite({**_STATE}, _config())

    assert model.method_seen == "function_calling"
    assert model.schema_seen is fast_rewrite.RewriteOutput


# ---- 校验失败：修复回喂 1 次 → 机制兜底（factoid 档 / 3 query 固定）----


async def test_validation_failure_repairs_once_then_mechanism_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验失败 → 错误文本回喂修复 1 次 → 仍败 → factoid 档/3 query 固定（不经夹取，
    ARCH §4.1 v1.4 澄清显式常量）；修复 prompt 携带校验错误文本；done.extra 标注机制兜底。"""
    bad = {**_OK_OUTPUT, "query_type": "opinion"}  # 枚举外值（契约禁止形态）
    chain = _FakeChain([bad, bad])
    _, emitted = _install(monkeypatch, chain)

    update = await rewrite({**_STATE}, _config())

    assert len(chain.prompts) == 2  # 初始调用 + 修复回喂恰好 1 次
    assert "校验" in chain.prompts[1] or "validation" in chain.prompts[1].lower()
    # 机制兜底显式常量：类型按 factoid、query 数固定 3（原文重复展开保扇出宽度，
    # merge_dedupe 按 URL 归并去重——重复 query 无额外检索面收益、无语义损失）
    assert update["query_type"] == "factoid"
    assert update["rewritten_queries"] == [_STATE["query"]] * 3
    assert update["answerable_from_knowledge"] is False
    assert update["degraded"] is True
    assert update["errors"] == [{"stage": "rewrite", "reason": "structured_output_invalid"}]
    assert [(key, status) for key, status, _ in emitted] == [("rewrite", "done")]
    assert emitted[-1][2] == {"extra": {"fallback": "mechanism"}}  # 不终止：降级留痕


async def test_validation_failure_repaired_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修复回喂成功路径：第二次调用合法 → 正常产出（夹取照走），零兜底痕迹。"""
    bad = {**_OK_OUTPUT, "queries": []}  # queries 空 → 校验失败
    chain = _FakeChain([bad, {**_OK_OUTPUT, "queries": ["修复后的检索 query 一", "修复后的检索 query 二"]}])
    _install(monkeypatch, chain)

    update = await rewrite({**_STATE}, _config())

    assert update["rewritten_queries"] == ["修复后的检索 query 一", "修复后的检索 query 二"]
    assert update["degraded"] is False
    assert "errors" not in update


# ---- LLM 传输失败：spec RetryPolicy 重试 → 耗尽降级原始 query 单发 ----


async def test_transport_failure_retries_then_degrades_to_single_raw_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """传输类失败按 spec.retry 重试（max_attempts=2）→ 耗尽 → 降级原始 query 单发（不终止）。"""
    request = httpx.Request("POST", "http://litellm/v1")
    chain = _FakeChain([
        httpx.ConnectError("refused", request=request),
        httpx.ConnectError("refused", request=request),
    ])
    _, emitted = _install(monkeypatch, chain)

    update = await rewrite({**_STATE}, _config())

    assert len(chain.prompts) == 2  # 恰好 max_attempts 次
    assert update["rewritten_queries"] == [_STATE["query"]]  # 原始 query 单发
    assert update["query_type"] == "other"  # 未分类诚实标注（评测按 other 分桶）
    assert update["answerable_from_knowledge"] is False
    assert update["degraded"] is True
    assert update["errors"] == [{"stage": "rewrite", "reason": "llm_retry_exhausted"}]
    assert emitted[-1][2] == {"extra": {"fallback": "degraded"}}


async def test_deterministic_failure_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """确定性失败（校验族）不进重试循环——直入修复回喂（default_retry_on 同判据）。"""
    bad = {**_OK_OUTPUT, "query_type": 123}  # 非字符串枚举 → ValidationError
    chain = _FakeChain([bad, bad])
    _install(monkeypatch, chain)

    update = await rewrite({**_STATE}, _config())

    assert len(chain.prompts) == 2  # 1 次初始 + 1 次修复（无传输重试调用）
    assert update["query_type"] == "factoid"  # 机制兜底


async def test_retry_honors_deadline_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """deadline-aware（§6.2 P0）：轮级剩余预算 < 预计单次成本 → 不再重试直接降级。"""
    request = httpx.Request("POST", "http://litellm/v1")
    spec = {
        **_SPEC,
        "fast": {
            **_SPEC["fast"],
            "turn_timeout_s": 0.0,
            "retry": {**_SPEC["fast"]["retry"], "max_attempts": 3},
        },
    }
    chain = _FakeChain([httpx.ConnectError("refused", request=request) for _ in range(3)])
    _install(monkeypatch, chain)

    update = await rewrite({**_STATE, "spec": spec}, _config())

    assert len(chain.prompts) == 1  # 首试后剩余预算 0（< 30s 单调用成本）——重试放弃
    assert update["rewritten_queries"] == [_STATE["query"]]
    assert update["errors"] == [{"stage": "rewrite", "reason": "llm_retry_exhausted"}]


# ---- state 契约与纯函数边界 ----


async def test_rewrite_writes_only_faststate_declared_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """state 写入与 FastState 声明一致（§5.4 DoD：无未声明字段）。"""
    chain = _FakeChain([_OK_OUTPUT])
    _install(monkeypatch, chain)

    update = await rewrite({**_STATE}, _config())

    from app.agent.state import FastState

    assert set(update) <= set(FastState.__annotations__)


async def test_rewrite_empty_history_and_missing_lang_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空历史/缺 query_lang（Studio 手填形态）→ 渲染不炸（变量以空串/缺省值提供）。"""
    chain = _FakeChain([_OK_OUTPUT])
    _install(monkeypatch, chain)

    state = {"query": "x", "spec": _SPEC}  # 无 query_lang / history
    update = await rewrite(state, _config())  # type: ignore[arg-type]

    assert update["rewritten_queries"] == _OK_OUTPUT["queries"][:2]  # comparison [2,4] 夹取


def test_fallback_constants_are_explicit() -> None:
    """机制兜底显式常量（ARCH §4.1 v1.4 澄清：不经 type_query_map 夹取）。"""
    assert fast_rewrite.MECHANISM_FALLBACK_QUERY_COUNT == 3
    assert fast_rewrite.MECHANISM_FALLBACK_QUERY_TYPE == "factoid"
    assert fast_rewrite.TRANSPORT_FALLBACK_QUERY_TYPE == "other"
