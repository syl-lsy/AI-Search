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

import pytest

from app.config_center.defaults import BUILTIN_DEFAULTS
from app.config_center.validator import (
    AssetValidationError,
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
        """非开头子串（如 task-xxx）不误伤——契约正则为开头锚定。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update({"fast.rewrite": "前缀 task-123 后缀"}),
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
