"""unit 层：safety 包（B13 输入安全检测件——config / sanitizer / lexicon / reply）+ guards.ContentRejectedError。

钉住的语义（冻结契约，逐条对应测试）：
- **config（fail-open）**：spec 缺 safety 节 / 结构非法 / mode 非法 → None + AgentLog
  告警一次（安全检查不挡检索，nacos-config.md §3 v1.30）；enabled=false → None 且
  **不告警**（配置门合法直通，单键热更即全链直通）；grey_rules 单条编译失败跳过该条
  并告警、其余保留（不整体失败）；timeout 以 ms 进、以 s 出（classify/reply_timeout_ms
  → *_timeout_s），非法值静默回落契约缺省（guard 不阻塞主链路，同 guards.py 先例）。
- **sanitizer（纯函数 PII）**：四形态（CN 手机号 1[3-9] 11 位 / 18 位身份证含末位 X /
  13-19 位银行卡 / 常见邮箱）边界断言防子串误伤；银行卡过 Luhn、身份证过 GB11643
  校验位（订单号/时间戳/日期/年份不误报的关键）；**负例（自指查询与短数字串）与正例
  同等对待**；重叠裁决 id_card > bank_card（18 位纯数字双合法时证件优先）。
- **lexicon**：词表精确（子串）匹配、latin 大小写不敏感；grey = grey_rules 任一命中
  且词表未硬命中（硬命中压 grey，nacos-config.md §1「词表硬命中代码层直拦」）。
- **reply**：行级类别切分（沿 synthesis._apply_answerable_variant 手法——只删另一
  类别的段行）→ render {{query_lang}} → light 模型非流式单调用（可注入 mock，全测
  零网络）；失败/超时/空输出回落切分后的模板段文本兜底（拒答不因 LLM 故障失效）→
  截断 reply_max_chars。
- **guards.ContentRejectedError**（api.md §0 v1.23 错误码 403002）：category/reply/
  layer 三字段 + 类属性 code，TurnCanceledSignal 旁同风格；取消信号仍完好（共用文件
  回归护栏）。

全 mock 零网络：模型经 reply._build_light_model 注入点打桩，Ainvoke 即假对象。
"""

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from app.agent.guards import ContentRejectedError, TurnCanceledSignal
from app.agent.prompts import load_builtin_prompt
from app.agent.safety.config import SafetyConfig, load_safety_config
from app.agent.safety.lexicon import LexScan, scan_lexicon
from app.agent.safety.reply import (
    _keep_category_section,
    generate_rejection_reply,
)
from app.agent.safety.sanitizer import PiiFinding, detect_pii, sanitize

# ---- 夹具（Luhn / GB11643 校验位均经独立脚本验算，见交付说明） ----

_VALID_SECTION: dict[str, Any] = {
    "enabled": True,
    "mode": "hybrid",
    "danger_terms": ["制造炸弹", "制毒方法"],
    "profanity_terms": ["傻逼", "SB"],
    "grey_rules": ["炸\\s*[药弹]", "傻[Bb]|S[Bb]"],
    "classify_timeout_ms": 2000,
    "reply_timeout_ms": 4000,
    "reply_max_chars": 500,
}

#: 手机号正例（1[3-9] 开头 11 位）
_MOBILES = ["13800138000", "15912345678", "18600001111"]
#: 身份证正例（GB11643 校验位合法，含末位 X 与小写 x）
_IDS = ["110101199003077774", "44052419901010004X", "110101199003074717"]
#: 银行卡正例（Luhn 合法：13/16/19 位）
_BANKS = ["4111111111119", "6222020200112230", "6217000010000000003"]
#: 邮箱正例
_EMAILS = ["user@example.com", "zhang.san@mail.qq.com", "a_b%test@sub.org.cn"]


def _safety_warnings(log_records: list[Any]) -> list[str]:
    """safety 域 WARNING 日志消息（AgentLog extra.graph == "safety"）。"""
    return [
        r["message"]
        for r in log_records
        if r["level"].name == "WARNING" and r["extra"].get("graph") == "safety"
    ]


# ============================== config ==============================


class TestLoadSafetyConfig:
    def test_loads_full_section(self) -> None:
        """完整节 → SafetyConfig：词表成 tuple、grey 编译成 Pattern、ms→s 换算。"""
        config = load_safety_config({"safety": _VALID_SECTION})
        assert config is not None
        assert config.enabled is True
        assert config.mode == "hybrid"
        assert config.danger_terms == ("制造炸弹", "制毒方法")
        assert config.profanity_terms == ("傻逼", "SB")
        assert len(config.grey_rules) == 2
        assert all(isinstance(p, re.Pattern) for p in config.grey_rules)
        assert config.classify_timeout_s == 2.0  # 2000ms → 2s
        assert config.reply_timeout_s == 4.0  # 4000ms → 4s
        assert config.reply_max_chars == 500

    def test_missing_section_fails_open_with_warning(
        self, log_records: list[Any]
    ) -> None:
        """spec 缺 safety 节 → None + 告警一次（fail-open 直通）。"""
        assert load_safety_config({"fast": {}}) is None
        assert len(_safety_warnings(log_records)) == 1

    def test_none_spec_fails_open_with_warning(self, log_records: list[Any]) -> None:
        """spec=None（无注入面）→ None + 告警一次。"""
        assert load_safety_config(None) is None
        assert len(_safety_warnings(log_records)) == 1

    def test_non_mapping_section_fails_open(self, log_records: list[Any]) -> None:
        """safety 节非对象（结构漂移）→ None + 告警一次。"""
        assert load_safety_config({"safety": "on"}) is None
        assert len(_safety_warnings(log_records)) == 1

    def test_disabled_returns_none_without_warning(
        self, log_records: list[Any]
    ) -> None:
        """enabled=false → None（合法直通），零告警（回退门语义，§1 v1.30）。"""
        section = {**_VALID_SECTION, "enabled": False}
        assert load_safety_config({"safety": section}) is None
        assert _safety_warnings(log_records) == []

    @pytest.mark.parametrize("value", ["false", "0", 1, None])
    def test_enabled_non_bool_treated_as_off(
        self, value: Any, log_records: list[Any]
    ) -> None:
        """enabled 非严格 true → 一律按关闸直通（kill switch 第二道防线）。

        JSON 字符串布尔形态漂移（热更手写 `"false"`）经 validator 拒收之外的运行面
        严格判定兜底——bool("false")=True 的宽松判定会让「误拦批量发生时运维关闸」
        反向失效（fail-closed），评审 P2；零告警（与 enabled=false 同为静默门）。
        """
        section = {**_VALID_SECTION, "enabled": value}
        assert load_safety_config({"safety": section}) is None
        assert _safety_warnings(log_records) == []

    def test_invalid_mode_fails_open_with_warning(
        self, log_records: list[Any]
    ) -> None:
        """mode 非枚举值 → None + 告警一次（validator 拒收之外的运行面第二道防线）。"""
        section = {**_VALID_SECTION, "mode": "strict"}
        assert load_safety_config({"safety": section}) is None
        assert len(_safety_warnings(log_records)) == 1

    def test_bad_grey_rule_skipped_not_fatal(self, log_records: list[Any]) -> None:
        """单条 grey 正则编译失败 → 跳过该条 + 告警，其余保留（不整体失败）。"""
        section = {
            **_VALID_SECTION,
            "grey_rules": ["炸\\s*[药弹]", "坏正则([", "傻[Bb]"],
        }
        config = load_safety_config({"safety": section})
        assert config is not None
        assert len(config.grey_rules) == 2  # 坏条目跳过、好条目保留
        assert len(_safety_warnings(log_records)) == 1  # 汇总告警一次

    def test_invalid_timeouts_fall_back_to_contract_defaults(self) -> None:
        """timeout 非法（非数/≤0）静默回落契约缺省（2s/4s，guard 不阻塞主链路）。"""
        section = {
            **_VALID_SECTION,
            "classify_timeout_ms": "abc",
            "reply_timeout_ms": -1,
            "reply_max_chars": 0,
        }
        config = load_safety_config({"safety": section})
        assert config is not None
        assert config.classify_timeout_s == 2.0
        assert config.reply_timeout_s == 4.0
        assert config.reply_max_chars == 500

    def test_missing_keys_fall_back_to_contract_defaults(self) -> None:
        """键全缺 → timeout/上限回落契约缺省、词表空 tuple（节存在即可用）。"""
        config = load_safety_config({"safety": {"enabled": True}})
        assert config is not None
        assert config.danger_terms == ()
        assert config.profanity_terms == ()
        assert config.grey_rules == ()
        assert config.classify_timeout_s == 2.0
        assert config.reply_timeout_s == 4.0
        assert config.reply_max_chars == 500

    def test_non_string_terms_dropped_with_warning(
        self, log_records: list[Any]
    ) -> None:
        """词表混入非字符串条目 → 丢弃 + 汇总告警一次，不整体失败。"""
        section = {**_VALID_SECTION, "danger_terms": ["制造炸弹", 123, None]}
        config = load_safety_config({"safety": section})
        assert config is not None
        assert config.danger_terms == ("制造炸弹",)
        assert len(_safety_warnings(log_records)) == 1


# ============================== sanitizer ==============================


class TestDetectPii:
    def test_mobile_positives_with_spans(self) -> None:
        """手机号正例 ≥3：kind/start/end 精确。"""
        text = "联系13800138000或15912345678，备18600001111。"
        findings = detect_pii(text)
        kinds = [f.kind for f in findings]
        assert kinds == ["mobile", "mobile", "mobile"]
        assert text[findings[0].start : findings[0].end] == "13800138000"
        assert (findings[0].start, findings[0].end) == (2, 13)

    def test_mobile_inside_longer_digit_run_no_match(self) -> None:
        """负例：手机号片段嵌在更长数字串（订单号）里不误报。"""
        assert detect_pii("1380013800012345") == []

    def test_id_card_positives_including_x(self) -> None:
        """身份证正例 ≥3（含末位大写 X 与小写 x）：校验位合法才命中。"""
        for value in _IDS:
            text = f"证件号{value}好"
            findings = detect_pii(text)
            assert len(findings) == 1, value
            assert findings[0].kind == "id_card"
            assert text[findings[0].start : findings[0].end] == value

    def test_id_card_bad_checksum_no_match(self) -> None:
        """负例：18 位数字但校验位不合法（订单号）不误报。"""
        assert detect_pii("110101199003077779") == []

    def test_bank_card_positives(self) -> None:
        """银行卡正例 ≥3（13/16/19 位，Luhn 合法）。"""
        for value in _BANKS:
            text = f"卡号{value}尾号查询"
            findings = detect_pii(text)
            assert len(findings) == 1, value
            assert findings[0].kind == "bank_card"

    def test_bank_card_luhn_failures_no_match(self) -> None:
        """负例：13-19 位数字串但 Luhn 不过（订单号/毫秒时间戳）不误报。"""
        for value in [
            "1234567890123456",  # 16 位订单号
            "2026111712345670",  # 16 位日期+序号
            "1737000000000",  # 13 位毫秒时间戳
            "9876543210987654321",  # 19 位
        ]:
            assert detect_pii(f"订单{value}已发货") == [], value

    def test_email_positives(self) -> None:
        """邮箱正例 ≥3。"""
        text = "发到user@example.com和zhang.san@mail.qq.com，抄送a_b%test@sub.org.cn"
        findings = detect_pii(text)
        assert [f.kind for f in findings] == ["email", "email", "email"]
        assert text[findings[0].start : findings[0].end] == "user@example.com"

    def test_email_negatives(self) -> None:
        """负例：无 TLD / 中文句点域名不误报。"""
        assert detect_pii("联系 foo@bar 谢谢") == []
        assert detect_pii("邮箱是 user@example。com") == []

    def test_self_referential_queries_no_match(self) -> None:
        """负例（自指查询）：问正则/校验算法本身不是提供 PII。"""
        assert detect_pii("手机号正则怎么写") == []
        assert detect_pii("身份证校验算法是什么") == []
        assert detect_pii("银行卡 Luhn 校验怎么实现") == []

    def test_short_digit_strings_no_match(self) -> None:
        """负例：日期/年份/短序号不误报。"""
        assert detect_pii("20261117 发货") == []
        assert detect_pii("2026 年之前的历史") == []
        assert detect_pii("订单尾号 1234") == []

    def test_dual_valid_18_digit_prefers_id_card(self) -> None:
        """重叠裁决：18 位纯数字同时过 GB11643 与 Luhn → id_card 优先（单条不重复）。"""
        findings = detect_pii("110101199003000101")
        assert len(findings) == 1
        assert findings[0].kind == "id_card"

    def test_digit_run_over_19_digits_no_match(self) -> None:
        """负例：>19 位连续数字（边界断言）整体不命中。"""
        assert detect_pii("96217000010000000004") == []

    # ---- 归一化第二遍（分隔符/全角抄写变体——评审 P2，PII 出域主通道）----

    @pytest.mark.parametrize(
        ("text", "expect"),
        [
            ("联系138-0013-8000找他", "138-0013-8000"),
            ("联系138 0013 8000找他", "138 0013 8000"),
            ("联系138.0013.8000找他", "138.0013.8000"),
            ("联系１３８００１３８０００找他", "１３８００１３８０００"),
        ],
    )
    def test_mobile_separated_and_fullwidth_variants(self, text: str, expect: str) -> None:
        """手机号分隔符/全角变体：归一化视图命中并映射回原文区间（含分隔符）。"""
        findings = detect_pii(text)
        assert len(findings) == 1, text
        assert findings[0].kind == "mobile"
        assert text[findings[0].start : findings[0].end] == expect, text

    def test_bank_card_spaced_variant(self) -> None:
        """带空格卡号（Luhn 合法数字的分组抄写形态）→ bank_card 命中。"""
        text = "卡号6222 0202 0011 2230查询"
        findings = detect_pii(text)
        assert len(findings) == 1
        assert findings[0].kind == "bank_card"
        assert text[findings[0].start : findings[0].end] == "6222 0202 0011 2230"

    def test_separated_luhn_fail_no_match(self) -> None:
        """负例：分隔变体同样过校验算法——Luhn 不过的分组数字串（订单号）不误报。"""
        assert detect_pii("订单1234 5678 9012 3456已发货") == []

    def test_date_and_version_negatives_survive_folding(self) -> None:
        """负例：折叠不改变负例画像——日期/版本号折叠后位数不足或校验不过。"""
        assert detect_pii("2026-11-17 发布") == []
        assert detect_pii("版本 3.14.15 发布了") == []
        assert detect_pii("１３８００１３ 是订单尾号") == []

    def test_empty_and_non_string_input(self) -> None:
        """空串/非字符串 → 空列表（纯函数不抛）。"""
        assert detect_pii("") == []
        assert detect_pii(None) == []  # type: ignore[arg-type]

    def test_finding_shape(self) -> None:
        """PiiFinding 冻结可比较（kind/start/end）。"""
        finding = PiiFinding(kind="mobile", start=0, end=11)
        assert finding == PiiFinding("mobile", 0, 11)
        with pytest.raises(Exception):  # frozen dataclass 禁赋值
            finding.start = 1  # type: ignore[misc]


class TestSanitize:
    def test_sanitize_mixed_text_with_placeholders(self) -> None:
        """混合形态脱敏：命中片段 → 语义占位词，其余文本原样保留。"""
        text = "我手机13800138000，邮箱user@example.com，卡6222020200112230"
        cleaned = sanitize(text, detect_pii(text))
        assert "13800138000" not in cleaned
        assert "user@example.com" not in cleaned
        assert "6222020200112230" not in cleaned
        assert "[手机号]" in cleaned
        assert "[邮箱]" in cleaned
        assert "[卡号]" in cleaned
        assert cleaned.startswith("我手机[手机号]，邮箱")
        assert cleaned.endswith("，卡[卡号]")

    def test_sanitize_id_card_x_form(self) -> None:
        cleaned = sanitize("证件44052419901010004X待核", detect_pii("证件44052419901010004X待核"))
        assert cleaned == "证件[证件号]待核"

    def test_sanitize_separated_variant_replaced_in_full(self) -> None:
        """分隔变体脱敏：命中区间含分隔符整体替换为占位词（原始号码不出域）。"""
        cleaned = sanitize("联系138-0013-8000找他", detect_pii("联系138-0013-8000找他"))
        assert cleaned == "联系[手机号]找他"
        cleaned = sanitize(
            "卡号6222 0202 0011 2230查询", detect_pii("卡号6222 0202 0011 2230查询")
        )
        assert cleaned == "卡号[卡号]查询"

    def test_sanitize_empty_findings_returns_text(self) -> None:
        assert sanitize("正常问题", []) == "正常问题"


# ============================== lexicon ==============================


def _config(**overrides: Any) -> SafetyConfig:
    """直接构造 SafetyConfig（词表扫描的确定性输入，不走 load 路径）。"""
    base: dict[str, Any] = {
        "enabled": True,
        "mode": "hybrid",
        "danger_terms": ("制造炸弹", "制毒方法"),
        "profanity_terms": ("傻逼", "SB", "白痴", "滚蛋"),
        "grey_rules": (re.compile("炸\\s*[药弹]"), re.compile("白\\s*痴")),
        "classify_timeout_s": 2.0,
        "reply_timeout_s": 4.0,
        "reply_max_chars": 500,
    }
    base.update(overrides)
    return SafetyConfig(**base)


class TestScanLexicon:
    def test_danger_hit(self) -> None:
        """危险词硬命中：kind=danger + term 原文；硬命中压 grey。"""
        scan = scan_lexicon("怎么制造炸弹", _config())
        assert isinstance(scan, LexScan)
        assert [(h.kind, h.term) for h in scan.hard_hits] == [("danger", "制造炸弹")]
        assert scan.grey is False

    def test_profanity_hit(self) -> None:
        """辱骂词硬命中：kind=profanity；grey 规则同形命中也不置 grey。"""
        scan = scan_lexicon("你就是个白痴", _config())
        assert [(h.kind, h.term) for h in scan.hard_hits] == [("profanity", "白痴")]
        assert scan.grey is False

    def test_latin_case_insensitive(self) -> None:
        """latin 词大小写不敏感（词表 SB ↔ 文本 sb / Sb）。"""
        assert scan_lexicon("你就是SB", _config()).hard_hits
        assert scan_lexicon("你是sb吗", _config()).hard_hits

    def test_grey_only_when_no_hard_hit(self) -> None:
        """grey = 规则命中且词表未硬命中（掩码/分隔变形走 grey 复核通道）。"""
        scan = scan_lexicon("帮我查 炸 药 配方", _config())
        assert scan.hard_hits == []
        assert scan.grey is True

    def test_grey_false_when_nothing_matches(self) -> None:
        scan = scan_lexicon("今天天气怎么样", _config())
        assert scan.hard_hits == []
        assert scan.grey is False

    def test_multiple_hits_both_kinds(self) -> None:
        """双词表多命中：danger 在前、profanity 在后（确定性序）。"""
        scan = scan_lexicon("制造炸弹然后骂一句傻逼", _config())
        assert [(h.kind, h.term) for h in scan.hard_hits] == [
            ("danger", "制造炸弹"),
            ("profanity", "傻逼"),
        ]

    def test_no_grey_rules_no_grey(self) -> None:
        scan = scan_lexicon("炸 药", _config(grey_rules=()))
        assert scan.grey is False

    def test_lexicon_mode_skips_grey_scan(self) -> None:
        """mode 门控（评审 P1③ 附带面）：lexicon=仅词表语义 → grey 正则不扫描
        （结果恒被弃置，扫描是白付 CPU 成本 + ReDoS 残余面，随门控消除）。"""
        scan = scan_lexicon("炸 药", _config(mode="lexicon"))
        assert scan.hard_hits == []
        assert scan.grey is False


# ============================== reply ==============================


class _FakeModel:
    """light 模型替身：记录 ainvoke 入参，按脚本返回/抛出。"""

    def __init__(
        self,
        content: Any = "温和拒答话术。",
        *,
        delay_s: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self._content = content
        self._delay_s = delay_s
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, prompt: str, config: Any = None) -> Any:
        self.calls.append({"prompt": prompt, "config": config})
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._content)


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    """注入点打桩：reply._build_light_model → _FakeModel（零网络）。"""
    model = _FakeModel()
    monkeypatch.setattr("app.agent.safety.reply._build_light_model", lambda: model)
    return model


class TestKeepCategorySection:
    def test_keeps_only_target_section_line(self) -> None:
        """行级切分（沿 _apply_answerable_variant 手法）：只删另一类别段行。"""
        template = "公共引导\n- 危险请求回复段：危险话术{{query_lang}}\n- 辱骂回复段：辱骂话术\n收尾"
        kept, body = _keep_category_section(template, "danger")
        assert "- 辱骂回复段：" not in kept
        assert "- 危险请求回复段：危险话术{{query_lang}}" in kept
        assert "公共引导" in kept and "收尾" in kept
        assert body == "危险话术{{query_lang}}"  # 段正文 = 去前缀去空白

    def test_profanity_inverse(self) -> None:
        template = "- 危险请求回复段：危险\n- 辱骂回复段：辱骂话术"
        kept, body = _keep_category_section(template, "profanity")
        assert "- 危险请求回复段：" not in kept
        assert body == "辱骂话术"

    def test_missing_section_yields_empty_body(self) -> None:
        """旧模板缺段（validator 拒收之外的兜底面）：body 为空串。"""
        kept, body = _keep_category_section("没有任何段行", "danger")
        assert kept == "没有任何段行"
        assert body == ""


class TestGenerateRejectionReply:
    async def test_llm_success_returns_model_text(
        self, fake_model: _FakeModel
    ) -> None:
        """成功路径：返回模型话术；prompt 已切分 + 渲染 + runnable_config 透传。"""
        runnable: RunnableConfig = {"tags": ["safety"]}
        reply = await generate_rejection_reply(
            "danger", "中文", load_safety_config({"safety": _VALID_SECTION}), runnable
        )
        assert reply == "温和拒答话术。"
        prompt = fake_model.calls[0]["prompt"]
        assert "- 危险请求回复段：" in prompt
        assert "- 辱骂回复段：" not in prompt
        assert "{{query_lang}}" not in prompt
        assert "中文" in prompt
        assert fake_model.calls[0]["config"] is runnable

    async def test_profanity_prompt_sections(self, fake_model: _FakeModel) -> None:
        """profanity：只保留辱骂段。"""
        await generate_rejection_reply(
            "profanity", "中文", load_safety_config({"safety": _VALID_SECTION})
        )
        prompt = fake_model.calls[0]["prompt"]
        assert "- 辱骂回复段：" in prompt
        assert "- 危险请求回复段：" not in prompt

    async def test_llm_failure_falls_back_to_section_text(
        self, fake_model: _FakeModel
    ) -> None:
        """LLM 异常 → 回落切分后的模板段文本（拒答不因 LLM 故障失效）。"""
        fake_model._error = RuntimeError("boom")
        reply = await generate_rejection_reply(
            "danger", "中文", load_safety_config({"safety": _VALID_SECTION})
        )
        assert "温和告知此类请求无法协助" in reply
        assert "{{query_lang}}" not in reply  # 段文本已渲染
        assert reply.startswith("以中文作答")

    async def test_llm_timeout_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超时（reply_timeout_s 包络）→ 回落段文本。"""
        model = _FakeModel(delay_s=0.5)
        monkeypatch.setattr("app.agent.safety.reply._build_light_model", lambda: model)
        config = load_safety_config(
            {"safety": {**_VALID_SECTION, "reply_timeout_ms": 50}}
        )
        reply = await generate_rejection_reply("danger", "中文", config)
        assert "温和告知此类请求无法协助" in reply

    async def test_empty_output_falls_back(self, fake_model: _FakeModel) -> None:
        """空输出（str 空 / None / 列表空）→ 回落段文本。"""
        for empty in ("", None, []):
            fake_model._content = empty
            reply = await generate_rejection_reply(
                "danger", "中文", load_safety_config({"safety": _VALID_SECTION})
            )
            assert "温和告知此类请求无法协助" in reply

    async def test_truncation_to_reply_max_chars(
        self, fake_model: _FakeModel
    ) -> None:
        """截断到 reply_max_chars。"""
        fake_model._content = "长" * 800
        config = load_safety_config(
            {"safety": {**_VALID_SECTION, "reply_max_chars": 300}}
        )
        reply = await generate_rejection_reply("danger", "中文", config)
        assert len(reply) == 300

    async def test_list_content_extracted(self, fake_model: _FakeModel) -> None:
        """AIMessage content 为分块列表时拼接 text 块。"""
        fake_model._content = [{"type": "text", "text": "分块"}, {"type": "text", "text": "话术"}]
        reply = await generate_rejection_reply(
            "danger", "中文", load_safety_config({"safety": _VALID_SECTION})
        )
        assert reply == "分块话术"

    async def test_missing_section_falls_back_to_kept_template(
        self, monkeypatch: pytest.MonkeyPatch, fake_model: _FakeModel
    ) -> None:
        """模板缺类别段（body 空）→ 回落整份切分后模板文本。"""
        monkeypatch.setattr(
            "app.agent.safety.reply.resolve_prompt", lambda key: "旧模板无段行"
        )
        fake_model._error = RuntimeError("boom")
        reply = await generate_rejection_reply(
            "danger", "中文", load_safety_config({"safety": _VALID_SECTION})
        )
        assert reply == "旧模板无段行"

    async def test_render_error_skips_llm_and_uses_raw_section(
        self, monkeypatch: pytest.MonkeyPatch, fake_model: _FakeModel
    ) -> None:
        """模板含未知占位符（渲染 fail-fast）→ 不调 LLM，回落原始段文本。"""
        monkeypatch.setattr(
            "app.agent.safety.reply.resolve_prompt",
            lambda key: "- 危险请求回复段：段文本{{oops}}",
        )
        reply = await generate_rejection_reply(
            "danger", "中文", load_safety_config({"safety": _VALID_SECTION})
        )
        assert fake_model.calls == []
        assert "段文本{{oops}}" in reply

    async def test_builtin_template_is_consumable(self, fake_model: _FakeModel) -> None:
        """内置 safety_reply 默认模板被本函数端到端消费（两段前缀齐备）。"""
        template = load_builtin_prompt("common.safety_reply")
        assert "- 危险请求回复段：" in template
        assert "- 辱骂回复段：" in template
        reply = await generate_rejection_reply(
            "profanity", "中文", load_safety_config({"safety": _VALID_SECTION})
        )
        assert reply == "温和拒答话术。"

    async def test_cancel_signal_propagates_not_template_fallback(
        self, fake_model: _FakeModel
    ) -> None:
        """LLM 抛 TurnCanceledSignal → 信号原样上抛，不回落模板兜底话术。

        变异防护（锁 reply.py 的 `except TurnCanceledSignal: raise` 先于
        `except Exception`）：两分支换序则取消被兜底吞成段文本，取消轮被当作
        拒答轮收束（而非 TurnRunner 取消收束）。
        """
        fake_model._error = TurnCanceledSignal("superseded")
        with pytest.raises(TurnCanceledSignal) as exc_info:
            await generate_rejection_reply(
                "danger", "中文", load_safety_config({"safety": _VALID_SECTION})
            )
        assert exc_info.value.reason == "superseded"
        assert len(fake_model.calls) == 1  # 确经 LLM 调用抛出（非渲染早退）

    async def test_unknown_category_raises(self, fake_model: _FakeModel) -> None:
        """负例：pii 等非 reject 类别 → ValueError（契约「pii 一律 sanitize 不 reject」）。"""
        with pytest.raises(ValueError):
            await generate_rejection_reply(
                "pii", "中文", load_safety_config({"safety": _VALID_SECTION})
            )


# ============================== guards.ContentRejectedError ==============================


class TestContentRejectedError:
    def test_fields_and_code(self) -> None:
        """category/reply/layer 三字段 + 类属性 code=403002（api.md §0 v1.23）。"""
        exc = ContentRejectedError(
            category="danger", reply="无法协助。", layer="pre_route"
        )
        assert exc.category == "danger"
        assert exc.reply == "无法协助。"
        assert exc.layer == "pre_route"
        assert ContentRejectedError.code == "403002"
        assert isinstance(exc, Exception)

    def test_message_carries_summary(self) -> None:
        """str() 带层与类别摘要（沿 TurnCanceledSignal 风格）。"""
        exc = ContentRejectedError(
            category="profanity", reply="话术", layer="rewrite"
        )
        assert "rewrite" in str(exc)
        assert "profanity" in str(exc)

    def test_turn_canceled_signal_untouched(self) -> None:
        """共用文件回归护栏：取消信号仍按 R14② 形态工作。"""
        exc = TurnCanceledSignal("user")
        assert exc.reason == "user"
