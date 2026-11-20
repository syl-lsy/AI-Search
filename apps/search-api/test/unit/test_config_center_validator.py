"""unit 层：config_center/validator.py——五 dataId 结构校验 + 密钥嗅探（nacos-config.md §1~§3）。

钉住的语义：
- 校验范围 = 键位结构 + §3 数值范围规则 + 顶层信封（schema_version/updated_at 必带）；
  prompts 键缺失不拒收（§1「Nacos 键缺失时回落默认」）；
- 密钥嗅探：值中出现疑似密钥形态（`^(sk-|tvly-|fc-|as_sk-)`）或敏感字段名（key/secret/
  token/password）带非 env 名值 → 整版拒收（§2 ADR-005 安全条款）；错误信息只含字段
  定位路径，绝不含值本身（密钥内容不入日志）；
- 单条规则与契约逐条对应（见各用例 docstring 的 § 标注）。
"""
import copy
import json
import re

import pytest

from app.config_center.defaults import BUILTIN_DEFAULTS
from app.config_center.validator import (
    SAFETY_FIELD_TOKEN,
    SAFETY_REPLY_LINE_PREFIXES,
    AssetValidationError,
    SYNTHESIS_VARIANT_PREFIXES,
    safety_token_warnings,
    sniff_secret_violations,
    validate_asset,
)

SPEC_DATA_ID = "ai_search.agent.spec.json"
MCP_DATA_ID = "ai_search.agent.mcp.json"
PROMPTS_DATA_ID = "ai_search.agent.prompts.json"
SKILLS_DATA_ID = "ai_search.agent.skills.json"
MODEL_DATA_ID = "ai_search.model.mapping.json"


def _mutated(data_id: str, mutate) -> dict:
    payload = copy.deepcopy(BUILTIN_DEFAULTS[data_id])
    mutate(payload)
    return payload


class TestDefaultsValidate:
    def test_all_five_defaults_validate(self):
        """L4 内置默认必须全部过校验——降级阶梯的最低一级自身合法。"""
        for data_id, payload in BUILTIN_DEFAULTS.items():
            assert validate_asset(data_id, payload) == payload

    def test_unknown_data_id_rejected(self):
        with pytest.raises(AssetValidationError):
            validate_asset("ai_search.agent.unknown.json", {"schema_version": 1})


class TestEnvelope:
    def test_missing_schema_version_rejected(self):
        payload = _mutated(MCP_DATA_ID, lambda p: p.pop("schema_version"))
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)

    def test_empty_updated_at_rejected(self):
        payload = _mutated(MCP_DATA_ID, lambda p: p.update({"updated_at": ""}))
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)


class TestSecretSniffing:
    @pytest.mark.parametrize(
        "literal",
        ["sk-abc123", "tvly-real-key-9", "fc-live-1", "as_sk-xyz"],
    )
    def test_secret_literal_in_any_value_rejected(self, literal):
        """密钥字面量出现在任何字符串值中 → 整版拒收（§2 安全条款）。"""
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["tavily"].update({"auth": literal}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)

    def test_sensitive_field_with_non_env_value_rejected(self):
        """字段名含 key/secret/token/password 且值非 env 名 → 拒收（§2）。"""
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["tavily"].update({"api_key": "hunter2"}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)

    def test_sniff_violations_report_paths_not_values(self):
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["tavily"].update({"auth": "tvly-secret-value"}),
        )
        violations = sniff_secret_violations(payload)
        assert violations, "应检出违规"
        assert all("tvly-secret-value" not in v for v in violations), "违规描述只含路径不含值"
        joined = "\n".join(violations)
        assert "tavily" in joined and "auth" in joined

    def test_env_name_values_pass(self):
        """env 指针值（TAVILY_API_KEY 等）不是密钥——放行（§2「值只能是指针」）。"""
        assert sniff_secret_violations(BUILTIN_DEFAULTS[MCP_DATA_ID]) == []

    def test_nested_secret_in_prompts_rejected(self):
        """嗅探按契约锚定口径（匹配 `^(sk-|tvly-|fc-|as_sk-)`）——值以前缀开头的深层字符串也拒收。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"fast.rewrite": "sk-abc123def456"}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_mid_string_dash_not_false_positive(self):
        """非开头子串（如 task-xxx）不误伤——契约正则为开头锚定。

        注：v1.20 起 fast.rewrite 覆盖值须含 `"search_strategy"`（§3 校验条 1）、v1.27 起
        还须含**枚举串本体**（`"search_strategy":"direct|verify|web|recall"` 与
        `|navigational|other"`，散文提及不算），故本用例取值带上两处契约形态
        （本用例只验密钥嗅探不误伤，与内容规则正交）。
        """
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.rewrite": (
                        '前缀 task-123 "search_strategy":"direct|verify|web|recall" '
                        '"query_type":"factoid|how_to|navigational|other" 后缀'
                    )
                }
            ),
        )
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload


class TestMcpRules:
    def test_env_pointer_must_be_env_name(self):
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["tavily"].update({"url_env": "https://evil.example/mcp"}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)

    def test_http_transport_requires_url_env(self):
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["tavily"].update({"url_env": ""}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)

    def test_stdio_transport_requires_command_env(self):
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["bocha"].update({"command_env": ""}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)

    def test_unknown_role_rejected(self):
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["tavily"].update({"role": "backup_search"}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)

    def test_unknown_transport_rejected(self):
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["tavily"].update({"transport": "grpc"}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(MCP_DATA_ID, payload)


class TestMcpSearxngChain:
    """§2 v1.18：搜索兜底链式化——searxng（fallback_search 链首）→ anysearch
    （fallback_search_secondary 次级）。客户端（validator/defaults）必须先于 v1.18
    mcp.json 发布同步，否则发布时整份配置被校验拒收（契约 §8 init 顺序红线）。"""

    def test_defaults_full_mcp_list_passes_validator(self):
        """L4 内置默认 mcp 清单（v1.18 六源）全量过校验——发布前置条件。"""
        payload = BUILTIN_DEFAULTS[MCP_DATA_ID]
        assert validate_asset(MCP_DATA_ID, payload) == payload

    def test_searxng_entry_matches_contract(self):
        """§2 v1.18 searxng 条目逐字对齐：stdio 指针 + timeout 5 + 无 auth
        （自部署环回无密钥——auth 指针不得出现）。"""
        entry = BUILTIN_DEFAULTS[MCP_DATA_ID]["servers"].get("searxng")
        assert entry is not None, "searxng 条目缺失"
        assert entry["enabled"] is True
        assert entry["role"] == "fallback_search"
        assert entry["transport"] == "stdio"
        assert entry["command_env"] == "SEARXNG_MCP_COMMAND"
        assert entry["args_env"] == "SEARXNG_MCP_ARGS"
        assert entry["timeout_s"] == 5
        assert entry["circuit"] == {"failure_threshold": 3, "cooldown_s": 60}
        assert "auth" not in entry, "自部署环回无密钥——searxng 条目不得带 auth 字段"

    def test_searxng_precedes_anysearch_in_chain_order(self):
        """链序锁死：searxng（fallback_search 链首）在 anysearch（次级）之前。"""
        order = list(BUILTIN_DEFAULTS[MCP_DATA_ID]["servers"])
        assert "searxng" in order and "anysearch" in order
        assert order.index("searxng") < order.index("anysearch")

    def test_anysearch_role_is_fallback_search_secondary(self):
        """§2 v1.18：anysearch 降为链上次级——role=fallback_search_secondary，
        其余字段不动（链序 searxng→anysearch）。"""
        entry = BUILTIN_DEFAULTS[MCP_DATA_ID]["servers"]["anysearch"]
        assert entry["role"] == "fallback_search_secondary"

    def test_fallback_search_secondary_role_accepted(self):
        """validator 枚举已扩 fallback_search_secondary——v1.18 mcp.json 可发布的前提。"""
        payload = _mutated(
            MCP_DATA_ID,
            lambda p: p["servers"]["tavily"].update({"role": "fallback_search_secondary"}),
        )
        assert validate_asset(MCP_DATA_ID, payload) == payload


class TestPromptsRules:
    def test_prompt_value_must_be_string(self):
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"fast.rewrite": 123}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_suggest_pool_over_eight_rejected(self):
        """§1 v1.11：数组长度 0~8，越界拒收。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"home.suggest_pool": [f"问题{i}" for i in range(9)]}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_suggest_pool_empty_string_entry_rejected(self):
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"home.suggest_pool": ["合法问题", ""]}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_suggest_pool_overlong_entry_rejected(self):
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"home.suggest_pool": ["x" * 201]}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_suggest_pool_empty_array_legal(self):
        """§1 v1.11：空数组合法（前端聚焦态不渲染面板）。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"home.suggest_pool": []}),
        )
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload

    def test_missing_prompt_key_allowed(self):
        """§1：Nacos 键缺失回落内置默认——缺键不拒收。"""
        payload = _mutated(PROMPTS_DATA_ID, lambda p: p["prompts"].pop("fast.rewrite"))
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload


class TestSpecRules:
    def test_type_query_map_missing_other_rejected(self):
        """§3：type_query_map 键必须覆盖 7 类枚举（缺 other 拒收）。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["fast"]["type_query_map"].pop("other"))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize("pair", [[3, 2], [0, 1], [2, 5]])
    def test_type_query_map_range_rejected(self, pair):
        """§3：各区间 1≤min≤max≤4。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["fast"]["type_query_map"].update({"other": pair}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_read_min_over_read_max_rejected(self):
        """§3：read_min ≤ read_max。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["deep"].update({"read_min": 6}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_reflect_rounds_over_max_rejected(self):
        """§3：reflect_rounds ≤ max_reflect_rounds。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["deep"].update({"reflect_rounds": 4}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_recursion_limit_zero_rejected(self):
        """§3：recursion_limit > 0。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["fast"].update({"recursion_limit": 0}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_retry_backoff_factor_below_one_rejected(self):
        """§3：retry.initial_interval_s > 0 且 backoff_factor ≥ 1。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["fast"]["retry"].update({"backoff_factor": 0.5}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_compact_fraction_order_rejected(self):
        """§3：0 < keep_fraction < trigger_fraction < 1。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["deep"]["compact"].update({"keep_fraction": 0.9}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_code_exec_cpu_limit_over_four_rejected(self):
        """§3 v1.5：0 < code_exec_cpu_limit ≤ 4（核）。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["deep"].update({"code_exec_cpu_limit": 5}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_code_exec_mem_limit_out_of_range_rejected(self):
        """§3 v1.5：256 ≤ code_exec_mem_limit_mb ≤ 8192（MB）。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["deep"].update({"code_exec_mem_limit_mb": 128}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_turn_conflict_invalid_value_rejected(self):
        """§3 v1.6：default_on_conflict ∈ {reject, enqueue, interrupt, rollback}。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["turn"].update({"default_on_conflict": "bogus"}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_turn_section_missing_allowed(self):
        """§3 v1.6：turn 节整节缺失 → 回落代码内置默认（组装层告警），校验不拒收。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p.pop("turn"))
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    def test_followups_count_over_five_rejected(self):
        """§3 v1.10：1 ≤ followups.count ≤ 5（与 turns.followups ≤5 对齐）。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["followups"].update({"count": 6}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_followups_hold_window_margin_rejected(self):
        """§3 v1.10 P1：sse_hold_window_ms ≥ generate_timeout_ms + 2000（强制裕量）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["followups"].update({"sse_hold_window_ms": 8_500}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_routing_default_mode_not_in_modes_rejected(self):
        """§3 v1.13：default_mode 须存在于 modes（经 modes[default_mode] 解析为图名）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["routing"].update({"default_mode": "ultra"}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_routing_empty_modes_rejected(self):
        payload = _mutated(SPEC_DATA_ID, lambda p: p["routing"].update({"modes": {}}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_max_answer_chars_zero_rejected(self):
        """§3 v1.14：max_answer_chars 为正整数（>0）。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["fast"].update({"max_answer_chars": 0}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_memory_inject_top_k_over_twenty_rejected(self):
        """§3 memory 节：0 < inject_top_k ≤ 20。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["memory"].update({"inject_top_k": 21}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_confidence_out_of_range_rejected(self):
        """§3 memory 节：0 < candidate_min_confidence ≤ 1。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["memory"]["extraction"].update({"candidate_min_confidence": 1.5}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)


class TestSkillsRules:
    def test_empty_skill_name_rejected(self):
        payload = _mutated(
            SKILLS_DATA_ID,
            lambda p: p["skills"][0].update({"name": ""}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SKILLS_DATA_ID, payload)


class TestModelMappingRules:
    def test_default_not_in_options_rejected(self):
        """§5：chat.default 必须是 chat.options 的键（消费方按 options[default] 取别名）。"""
        payload = _mutated(MODEL_DATA_ID, lambda p: p["chat"].update({"default": "kimi"}))
        with pytest.raises(AssetValidationError):
            validate_asset(MODEL_DATA_ID, payload)

    def test_empty_alias_rejected(self):
        payload = _mutated(
            MODEL_DATA_ID,
            lambda p: p["chat"]["options"].update({"qwen": ""}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(MODEL_DATA_ID, payload)

    def test_empty_single_alias_rejected(self):
        payload = _mutated(MODEL_DATA_ID, lambda p: p.update({"light": ""}))
        with pytest.raises(AssetValidationError):
            validate_asset(MODEL_DATA_ID, payload)


class TestPromptsSearchStrategyRules:
    """§3 v1.20 两条 prompts 校验（检索策略增量防热更漂移）。

    **v1.33 退役注**：fast.rewrite schema 枚举串硬校验已退役（减面后判据唯一事实源 =
    fast.decision.questions_json——方言硬校验见 TestDecisionPromptsRules；策略枚举
    options 防漂移软告警见 decision_prompts_warnings）。synthesis 两条结构性校验保留。
    """

    def test_slim_rewrite_without_strategy_schema_accepted(self):
        """v1.33 减面形态：fast.rewrite 无 search_strategy schema 串 → 合法通过
        （分类判据面迁 questions_json——slim 形态即契约形态）。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.rewrite": p["prompts"]["fast.rewrite"].replace(
                        '"search_strategy":"direct|verify|web|recall",', ""
                    )
                }
            ),
        )
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload

    def test_synthesis_template_missing_direct_prefix_rejected(self):
        """fast.synthesis 缺「免检索直答路径」行前缀 → 整版拒收（行级切分结构性校验）。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {"fast.synthesis": p["prompts"]["fast.synthesis"].replace("- 免检索直答路径", "- 其他路径")}
            ),
        )
        with pytest.raises(AssetValidationError, match="免检索直答路径"):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_synthesis_template_missing_regular_prefix_rejected(self):
        """三段中任一缺失即拒收（常规段缺失同判）。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {"fast.synthesis": p["prompts"]["fast.synthesis"].replace("- 常规路径", "- 通用路径")}
            ),
        )
        with pytest.raises(AssetValidationError, match="常规路径"):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_defaults_pass_new_prompt_rules(self):
        """内置默认（v1.20 文本）过新校验——校验面与契约文本同源。"""
        payload = _mutated(PROMPTS_DATA_ID, lambda p: None)
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload


class TestConnectionRetryNode:
    """§3 v1.24（B11 P1a）：`retry.connection` 五键——**告警不拒收**（软校验）。"""

    def test_defaults_include_connection_node(self):
        """L4 内置默认含五键连接类节（生产 floor 与运行态同源）。

        v1.59 增量 A（B14）：`total_window_s` 20.0 → **60.0**（窗-包络对齐——一次完整
        包络 60s + 全退避链 15s + 余量；nacos-config.md §3 v1.32）。
        """
        node = BUILTIN_DEFAULTS[SPEC_DATA_ID]["fast"]["retry"]["connection"]
        assert node == {
            "max_attempts": 5,
            "initial_interval_s": 1.0,
            "backoff_factor": 2.0,
            "max_interval_s": 8.0,
            "total_window_s": 60.0,
        }

    def test_invalid_connection_node_not_rejected(self):
        """任一键非法/节点形态非法 → 不拒收（运行期整节回落 retry 主节，灰度安全）。"""
        for mutate in (
            lambda p: p["fast"]["retry"]["connection"].update({"max_attempts": 0}),
            lambda p: p["fast"]["retry"]["connection"].update({"initial_interval_s": 0}),
            lambda p: p["fast"]["retry"]["connection"].update({"backoff_factor": 0.5}),
            lambda p: p["fast"]["retry"]["connection"].pop("total_window_s"),
            lambda p: p["fast"]["retry"].update({"connection": "not-a-map"}),
        ):
            payload = _mutated(SPEC_DATA_ID, mutate)
            assert validate_asset(SPEC_DATA_ID, payload) == payload

    def test_connection_warnings_cover_invalid_and_linkage(self):
        """软校验文案：键非法告警 + 联动约束告警（10s + total_window_s + 8s > turn_timeout_s）。

        v1.59 值口径（fast.llm_timeout_s=60 / total_window_s=60 / turn_timeout_s=120）：
        内置默认组合零告警；total_window_s 拉大到 110 触发联动约束③。
        """
        from app.config_center.validator import connection_retry_warnings

        assert connection_retry_warnings(
            {"retry": {"connection": BUILTIN_DEFAULTS[SPEC_DATA_ID]["fast"]["retry"]["connection"]},
             "llm_timeout_s": 60, "turn_timeout_s": 120}
        ) == []

        invalid = connection_retry_warnings({"retry": {"connection": {"max_attempts": 0}},
                                            "llm_timeout_s": 60, "turn_timeout_s": 120})
        assert len(invalid) == 1 and "回落 retry 主节" in invalid[0]

        relaxed = connection_retry_warnings(
            {"retry": {"connection": {
                "max_attempts": 5, "initial_interval_s": 1.0, "backoff_factor": 2.0,
                "max_interval_s": 8.0, "total_window_s": 110.0,
            }}, "llm_timeout_s": 60, "turn_timeout_s": 120}
        )
        assert len(relaxed) == 1 and "turn_timeout_s" in relaxed[0]

    def test_connection_window_smaller_than_envelope_warns(self):
        """软校验④（v1.59，trace 89043f23 事故形态）：total_window_s < llm_timeout_s →
        WARN 不拒收——窗小于包络时首个重试决策点即被窗闸饿死（max_attempts 名实不符）。"""
        from app.config_center.validator import connection_retry_warnings

        warnings = connection_retry_warnings(
            {"retry": {"connection": {
                "max_attempts": 5, "initial_interval_s": 1.0, "backoff_factor": 2.0,
                "max_interval_s": 8.0, "total_window_s": 30.0,
            }}, "llm_timeout_s": 60, "turn_timeout_s": 120}
        )
        assert len(warnings) == 1
        assert "total_window_s" in warnings[0] and "llm_timeout_s" in warnings[0]

        # 软校验不拒收：整份 payload 仍可发布（运行期整节可用）
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["fast"]["retry"]["connection"].update({"total_window_s": 30.0}),
        )
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    def test_missing_connection_node_is_silent(self):
        """节缺失 = 现状行为：零告警零拒收（回退门 = 单键热更删节）。"""
        from app.config_center.validator import connection_retry_warnings

        payload = _mutated(SPEC_DATA_ID, lambda p: p["fast"]["retry"].pop("connection"))
        assert validate_asset(SPEC_DATA_ID, payload) == payload
        assert connection_retry_warnings(payload["fast"]) == []


class TestRecallNavigationalRules:
    """§3 v1.27 校验扩容（recall/navigational 防热更漂移）。"""

    def test_type_query_map_missing_navigational_rejected(self):
        """§3 v1.27：type_query_map 须覆盖 8 类枚举（缺 navigational 拒收）。"""
        payload = _mutated(
            SPEC_DATA_ID, lambda p: p["fast"]["type_query_map"].pop("navigational")
        )
        with pytest.raises(AssetValidationError, match="navigational"):
            validate_asset(SPEC_DATA_ID, payload)

    def test_synthesis_template_missing_recall_prefix_rejected(self):
        """fast.synthesis 缺「回顾整理路径」行前缀 → 整版拒收（四段结构性校验扩容）。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.synthesis": p["prompts"]["fast.synthesis"].replace(
                        "- 回顾整理路径", "- 其他路径"
                    )
                }
            ),
        )
        with pytest.raises(AssetValidationError, match="回顾整理路径"):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_rewrite_template_without_recall_accepted_after_slim(self):
        """【v1.33 退役翻正】减面后 fast.rewrite 全文无 recall/navigational 字样 →
        合法通过（分类判据面迁 questions_json——防漂移载体 = search_strategy options
        软告警；synthesis/type_query_map 侧校验保留不变）。"""
        slim = BUILTIN_DEFAULTS[PROMPTS_DATA_ID]["prompts"]["fast.rewrite"]
        assert "recall" not in slim and "navigational" not in slim  # 减面前置
        payload = _mutated(PROMPTS_DATA_ID, lambda p: None)
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload

    def test_strategy_options_missing_recall_warns_not_rejected(self):
        """防漂移迁移载体：questions_json.search_strategy options 缺 recall → 软告警
        不拒收（decision 永不产出 recall 的 R17 漂移面应当被看见）。"""
        from app.config_center.validator import decision_prompts_warnings
        import json as _json

        questions = {
            "search_strategy": {
                "type": "choice",
                "options": ["direct", "verify", "web"],
                "criteria": {"direct": "x", "verify": "y", "web": "z"},
            }
        }
        prompts = {
            "fast.decision.questions_json": _json.dumps(questions, ensure_ascii=False)
        }
        warnings = decision_prompts_warnings(prompts)
        assert any("recall" in warning for warning in warnings)
        # 软校验不拒收：整份 payload 仍可发布
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {"fast.decision.questions_json": _json.dumps(questions, ensure_ascii=False)}
            ),
        )
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload

    @pytest.mark.parametrize("prefix", SYNTHESIS_VARIANT_PREFIXES)
    def test_synthesis_any_variant_prefix_missing_rejected(self, prefix):
        """四段行前缀完整性（§3 v1.27）：任一段缺失（含既有三段）一律整版拒收。

        既有用例只覆盖「常规路径」/「免检索直答路径」缺失 + 本批新增「回顾整理路径」，
        「最小检索路径」此前无拒收用例——本参数据逐段回归常量与提示词的四段对齐
        （该校验器按 `SYNTHESIS_VARIANT_PREFIXES` 遍历，是行级切分的唯一锚点）。
        """
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {"fast.synthesis": p["prompts"]["fast.synthesis"].replace(prefix, "- 其他路径")}
            ),
        )
        with pytest.raises(AssetValidationError, match=prefix[2:]):
            validate_asset(PROMPTS_DATA_ID, payload)


class TestValidatorProseBypassHardening:
    """评审修复轮（security P3②/③ + 评审 P3④）：枚举串精确形态 + history_block 占位符。

    **v1.33 退役注**：fast.rewrite 枚举串散文绕过硬校验已随减面退役（判据唯一事实源 =
    fast.decision.questions_json——questions_json 的 search_strategy options 缺策略
    枚举值走 decision_prompts_warnings 软告警，见 TestRecallNavigationalRules）；
    history_block 占位符校验保留不变。
    """

    def test_enum_recall_removed_prose_mentions_still_accepted_after_slim(self):
        """【v1.33 退役翻正】旧全量形态（v1.32 文本——枚举串含 recall + 散文提及）
        亦合法通过（发布过渡窗容忍：Nacos 旧值 + 新代码，判据双份冗余无害；
        防漂移面迁移至 questions_json options 软告警）。"""
        legacy_full_rewrite = (
            "你是检索查询改写器（light 模型）。一次完成五件事……\n"
            '{"queries":["..."],"query_type":"factoid|list|comparison|multi_facet|'
            'temporal|how_to|navigational|other","answerable_from_knowledge":false,'
            '"search_strategy":"direct|verify|web|recall","safety":'
            '"none|danger|profanity|privacy|politics","time_range":null,'
            '"memory_directive":null}\n'
            "④ 检索策略判定——recall 仅限回顾整理请求（散文面提及 recall）。"
        )
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"fast.rewrite": legacy_full_rewrite}),
        )
        assert "recall" in payload["prompts"]["fast.rewrite"]
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload

    def test_enum_navigational_removed_prose_mentions_it_still_accepted_after_slim(self):
        """【v1.33 退役翻正】旧全量形态缺 navigational 枚举尾段 → 合法通过（同上——
        判据面迁移，synthesis/type_query_map 侧校验保留不变）。"""
        legacy_full_rewrite = (
            "你是检索查询改写器（light 模型）。一次完成五件事……\n"
            '{"queries":["..."],"query_type":"factoid|list|comparison|multi_facet|'
            'temporal|how_to|other","answerable_from_knowledge":false,'
            '"search_strategy":"direct|verify|web","safety":'
            '"none|danger|profanity|privacy|politics","time_range":null,'
            '"memory_directive":null}\n'
            "导航型题目在正文提及 navigational 判定（散文面）。"
        )
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"fast.rewrite": legacy_full_rewrite}),
        )
        assert "navigational" in payload["prompts"]["fast.rewrite"]
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload

    def test_synthesis_template_without_history_block_placeholder_rejected(self):
        """fast.synthesis 缺 `{{history_block}}` 占位符 → 拒收（security P3③）。

        实证风险：热更删占位符仍过校验 → recall 轮静默丢历史注入（模板无占位符时
        render_prompt 不报错，历史块被无提示地弃用）——无占位符即无 recall 能力。
        """
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.synthesis": p["prompts"]["fast.synthesis"].replace(
                        "{{history_block}}", ""
                    )
                }
            ),
        )
        with pytest.raises(AssetValidationError, match="history_block"):
            validate_asset(PROMPTS_DATA_ID, payload)


class TestSafetySpecRules:
    """§3 B13：safety 节——节缺失=合法（fail-open）；词表/正则/边界越界拒收面。"""

    def test_safety_section_missing_allowed(self):
        """safety 节缺失 = 合法（fail-open：安全检查不挡检索，B13）。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p.pop("safety"))
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    def test_defaults_seed_safety_section_conservative(self):
        """L4 种子：enabled=True（v1.59 增量 B 裁决④「全类别生效」——nacos-config.md §3
        v1.32；kill switch 单键回退门保留）+ mode=hybrid + 三表齐备且保守
        （danger/profanity/privacy；宁可漏不可误）+ politics_terms 初值空数组（词表内容
        用户自填、AI 零生成）+ grey_rules 均可编译且非灾难性回溯形态。"""
        safety = BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]
        assert safety["enabled"] is True
        assert safety["mode"] == "hybrid"
        assert 0 < len(safety["danger_terms"]) <= 200
        assert 0 < len(safety["profanity_terms"]) <= 200
        assert 0 < len(safety["privacy_terms"]) <= 200
        assert safety["politics_terms"] == []  # v1.32：初值空数组（用户 Nacos 控制台自填）
        assert 0 < len(safety["grey_rules"]) <= 50
        for rule in safety["grey_rules"]:
            re.compile(rule)  # 不抛即合法

    def test_bad_regex_in_grey_rules_rejected(self):
        """grey_rules 逐条 re.compile 校验：编译失败 = 拒收 violation。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"]["grey_rules"].append("("),  # 未闭合分组
        )
        with pytest.raises(AssetValidationError, match="grey_rules"):
            validate_asset(SPEC_DATA_ID, payload)

    def test_grey_rules_over_fifty_rejected(self):
        payload = _mutated(
            SPEC_DATA_ID, lambda p: p["safety"].update({"grey_rules": ["x"] * 51})
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_grey_rules_entry_over_120_chars_rejected(self):
        payload = _mutated(
            SPEC_DATA_ID, lambda p: p["safety"]["grey_rules"].append("x" * 121)
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        "field", ["danger_terms", "profanity_terms", "privacy_terms", "politics_terms"]
    )
    def test_term_lexicon_over_200_rejected(self, field):
        payload = _mutated(
            SPEC_DATA_ID, lambda p, f=field: p["safety"].update({f: ["词"] * 201})
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        "field", ["danger_terms", "profanity_terms", "privacy_terms", "politics_terms"]
    )
    def test_term_entry_empty_rejected(self, field):
        payload = _mutated(
            SPEC_DATA_ID, lambda p, f=field: p["safety"][f].append("")
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        "field", ["danger_terms", "profanity_terms", "privacy_terms", "politics_terms"]
    )
    def test_term_entry_over_64_chars_rejected(self, field):
        """§3 校验规则 v1.30：词表单条 ≤64 字符（越界拒收）。"""
        payload = _mutated(
            SPEC_DATA_ID, lambda p, f=field: p["safety"][f].append("x" * 65)
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        "field", ["danger_terms", "profanity_terms", "privacy_terms", "politics_terms"]
    )
    def test_term_entry_duplicated_rejected(self, field):
        """§3 校验规则 v1.30：词表条目去重（重复条目拒收）；politics_terms 初值空表
        （v1.32）→ 先播种再重复。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p, f=field: (
                p["safety"][f].append(p["safety"][f][0])
                if p["safety"][f]
                else p["safety"].update({f: ["涉政词条", "涉政词条"]})
            ),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_politics_terms_only_lexicon_accepted_and_usable(self):
        """v1.32 四表口径：politics_terms 非空即满足「至少其一非空」，且条目规则同
        danger_terms（≤200 / 非空 ≤64 / 去重）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"].update(
                {
                    "enabled": True,
                    "danger_terms": [],
                    "profanity_terms": [],
                    "privacy_terms": [],
                    "politics_terms": ["涉政词条甲", "涉政词条乙"],
                }
            ),
        )
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    def test_enabled_true_empty_lexicons_rejected(self):
        """§3 校验规则 v1.32：enabled=true 时四词表（danger/profanity/privacy/
        politics）至少其一非空（空词表开启=矛盾）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"].update(
                {
                    "enabled": True,
                    "danger_terms": [],
                    "profanity_terms": [],
                    "privacy_terms": [],
                    "politics_terms": [],
                }
            ),
        )
        with pytest.raises(AssetValidationError, match="至少其一非空"):
            validate_asset(SPEC_DATA_ID, payload)

    def test_enabled_true_privacy_only_lexicon_accepted(self):
        """v1.32 四表口径：仅 privacy_terms 非空也满足「至少其一非空」。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"].update(
                {
                    "enabled": True,
                    "danger_terms": [],
                    "profanity_terms": [],
                    "privacy_terms": ["人肉搜索"],
                    "politics_terms": [],
                }
            ),
        )
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    @pytest.mark.parametrize("value", ["false", "0", 1, None])
    def test_enabled_non_bool_rejected(self, value):
        """enabled 严格布尔：字符串/数字/null 形态一律拒收（kill switch 防
        `"false"` 字符串形态漂移——validator 拒收 + 运行面严格判定双防线之一）。"""
        payload = _mutated(
            SPEC_DATA_ID, lambda p, v=value: p["safety"].update({"enabled": v})
        )
        with pytest.raises(AssetValidationError, match="布尔"):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        "rule",
        [
            "(a+)+$",  # 经典嵌套量词（评审实测：29 字符 payload 单次 0.035s 指数增长）
            "^(([a-z])+.)+[A-Z]([a-z])+$",  # 量词分组嵌量词分组（评审实测 35 字符 0.637s）
            "(a|aa)+b",  # 量词分组内交替（前缀重叠族）
            "(ab?)+c",  # 量词分组内可选量词（每轮两歧义仍指数）
        ],
    )
    def test_grey_rules_catastrophic_form_rejected(self, rule):
        """ReDoS 兜底第一层：灾难性回溯形态（无限量词分组内嵌量词/交替）静态拒收
        ——运行面扫描在 async 节点内，恶意正则可挂死整个事件循环（评审 P1）。"""
        payload = _mutated(
            SPEC_DATA_ID, lambda p, r=rule: p["safety"]["grey_rules"].append(r)
        )
        with pytest.raises(AssetValidationError, match="灾难性回溯"):
            validate_asset(SPEC_DATA_ID, payload)

    def test_grey_rules_quantified_disjoint_group_accepted(self):
        """非灾难形态（有限量词/无嵌套交替的非量词分组）不误拒：`(https?|ftp)://`
        分组未被量词修饰、`自杀.{0,8}(方法|办法)` 分组无外层量词——均在合法面。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"]["grey_rules"].extend(
                ["(https?|ftp)://", "自杀.{0,8}(方法|办法|教程)"]
            ),
        )
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("classify_timeout_ms", 199),  # < 200（§3 契约下界）
            ("classify_timeout_ms", 10001),  # > 10000
            ("reply_timeout_ms", 199),  # < 200
            ("reply_timeout_ms", 10001),  # > 10000（契约上界收口——12000 旧实现误放行）
            ("reply_max_chars", 99),  # < 100（§3 契约下界）
            ("reply_max_chars", 1001),  # > 1000
        ],
    )
    def test_timeout_and_length_bounds_rejected(self, field, value):
        """§3 校验规则 v1.30：classify/reply_timeout_ms 200≤x≤10000、
        reply_max_chars 100≤x≤1000（越界拒收）。"""
        payload = _mutated(
            SPEC_DATA_ID, lambda p, f=field, v=value: p["safety"].update({f: v})
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("classify_timeout_ms", 1500),
            ("reply_timeout_ms", 2000),
            ("reply_max_chars", 300),
        ],
    )
    def test_timeout_and_length_defaults_legal(self, field, value):
        """§3 现行示例值在场且合法（B13 种子：classify 1500 / reply 2000 / chars 300
        ——nacos-config.md §8 v1.30「初值取 §1/§3 现行示例」）。"""
        assert BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"][field] == value
        payload = _mutated(SPEC_DATA_ID, lambda p, f=field, v=value: p["safety"].update({f: v}))
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    def test_mode_invalid_rejected(self):
        """mode 只收 lexicon|hybrid（缺省 hybrid）。"""
        payload = _mutated(SPEC_DATA_ID, lambda p: p["safety"].update({"mode": "ml"}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_safety_field_names_avoid_secret_sniff(self):
        """字段命名纪律：safety 节内不含 key/secret/token/password 字段名
        （防撞密钥嗅探 §2——B13 硬约束）。"""
        for field in BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]:
            assert not re.search(r"key|secret|token|password", field, re.IGNORECASE), field


class TestSafetyReplyPrefixRules:
    """§1 B13：common.safety_reply 三段行前缀硬校验（危险/辱骂/隐私；缺段拒收；键缺失不拒收）。"""

    @pytest.mark.parametrize("prefix", SAFETY_REPLY_LINE_PREFIXES)
    def test_safety_reply_missing_line_prefix_rejected(self, prefix):
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "common.safety_reply": p["prompts"]["common.safety_reply"].replace(
                        prefix, "- 其他段："
                    )
                }
            ),
        )
        with pytest.raises(AssetValidationError, match=prefix[2:]):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_safety_reply_key_missing_allowed(self):
        """键缺失不拒收（§1「Nacos 键缺失时回落默认」——内置默认自带三段）。"""
        payload = _mutated(
            PROMPTS_DATA_ID, lambda p: p["prompts"].pop("common.safety_reply")
        )
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload

    def test_defaults_safety_reply_carries_both_prefixes(self):
        """内置默认 safety_reply 三段齐备（L4 回落值自身合法）。"""
        text = BUILTIN_DEFAULTS[PROMPTS_DATA_ID]["prompts"]["common.safety_reply"]
        lines = text.splitlines()
        for prefix in SAFETY_REPLY_LINE_PREFIXES:
            assert any(line.startswith(prefix) for line in lines)


class TestSafetyTokenSoftCheck:
    """§1 B13：fast.rewrite / deep.planner 的 safety 字段 token 软校验——告警不拒收。"""

    def test_defaults_have_no_safety_warnings(self):
        """内置默认已带 token——零告警（否则每次校验刷 WARNING 噪音）。"""
        prompts = BUILTIN_DEFAULTS[PROMPTS_DATA_ID]["prompts"]
        assert safety_token_warnings(prompts) == []

    def test_safety_token_carries_five_values(self):
        """v1.32 五值枚举：token 精确形态含 privacy|politics（软校验锚点即契约面）。"""
        assert SAFETY_FIELD_TOKEN == (
            '"safety":"none|danger|profanity|privacy|politics"'
        )
        for key in ("fast.rewrite", "deep.planner"):
            assert SAFETY_FIELD_TOKEN in BUILTIN_DEFAULTS[PROMPTS_DATA_ID]["prompts"][key]

    def test_old_rewrite_without_safety_token_passes_with_warning(self):
        """旧提示词（无 safety token）→ 校验**通过**且产生警告（不拒收）。

        硬拒会把生产部署打回 L4 内置默认——运行态提示词被静默替换（提示词漂移陷阱），
        故沿 retry.connection 软校验先例只告警。
        """
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.rewrite": p["prompts"]["fast.rewrite"].replace(
                        SAFETY_FIELD_TOKEN, ""
                    )
                }
            ),
        )
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload
        warnings = safety_token_warnings(payload["prompts"])
        assert len(warnings) == 1
        assert "fast.rewrite" in warnings[0]
        assert "不拒收" in warnings[0]

    def test_planner_without_safety_token_passes_with_warning(self):
        """deep.planner 同构软校验：缺 token → 通过 + 一条告警。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "deep.planner": p["prompts"]["deep.planner"].replace(
                        SAFETY_FIELD_TOKEN, ""
                    )
                }
            ),
        )
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload
        warnings = safety_token_warnings(payload["prompts"])
        assert len(warnings) == 1
        assert "deep.planner" in warnings[0]

    def test_missing_keys_are_silent(self):
        """键缺失不告警（回落内置默认由既有键级告警通道覆盖，不重复计数）。"""
        assert safety_token_warnings({}) == []
        assert safety_token_warnings({"fast.rewrite": 123}) == []  # 非字符串形态不碰


class TestThinkingSpec:
    """§3 v1.32（B14 增量 C）：`fast.thinking` / `deep.thinking` —— 枚举 on|off，
    键缺失合法（pydantic 默认值承载代码缺省 fast=off / deep=on），非法值拒收。"""

    def test_defaults_carry_mode_thinking(self):
        fast = BUILTIN_DEFAULTS[SPEC_DATA_ID]["fast"]
        deep = BUILTIN_DEFAULTS[SPEC_DATA_ID]["deep"]
        assert fast["thinking"] == "off"  # 裁决值：快速模式不开思考
        assert deep["thinking"] == "on"  # 裁决值：深度模式开思考

    @pytest.mark.parametrize("mode, value", [("fast", "on"), ("fast", "off"),
                                             ("deep", "on"), ("deep", "off")])
    def test_valid_thinking_values_accepted(self, mode, value):
        payload = _mutated(SPEC_DATA_ID, lambda p: p[mode].update({"thinking": value}))
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    @pytest.mark.parametrize("mode, expected", [("fast", "off"), ("deep", "on")])
    def test_missing_thinking_key_legal_with_mode_default(self, mode, expected):
        """键缺失合法（旧 spec 可过校验）：默认值承载裁决值 fast=off / deep=on。"""
        from app.config_center.validator import DeepSpec, FastSpec

        payload = _mutated(SPEC_DATA_ID, lambda p: p[mode].pop("thinking"))
        assert validate_asset(SPEC_DATA_ID, payload) == payload
        model_cls = FastSpec if mode == "fast" else DeepSpec
        assert getattr(model_cls.model_validate(payload[mode]), "thinking") == expected

    @pytest.mark.parametrize("value", ["auto", "true", True, 1, ""])
    def test_invalid_thinking_value_rejected(self, value):
        payload = _mutated(SPEC_DATA_ID, lambda p: p["fast"].update({"thinking": value}))
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)


class TestPoliticsCategory:
    """§3 v1.32（B14 增量 B）：politics 类别——拒答话术第四段 + safety 枚举五值。"""

    def test_safety_reply_politics_prefix_is_fourth_segment(self):
        from app.config_center.validator import SAFETY_REPLY_CATEGORY_PREFIXES

        assert set(SAFETY_REPLY_CATEGORY_PREFIXES) == {
            "danger", "profanity", "privacy", "politics"
        }
        assert SAFETY_REPLY_CATEGORY_PREFIXES["politics"] == "- 涉政话题回复段："

    def test_safety_reply_missing_politics_segment_rejected(self):
        """四段任一缺失即拒收（拒答话术行级锚点缺失=该类别话术失锚）。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "common.safety_reply": p["prompts"]["common.safety_reply"].replace(
                        "- 涉政话题回复段：", "- 其它回复段："
                    )
                }
            ),
        )
        with pytest.raises(AssetValidationError, match="涉政话题回复段"):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_safety_field_token_is_five_values(self):
        """safety 字段枚举五值（none|danger|profanity|privacy|politics）——软校验 token
        与内置默认同源；planner/rewrite 双键均在场。"""
        assert SAFETY_FIELD_TOKEN == '"safety":"none|danger|profanity|privacy|politics"'
        for key in ("fast.rewrite", "deep.planner"):
            assert SAFETY_FIELD_TOKEN in BUILTIN_DEFAULTS[PROMPTS_DATA_ID]["prompts"][key]


class TestContractLexiconAlignment:
    """第⑩件（v1.30 遗留漂移收口）：代码内置词表初值逐表对齐契约 §3 示例值。

    契约侧为准（2026-11-18 用户裁决）——danger/profanity/grey_rules 三表与旧内置种子
    存在出入；本批三源（.txt/defaults.py/nacos_init.py）一并按契约示例值落位。
    """

    def test_danger_terms_match_contract_example(self):
        assert BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]["danger_terms"] == [
            "制造炸弹", "合成毒品", "生物武器", "制爆教程",
        ]

    def test_profanity_terms_match_contract_example(self):
        assert BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]["profanity_terms"] == [
            "傻逼", "滚蛋", "废物东西",
        ]

    def test_privacy_terms_match_contract_example(self):
        assert BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]["privacy_terms"] == [
            "人肉搜索", "开盒挂人", "查开房记录",
        ]

    def test_grey_rules_match_contract_example(self):
        assert BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]["grey_rules"] == [
            "how\\s+to\\s+(make|build)\\s+(a\\s+)?(bomb|explosive|weapon)",
            "(教|教我).{0,6}(制毒|制爆)",
            "kill\\s+(myself|oneself)\\s+how",
            "(查|帮我查).{0,8}(住址|身份证号|开房记录|定位|行踪|轨迹)",
            "(帮忙|帮我).{0,6}(找人|查人)",
        ]


class TestFastLlmTimeout:
    """§3 v1.32（B14 增量 A 裁决①）：fast.llm_timeout_s 30 → 60（全节点共享键）。"""

    def test_fast_llm_timeout_default_is_60(self):
        assert BUILTIN_DEFAULTS[SPEC_DATA_ID]["fast"]["llm_timeout_s"] == 60

    def test_deep_llm_timeout_stays_60(self):
        assert BUILTIN_DEFAULTS[SPEC_DATA_ID]["deep"]["llm_timeout_s"] == 60


# ==== 决策模型接入键组（v1.33 Decision Model 工单批次 2，ADR-016）====
#
# 钉住的语义（nacos-config.md §1/§5 v1.33）：
# - `model.mapping.decision` 缺省空串 = kill switch 关闭——**映射键唯一允许空串者**
#   （其余五键 min_length=1 惯例不变）；非空值不做别名白名单校验（灰度安全）；
# - prompts `fast.decision.*` 标量键组白名单定向扩容（home.suggest_pool 先例）——
#   questions_json=JSON 字符串（方言硬校验：type∈{noul,choice,score}/choice.criteria=
#   对象/score.criteria=数组，违规拒收——发上去必 400，坏配置不上线沿 ADR-005 语义）；
#   confidence_floor/timeout_ms/shadow 类型越界拒收该 dataId；
# - **灰度安全（硬约束）**：四键全部可选——旧 Nacos payload（无 decision 键组）+ 新
#   validator 启动零拒收（专项测试）；
# - 软校验：四约定题键齐备与 query_type other 兜底（告警不拒收；v1.33 原文口径——
#   strategy/safety_shadow 选项为契约原值不含 other）。


class TestDecisionMappingRules:
    """§5 v1.33：model.mapping.decision 键——空串例外 + 非空别名容忍。"""

    def test_defaults_decision_key_empty_kill_switch(self):
        assert BUILTIN_DEFAULTS[MODEL_DATA_ID]["decision"] == ""

    def test_empty_decision_accepted(self):
        """decision=""（缺省 kill switch）→ 通过（映射键唯一空串例外）。"""
        payload = _mutated(MODEL_DATA_ID, lambda p: p.update({"decision": ""}))
        validate_asset(MODEL_DATA_ID, payload)

    def test_legacy_mapping_without_decision_key_accepted(self):
        """旧 Nacos payload（无 decision 键）+ 新 validator → 零拒收（extra=allow +
        可选缺省——kill switch 缺省关闭，灰度安全专项）。"""
        payload = _mutated(MODEL_DATA_ID, lambda p: p.pop("decision", None))
        validate_asset(MODEL_DATA_ID, payload)  # 不抛即过

    def test_nonempty_decision_alias_accepted_without_whitelist(self):
        """非空别名不做白名单校验（别名注册面在 litellm config.yaml 网关静态层）——
        未知别名值容忍不拒收（调用时网关报错 → decision 失败兜底回 light）。"""
        payload = _mutated(MODEL_DATA_ID, lambda p: p.update({"decision": "decision-model"}))
        validate_asset(MODEL_DATA_ID, payload)
        payload = _mutated(MODEL_DATA_ID, lambda p: p.update({"decision": "unknown-alias"}))
        validate_asset(MODEL_DATA_ID, payload)

    def test_other_keys_empty_still_rejected(self):
        """五键 min_length=1 惯例不变（decision 是唯一空串例外）。"""
        payload = _mutated(MODEL_DATA_ID, lambda p: p.update({"memory": ""}))
        with pytest.raises(AssetValidationError):
            validate_asset(MODEL_DATA_ID, payload)


class TestDecisionPromptsRules:
    """§1 v1.33：fast.decision.* 标量键组——questions_json 方言硬校验 + 标量类型闸。"""

    def test_defaults_decision_keys_validate(self):
        """内置默认（含四键）整份通过（四源同步目标形态）。"""
        validate_asset(PROMPTS_DATA_ID, copy.deepcopy(BUILTIN_DEFAULTS[PROMPTS_DATA_ID]))

    def test_valid_questions_json_accepted(self):
        import json

        questions = {
            "query_type": {
                "type": "choice",
                "options": ["a", "other"],
                "criteria": {"a": "判据", "other": "兜底"},
            },
            "answerable": {"type": "noul", "instructions": "是否判据"},
        }
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {"fast.decision.questions_json": json.dumps(questions, ensure_ascii=False)}
            ),
        )
        validate_asset(PROMPTS_DATA_ID, payload)

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (
                lambda prompts: prompts.update({"fast.decision.questions_json": "not-json{{{"}),
                "非法 JSON",
            ),
            (
                lambda prompts: prompts.update(
                    {"fast.decision.questions_json": '{"q": {"type": "box", "criteria": {}}}'}
                ),
                "box 方言（上游 400 拒收）",
            ),
            (
                lambda prompts: prompts.update(
                    {
                        "fast.decision.questions_json": json.dumps(
                            {"q": {"type": "choice", "options": ["a"], "criteria": "字符串判据"}},
                            ensure_ascii=False,
                        )
                    }
                ),
                "choice criteria 须为对象",
            ),
            (
                lambda prompts: prompts.update(
                    {
                        "fast.decision.questions_json": json.dumps(
                            {"q": {"type": "choice", "options": ["a"]}},
                            ensure_ascii=False,
                        )
                    }
                ),
                "choice 缺 criteria",
            ),
            (
                lambda prompts: prompts.update(
                    {
                        "fast.decision.questions_json": json.dumps(
                            {"q": {"type": "score", "criteria": {"0": "锚"}}},
                            ensure_ascii=False,
                        )
                    }
                ),
                "score criteria 须为数组",
            ),
            (
                lambda prompts: prompts.update({"fast.decision.questions_json": '{"q": "平铺字符串"}'}),
                "题对象须为对象",
            ),
        ],
    )
    def test_questions_json_dialect_violations_rejected(self, mutate, reason):
        """方言硬校验违规 → 整份拒收（发上去必 400——坏配置不上线沿 ADR-005 语义）。"""
        payload = _mutated(PROMPTS_DATA_ID, lambda p: mutate(p["prompts"]))
        with pytest.raises(AssetValidationError) as exc_info:
            validate_asset(PROMPTS_DATA_ID, payload)
        assert "fast.decision.questions_json" in str(exc_info.value)

    def test_legacy_prompts_without_decision_keys_accepted(self):
        """旧 Nacos prompts payload（无 fast.decision.* 键）+ 新 validator → 零拒收
        （四键全部可选、键缺失回落代码内置默认=旧行为——灰度安全专项）。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].pop("fast.decision.questions_json", None)
            or p["prompts"].pop("fast.decision.confidence_floor", None),
        )
        payload["prompts"].pop("fast.decision.timeout_ms", None)
        payload["prompts"].pop("fast.decision.shadow", None)
        validate_asset(PROMPTS_DATA_ID, payload)  # 不抛即过

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda prompts: prompts.update({"fast.decision.confidence_floor": 1.5}),  # 越上界
            lambda prompts: prompts.update({"fast.decision.confidence_floor": 0}),  # 越下界
            lambda prompts: prompts.update({"fast.decision.confidence_floor": "0.5"}),  # 字符串
            lambda prompts: prompts.update({"fast.decision.timeout_ms": 50}),  # 低于下界
            lambda prompts: prompts.update({"fast.decision.timeout_ms": 20000}),  # 越上界
            lambda prompts: prompts.update({"fast.decision.timeout_ms": "1000"}),  # 字符串
            lambda prompts: prompts.update({"fast.decision.shadow": "false"}),  # 字符串形态
            lambda prompts: prompts.update({"fast.decision.shadow": 0}),  # 数字形态
        ],
    )
    def test_scalar_type_violations_rejected(self, mutate):
        """标量键类型/范围越界 → 拒收该 dataId（validator 白名单扩容的类型闸面）。"""
        payload = _mutated(PROMPTS_DATA_ID, lambda p: mutate(p["prompts"]))
        with pytest.raises(AssetValidationError):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_scalar_valid_values_accepted(self):
        """合法标量值（含边界 1 与 10000）→ 通过。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.decision.confidence_floor": 1,
                    "fast.decision.timeout_ms": 10000,
                    "fast.decision.shadow": True,
                }
            ),
        )
        validate_asset(PROMPTS_DATA_ID, payload)

    def test_decision_prompts_warnings_missing_question_keys(self):
        """软校验：questions_json 缺约定题键 → 告警不拒收（decision 侧该题保守缺省）。"""
        from app.config_center.validator import decision_prompts_warnings
        import json

        prompts = {
            "fast.decision.questions_json": json.dumps(
                {"query_type": {"type": "choice", "options": ["a", "other"], "criteria": {"a": "x"}}},
                ensure_ascii=False,
            )
        }
        warnings = decision_prompts_warnings(prompts)
        assert any("search_strategy" in warning for warning in warnings)
        assert any("answerable" in warning for warning in warnings)

    def test_decision_prompts_warnings_choice_without_other(self):
        """软校验：choice 缺 other 兜底选项 → 告警不拒收（百炼官方建议）。"""
        from app.config_center.validator import decision_prompts_warnings
        import json

        prompts = {
            "fast.decision.questions_json": json.dumps(
                {
                    "query_type": {
                        "type": "choice",
                        "options": ["a", "b"],
                        "criteria": {"a": "x", "b": "y"},
                    }
                },
                ensure_ascii=False,
            )
        }
        warnings = decision_prompts_warnings(prompts)
        assert any("other" in warning for warning in warnings)

    def test_decision_prompts_no_warning_for_valid_questions(self):
        """内置题面（四题键齐备 + other 兜底齐备）→ 零告警。"""
        from app.config_center.validator import decision_prompts_warnings

        prompts = BUILTIN_DEFAULTS[PROMPTS_DATA_ID]["prompts"]
        assert decision_prompts_warnings(prompts) == []


class TestContentGuardSpecRules:
    """§3 B15（v1.37）：safety.url_guard / safety.injection_filter 两子节校验面——
    嵌套模型挂 SafetySpec（缺失=None 合法）；正则共用 check_regex_rule（compile +
    ReDoS 灾难性回溯形态硬拒收，复用 grey_rules 逻辑）；范围越界拒收。"""

    def test_defaults_seed_content_guard_sections_conservative(self):
        """L4 种子自合法：url_guard 空黑名单 + max_url_length 2048；injection_filter
        种子（8~20 条）可编译非灾难形态 + on_hit=flag + max_scan_chars 16000。"""
        safety = BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]
        url_guard = safety["url_guard"]
        injection = safety["injection_filter"]
        assert url_guard["enabled"] is True
        assert url_guard["blocked_hosts"] == []
        assert url_guard["blocked_host_patterns"] == []
        assert url_guard["max_url_length"] == 2048
        assert injection["enabled"] is True
        assert 8 <= len(injection["patterns"]) <= 20
        for pattern in injection["patterns"]:
            re.compile(pattern)  # 不抛即合法
        assert injection["on_hit"] == "flag"
        assert injection["max_scan_chars"] == 16000

    def test_content_guard_sections_missing_allowed(self):
        """两子节缺失 = 合法（None；运行面 loader 回落 defaults 种子——部署即启用）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: (
                p["safety"].pop("url_guard"),
                p["safety"].pop("injection_filter"),
            ),
        )
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    def test_content_guard_full_sections_accepted(self):
        """两子节全键合法形态：黑名单/正则/on_hit=drop/范围边界值全部通过。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"].update(
                {
                    "url_guard": {
                        "enabled": True,
                        "blocked_hosts": ["evil.example", "badsite.cn"],
                        "blocked_host_patterns": [r"\.internal\.corp$", "^gw\\."],
                        "max_url_length": 8192,
                    },
                    "injection_filter": {
                        "enabled": True,
                        "patterns": [r"acme_payload_marker", r"(?i)evil\s+corp"],
                        "on_hit": "drop",
                        "max_scan_chars": 20000,
                    },
                }
            ),
        )
        assert validate_asset(SPEC_DATA_ID, payload) == payload

    @pytest.mark.parametrize("value", ["false", "0", 1, None])
    @pytest.mark.parametrize("section", ["url_guard", "injection_filter"])
    def test_subsection_enabled_non_bool_rejected(self, section, value):
        """子节 enabled 严格布尔（kill switch 防 `"false"` 形态漂移——SafetySpec
        enabled 同款双防线之一）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p, s=section, v=value: p["safety"][s].update({"enabled": v}),
        )
        with pytest.raises(AssetValidationError, match="布尔"):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize("value", ["kill", "reject", "FLAG", ""])
    def test_injection_on_hit_literal_rejected(self, value):
        """on_hit Literal["flag","drop"]：枚举外形态整版拒收。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p, v=value: p["safety"]["injection_filter"].update({"on_hit": v}),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        ("value", "legal"),
        [(32, False), (8193, False), (64, True), (8192, True), (2048, True)],
    )
    def test_max_url_length_bounds(self, value, legal):
        """max_url_length 范围 64~8192（含端点）：越界拒收、边界值合法。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p, v=value: p["safety"]["url_guard"].update({"max_url_length": v}),
        )
        if legal:
            assert validate_asset(SPEC_DATA_ID, payload) == payload
        else:
            with pytest.raises(AssetValidationError):
                validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        ("value", "legal"),
        [(999, False), (20001, False), (1000, True), (20000, True), (8000, True)],
    )
    def test_max_scan_chars_bounds(self, value, legal):
        """max_scan_chars 范围 1000~20000（含端点）：越界拒收、边界值合法。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p, v=value: p["safety"]["injection_filter"].update(
                {"max_scan_chars": v}
            ),
        )
        if legal:
            assert validate_asset(SPEC_DATA_ID, payload) == payload
        else:
            with pytest.raises(AssetValidationError):
                validate_asset(SPEC_DATA_ID, payload)

    def test_blocked_hosts_over_200_rejected(self):
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"]["url_guard"].update(
                {"blocked_hosts": ["h.example"] * 201}
            ),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_blocked_hosts_entry_over_253_chars_rejected(self):
        """单条 ≤253 字符（DNS 主机名上限）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"]["url_guard"]["blocked_hosts"].append("x" * 254),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_blocked_hosts_empty_entry_rejected(self):
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"]["url_guard"]["blocked_hosts"].append(""),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_blocked_host_patterns_bad_regex_rejected(self):
        """blocked_host_patterns 共用正则校验：编译失败拒收（消息带定位）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"]["url_guard"]["blocked_host_patterns"].append("("),
        )
        with pytest.raises(AssetValidationError, match="blocked_host_patterns"):
            validate_asset(SPEC_DATA_ID, payload)

    def test_blocked_host_patterns_catastrophic_rejected(self):
        """blocked_host_patterns ReDoS 灾难性回溯形态拒收（与 grey_rules 同判据）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"]["url_guard"]["blocked_host_patterns"].append(
                "(a|aa)+$"
            ),
        )
        with pytest.raises(AssetValidationError, match="灾难性回溯"):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize(
        "pattern",
        ["(", "(a+)+$", "x" * 121],
    )
    def test_injection_patterns_invalid_rejected(self, pattern):
        """injection_filter.patterns 共用正则校验：编译失败/ReDoS/超长（≤120）拒收。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p, r=pattern: p["safety"]["injection_filter"]["patterns"].append(r),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_injection_patterns_over_fifty_rejected(self):
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"]["injection_filter"].update(
                {"patterns": ["x"] * 51}
            ),
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_check_regex_rule_shared_helper_direct(self):
        """抽取的公共函数直测：长度/编译/ReDoS 三类消息齐备且带定位标签。"""
        from app.config_center.validator import check_regex_rule

        with pytest.raises(ValueError, match="≤60 字符"):
            check_regex_rule("x" * 61, label="grey_rules", max_len=60)
        with pytest.raises(ValueError, match="编译失败"):
            check_regex_rule("(", label="acme.label", max_len=120)
        with pytest.raises(ValueError, match="灾难性回溯"):
            check_regex_rule("(a+)+$", label="acme.label", max_len=120)
        check_regex_rule("(a|b)?c", label="acme.label", max_len=120)  # 合法不抛

    def test_content_guard_seed_entries_avoid_secret_prefix(self):
        """命名纪律（§2 嗅探联动）：种子条目零密钥前缀形态（sk-/tvly-/fc-/as_sk-）。"""
        patterns = BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]["injection_filter"][
            "patterns"
        ]
        for pattern in patterns:
            assert not re.match(r"^(sk-|tvly-|fc-|as_sk-)", pattern), pattern
        for field in BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]["url_guard"]:
            assert not re.search(r"key|secret|token|password", field, re.IGNORECASE), (
                field
            )
        for field in BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]["injection_filter"]:
            assert not re.search(r"key|secret|token|password", field, re.IGNORECASE), (
                field
            )
