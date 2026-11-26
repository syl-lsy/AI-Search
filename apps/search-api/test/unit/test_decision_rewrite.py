"""unit 层：rewrite 节点前置 decision 步骤（Decision Model 工单批次 2——ADR-016 串行先判 /
api.md v1.26 stage extra `decision_source` / nacos-config.md §1 v1.33 `fast.decision.*`）。

钉住的语义（冻结契约，逐条对应测试）：
- **kill switch**：`model.mapping.decision` 空/缺失 → decision 整体跳过——build_light_model
  全职责路径照旧、`run_decision` 零调用、done.extra **零 decision_source 键**（键可省略
  口径沿 v1.12 images 先例——存量轮零行为变化回归锁）；
- **decision 生效 direct/recall**：跳过 light 调用（省 1.5~6s），update 全由 decision 承载
  （rewritten_queries=[]——零检索轮 queries 不参与路由，search_fanout 判定先于空 queries），
  answerable 强制 False（历史 ≠ 知识对齐纪律）；
- **decision 生效 verify/web**：light 瘦身调用只生成 queries/time_range/memory_directive
  （+safety 初判——判定面留 light），query_type/answerable/search_strategy 由 decision
  回填，queries 经 type_query_map 夹取（夹取键 = decision 的 query_type）；
- **失败回退（零能力回退）**：decision 失败/超时/解析失败（run_decision=None）→ light
  全职责路径（含分类+safety，行为等同现状），done.extra.decision_source=light_fallback，
  **不置 degraded**（对齐 rewrite 兜底口径）；瘦身调用失败同样回退全职责；
- **shadow 双跑**：decision 只观测不生效（light 全职责照常），双结果落 done.extra
  （decision_shadow 观测载荷 + light 侧 update）+ decision_source=shadow；
- **safety_shadow 仅观测**：decision direct/recall 轮零 light 判读面——Layer 1 前置覆盖，
  零拒答话术调用（safety 判定面留 light 的保守口径）；
- **混合规则护栏（nacos-config.md §1 v1.20 两字段一致纪律）**：verify→answerable=True
  强制、direct/recall→False 强制、web 保留原值——解析侧回填（decision_client）与
  消费侧（`_aligned_answerable`）幂等双防线，护栏介入以 `answerable_source=guard`
  标注（观测面 M4 分桶可达）；
- **全职责模板保留**：light 全职责路径（kill switch 关闭态 / decision 失败兜底态）
  prompt = 原五件事全量模板（`_load_full_rewrite_template`——e2e 实证瘦身 schema 串
  与全量 RewriteOutput 工具 schema 冲突会致本地 light 校验失败率陡增，prompt schema
  串必须与工具 schema 同形）；瘦身模板（Nacos 键 fast.rewrite 减面版）仅 decision
  生效 verify/web 轮的瘦身调用消费。

单测全 mock 零网络：decision 调用经 `run_decision` 注入面打桩，light 链经
`build_light_model` 打桩。
"""

from typing import Any

import pytest

from app.agent.guards import ContentRejectedError
from app.agent.nodes import fast_rewrite
from app.agent.nodes.fast_rewrite import RewriteOutput, RewriteSlimOutput, rewrite
from app.agent import decision_client

#: 成功 fixture（decision 判定输出形态——DecisionOutcome 等价结构）
_DECISION_DIRECT: dict[str, Any] = {
    "query_type": "other",
    "search_strategy": "direct",
    "answerable": False,
    "safety_shadow": "none",
}

_DECISION_VERIFY: dict[str, Any] = {
    "query_type": "comparison",
    "search_strategy": "verify",
    "answerable": True,
    "safety_shadow": "none",
}

#: light 瘦身 fixture（queries/time_range/memory_directive/safety 四字段）
_SLIM_OK: dict[str, Any] = {
    "queries": ["pgvector Milvus 个人知识库选型对比", "pgvector vs Milvus 性能成本"],
    "safety": "none",
    "time_range": None,
    "memory_directive": None,
}

_STATE: dict[str, Any] = {
    "query": "pgvector 和 Milvus 个人知识库场景怎么选？",
    "query_lang": "zh",
    "history": [{"q": "什么是向量数据库", "a_head": "向量数据库是…"}],
    "spec": {
        "fast": {
            "type_query_map": {
                "comparison": [2, 4], "other": [2, 3], "factoid": [1, 2],
            },
            "llm_timeout_s": 30,
            "turn_timeout_s": 60,
            "retry": {"max_attempts": 2, "initial_interval_s": 0.01, "backoff_factor": 2.0, "jitter": False},
        },
        # safety 节（hybrid——瘦身调用 safety 初判拦截面需要；键位与 defaults.py 同源）
        "safety": {
            "enabled": True,
            "mode": "hybrid",
            "danger_terms": ["制造炸弹"],
            "profanity_terms": ["傻逼"],
            "privacy_terms": ["人肉搜索"],
            "politics_terms": [],
            "grey_rules": [],
            "classify_timeout_ms": 2000,
            "reply_timeout_ms": 4000,
            "reply_max_chars": 500,
        },
    },
}


class _FakeChain:
    """结构化输出链假体：ainvoke 按脚本逐次弹出 dict/异常；记录收到的 prompt。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: Any, config: Any = None) -> Any:
        self.prompts.append(str(prompt))
        if not self.script:
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
    monkeypatch: pytest.MonkeyPatch,
    chain: _FakeChain,
    *,
    decision_outcome: Any = "unset",
    decision_params: Any = None,
) -> tuple[_FakeModel, list[tuple[str, str, dict[str, Any]]], list[Any]]:
    """注入 light 链假体 + stage 记录器 + decision 调用记录器。

    decision_outcome="unset" → 不打桩 run_decision（kill switch 形态：stub 出现调用
    即失败）；None → run_decision 返回 None（decision 失败）；dict → 成功 outcome。
    """
    model = _FakeModel(chain)
    monkeypatch.setattr(fast_rewrite, "build_light_model", lambda m: model)
    emitted: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        fast_rewrite, "emit_stage", lambda key, status, **kw: emitted.append((key, status, kw))
    )
    decision_calls: list[Any] = []

    async def _fake_run_decision(**kwargs: Any) -> Any:
        decision_calls.append(kwargs)
        if decision_outcome == "unset":
            raise AssertionError("kill switch 关闭态不应调用 decision")
        if decision_outcome is None:
            return None
        from app.agent.decision_client import DecisionOutcome

        payload = dict(decision_outcome)
        return DecisionOutcome(
            query_type=payload["query_type"],
            query_type_source="decision",
            search_strategy=payload["search_strategy"],
            search_strategy_source="decision",
            answerable=payload.get("answerable", False),
            safety_shadow=payload.get("safety_shadow"),
            extra_observation={
                "query_type": payload["query_type"],
                "search_strategy": payload["search_strategy"],
                "answerable": payload.get("answerable", False),
                "safety_shadow": payload.get("safety_shadow"),
                "decision_source_fields": {
                    "query_type": "decision",
                    "search_strategy": "decision",
                },
                "confidence": {},
            },
            metadata={"answers": dict(payload)},
        )

    monkeypatch.setattr(fast_rewrite, "run_decision", _fake_run_decision)
    monkeypatch.setattr(
        fast_rewrite,
        "resolve_decision_params",
        lambda mapping, questions=None: decision_params,
    )
    return model, emitted, decision_calls


def _enabled_params() -> Any:
    from app.agent.decision_client import DecisionParams

    return DecisionParams(
        alias="decision-model",
        questions=decision_client.load_questions(),
        confidence_floor=0.5,
        timeout_ms=1000,
        shadow=False,
    )


def _config(mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    """TurnRunner 注入形态的 RunnableConfig（configurable 三键）。"""
    return {
        "configurable": {
            "thread_id": "th-1",
            "turn_id": "t-1",
            "model_mapping": mapping if mapping is not None else {"light": "qwen3.5-4b-ollama"},
        }
    }


# ---- kill switch：decision 空/缺失 → 完全跳过（现状行为逐字节回归锁）----


async def test_kill_switch_missing_decision_key_runs_light_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧映射 payload（无 decision 键）+ 新代码 → decision 零调用、light 全职责路径、
    done.extra 零 decision_source 键（api.md v1.26 键可省略口径——存量轮零行为变化）。"""
    chain = _FakeChain([
        {
            "queries": ["pgvector Milvus 对比", "pgvector vs Milvus"],
            "query_type": "comparison",
            "answerable_from_knowledge": False,
            "search_strategy": "web",
            "safety": "none",
            "time_range": None,
            "memory_directive": None,
        }
    ])
    model, emitted, decision_calls = _install(monkeypatch, chain, decision_outcome="unset")

    update = await rewrite({**_STATE}, _config())

    assert decision_calls == []  # decision 零调用
    assert model.schema_seen is RewriteOutput  # light 全职责 schema（=现状）
    assert update["query_type"] == "comparison"
    assert update["search_strategy"] == "web"
    assert update["rewritten_queries"] == ["pgvector Milvus 对比", "pgvector vs Milvus"]
    assert [(key, status) for key, status, _ in emitted] == [("rewrite", "done")]
    assert "decision_source" not in emitted[-1][2]["extra"]  # 键可省略——存量轮不携带


async def test_kill_switch_empty_string_runs_light_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decision=""（缺省 kill switch）→ 同缺失形态：零 decision 调用、extra 零键。"""
    chain = _FakeChain([
        {
            "queries": ["q1", "q2"],
            "query_type": "factoid",
            "answerable_from_knowledge": True,
            "search_strategy": "verify",
        }
    ])
    model, emitted, decision_calls = _install(
        monkeypatch, chain, decision_outcome="unset", decision_params=None
    )

    update = await rewrite({**_STATE}, _config({"light": "qwen3.5-4b-ollama", "decision": ""}))

    assert decision_calls == []
    assert model.schema_seen is RewriteOutput
    assert update["search_strategy"] == "verify"
    assert "decision_source" not in emitted[-1][2]["extra"]


async def test_kill_switch_prompt_is_original_full_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill switch 关闭态 prompt = 原五件事全量模板（v1.33 前现状逐字节——回归锁的
    prompt 面：e2e 实证瘦身 schema 串与全量工具 schema 冲突会致本地 light 校验失败
    率陡增，全职责路径 prompt schema 串必须与 RewriteOutput 工具 schema 同形）；
    判据面 = common.type_rules（热更）。"""
    chain = _FakeChain([
        {
            "queries": ["q1"],
            "query_type": "other",
            "answerable_from_knowledge": False,
            "search_strategy": "web",
        }
    ])
    _install(monkeypatch, chain, decision_outcome="unset")

    await rewrite({**_STATE}, _config())

    prompt = chain.prompts[0]
    assert "一次完成六件事" in prompt  # B19 六件事形态（+KB 检索档位判定）
    assert "类型判定标准" in prompt  # {{type_rules}} 判据解析注入
    assert "Q: 什么是向量数据库" in prompt  # 历史装配照旧
    # schema 串与 RewriteOutput 工具 schema 同形（七字段全量枚举串）
    assert '"search_strategy":"direct|verify|web|recall"' in prompt
    assert "recall 仅限回顾整理请求" in prompt  # 判据正文（原模板保留）


# ---- decision 生效 direct/recall：跳过 light ----


async def test_decision_direct_skips_light_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decision 判 direct（confidence ≥ floor）→ 跳过 light 调用，update 全由 decision
    承载（rewritten_queries=[]——零检索轮 queries 不参与路由）；done.extra =
    {strategy, decision_source}（api.md §7.4 direct 形态钉死 extra.strategy=direct）。"""
    chain = _FakeChain([])  # light 零调用——脚本耗尽即失败（防被测代码静默多调）
    model, emitted, decision_calls = _install(
        monkeypatch, chain, decision_outcome=_DECISION_DIRECT, decision_params=_enabled_params()
    )

    update = await rewrite({**_STATE}, _config())

    assert len(decision_calls) == 1
    assert decision_calls[0]["params"].alias == "decision-model"
    assert decision_calls[0]["query"] == _STATE["query"]
    assert model.schema_seen is None  # light 零构建（省 1.5~6s light 调用）
    assert update["search_strategy"] == "direct"
    assert update["query_type"] == "other"
    assert update["answerable_from_knowledge"] is False  # direct → False（两字段一致纪律）
    assert update["rewritten_queries"] == []
    assert [(key, status) for key, status, _ in emitted] == [("rewrite", "done")]
    extra = emitted[-1][2]["extra"]
    assert extra == {"strategy": "direct", "decision_source": "decision"}


async def test_decision_recall_skips_light_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decision 判 recall → 跳过 light；answerable 强制 False（历史 ≠ 知识——即使
    decision 误判 true 亦对齐）。"""
    chain = _FakeChain([])
    _, emitted, _ = _install(
        monkeypatch,
        chain,
        decision_outcome={**_DECISION_DIRECT, "search_strategy": "recall", "answerable": True},
        decision_params=_enabled_params(),
    )

    update = await rewrite({**_STATE}, _config())

    assert update["search_strategy"] == "recall"
    assert update["answerable_from_knowledge"] is False  # 模型误置 true 亦被对齐
    assert emitted[-1][2]["extra"] == {"strategy": "recall", "decision_source": "decision"}


# ---- 混合规则护栏（nacos v1.20 两字段一致纪律）——消费侧终态锁定 ----


async def test_decision_verify_guard_model_answer_no_final_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """护栏终态锁定①：decision outcome 携带未对齐原始值（verify + 模型答 no）→
    消费侧 `_aligned_answerable` 复判修正，终态 answerable_from_knowledge=True
    （nacos v1.20「verify → answerable=True」——解析侧回填已护栏的双防线：纵使
    上游漏护栏亦被消费侧强制）。"""
    chain = _FakeChain([{**_SLIM_OK}])
    _, emitted, _ = _install(
        monkeypatch,
        chain,
        decision_outcome={**_DECISION_VERIFY, "answerable": False},  # 模型答 no（原始值）
        decision_params=_enabled_params(),
    )

    update = await rewrite({**_STATE}, _config())

    assert update["search_strategy"] == "verify"
    assert update["answerable_from_knowledge"] is True  # 护栏终态——模型答 no 亦强制 true
    assert emitted[-1][2]["extra"] == {"strategy": "verify", "decision_source": "decision"}


async def test_decision_direct_recall_guard_forced_false_maintained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """护栏终态锁定②：direct/recall + 模型答 yes（原始值）→ 终态 False 维持
    （历史 ≠ 知识纪律——消费侧对上游未对齐值同样强制）。"""
    for strategy in ("direct", "recall"):
        chain = _FakeChain([])  # 零检索轮 light 零调用
        _install(
            monkeypatch,
            chain,
            decision_outcome={**_DECISION_DIRECT, "search_strategy": strategy, "answerable": True},
            decision_params=_enabled_params(),
        )

        update = await rewrite({**_STATE}, _config())

        assert update["search_strategy"] == strategy
        assert update["answerable_from_knowledge"] is False  # 强制 false 维持


async def test_decision_direct_does_not_intercept_on_safety_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """safety_shadow 仅观测不参与拦截（契约保守口径——safety 判定面留 light）：decision
    direct 轮 safety_shadow=danger → 零拒答话术调用、正常收束 done（Layer 1 pre_route
    已前置覆盖）。"""
    chain = _FakeChain([])
    rejection_calls: list[Any] = []

    async def _stub(*args: Any, **kwargs: Any) -> str:
        rejection_calls.append(args)
        return "不应被调用"

    monkeypatch.setattr(fast_rewrite, "generate_rejection_reply", _stub)
    _install(
        monkeypatch,
        chain,
        decision_outcome={**_DECISION_DIRECT, "safety_shadow": "danger"},
        decision_params=_enabled_params(),
    )

    update = await rewrite({**_STATE}, _config())

    assert rejection_calls == []  # safety_shadow 仅观测——零拦截
    assert update["search_strategy"] == "direct"


# ---- decision 生效 verify/web：light 瘦身调用 + decision 回填 ----


async def test_decision_verify_uses_slim_light_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify 轮：light 瘦身调用只生成 queries/time_range/memory_directive（+safety）；
    query_type/answerable/search_strategy 由 decision 回填；queries 经 type_query_map
    夹取（夹取键 = decision 的 query_type）。"""
    chain = _FakeChain([{**_SLIM_OK, "queries": ["q1", "q2", "q3", "q4", "q5"]}])
    model, emitted, _ = _install(
        monkeypatch, chain, decision_outcome=_DECISION_VERIFY, decision_params=_enabled_params()
    )

    update = await rewrite({**_STATE}, _config())

    assert model.schema_seen is RewriteSlimOutput  # 瘦身 schema
    assert model.method_seen == "function_calling"
    assert update["query_type"] == "comparison"  # decision 回填
    assert update["search_strategy"] == "verify"
    assert update["answerable_from_knowledge"] is True  # verify ⇔ answerable=true 对齐
    assert update["rewritten_queries"] == ["q1", "q2", "q3", "q4"]  # comparison [2,4] 夹取上限（light 产出）
    assert [(key, status) for key, status, _ in emitted] == [("rewrite", "done")]
    assert emitted[-1][2]["extra"] == {"strategy": "verify", "decision_source": "decision"}


async def test_decision_web_round_merges_slim_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """web 轮：decision 回填 + light 瘦身字段合并；memory_directive 经 done.extra 留档。"""
    chain = _FakeChain([
        {**_SLIM_OK, "time_range": "month", "memory_directive": "记住：偏好官方文档源"}
    ])
    model, emitted, _ = _install(
        monkeypatch,
        chain,
        decision_outcome={**_DECISION_VERIFY, "search_strategy": "web", "answerable": False},
        decision_params=_enabled_params(),
    )

    update = await rewrite({**_STATE}, _config())

    assert update["search_strategy"] == "web"
    assert update["answerable_from_knowledge"] is False  # web 保留 decision 原值
    assert update["time_range"] == "month"
    assert update["rewritten_queries"] == _SLIM_OK["queries"][:2]  # comparison 夹取
    assert emitted[-1][2]["extra"] == {
        "strategy": "web",
        "decision_source": "decision",
        "memory_directive": "记住：偏好官方文档源",
    }


async def test_decision_verify_slim_safety_gate_still_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """瘦身调用 safety 初判命中（判定面留 light）→ ContentRejectedError 照常触发
    （stage(rewrite, failed)——B13 两层防御不动）。"""
    chain = _FakeChain([{**_SLIM_OK, "safety": "privacy"}])
    _, emitted, _ = _install(
        monkeypatch, chain, decision_outcome=_DECISION_VERIFY, decision_params=_enabled_params()
    )

    async def _stub(*args: Any, **kwargs: Any) -> str:
        return "温和拒答话术。"

    monkeypatch.setattr(fast_rewrite, "generate_rejection_reply", _stub)

    with pytest.raises(ContentRejectedError) as exc_info:
        await rewrite({**_STATE}, _config())

    assert exc_info.value.category == "privacy"
    assert exc_info.value.layer == "rewrite"
    assert [(key, status) for key, status, _ in emitted] == [("rewrite", "failed")]


# ---- decision 失败 → light 全职责兜底（零能力回退）----


async def test_decision_failure_falls_back_to_light_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decision 失败（run_decision=None——超时/连接/解析失败已由 client 收敛）→ light
    全职责路径（含分类+safety，行为等同现状），decision_source=light_fallback、
    不置 degraded。"""
    chain = _FakeChain([
        {
            "queries": ["pgvector Milvus 对比", "pgvector vs Milvus"],
            "query_type": "comparison",
            "answerable_from_knowledge": False,
            "search_strategy": "web",
        }
    ])
    model, emitted, decision_calls = _install(
        monkeypatch, chain, decision_outcome=None, decision_params=_enabled_params()
    )

    update = await rewrite({**_STATE}, _config())

    assert len(decision_calls) == 1  # decision 先行尝试
    assert model.schema_seen is RewriteOutput  # light 全职责 schema
    assert update["query_type"] == "comparison"  # light 分类生效（非 decision）
    assert update["search_strategy"] == "web"
    assert update["degraded"] is False  # 成功路径 degraded=False；失败兜底轮不置位（rewrite 兜底口径）
    assert [(key, status) for key, status, _ in emitted] == [("rewrite", "done")]
    assert emitted[-1][2]["extra"] == {"strategy": "web", "decision_source": "light_fallback"}


async def test_decision_slim_failure_falls_back_to_light_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """瘦身调用失败（校验失败且修复回喂仍败）→ light 全职责路径（decision 字段弃用），
    decision_source=light_fallback。"""
    bad = {"queries": [], "safety": "none"}  # queries 空 → 校验失败
    chain = _FakeChain([
        bad,
        bad,  # 修复回喂仍败
        {  # light 全职责路径接管
            "queries": ["全职责 q1", "全职责 q2", "全职责 q3"],
            "query_type": "comparison",
            "answerable_from_knowledge": False,
            "search_strategy": "web",
        },
    ])
    model, emitted, _ = _install(
        monkeypatch, chain, decision_outcome=_DECISION_VERIFY, decision_params=_enabled_params()
    )

    update = await rewrite({**_STATE}, _config())

    # 三次 light 调用：瘦身 + 瘦身修复回喂 + 全职责
    assert len(chain.prompts) == 3
    assert model.schema_seen is RewriteOutput  # 最终全职责 schema
    assert update["query_type"] == "comparison"  # light 全职责分类（非 decision 回填）
    assert update["rewritten_queries"] == ["全职责 q1", "全职责 q2", "全职责 q3"]  # comparison [2,4] 上限内原样
    assert emitted[-1][2]["extra"] == {"strategy": "web", "decision_source": "light_fallback"}


# ---- shadow 双跑：decision 只观测不生效 ----


async def test_shadow_mode_decision_observed_not_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shadow=true：decision 只观测（双结果落 done.extra.decision_shadow），light 全职责
    照常生效（update 与 decision 结果无关），decision_source=shadow。"""
    chain = _FakeChain([
        {
            "queries": ["light q1"],
            "query_type": "factoid",  # light 判定（与 decision 的 comparison 不同——不生效）
            "answerable_from_knowledge": False,
            "search_strategy": "web",
        }
    ])
    model, emitted, decision_calls = _install(
        monkeypatch, chain, decision_outcome=_DECISION_VERIFY, decision_params=_enabled_params()
    )
    # shadow 参数（独立构造，绕过 _enabled_params 的 shadow=False）
    from app.agent.decision_client import DecisionParams

    shadow_params = DecisionParams(
        alias="decision-model",
        questions=decision_client.load_questions(),
        confidence_floor=0.5,
        timeout_ms=1000,
        shadow=True,
    )
    # 重新打桩 resolve_decision_params 返回 shadow 形态
    monkeypatch.setattr(
        fast_rewrite, "resolve_decision_params", lambda mapping, questions=None: shadow_params
    )

    update = await rewrite({**_STATE}, _config())

    assert len(decision_calls) == 1  # decision 双跑调用发生
    assert model.schema_seen is RewriteOutput  # light 全职责（decision 不生效）
    assert update["query_type"] == "factoid"  # light 判定生效（非 decision 的 comparison）
    assert update["search_strategy"] == "web"
    assert [(key, status) for key, status, _ in emitted] == [("rewrite", "done")]
    extra = emitted[-1][2]["extra"]
    assert extra["decision_source"] == "shadow"
    assert extra["strategy"] == "web"
    # 双结果落 stage extra：decision 侧观测载荷（light 侧结果在 update 本体）
    assert extra["decision_shadow"]["query_type"] == "comparison"
    assert extra["decision_shadow"]["search_strategy"] == "verify"


# ---- 全职责模板与瘦身模板分离（e2e 实证锚定）----


def test_full_rewrite_template_is_original_five_task_form() -> None:
    """全职责模板 = v1.33 全量形态 + B19 kb_route 增量：含 {{type_rules}} 引用、
    六件事正文、八字段全量 schema 串（与 RewriteOutput 工具 schema 同形——本地
    light 校验率锚定）与坏例警示骨架。"""
    template = fast_rewrite._load_full_rewrite_template()
    assert "一次完成六件事" in template
    assert "{{type_rules}}" in template
    # B19：kb_route 字段说明+判据词表+schema 串同形（修订注 3——prompt schema 与
    # 工具 schema 必须同形，本地 light 校验率锚定）
    assert '"kb_route":"hybrid|kb_first|kb_only"' in template
    assert "KB 检索档位判定" in template
    assert '"search_strategy":"direct|verify|web|recall"' in template
    assert '"query_type":"factoid|list|comparison|multi_facet|temporal|how_to|navigational|other"' in template
    assert '"safety":"none|danger|profanity|privacy|politics"' in template
    assert "坏例警示" in template


def test_slim_prompt_is_reduced_form() -> None:
    """瘦身模板（Nacos 键 fast.rewrite 内置默认）= 三件事减面形态（decision 生效轮
    专用，零分类判据）；与全职责模板分离。"""
    slim = fast_rewrite.resolve_prompt("fast.rewrite")
    assert "一次完成四件事" in slim  # B19 四件事（+KB 检索档位初判）
    assert '"kb_route":"hybrid|kb_first|kb_only"' in slim  # B19 schema 串同形
    assert "KB 检索档位" in slim  # 判据词表在位（修订注 3 Nacos fast.rewrite 键增量）
    assert '"query_type"' not in slim
    assert fast_rewrite._load_full_rewrite_template() != slim


def test_slim_output_schema_shape() -> None:
    """RewriteSlimOutput 五字段形状（queries/kb_route/time_range/memory_directive/
    safety——B19 +kb_route，safety 判定面留 light 的瘦身 schema）；缺省值与
    RewriteOutput 同口径（kb_route=hybrid 灰度安全）。"""
    slim = RewriteSlimOutput(queries=["q"])
    assert slim.kb_route == "hybrid"  # B19 缺省灰度
    assert slim.safety == "none"
    assert slim.time_range is None
    assert slim.memory_directive is None
    with pytest.raises(Exception):
        RewriteSlimOutput(queries=[])  # queries min_length=1（与 RewriteOutput 同判）
