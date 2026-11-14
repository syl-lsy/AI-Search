"""integration：检索策略判定探针（B9⑨，R7 同款验证法——20+ 条小模型三分判定）。

口径（ARCH §8 B9⑨ / nacos-config.md §1 v1.20「检索策略判定标准」）：
- 探针对象 = `rewrite` 节点的真实判定路径（light 模型 + 内置 v1.20 提示词文本——
  进程内 AssetCenter 未启动时 `resolve_prompt` 回落 `PROMPTS_CONFIG`/内置 .txt，
  即本批同步的判定正文；Nacos 发布 v1.20 后同一路径自动切到快照文本，代码零改）；
- 三桶样本：direct 正例（闲聊/创作/翻译/纯计算/自带材料）、verify 正例（静态通用
  知识）、web 正例（时效/多面/长尾事实），外加**误判反例**（事实断言混入寒暄、
  时效状态、长尾数值——一律不得判 direct）；
- 结论指标：合法率（结构化输出可解析率）+ 三桶准确率（direct/verify/web 各 ≥80%）+
  误判数（**非 direct 样本**——verify/web 正例与 counter 反例——一律不得判 direct，
  幻觉风险面，风险报告 §7.2）。断言阈值取 R7 口径（合法率 ≥90%）。

**执行纪律（默认不进门禁）**：本探针是判定质量的**抽样实验**，结论随模型/提示词版本
漂移，故默认 skip——`RUN_STRATEGY_PROBE=1 make test-integration` 或直接 `pytest -k
strategy_probe` 显式启用后执行，输出逐条判定表（`-rP` 可见）。确定性门禁（三值
解析/回落/对齐/路由）在 unit 层锁死，不依赖本探针。Nacos 尚未发布 v1.20 时若显式
启用，探针按内置 v1.20 文本判定并在报告首行标注实际消费的文本来源。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.requires_llm

#: 探针显式启用开关（默认零执行：模型判定质量属抽样实验，不进确定性门禁）
PROBE_ENV = "RUN_STRATEGY_PROBE"

#: 判定正文标记（缺失即说明消费的不是 v1.20 文本——探针结论不可用于 direct 上线判断）
POLICY_MARKER = "检索策略判定标准"


@dataclass(frozen=True)
class StrategyCase:
    """单条判定样本：query + 期望 strategy + 分桶（direct/verify/web/counter）。"""

    query: str
    expected: str
    case_class: str
    note: str = ""


#: 26 条判定矩阵（direct 7 / verify 6 / web 7 / 误判反例 6——覆盖 nacos §1 v1.20 判据清单）
CASES: tuple[StrategyCase, ...] = (
    # ---- direct 正例（零事实面清单） ----
    StrategyCase("你好，能做什么？", "direct", "direct", "寒暄+能力询问（契约判例）"),
    StrategyCase("谢谢你，帮了大忙！", "direct", "direct", "纯致谢"),
    StrategyCase("帮我写一首关于秋天的诗", "direct", "direct", "创作（契约判例）"),
    StrategyCase("把这句话翻译成英文：今天天气很好，我们去公园吧", "direct", "direct", "翻译（契约判例）"),
    StrategyCase("1234 乘以 5678 等于多少？", "direct", "direct", "纯计算（数据全在题内）"),
    StrategyCase("解释这段代码做了什么：for i in range(3): print(i)", "direct", "direct", "解释题目自带材料"),
    StrategyCase("用一句话总结我们刚才的对话", "direct", "direct", "对既有对话的确认/改写"),
    # ---- verify 正例（通用稳定知识，四排除项全不命中） ----
    StrategyCase("什么是 REST API？", "verify", "verify", "定义（契约判例）"),
    StrategyCase("FastAPI 怎么定义一个路由？", "verify", "verify", "成熟文档知识（契约判例）"),
    StrategyCase("TCP 三次握手的过程是什么？", "verify", "verify", "公认协议原理"),
    StrategyCase("Python 的列表推导式是什么？", "verify", "verify", "语言基础概念"),
    StrategyCase("什么是哈希表？它的查找复杂度是多少？", "verify", "verify", "数据结构原理"),
    StrategyCase("Git 的 rebase 和 merge 分别是什么操作？", "verify", "verify", "解释型静态知识"),
    # ---- web 正例（时效敏感/长尾/多源/个人上下文） ----
    StrategyCase("LangGraph 最新版本号是多少？", "web", "web", "时效敏感（版本号）"),
    StrategyCase("今天的 A 股大盘收盘多少点？", "web", "web", "时效敏感（行情）"),
    StrategyCase("pgvector 和 Milvus 在个人知识库场景怎么选？", "web", "web", "需多源佐证的对比结论"),
    StrategyCase("Rust 有哪些主流 Web 框架？", "web", "web", "长尾清单需取证"),
    StrategyCase("qwen3.5-4b 的上下文窗口多大？", "web", "web", "小众精确数值（契约 false 判例）"),
    StrategyCase("2026 年国内有哪些新的生成式 AI 管理办法？", "web", "web", "时效+法规事实"),
    StrategyCase("我们公司内部知识库的检索延迟怎么优化？", "web", "web", "依赖个人/组织上下文"),
    # ---- 误判反例（含事实断言 → 一律不得判 direct） ----
    StrategyCase("你好，顺便问一下 OpenAI 现任 CEO 是谁？", "web", "counter", "寒暄包裹事实断言（direct 陷阱）"),
    StrategyCase("帮我写一段介绍，说明 LangGraph 1.1 版本新增了哪些能力", "web", "counter", "创作包裹版本事实"),
    StrategyCase("谢谢你！另外今天人民币兑美元汇率是多少？", "web", "counter", "致谢包裹时效数据"),
    StrategyCase("把这段话翻译成英文：最近发布的 DeepSeek 模型有多少参数？", "web", "counter", "翻译包裹长尾数值"),
    StrategyCase("鲁迅是哪一年去世的？", "verify", "counter", "单一历史事实（verify 边缘：不得 direct）"),
    StrategyCase("FastAPI 最新版对 CORS 配置有什么变化？", "web", "counter", "静态知识包裹时效变化"),
)


def _light_alias_ready() -> str | None:
    """网关/light 别名探活；返回不可达原因（None=就绪）。不打印任何凭据。"""
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
    if "qwen3.5-4b-ollama" not in ids:
        return "light 别名未装载（qwen3.5-4b-ollama 不在网关模型清单）"
    return None


def _probe_skip_reason() -> str | None:
    """探针跳过原因：显式启用开关 + 网关就绪 + 判定正文含 v1.20 策略段。"""
    if os.environ.get(PROBE_ENV) != "1":
        return f"抽样探针默认不执行（{PROBE_ENV}=1 显式启用）；确定性门禁在 unit 层"
    return _light_alias_ready()


def test_probe_matrix_shape() -> None:
    """矩阵自检（离线、恒执行）：≥20 条、三桶各 ≥5、误判反例 ≥3，判例不重复。"""
    assert len(CASES) >= 20
    classes = [case.case_class for case in CASES]
    for name in ("direct", "verify", "web"):
        assert classes.count(name) >= 5, f"{name} 正例不足"
    assert classes.count("counter") >= 3
    assert len({case.query for case in CASES}) == len(CASES)
    assert {case.expected for case in CASES} <= {"direct", "verify", "web"}


def test_probe_policy_text_present_in_judgment_prompt() -> None:
    """判定正文含 v1.20 策略段（探针所测文本与契约同步；缺则探针结论无效）。"""
    from app.agent.prompts import resolve_prompt

    assert POLICY_MARKER in resolve_prompt("common.type_rules")


def _prompt_text_source() -> str:
    """标注本探针**实际消费**的判定正文来源（Nacos 快照 / 内置默认）。

    模块 docstring 承诺：报告首行标注文本来源（探针结论只在消费 v1.20 正文时有效）。
    解析路径与 `resolve_prompt` 一致（AssetCenter 内存快照 → 键缺失回落内置默认），
    此处只复刻**判定来源**、不重复取文本；不打印任何凭据（版本号可打印）。
    """
    from app.agent.prompts import PROMPTS_DATA_ID
    from app.config_center import get_asset_center

    try:
        snapshot = get_asset_center().snapshot(PROMPTS_DATA_ID)
    except Exception as exc:  # noqa: BLE001 —— 资产中心未启动=内置默认（预期运行形态）
        return f"内置默认（agent/prompts/common/type_rules.txt；资产中心未启动：{type(exc).__name__}）"
    prompts = snapshot.payload.get("prompts") if snapshot is not None else None
    value = prompts.get("common.type_rules") if isinstance(prompts, Mapping) else None
    if isinstance(value, str) and value:
        return f"Nacos 快照 ai_search.agent.prompts.json（version={snapshot.version}）"
    return "内置默认（agent/prompts/common/type_rules.txt；快照无该键）"


async def test_search_strategy_probe_real_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真模型三分判定探针：合法率 ≥90% + 三桶准确率 ≥80% + 非 direct 样本不得判 direct。

    节点直调（非图内运行）：`emit_stage` 依赖 runnable context（get_stream_writer），
    探针目标只是判定质量——stage 信号面在 unit 层锁死，故此处以 no-op 替换发射器。
    """
    skip_reason = _probe_skip_reason()
    if skip_reason:
        pytest.skip(skip_reason)

    from app.agent.nodes import fast_rewrite as rewrite_module
    from app.agent.nodes.fast_rewrite import rewrite
    from app.config_center.defaults import SPEC_CONFIG

    monkeypatch.setattr(rewrite_module, "emit_stage", lambda *a, **kw: None)

    config = {
        "configurable": {
            "thread_id": "probe-th",
            "turn_id": "probe-turn",
            "model_mapping": {"light": "qwen3.5-4b-ollama"},
        }
    }
    rows: list[tuple[StrategyCase, str | None, str]] = []
    parse_failures = 0
    for case in CASES:
        state: dict[str, Any] = {
            "query": case.query,
            "query_lang": "zh",
            "history": [],
            "spec": SPEC_CONFIG,
        }
        try:
            update = await rewrite(state, config)  # type: ignore[arg-type]
            predicted = str(update.get("search_strategy"))
            reasons = [item.get("reason") for item in update.get("errors") or []]
            status = "fallback" if reasons else "parsed"
            if status == "fallback":
                parse_failures += 1
        except Exception as exc:  # noqa: BLE001 —— 探针记录失败不炸矩阵
            predicted, status = None, f"error:{type(exc).__name__}"
            parse_failures += 1
        rows.append((case, predicted, status))

    legal_rate = 1 - parse_failures / len(CASES)
    # 三桶准确率（正例桶：direct/verify/web；counter 为误判反例、另设门）
    bucket_hits: dict[str, int] = {name: 0 for name in ("direct", "verify", "web")}
    bucket_sizes: dict[str, int] = {name: 0 for name in ("direct", "verify", "web")}
    for case, predicted, _ in rows:
        if case.case_class in bucket_hits:
            bucket_sizes[case.case_class] += 1
            if predicted == case.expected:
                bucket_hits[case.case_class] += 1
    bucket_rates = {
        name: bucket_hits[name] / bucket_sizes[name] for name in bucket_hits
    }
    report = [
        "",
        f"[strategy-probe] 文本来源={_prompt_text_source()}",  # 首行：实际消费正文来源
        f"[strategy-probe] 合法率={legal_rate:.1%}（阈值 ≥90%） "
        + " ".join(
            f"{name} 准确率={bucket_rates[name]:.1%}({bucket_hits[name]}/{bucket_sizes[name]}，阈值 ≥80%)"
            for name in ("direct", "verify", "web")
        ),
    ]
    for case, predicted, status in rows:
        flag = "OK " if predicted == case.expected else "MISS"
        report.append(
            f"{flag} [{case.case_class:7s}] 期望={case.expected:6s} 实得={predicted!s:6s} "
            f"({status}) {case.query} — {case.note}"
        )
    print("\n".join(report))  # -rP 可见（make test-integration 口径）

    assert legal_rate >= 0.90, f"结构化输出合法率不足：{legal_rate:.1%} < 90%"
    for name, rate in bucket_rates.items():
        assert rate >= 0.80, (
            f"{name} 桶准确率不足：{rate:.1%} < 80%"
            f"（{bucket_hits[name]}/{bucket_sizes[name]}）"
        )
    # 误判门收窄（B9 测试审计 #2）：扫描范围为准 direct 的**全部非 direct 样本**——
    # verify/web 正例被判 direct 与 counter 反例同属幻觉风险面（原先只扫 counter，
    # verify/web 正例被吞成 direct 时静默通过）。
    false_direct = [
        f"[{case.case_class}] {case.query}"
        for case, predicted, _ in rows
        if case.case_class in ("verify", "web", "counter") and predicted == "direct"
    ]
    assert not false_direct, (
        f"非 direct 样本被误判为 direct（幻觉风险：零检索直答无来源可核）：{false_direct}"
    )
