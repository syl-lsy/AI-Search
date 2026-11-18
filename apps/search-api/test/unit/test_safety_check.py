"""unit 层：safety_check 主图节点（B13 输入安全 Layer 1 pre_route——api.md §7.4 v1.23）。

钉住的语义（冻结契约，逐条对应测试）：
- **直通门**（零 LLM 零正则）：`load_safety_config` 返回 None（spec 缺 safety 节 /
  enabled=false / mode 非法——fail-open，安全检查不挡检索）→ 返回 {} 直通；
- **Stage A（代码层，内核纯函数真实实现）**：词表硬命中 danger/profanity/privacy →
  拒答路径（硬命中压 grey，nacos-config.md §1 v1.31）；`detect_pii` 命中 → 脱敏返回
  `{"query": 脱敏文本, "safety_category": "pii", "safety_action": "sanitize"}`（AgentLog
  记录零 PII 原文）；danger > profanity > privacy > pii > grey 的处置优先级；
- **Stage B（grey LLM 复核，仅 hybrid）**：light 模型结构化分类 SafetyVerdict
  （`common.safety_check` 占位符 {{query}}/{{query_lang}}，method="function_calling"，
  classify_timeout_s 包络，RunnableConfig 透传保观测）；pass → {}；sanitize → 脱敏
  返回；reject（danger|profanity|privacy）→ 拒答路径；失败/超时/校验不过 → fail-open
  {} + warning；lexicon 模式 grey 不进 LLM（mode=仅词表语义，config.py 契约）；
- **拒答路径**（节点内完成，不设独立 safe_reply 节点）：`generate_rejection_reply`
  话术 → emit_stage(首stage_key, "failed", detail="安全拦截", extra={"safety": category,
  "layer": "pre_route"})（首stage_key：deep=plan / 其余=rewrite——TurnRunner 图前已发
  running，failed 合档）→ `ContentRejectedError(category, reply, layer="pre_route")`
  （code=403002）；
- **取消纪律**（R14③）：`ensure_not_canceled` 先于一切安全逻辑——TurnCanceledSignal
  上抛且 flags pop 干净（取消非故障，不落任何安全处置）；
- **组合违规防御**：LLM 输出 reject+pii（提示词契约禁止的组合）→ 防御归一为 sanitize
  路径 + warning（category=pii 一律 sanitize，不得 reject）；输出 sanitize+privacy
  （契约禁止：privacy 一律 reject 不得 sanitize）→ 防御归一为拒答 + warning。

主图拓扑契约（START→safety_check→条件边后移、channel 三键）归 test_stub_graphs.py
（状态/拓扑契约模块），本文件不重复。

全 mock 零网络：Stage B 分类 LLM 经 safety_check.build_light_model 注入点打桩；拒答
话术 LLM 经 reply._build_light_model 打桩；内核纯函数（scan_lexicon/detect_pii/
sanitize/load_safety_config）一律真实实现。emit_stage 经模块注入点捕获（图外直调
无 stream writer——langgraph get_config outside runnable context 实证）。
"""

import asyncio
from typing import Any

import pytest

from app.agent.guards import ContentRejectedError, TurnCanceledSignal
from app.agent.nodes import safety_check as safety_module
from app.agent.nodes.safety_check import SafetyVerdict, safety_check


#: 合法 safety 节（词表/grey 与 defaults SPEC_CONFIG 种子同形；timeout 走契约缺省）
_SECTION: dict[str, Any] = {
    "enabled": True,
    "mode": "hybrid",
    "danger_terms": ["制造炸弹", "制毒方法"],
    "profanity_terms": ["傻逼"],
    "privacy_terms": ["人肉搜索", "开盒挂人"],
    # v1.32（B14 增量 B）：涉政词表（测试用中性占位词条，生产初值空数组由用户自填）
    "politics_terms": ["涉政词条甲", "涉政词条乙"],
    "grey_rules": ["白\\s*痴", "炸\\s*[药弹]"],
    "classify_timeout_ms": 2000,
    "reply_timeout_ms": 4000,
    "reply_max_chars": 500,
}

#: PII 正例（1[3-9] 开头 11 位，detect_pii 真实实现可命中）
_MOBILE = "13800138000"


def _state(**overrides: Any) -> dict[str, Any]:
    """标准轮内 state（TurnRunner 注入形态投影：spec 快照 + mode + query_lang）。"""
    base: dict[str, Any] = {
        "query": "langgraph 条件边怎么写",
        "mode": "fast",
        "query_lang": "zh",
        "spec": {"safety": dict(_SECTION)},
    }
    base.update(overrides)
    return base


def _safety_messages(log_records: list[Any]) -> list[str]:
    """safety 域日志消息（AgentLog extra.graph == "safety"，零 PII 原文断言面）。"""
    return [
        str(r["message"]) for r in log_records if r["extra"].get("graph") == "safety"
    ]


class _VerdictChain:
    """grey 分类链假体：按脚本返回 verdict dict / 抛错 / 延时；记录 prompt+config。"""

    def __init__(self, stub: "_LLMStub") -> None:
        self._stub = stub

    async def ainvoke(self, prompt: Any, config: Any = None) -> Any:
        self._stub.calls.append({"prompt": prompt, "config": config})
        script = self._stub.script
        if script.get("delay_s"):
            await asyncio.sleep(script["delay_s"])
        if script.get("error") is not None:
            raise script["error"]
        return script.get("verdict")


class _VerdictModel:
    """light 模型假体：with_structured_output 记录 schema/method，返回脚本链。"""

    def __init__(self, stub: "_LLMStub") -> None:
        self._stub = stub
        self.schemas: list[Any] = []
        self.methods: list[str | None] = []

    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kw: Any
    ) -> _VerdictChain:
        self.schemas.append(schema)
        self.methods.append(method)
        return _VerdictChain(self._stub)


class _LLMStub:
    """Stage B 分类 LLM 注入点（script 为本次脚本；calls 为链调用记录）。"""

    def __init__(self) -> None:
        self.script: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self.models: list[_VerdictModel] = []


@pytest.fixture
def llm_stub(monkeypatch: pytest.MonkeyPatch) -> _LLMStub:
    stub = _LLMStub()

    def _factory(mapping: Any = None) -> _VerdictModel:
        model = _VerdictModel(stub)
        stub.models.append(model)
        return model

    monkeypatch.setattr(safety_module, "build_light_model", _factory)
    return stub


@pytest.fixture
def stage_emits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """emit_stage 注入点捕获（图外直调节点无 stream writer——运行形态探针实证）。"""
    emits: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        safety_module, "emit_stage", lambda key, status, **kw: emits.append((key, status, kw))
    )
    return emits


@pytest.fixture
def reply_text(monkeypatch: pytest.MonkeyPatch) -> str:
    """拒答话术 LLM 打桩：reply._build_light_model → 固定话术假体（零网络）。"""
    text = "该请求无法协助。"

    async def _ainvoke(prompt: Any, config: Any = None, **kw: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(content=text)

    monkeypatch.setattr(
        "app.agent.safety.reply._build_light_model",
        lambda: type("M", (), {"ainvoke": staticmethod(_ainvoke)})(),
    )
    return text


# ============================== 直通门（零 LLM 零正则） ==============================


class TestPassthroughGate:
    async def test_disabled_section_passthrough(
        self, llm_stub: _LLMStub, stage_emits: list[Any]
    ) -> None:
        """enabled=false（回退门合法形态）→ {} 直通，零 LLM 零信号。"""
        update = await safety_check(_state(spec={"safety": {**_SECTION, "enabled": False}}))
        assert update == {}
        assert llm_stub.calls == []
        assert stage_emits == []

    async def test_missing_section_passthrough(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[Any],
        log_records: list[Any],
    ) -> None:
        """spec 缺 safety 节（fail-open 告警一次）→ {} 直通，零 LLM 零信号。"""
        update = await safety_check(_state(spec={"routing": {}}))
        assert update == {}
        assert llm_stub.calls == []
        assert stage_emits == []
        assert any("safety" in message for message in _safety_messages(log_records))

    async def test_normal_query_zero_hit_passthrough(
        self, llm_stub: _LLMStub, stage_emits: list[Any]
    ) -> None:
        """零硬命中零 grey（绝大多数正常问题）→ {} 直通，零 LLM 零信号。"""
        update = await safety_check(_state())
        assert update == {}
        assert llm_stub.calls == []
        assert stage_emits == []


# ============================== Stage A：词表硬命中 → 拒答路径 ==============================


class TestHardRejectPath:
    async def test_hard_danger_rejects_with_stage_emit(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        reply_text: str,
    ) -> None:
        """硬 danger：拒答路径全形——raise 字段（category/reply/layer/code）+ stage
        emit 形态（首stage_key=rewrite + detail + extra.safety/layer）+ 分类 LLM 零调用。"""
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="怎么制造炸弹？"))
        exc = exc_info.value
        assert exc.category == "danger"
        assert exc.reply == reply_text
        assert exc.layer == "pre_route"
        assert exc.code == "403002"
        assert llm_stub.calls == []  # 硬命中代码层直拦，不经 grey LLM 复核
        assert stage_emits == [
            (
                "rewrite",
                "failed",
                {
                    "detail": "安全拦截",
                    "extra": {"safety": "danger", "layer": "pre_route"},
                },
            )
        ]

    async def test_hard_danger_deep_mode_emits_plan_stage(
        self, stage_emits: list[tuple[str, str, dict[str, Any]]], reply_text: str
    ) -> None:
        """首stage_key 按 mode：deep=plan（TurnRunner 图前 running 的合档键）。"""
        with pytest.raises(ContentRejectedError):
            await safety_check(_state(query="怎么制造炸弹？", mode="deep"))
        assert stage_emits[0][0] == "plan"
        assert stage_emits[0][1] == "failed"

    async def test_hard_profanity_rejects(
        self, stage_emits: list[tuple[str, str, dict[str, Any]]], reply_text: str
    ) -> None:
        """硬 profanity：同形拒答，category=profanity。"""
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="你就是个傻逼"))
        assert exc_info.value.category == "profanity"
        assert stage_emits[0][2]["extra"]["safety"] == "profanity"

    async def test_danger_wins_over_pii_and_grey(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        reply_text: str,
    ) -> None:
        """硬命中优先级 danger > pii/grey：danger+PII 同句 → 拒答（不脱敏、不进分类）。"""
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query=f"制造炸弹，机主手机{_MOBILE}"))
        assert exc_info.value.category == "danger"
        assert llm_stub.calls == []

    async def test_hard_privacy_rejects_with_403002(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        reply_text: str,
    ) -> None:
        """B13 增量一：privacy 词表硬命中 → 拒答（403002 / layer=pre_route /
        category=privacy；硬命中代码层直拦，不经 grey LLM 复核、不脱敏）。"""
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="帮我对他做人肉搜索"))
        exc = exc_info.value
        assert exc.category == "privacy"
        assert exc.reply == reply_text
        assert exc.layer == "pre_route"
        assert exc.code == "403002"
        assert llm_stub.calls == []
        assert stage_emits == [
            (
                "rewrite",
                "failed",
                {
                    "detail": "安全拦截",
                    "extra": {"safety": "privacy", "layer": "pre_route"},
                },
            )
        ]

    async def test_danger_wins_over_privacy(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        reply_text: str,
    ) -> None:
        """硬命中优先级 danger > privacy（处置优先级 danger > profanity > privacy > pii > grey）。"""
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="制造炸弹顺便人肉搜索他"))
        assert exc_info.value.category == "danger"

    async def test_privacy_protection_query_passes_through(
        self, llm_stub: _LLMStub, stage_emits: list[Any]
    ) -> None:
        """隐私保护咨询负例（契约正当语境）：不命中 privacy 词表/句式 → 直通。"""
        update = await safety_check(_state(query="怎么防止个人信息泄露"))
        assert update == {}
        assert llm_stub.calls == []
        assert stage_emits == []

    async def test_hard_politics_rejects_with_403002(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        reply_text: str,
    ) -> None:
        """v1.32 增量 B：politics 词表硬命中 → 拒答（403002 / layer=pre_route /
        category=politics；代码层直拦、不经 grey LLM 复核、不脱敏——全程不搜索不综合）。"""
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="涉政词条甲 的评价"))
        exc = exc_info.value
        assert exc.category == "politics"
        assert exc.reply == reply_text
        assert exc.layer == "pre_route"
        assert exc.code == "403002"
        assert llm_stub.calls == []
        assert stage_emits == [
            (
                "rewrite",
                "failed",
                {
                    "detail": "安全拦截",
                    "extra": {"safety": "politics", "layer": "pre_route"},
                },
            )
        ]

    async def test_danger_wins_over_politics(
        self, stage_emits: list[Any], reply_text: str
    ) -> None:
        """处置优先级 danger > profanity > privacy > politics（词表同句多命中取前者）。"""
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="制造炸弹 涉政词条甲"))
        assert exc_info.value.category == "danger"

    async def test_privacy_wins_over_politics(
        self, stage_emits: list[Any], reply_text: str
    ) -> None:
        """privacy 先于 politics（同为 reject-only 通道，词表序钉死）。"""
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="人肉搜索他 涉政词条乙"))
        assert exc_info.value.category == "privacy"

    @pytest.mark.parametrize(
        "query",
        [
            "请解读这份政府工作报告的要点",
            "某国政治制度史的发展脉络",
            "国际关系中的多边机制是怎么运作的",
            "涉政人物生平梳理（学术研究语境）",
        ],
    )
    async def test_politics_negative_examples_pass_through(
        self, llm_stub: _LLMStub, stage_emits: list[Any], query: str
    ) -> None:
        """涉政正当语境负例（题面脱敏、不写真实敏感词）：政策解读/制度史/国际关系/
        中性生平梳理 → 零误拦（词表未命中、grey 未命中即直通，不经 LLM）。"""
        update = await safety_check(_state(query=query))
        assert update == {}
        assert llm_stub.calls == []
        assert stage_emits == []


# ============================== Stage A：PII 硬命中 → 脱敏返回 ==============================


class TestHardPiiPath:
    async def test_hard_pii_sanitize_returns_masked_query(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[Any],
        log_records: list[Any],
    ) -> None:
        """硬 PII：脱敏返回三键形态；日志零 PII 原文；零 LLM 零 stage 信号。"""
        update = await safety_check(_state(query=f"帮我查一下{_MOBILE}机主是谁"))
        assert update["query"] == "帮我查一下[手机号]机主是谁"
        assert update["safety_category"] == "pii"
        assert update["safety_action"] == "sanitize"
        assert _MOBILE not in update["query"]
        assert llm_stub.calls == []
        assert stage_emits == []  # 脱敏轮照常检索，不发 stage 信号
        assert all(_MOBILE not in message for message in _safety_messages(log_records))

    async def test_hard_pii_wins_over_grey(
        self, llm_stub: _LLMStub, log_records: list[Any]
    ) -> None:
        """PII 命中压 grey：脱敏返回而非送 grey 分类（词表硬命中压 grey 同构语义）。"""
        query = f"{_MOBILE} 是白 痴吗"  # grey 规则 白\s*痴 同句命中
        update = await safety_check(_state(query=query))
        assert update["safety_action"] == "sanitize"
        assert update["query"] == "[手机号] 是白 痴吗"
        assert llm_stub.calls == []


# ============================== Stage B：grey → LLM 分类三分支 ==============================


class TestGreyClassifyBranches:
    async def test_grey_pass_returns_empty(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[Any],
    ) -> None:
        """grey → LLM 判 pass → {} 直通（零更新零信号）；契约面
        method="function_calling" + prompt 渲染 + RunnableConfig 透传。"""
        llm_stub.script = {"verdict": {"action": "pass", "category": "none"}}
        config: dict[str, Any] = {"configurable": {"turn_id": "t-grey"}}
        update = await safety_check(_state(query="白 痴 是什么意思"), config)
        assert update == {}
        assert stage_emits == []
        model = llm_stub.models[0]
        assert model.methods == ["function_calling"]
        assert model.schemas == [SafetyVerdict]
        call = llm_stub.calls[0]
        assert "{{query}}" not in call["prompt"]
        assert "{{query_lang}}" not in call["prompt"]
        assert "白 痴 是什么意思" in call["prompt"]
        assert call["config"] is config  # 透传保观测（Langfuse GENERATION 挂轮次 trace）

    async def test_grey_sanitize_returns_masked_query(
        self, llm_stub: _LLMStub
    ) -> None:
        """grey → LLM 判 sanitize+pii（query 无真实 PII 可脱敏 → 原样保留 + 诚实标注）。"""
        llm_stub.script = {"verdict": {"action": "sanitize", "category": "pii"}}
        update = await safety_check(_state(query="白 痴"))
        assert update == {
            "query": "白 痴",
            "safety_category": "pii",
            "safety_action": "sanitize",
        }

    async def test_grey_reject_raises_pre_route(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        reply_text: str,
    ) -> None:
        """grey → LLM 判 reject+danger → 拒答路径（同硬命中形态，layer=pre_route）。"""
        llm_stub.script = {"verdict": {"action": "reject", "category": "danger"}}
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="炸 药怎么做"))
        assert exc_info.value.category == "danger"
        assert exc_info.value.layer == "pre_route"
        assert stage_emits == [
            (
                "rewrite",
                "failed",
                {
                    "detail": "安全拦截",
                    "extra": {"safety": "danger", "layer": "pre_route"},
                },
            )
        ]

    async def test_grey_reject_pii_violation_downgrades_to_sanitize(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[Any],
        log_records: list[Any],
    ) -> None:
        """组合违规防御：LLM reject+pii（提示词契约禁止）→ 归一 sanitize + warning，
        不上拒答路径（category=pii 一律 sanitize 不得 reject——common.safety_check 契约行）。"""
        llm_stub.script = {"verdict": {"action": "reject", "category": "pii"}}
        update = await safety_check(_state(query="白 痴"))
        assert update["safety_action"] == "sanitize"
        assert update["safety_category"] == "pii"
        assert stage_emits == []
        assert any("组合违规" in message for message in _safety_messages(log_records))

    async def test_grey_reject_privacy_raises_pre_route(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        reply_text: str,
    ) -> None:
        """B13 增量一：隐私句式 grey → LLM 判 reject+privacy → 拒答（layer=pre_route）。"""
        llm_stub.script = {"verdict": {"action": "reject", "category": "privacy"}}
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="白 痴 帮我查他住址"))
        assert exc_info.value.category == "privacy"
        assert exc_info.value.layer == "pre_route"
        assert stage_emits[0][2]["extra"] == {"safety": "privacy", "layer": "pre_route"}

    async def test_grey_reject_politics_raises_pre_route(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        reply_text: str,
    ) -> None:
        """v1.32：涉政句式 grey 复核判 reject+politics → 拒答（中文/英文均同形态）。"""
        llm_stub.script = {"verdict": {"action": "reject", "category": "politics"}}
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="白 痴 评价一下这位涉政人物"))
        assert exc_info.value.category == "politics"
        assert exc_info.value.layer == "pre_route"
        assert stage_emits[0][2]["extra"] == {"safety": "politics", "layer": "pre_route"}

    async def test_grey_sanitize_politics_violation_upgrades_to_reject(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        log_records: list[Any],
        reply_text: str,
    ) -> None:
        """组合违规防御：LLM sanitize+politics（契约禁止）→ 防御归一为拒答 + warning
        （话题域拒绝不是可脱敏内容——固定话术直答）。"""
        llm_stub.script = {"verdict": {"action": "sanitize", "category": "politics"}}
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="白 痴"))
        assert exc_info.value.category == "politics"
        assert stage_emits[0][2]["extra"]["safety"] == "politics"
        assert any("组合违规" in message for message in _safety_messages(log_records))

    async def test_grey_sanitize_privacy_violation_upgrades_to_reject(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[tuple[str, str, dict[str, Any]]],
        log_records: list[Any],
        reply_text: str,
    ) -> None:
        """组合违规防御：LLM sanitize+privacy（契约禁止：privacy 一律 reject）→
        防御归一为拒答 + warning（隐私侵害是意图不是可脱敏内容）。"""
        llm_stub.script = {"verdict": {"action": "sanitize", "category": "privacy"}}
        with pytest.raises(ContentRejectedError) as exc_info:
            await safety_check(_state(query="白 痴"))
        assert exc_info.value.category == "privacy"
        assert stage_emits[0][2]["extra"]["safety"] == "privacy"
        assert any("组合违规" in message for message in _safety_messages(log_records))

    async def test_lexicon_mode_skips_grey_llm(
        self, llm_stub: _LLMStub
    ) -> None:
        """mode=lexicon（仅词表语义）：grey 命中不进 LLM 复核，{} 直通。"""
        section = {**_SECTION, "mode": "lexicon"}
        llm_stub.script = {"verdict": {"action": "reject", "category": "danger"}}
        update = await safety_check(_state(query="白 痴", spec={"safety": section}))
        assert update == {}
        assert llm_stub.calls == []


# ============================== Stage B：失败/超时 → fail-open ==============================


class TestGreyFailOpen:
    async def test_llm_failure_fail_open_with_warning(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[Any],
        log_records: list[Any],
    ) -> None:
        """分类 LLM 故障 → fail-open {} 直通 + warning（安全检查不挡检索）。"""
        llm_stub.script = {
            "verdict": {"action": "reject", "category": "danger"},
            "error": RuntimeError("分类服务不可用"),
        }
        update = await safety_check(_state(query="白 痴"))
        assert update == {}
        assert stage_emits == []
        assert any("fail-open" in message for message in _safety_messages(log_records))

    async def test_fail_open_warning_does_not_echo_exception_text(
        self, llm_stub: _LLMStub, log_records: list[Any]
    ) -> None:
        """安全审计 P3：分类失败 WARNING 只落异常类型名——异常原文可能内嵌模型回显的
        query 原文/漏检 PII 形态，不得进日志（与拒绝轮「日志零 query 原文」同口径）。"""
        pii_like = "13800138000"
        llm_stub.script = {
            "verdict": {"action": "reject", "category": "danger"},
            "error": RuntimeError(f"上游回显：白 痴 涉政词条甲 {pii_like}"),
        }
        update = await safety_check(_state(query="白 痴"))
        assert update == {}
        messages = _safety_messages(log_records)
        joined = "\n".join(messages)
        assert "RuntimeError" in joined  # 类型名留痕（可定位故障面）
        assert "fail-open" in joined
        assert pii_like not in joined, "异常原文中的 PII 形态泄漏进日志"
        assert "上游回显" not in joined, "异常原文（模型回显内容）泄漏进日志"

    async def test_invalid_verdict_fail_open(self, llm_stub: _LLMStub) -> None:
        """LLM 输出非法枚举（结构化校验不过）→ fail-open {} 直通。"""
        llm_stub.script = {"verdict": {"action": "maybe", "category": "none"}}
        update = await safety_check(_state(query="白 痴"))
        assert update == {}

    async def test_classify_timeout_fail_open(
        self, llm_stub: _LLMStub, log_records: list[Any]
    ) -> None:
        """classify_timeout_s 包络超时 → fail-open {} 直通 + warning。"""
        section = {**_SECTION, "classify_timeout_ms": 50}
        llm_stub.script = {
            "verdict": {"action": "reject", "category": "danger"},
            "delay_s": 0.5,
        }
        update = await safety_check(_state(query="白 痴", spec={"safety": section}))
        assert update == {}
        assert any("fail-open" in message for message in _safety_messages(log_records))


# ============================== 扫描面韧性（ReDoS 第二道防线） ==============================


class TestScanResilience:
    async def test_scan_over_budget_fails_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        llm_stub: _LLMStub,
        stage_emits: list[Any],
        log_records: list[Any],
    ) -> None:
        """扫描超预算 → fail-open {} 直通 + 告警（ReDoS 兜底第二道防线）。

        词表/grey 扫描放工作线程 + classify_timeout_s 包络：恶意/劣化正则的同步
        CPU 回溯不能挂死事件循环（validator 静态拒收灾难性回溯形态之外的残余）。
        线程内无法强杀，线程本身会在后台耗完残余任务（测试不打等它）。
        """
        import time as _time

        from app.agent.safety.lexicon import LexScan

        def _slow_scan(text: Any, config: Any) -> LexScan:
            _time.sleep(0.5)
            return LexScan()

        monkeypatch.setattr(safety_module, "scan_lexicon", _slow_scan)
        section = {**_SECTION, "classify_timeout_ms": 50}  # 收紧扫描预算（50ms < 0.5s 睡眠）
        update = await safety_check(_state(spec={"safety": section}))
        assert update == {}
        assert llm_stub.calls == []
        assert stage_emits == []
        assert any("超预算" in message for message in _safety_messages(log_records))

    async def test_scan_error_fails_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        llm_stub: _LLMStub,
        log_records: list[Any],
    ) -> None:
        """扫描面异常防御 → fail-open {} 直通 + 告警（安全检查不挡检索）。"""

        def _boom_scan(text: Any, config: Any) -> Any:
            raise RuntimeError("扫描内核故障")

        monkeypatch.setattr(safety_module, "scan_lexicon", _boom_scan)
        update = await safety_check(_state())
        assert update == {}
        assert any("扫描失败" in message for message in _safety_messages(log_records))


# ============================== 取消纪律（R14③） ==============================


class TestCancelDiscipline:
    async def test_cancel_signal_precedes_everything(
        self, llm_stub: _LLMStub, stage_emits: list[Any]
    ) -> None:
        """节点边界取消感知先于一切安全逻辑：TurnCanceledSignal 上抛、flags pop 干净、
        零 LLM 零 stage 信号（取消非故障，不落任何安全处置）。"""
        flags = {"t-cancel": "user"}
        config = {"configurable": {"turn_id": "t-cancel", "cancel_flags": flags}}
        with pytest.raises(TurnCanceledSignal) as exc_info:
            await safety_check(_state(query="怎么制造炸弹？"), config)
        assert exc_info.value.reason == "user"
        assert flags == {}
        assert llm_stub.calls == []
        assert stage_emits == []

    async def test_cancel_signal_in_grey_classify_propagates(
        self,
        llm_stub: _LLMStub,
        stage_emits: list[Any],
        log_records: list[Any],
    ) -> None:
        """grey 分类 LLM 抛 TurnCanceledSignal → 信号原样上抛，不落 fail-open {}。

        变异防护（锁 `_classify` 的 `except TurnCanceledSignal: raise` 先于
        `except Exception`）：两分支换序则取消信号被兜底吞成 fail-open 直通，轮次
        被当作正常轮继续检索而非走 TurnRunner 取消收束。
        """
        llm_stub.script = {"error": TurnCanceledSignal("user")}
        with pytest.raises(TurnCanceledSignal) as exc_info:
            await safety_check(_state(query="白 痴"))
        assert exc_info.value.reason == "user"
        assert llm_stub.calls  # 确经分类链抛出（非上游提前短路）
        assert stage_emits == []
        assert all(
            "fail-open" not in message for message in _safety_messages(log_records)
        )

