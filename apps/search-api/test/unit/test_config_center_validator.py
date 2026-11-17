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
    """§3 v1.20 两条 prompts 校验（检索策略增量防热更漂移）。"""

    def test_rewrite_template_without_search_strategy_rejected(self):
        """fast.rewrite 输出 schema 串缺 search_strategy → 整版拒收（防漂移至旧契约）。"""
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
        with pytest.raises(AssetValidationError, match="search_strategy"):
            validate_asset(PROMPTS_DATA_ID, payload)

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
        """L4 内置默认含五键连接类节（生产 floor 与运行态同源）。"""
        node = BUILTIN_DEFAULTS[SPEC_DATA_ID]["fast"]["retry"]["connection"]
        assert node == {
            "max_attempts": 5,
            "initial_interval_s": 1.0,
            "backoff_factor": 2.0,
            "max_interval_s": 8.0,
            "total_window_s": 20.0,
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
        """软校验文案：键非法告警 + 联动约束告警（10s + total_window_s + 8s > turn_timeout_s）。"""
        from app.config_center.validator import connection_retry_warnings

        assert connection_retry_warnings(
            {"retry": {"connection": BUILTIN_DEFAULTS[SPEC_DATA_ID]["fast"]["retry"]["connection"]},
             "turn_timeout_s": 60}
        ) == []

        invalid = connection_retry_warnings({"retry": {"connection": {"max_attempts": 0}},
                                            "turn_timeout_s": 60})
        assert len(invalid) == 1 and "回落 retry 主节" in invalid[0]

        relaxed = connection_retry_warnings(
            {"retry": {"connection": {
                "max_attempts": 5, "initial_interval_s": 1.0, "backoff_factor": 2.0,
                "max_interval_s": 8.0, "total_window_s": 45.0,
            }}, "turn_timeout_s": 60}
        )
        assert len(relaxed) == 1 and "turn_timeout_s" in relaxed[0]

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

    def test_rewrite_template_enum_without_recall_rejected(self):
        """fast.rewrite 全文缺 recall 字样 → 整版拒收（沿 v1.20 防漂移先例；模拟漂移回
        v1.20 文本形态——schema 枚举串与判定正文同时回退）。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {"fast.rewrite": p["prompts"]["fast.rewrite"].replace("recall", "")}
            ),
        )
        with pytest.raises(AssetValidationError, match="recall"):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_rewrite_template_enum_without_navigational_rejected(self):
        """fast.rewrite 枚举串缺 navigational 字样 → 整版拒收。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.rewrite": p["prompts"]["fast.rewrite"].replace(
                        "how_to|navigational|other", "how_to|other"
                    )
                }
            ),
        )
        with pytest.raises(AssetValidationError, match="navigational"):
            validate_asset(PROMPTS_DATA_ID, payload)

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
    """评审修复轮（security P3②/③ + 评审 P3④）：枚举串精确形态 + history_block 占位符。"""

    def test_enum_recall_removed_but_prose_mentions_recall_still_rejected(self):
        """散文绕过探针：枚举串删 recall、正文仍多处提及 recall → 必须拒收。

        修复前 token 匹配全文子串（`"recall" in rewrite`）——散文提及即可蒙混，
        热更漂移回三值枚举时静默放过（recall 边永不触发）。
        """
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.rewrite": p["prompts"]["fast.rewrite"].replace(
                        '"search_strategy":"direct|verify|web|recall"',
                        '"search_strategy":"direct|verify|web"',
                        # 正文保留 recall 字样（④ 检索策略判定句 = 散文面）
                    )
                }
            ),
        )
        text = payload["prompts"]["fast.rewrite"]
        assert "recall" in text  # 前置：散文面确实还提到 recall
        with pytest.raises(AssetValidationError, match="recall"):
            validate_asset(PROMPTS_DATA_ID, payload)

    def test_enum_navigational_removed_but_prose_mentions_it_still_rejected(self):
        """散文绕过探针（题型侧）：枚举串删 navigational、正文仍提及 → 必须拒收。"""
        payload = _mutated(
            PROMPTS_DATA_ID,
            lambda p: p["prompts"].update(
                {
                    "fast.rewrite": p["prompts"]["fast.rewrite"].replace(
                        "how_to|navigational|other", "how_to|other"
                    )
                }
            ),
        )
        with pytest.raises(AssetValidationError, match="navigational"):
            validate_asset(PROMPTS_DATA_ID, payload)

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
        """L4 种子：enabled=False（契约灰度初值——nacos-config.md §3/§8 v1.30「初值
        enabled=false 灰度，A50 验收后用户裁决开启」）+ mode=hybrid + 三表齐备且保守
        （宁可漏不可误）；grey_rules 均可编译且非灾难性回溯形态。"""
        safety = BUILTIN_DEFAULTS[SPEC_DATA_ID]["safety"]
        assert safety["enabled"] is False
        assert safety["mode"] == "hybrid"
        assert 0 < len(safety["danger_terms"]) <= 200
        assert 0 < len(safety["profanity_terms"]) <= 200
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

    @pytest.mark.parametrize("field", ["danger_terms", "profanity_terms"])
    def test_term_lexicon_over_200_rejected(self, field):
        payload = _mutated(
            SPEC_DATA_ID, lambda p, f=field: p["safety"].update({f: ["词"] * 201})
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize("field", ["danger_terms", "profanity_terms"])
    def test_term_entry_empty_rejected(self, field):
        payload = _mutated(
            SPEC_DATA_ID, lambda p, f=field: p["safety"][f].append("")
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize("field", ["danger_terms", "profanity_terms"])
    def test_term_entry_over_64_chars_rejected(self, field):
        """§3 校验规则 v1.30：词表单条 ≤64 字符（越界拒收）。"""
        payload = _mutated(
            SPEC_DATA_ID, lambda p, f=field: p["safety"][f].append("x" * 65)
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    @pytest.mark.parametrize("field", ["danger_terms", "profanity_terms"])
    def test_term_entry_duplicated_rejected(self, field):
        """§3 校验规则 v1.30：词表条目去重（重复条目拒收）。"""
        payload = _mutated(
            SPEC_DATA_ID, lambda p, f=field: p["safety"][f].append(p["safety"][f][0])
        )
        with pytest.raises(AssetValidationError):
            validate_asset(SPEC_DATA_ID, payload)

    def test_enabled_true_empty_lexicons_rejected(self):
        """§3 校验规则 v1.30：enabled=true 时两词表至少其一非空（空词表开启=矛盾）。"""
        payload = _mutated(
            SPEC_DATA_ID,
            lambda p: p["safety"].update(
                {"enabled": True, "danger_terms": [], "profanity_terms": []}
            ),
        )
        with pytest.raises(AssetValidationError, match="至少其一非空"):
            validate_asset(SPEC_DATA_ID, payload)

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
    """§1 B13：common.safety_reply 两段行前缀硬校验（缺段拒收；键缺失不拒收）。"""

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
        """键缺失不拒收（§1「Nacos 键缺失时回落默认」——内置默认自带两段）。"""
        payload = _mutated(
            PROMPTS_DATA_ID, lambda p: p["prompts"].pop("common.safety_reply")
        )
        assert validate_asset(PROMPTS_DATA_ID, payload) == payload

    def test_defaults_safety_reply_carries_both_prefixes(self):
        """内置默认 safety_reply 两段齐备（L4 回落值自身合法）。"""
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
