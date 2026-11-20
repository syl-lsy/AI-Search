"""research 节点（M2 批次 2——ARCH §4.2 research 行 / DEV §5.4 五步闭环）。

deepagents 首次接入（ADR-012 子图内第三节点）：把 planner 产出的子课题转成
SubAgent spec，经 `create_deep_agent` 装配主协调器 + N 个研究员子代理；主协调器
（deepagents harness）经 task 工具派发子课题，子代理各自 search→选 URL→读原文→
结构化摘要回传，门控捕获回传后汇总进 DeepState。

**装配形态（OQ-33 探针钉正，scripts/probe_oq33_subagent_tools.py 实证）**：
- SubAgent spec 原生 `tools` 收窄（spec["tools"] 出现即覆盖「继承主 agent」缺省；
  0.7.32 无 custom_tools 字段）——契约三工具 `DEEP_TOOL_NAMES`
  （web_search/firecrawl_read/save_snapshot）；
- **两处默认泄漏面显式防**：
  ① SubAgent[middleware] 自带 `FilesystemMiddleware(tools=READ_ONLY_FS_TOOLS)`
  （[ls,read_file,glob,grep]，read_file 必含否则 ValueError）按 .name 原位替换默认栈
  （默认栈无条件前置全量文件中间件=写工具泄漏）；本节点经 `create_sub_agent`（deepagents
  公共编译器，不前置默认 FS 栈）编译子代理，中间件面即我方只读白名单；
  ② 自带同名 `general-purpose` spec 顶替 create_deep_agent 自动追加的 GP（默认 GP
  继承主 agent 全量工具面）——GP 顶替体只读文件工具、零检索工具；
  卸载取回只读形态探针已实证功能性完好（eviction 走 backend.write，与 write_file
  暴露无关）；
- 主 agent 自身工具面同步收敛：create_deep_agent(middleware=[只读 FS])——主面 =
  task + 只读文件工具，无写工具、无契约三工具（协调器不亲自检索）。

**模型（OQ-34，2026-11-16 用户裁决定档 Standard；裁决留痕=
docs/m2-batch2-delivery-20261116.md §裁决记录）**：research 子代理主循环与
摘要统一 Standard 档（chat-strong 经 `build_chat_model` / `resolve_chat_alias`，
OQ-11 轮内映射快照同源；探针数据=light 三轮零工具调用直答不胜任研究员，
`docs/m2-batch2-probe-oq33-oq34.md` §2）。契约侧已随批修订（2026-11-19 收官落档：
nacos-config v1.35 §5 light 行 / ARCHITECTURE v1.63 S14 行与 §6.7 / ADR-008 v1.63
增注——原「deep 子代理摘要固定走 light」注记废止），实现与契约一致。
主协调器与子代理同 alias。

**回传面 SummarySchema（response_format）**：SubAgent spec 原生 `response_format` 键
经 `create_sub_agent` 转发 create_agent——`structured_response` 落编译子代理终态，
父面 ToolMessage 自动 JSON 序列化；schema 形态=ARCH §4.2 subagent_reports 模板六字段
{subtopic, summary, hard_facts[], quotes[], sources[], failed}。**生效形态（机制增量
实测结论，2026-11-19 真 LLM 探针钉正：chat-standard 单子课题轮，读链假体全通形态）**：
`SummarySchema` 裸类型 = langchain **AutoStrategy 原生路径命中**（structured_response
直取 1/1，围栏剥离兜底 0 触发、failed 0；报告 909 字符≤summary_max_tokens、hard_facts
7 条、sources 全部来自已读 URL）——生效形态钉正为**裸类型 response_format**；门控侧
保留**围栏剥离解析**为模型漂移回退层（structured_response 缺失/校验失败时，剥离
```json 围栏后取末条 AIMessage JSON 子串重试——OQ-34 注②「markdown 围栏/散文包裹可
机械修复」形态）；两机制皆不达 → failed:true 报告（不终止、不崩溃）。

**约束与语义**：
- `max_subagents` 信号量并发：asyncio.Semaphore(spec.deep.max_subagents) 在门控内
  界定并行 task 派发的子代理运行数（主协调器可单帧并行多 task 调用，信号量兜住峰值）；
- `read_min/read_max` 引导提示：节点侧配额块追加进子代理系统提示词（契约占位符集
  {subtopic, summary_max_tokens} 零变更；批次 2 修复轮强化为硬性读取要求——live
  发现 A「真模型零 firecrawl_read」处置，M4 评测探针挂账）；
- `visited_urls/searched_queries` 去重回写：记录器全局去重（URL/查询首现登记），
  节点收口回写全量并集（含既有 state 值——普通列表覆盖语义）；
- 单子代理失败 `failed:true` 不阻断其余：门控捕获子代理异常 → 合成 failed 报告 +
  errors 留痕，task 工具正常返回（主循环存活，A2 情形 2）；
- 主循环失败：errors 留痕 + 已捕获报告保留、**不置 degraded**（2026-11-13 B 裁决
  fast_rewrite 同款——兜底不属 api.md §7.2 degraded_reason 枚举任一语义）；
- 搜索链未注入/为空 → SearchChainUnavailable（TurnRunner 500201 族收轮，fast 同语义）；
  读取链空（主备全断）→ 「跳过读原文」引导语义（nacos-config §2 v1.8，非搜索链 500201）。

**事件与快照链（api.md §7.4 深度主干子集）**：
- `stage(search,running,extra.subagents[])`：入口发射（每子任务 {subtopic, status}——
  §7.2 钉死键面，批次 2 修复轮移除未钉 name 键），子任务状态推进经同 key 重复发射
  （§7.2「同 key 多次出现合法」；终态按报告 failed 派生 failed|done——§7.2 枚举
  `running|done|failed` 的 failed 事件面可达，批次 2 修复轮钉死）；
- `stage(read,running,label=阅读第 N 篇)`：firecrawl_read 每次读取发射；节点收口
  `stage(read,done,progress={current,total}=总读取篇数)`；
- sources 批次 `emit_sources`：web_search 工具每次返回新候选即发——**ref_no 首现
  冻结、批次只增不改**（重叠 URL 只入首现批次；snapshot_available 按发射时点已知态，
  快照后到的完成态以 REST §2.5 终态为准——批次 1 交付说明钉死口径）；
- 快照链（读后回写）：firecrawl_read 读原文成功即 `save_snapshot`（对象全量入 MinIO
  + snapshots 表登记，url_hash 幂等）——OQ-33 探针 §3 建议 2「节点侧登记（读后回写）
  而非依赖模型自发调用」（两档模型均未主动发起快照，探针实证）；save_snapshot 仍按
  契约三工具面暴露（幂等复用）。快照失败非阻断（errors 留痕继续研究）。

**R14③（子代理 LLM 帧零泄漏）**：子代理嵌套帧 namespace 多段，批次 1 messages 过滤
（`_is_deep_writer_messages` 只放行 deep 顶层 writer）已拦——本节点真实帧形态验证归
test_r14_cross_subgraph 深基线修订与 integration。

**取消传播**：节点入口 `ensure_not_canceled`；子任务边界（门控内，信号量 acquire 后）
pop 进程级取消标志命中即 `TurnCanceledSignal` 上抛——经 task 工具穿透出节点（langgraph
ToolNode 默认错误处理器对未知异常**原样重抛**，sandbox 实测形态；TurnCanceledSignal
不做子代理故障转换）；运行中取消走 asyncio cancellation（TurnRunner aclose→
GeneratorExit→AsyncPregelLoop.__aexit__ 取消 in-flight 节点协程，VR-2 机制），
CancelledError 穿透 task 工具→子代理挂起调用（deepagents 子任务响应取消实测）。
图内只感知、不落库，canceled 收尾归 TurnRunner（ADR-010）。

纯函数边界：节点无 DB/EventBus 直连——registry/链经 TurnRunner configurable 注入
（`tools_registry`/`search_chain`/`read_chain`，与 fast web_search 同注入面；read_chain
为 M2 批次 2 增量注入），快照持久化经 save_snapshot 工具自身 IO 面（同 ARCH §6.3
把表登记显式分配给该工具）；提示词经 AssetCenter 内存快照解析（未启动回落内置默认）；
LLM 工厂可注入可 mock——单测全 mock 零网络。
"""

import asyncio
import json
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FsToolName, FilesystemMiddleware
from deepagents.middleware.subagents import CompiledSubAgent, SubAgent, create_sub_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import tool
from pydantic import BaseModel, Field, ValidationError

from app.agent.external_content import external_content
from app.agent.graphs.signals import emit_sources, emit_stage
from app.agent.guards import TurnCanceledSignal, ensure_not_canceled
from app.agent.llm import build_chat_model, resolve_chat_alias
from app.agent.nodes.web_search import WEB_SEARCH_TOP_K
from app.agent.prompts import render_prompt, resolve_prompt
from app.agent.safety.config import ContentGuardConfig, load_content_guard_config
from app.agent.safety.content_guard import (
    filter_candidates,
    filter_report,
    scan_text_async,
    url_host,
    validate_url_async,
)
from app.agent.tools.registry import (
    ReadChain,
    SearchChain,
    SearchChainUnavailable,
    ToolRegistry,
)
from app.agent.tools.save_snapshot import SnapshotError, save_snapshot
from app.agent.tools.url_reader import (
    DEFAULT_URL_READ_HEAD_TOKENS,
    _neutralize_content,
    estimate_tokens,
    token_window,
)
from app.core.logger import AgentLog

__all__ = [
    "GP_SUBAGENT_NAME",
    "READ_ONLY_FS_TOOLS",
    "DEFAULT_SUMMARY_MAX_TOKENS",
    "SummarySchema",
    "research",
    "subagent_spec",
]

#: 只读文件工具白名单（OQ-33 装配形态钉正；read_file 必含否则 FilesystemMiddleware
#: ValueError——卸载取回回路经 backend.write 与 write_file 暴露无关）
READ_ONLY_FS_TOOLS: tuple[FsToolName, ...] = ("ls", "read_file", "glob", "grep")

#: GP 顶替名（create_deep_agent 自动追加 general-purpose 的同名顶替——泄漏面②防）
GP_SUBAGENT_NAME = "general-purpose"


def _readonly_fs_middleware() -> list[AgentMiddleware]:
    """只读文件中间件（泄漏面①防——SubAgent[middleware]/主面共用装配助手）。

    cast 注（上游泛型不变性误报）：FilesystemMiddleware 声明 StateT=FilesystemState，
    AgentMiddleware 声明 StateT=AgentState——deepagents SubAgent["middleware"] 类型面
    用后者，运行时前者为其子类（OQ-33 探针实证形态，0.7.32 原生装配即此组合）。
    """
    return [
        cast(
            AgentMiddleware,
            FilesystemMiddleware(backend=StateBackend(), tools=list(READ_ONLY_FS_TOOLS)),
        )
    ]

#: spec.deep.summary_max_tokens 缺失/非法回落（defaults.py SPEC_CONFIG 同源）
DEFAULT_SUMMARY_MAX_TOKENS = 1200

#: spec.deep.max_subagents 缺失/非法回落（deep_planner.DEFAULT_MAX_SUBAGENTS 同源）
DEFAULT_MAX_SUBAGENTS = 3

#: spec.deep.recursion_limit 缺失/非法回落（defaults.py SPEC_CONFIG deep 节同源）
DEFAULT_RECURSION_LIMIT = 25


class SummarySchema(BaseModel):
    """子代理结构化回传契约（ARCH §4.2 subagent_reports 模板；response_format 面）。"""

    subtopic: str
    summary: str
    hard_facts: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    failed: bool = False


# ---- 节点侧提示词块（契约占位符集零变更；引导文本为代码模板——reflect 先例同款）----

#: read_min/read_max 配额引导（追加进子代理系统提示词）——批次 2 修复轮强化为
#: **硬性读取要求**（live 验证发现 A：真模型两轮零 firecrawl_read、read_min 引导未
#: 生效——原「配额与纪律」措辞被真模型当作软建议忽略）。点名工具名 + 回传禁止 +
#: 读链空逃生句（与「双断跳过读原文」引导语义不冲突）；占位符集 {read_min, read_max}
#: 零变更。M4 评测探针挂账（read 触达率归评测线观察）。
_QUOTA_GUIDANCE = (
    "\n\n读取要求（节点侧追加，硬性）：回传摘要前你必须先调用 web_search 检索，"
    "并实际调用 firecrawl_read 读取至少 {read_min} 篇不同 URL 的原文——实际读取不足"
    " {read_min} 篇时不得回传摘要（仅凭搜索结果摘要作答不合格，不得以「已搜索」替代"
    "「已阅读」）。读满 {read_max} 篇即停止阅读并沉淀摘要回传；择优不凑数：与子课题"
    "无关的页面不读、读取失败的页面跳过并另选来源。唯一例外：若系统注提示读取链"
    "不可用，则按系统注跳过读取，基于搜索结果回传（不受本配额约束）。"
)

#: 主协调器系统提示词（代码模板，deepagents harness 专属——无契约键）
_ORCHESTRATOR_TEMPLATE = (
    "你是深度研究协调器。你的唯一职责：把下列子课题逐一派发给对应的研究员子代理"
    "（task 工具，subagent_type 用对应名称，description 用子课题原文），等待全部回传后"
    "用一句话汇总即可。不要自己检索或阅读网页；不要派发同一子课题两次；"
    "子代理失败不要重试（失败以 failed=true 回传，隔离处理）。\n子课题清单：\n{lines}"
)

#: 搜索链空引导（该轮 search 失败语义由节点入口 500201 族收轮；工具面防御性文案）
_SEARCH_EMPTY_GUIDANCE = "（系统注：搜索链当前不可用，请基于已有信息直接沉淀摘要回传。）"

#: 读取链空引导（nacos-config §2 v1.8「双断跳过读原文」语义）
_READ_EMPTY_GUIDANCE = (
    "（系统注：读取链不可用——跳过读原文，仅基于搜索结果摘要继续研究并回传。）"
)

#: URL 协议白名单（api.md §7.2 安全面，fast fuse_rerank._url_allowed 同判据）
_URL_PROTOCOLS = ("http", "https")


# ---- 依赖容器与记录器 ----


@dataclass
class _Deps:
    """契约工具依赖容器（registry/链/标识/recorder 注入面——节点纯函数边界）。

    spec 为轮内 Agent Spec 快照全节（head 窗口读 spec.deep.compact.url_read_head_tokens，
    deep_planner/llm_timeout 同源 OQ-11）；time_range 为 planner 顺带产出（透传
    Tavily，ARCH §4.2）。
    """

    registry: ToolRegistry | None  # 测试面仅注链形态合法（registry 缺席→工具面引导文本）
    search_chain: SearchChain
    read_chain: ReadChain | None
    spec: Mapping[str, Any]
    recorder: "_Recorder"
    user_id: str
    thread_id: str
    turn_id: str
    time_range: str | None = None
    #: B15 内容侧护栏（research() 装载一次：load_content_guard_config(spec)——缺子节
    #: 回落 defaults 种子=部署即启用；None=直构测试面护栏关闭）
    guards: ContentGuardConfig | None = None


class _Recorder:
    """research 段记录器：ref_no 首现冻结 / 去重集 / 子任务状态 / 报告与错误汇拢。

    事件发射经 signals helper（模块级 emit_stage/emit_sources——单测 monkeypatch 面）；
    sources 批次「只增不改」：重叠 URL 只入首现批次，snapshot_available 按发射时点
    已知态（快照后到以 REST §2.5 终态为准——批次 1 交付说明钉死口径）。
    """

    def __init__(self, subtopics: list[str]) -> None:
        self.reports: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.ref_by_url: dict[str, int] = {}
        self.candidates: list[dict[str, Any]] = []
        self.visited: dict[str, None] = {}  # 有序去重集（dict 保插入序）
        self.searched: dict[str, None] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}  # url → {url, object_key, size}
        self.reads = 0
        self.status: dict[str, str] = {}
        self.subtopic_by_name: dict[str, str] = {
            f"research-{i}": subtopic for i, subtopic in enumerate(subtopics)
        }
        for name in self.subtopic_by_name:
            self.status[name] = "pending"

    # ---- 子任务状态（stage extra 推进）----

    def set_status(self, name: str, status: str) -> None:
        self.status[name] = status

    def subagents_extra(self) -> list[dict[str, Any]]:
        """stage extra.subagents[]（api.md §7.2 钉死键面 {subtopic, status}——name 为
        未钉键，批次 2 修复轮移除，契约对齐）。"""
        return [
            {"subtopic": self.subtopic_by_name[name], "status": self.status[name]}
            for name in self.subtopic_by_name
        ]

    def emit_search_progress(self) -> None:
        emit_stage("search", "running", extra={"subagents": self.subagents_extra()})

    # ---- 搜索 / 来源 ----

    def record_search(self, query: str) -> None:
        self.searched.setdefault(query, None)

    def add_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """新 URL 候选入册并发射 sources 批次（ref_no 首现冻结；重叠 URL 跳过）。"""
        batch: list[dict[str, Any]] = []
        for candidate in candidates:
            url = candidate.get("url")
            if not isinstance(url, str) or not url or url in self.ref_by_url:
                continue
            self.ref_by_url[url] = len(self.ref_by_url) + 1
            # M2 批次 3 增量：state 侧候选附 ref_no（增量键）——ref_no 冻结账本入 state，
            # supplement（deep_supplement._next_ref_no）与 finalize（批次 4）跨节点续编
            # 消费；批次 payload 形态零变更（下方 batch 仍 9 键）
            self.candidates.append({**candidate, "ref_no": self.ref_by_url[url]})
            batch.append(
                {
                    "source_id": str(uuid_module.uuid4()),  # 占位——TurnRunner 以行 id 回写
                    "ref_no": self.ref_by_url[url],
                    "source_type": "web",
                    "title": str(candidate.get("title") or url),
                    "url": url,
                    "snippet": str(candidate.get("snippet") or ""),
                    "score": float(candidate["score"]) if isinstance(candidate.get("score"), (int, float)) else 0.0,
                    "snapshot_available": url in self.snapshots,
                    "kb_doc_id": None,
                }
            )
        if batch:
            emit_sources(batch=batch, total=len(self.ref_by_url))
        return batch

    # ---- 读取 / 快照 ----

    def begin_read(self) -> int:
        self.reads += 1
        emit_stage("read", "running", label=f"阅读第 {self.reads} 篇")
        return self.reads

    def record_visited(self, url: str) -> None:
        self.visited.setdefault(url, None)

    def record_snapshot(self, url: str, meta: Mapping[str, Any]) -> None:
        self.snapshots[url] = {
            "url": url,
            "object_key": str(meta.get("object_key") or ""),
            "size": int(meta.get("size_bytes") or 0),
            # M2 批次 4（批次 2 注⑦闭合）：snapshot_id 入账——state.snapshots 四键
            # 形态，TurnRunner 终态回填 sources.snapshot_id（backfill_source_snapshot_ids）
            # 消费；save_snapshot._meta 返回面已有该键（save_snapshot.py:110）
            "snapshot_id": str(meta.get("snapshot_id") or ""),
        }

    def record_error(self, error: dict[str, Any]) -> None:
        self.errors.append(error)

    # ---- 报告 ----

    def complete(self, name: str, report: dict[str, Any]) -> None:
        """报告收口 + 子任务终态推进（api.md §7.2 枚举 running|done|failed）。

        终态按报告 failed 派生（批次 2 修复轮：曾无条件置 done——failed:true 报告
        （异常隔离合成/不可解析/模型自declared 失败）在 SSE extra.subagents 上被
        覆写为 done，failed 值在事件面不可达）；done 幂等推进语义不变。
        """
        self.reports.append(report)
        self.set_status(name, "failed" if report.get("failed") else "done")
        self.emit_search_progress()


# ---- 契约工具（deepagents 暴露面）----


async def _url_guard_rejection_reason(url: str, deps: _Deps) -> str | None:
    """URL 护栏校验（B15）：None=放行；拒收返回 reason 枚举词并落 safety 域告警
    （只记 reason/host——零完整 URL）。guard 关/None → 放行零成本。"""
    guards = deps.guards
    if guards is None or guards.url_guard is None:
        return None
    result = await validate_url_async(url, guards.url_guard)
    if result.ok:
        return None
    AgentLog(
        message=(
            f"url_guard 拒收（reason={result.reason}，host={url_host(url)}）——"
            "零物理调用零熔断计数"
        ),
        graph="deep",
        node="research",
    ).warning()
    return result.reason


async def _read_guidance_or_none(url: str, deps: _Deps) -> str | None:
    """读取前置守卫：链空/协议白名单外 → 引导文本；**B15 全量校验**（scheme-only →
    validate_url 全判据——字面私网/元数据/短主机名/黑名单），拒收 → 系统注 + errors
    url_rejected 留痕——**registry.read 之前返回，零物理调用、零熔断计数**。None=放行。
    """
    if deps.read_chain is None or not deps.read_chain.sources:
        return _READ_EMPTY_GUIDANCE
    if not url.strip():
        return "（系统注：url 必填（非空白）。）"
    parsed = url.strip()
    scheme = parsed.split("://", 1)[0].lower() if "://" in parsed else ""
    if scheme not in _URL_PROTOCOLS:
        return f"（系统注：仅支持 http/https URL，已跳过：{url[:120]}）"
    rejected = await _url_guard_rejection_reason(url.strip(), deps)
    if rejected is not None:
        deps.recorder.record_error({"stage": "read", "reason": "url_rejected"})
        return (
            f"（系统注：该 URL 未通过安全校验（{rejected}），已拦截——"
            "请跳过并继续研究其他来源。）"
        )
    return None


def _window_span(
    text: str, *, offset: int, limit: int | None, deep_spec: Mapping[str, Any]
) -> tuple[str, int, int, int]:
    """head 限幅解析 + token 窗口切分 → (窗口文本, start, end, total)。

    head 限幅解析与窗口语义复用 url_reader（token_window/estimate_tokens 同包安全
    助手——批次 1 已单测锁死，零复制漂移）。B15：窗口切分独立成步——注入扫描作用于
    原始窗口文本（先于转义包裹，顺序锁）。
    """
    compact = ((deep_spec.get("compact") or {}) if isinstance(deep_spec, Mapping) else {}) or {}
    try:
        head = int(compact.get("url_read_head_tokens", DEFAULT_URL_READ_HEAD_TOKENS))
    except (TypeError, ValueError):
        head = DEFAULT_URL_READ_HEAD_TOKENS
    if head <= 0:
        head = DEFAULT_URL_READ_HEAD_TOKENS
    window, start, end = token_window(text, offset=offset, limit=head if limit is None else limit)
    return window, start, end, estimate_tokens(text)


def _wrap_window(window: str, *, url: str, start: int, end: int, total: int) -> str:
    """窗口 → 中性化 → `<external_content>` 包裹 → 非全文追加截断注记。"""
    wrapped = external_content(_neutralize_content(window), source=url, kind="web")
    if end < total:
        wrapped += (
            f"\n\n[系统注：正文窗口截断——以上仅为该页正文 token {start}~{end}，"
            f"全文约 {total} token；携带 offset={end} 可续读后续内容。]"
        )
    return wrapped


def _window_block(text: str, *, url: str, offset: int, limit: int | None, deep_spec: Mapping[str, Any]) -> str:
    """正文窗口 → 中性化 → `<external_content>` 包裹 → 非全文追加截断注记。

    B15 起为 `_window_span` + `_wrap_window` 的组合便利（firecrawl_read 主路径拆两步
    以在中间插入注入扫描——顺序锁「过滤先于转义包裹」）；本函数保持既有签名不变。
    """
    window, start, end, total = _window_span(text, offset=offset, limit=limit, deep_spec=deep_spec)
    return _wrap_window(window, url=url, start=start, end=end, total=total)


def _build_contract_tools(deps: _Deps) -> list[Any]:
    """契约三工具（DEEP_TOOL_NAMES）——闭包注入 deps，零全局单例直连。

    - web_search：冻结搜索链逐源并行（time_range 仅透传 Tavily，fast web_search 同款）；
      结果即发 sources 批次（ref_no 首现冻结）；
    - firecrawl_read：读原文全量 → save_snapshot（读后回写）→ token 窗口包裹；
    - save_snapshot：显式补存面（读后回写通常已自动触发；幂等）。
    """
    recorder = deps.recorder

    @tool
    async def web_search(query: str) -> str:
        """搜索本子课题：返回带 URL/标题/摘要/相关度的检索结果列表。"""
        if deps.registry is None or not deps.search_chain.sources:
            return _SEARCH_EMPTY_GUIDANCE
        query = (query or "").strip()
        if not query:
            return "（系统注：query 不能为空，请给出检索词。）"
        recorder.record_search(query)
        outcomes = await asyncio.gather(
            *(
                deps.registry.search(
                    query,
                    source,
                    time_range=(deps.time_range if source == "tavily" else None),
                    top_k=WEB_SEARCH_TOP_K,
                )
                for source in deps.search_chain.sources
            ),
            return_exceptions=True,
        )
        candidates: list[dict[str, Any]] = []
        failed: list[str] = []
        for source, outcome in zip(deps.search_chain.sources, outcomes):
            if isinstance(outcome, BaseException):
                failed.append(source)
                recorder.record_error(
                    {"stage": "search", "reason": "source_failure", "source": source}
                )
            else:
                # B15：add_candidates 前逐候选注入扫描——drop 不占 ref_no（ref_no 首现
                # 冻结发生在 add_candidates，过滤先于入册即来源卡/[n] 自然一致）
                filtered, filter_errors = filter_candidates(outcome, deps.guards, stage="search")
                for error in filter_errors:
                    recorder.record_error(error)
                candidates.extend(filtered)
        recorder.add_candidates(candidates)
        if not candidates:
            return "（系统注：检索无结果，请调整检索词或直接沉淀已有信息。）"
        lines = [
            f"[{recorder.ref_by_url[str(item.get('url'))]}] {item.get('title') or item.get('url')}"
            f"（score={item.get('score', 0.0)}）\nURL: {item.get('url')}\n摘要: {str(item.get('snippet') or '')[:300]}"
            for item in candidates
            if isinstance(item, Mapping) and isinstance(item.get("url"), str)
        ]
        text = "\n\n".join(lines)
        if failed:
            text += f"\n\n（系统注：部分搜索源失败（{','.join(failed)}），结果可能不全。）"
        return external_content(_neutralize_content(text), source="web_search", kind="web")

    @tool
    async def firecrawl_read(url: str, offset: int = 0, limit: int | None = None) -> str:
        """读取 URL 页面正文（token 窗口；外部内容包裹；读取成功自动保存网页快照）。"""
        guidance = await _read_guidance_or_none(url, deps)
        if guidance is not None:
            return guidance
        url = url.strip()
        if offset < 0 or (limit is not None and limit < 1):
            return "（系统注：offset 不可为负、limit 须 ≥1。）"
        recorder.begin_read()
        try:
            if deps.registry is None:  # pragma: no cover —— 入口链判据下 registry 缺席的防御面
                raise SearchChainUnavailable("registry 未注入")
            text = await deps.registry.read(url, deps.read_chain.sources[0])  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 —— 读取失败非阻断：跳过该 URL 继续研究
            recorder.record_error({"stage": "read", "reason": "read_failed", "url": url})
            AgentLog(
                message=f"子代理读取原文失败（跳过继续）：url={url} error={type(exc).__name__}",
                graph="deep",
                node="research",
            ).warning()
            return f"（系统注：该 URL 读取失败（{type(exc).__name__}），请跳过并继续研究其他来源。）"
        deep_spec = (deps.spec.get("deep") or {}) if isinstance(deps.spec, Mapping) else {}
        window, start, end, total = _window_span(
            text, offset=offset, limit=limit, deep_spec=deep_spec
        )
        # B15 顺序锁：注入扫描作用于**原始窗口文本**（先于转义包裹——原始闭合标记是
        # 结构种子，转义后不可检出）；drop=跳过快照/登记+系统注（零 payload 进上下文），
        # flag=照常包裹+errors 留痕
        inj = deps.guards.injection_filter if deps.guards is not None else None
        scan = await scan_text_async(window, inj) if inj is not None else None
        if inj is not None and scan is not None and scan.hits and inj.on_hit == "drop":
            recorder.record_error({"stage": "read", "reason": "injection_blocked"})
            AgentLog(
                message=(
                    "子代理读窗注入命中已整页拦截（on_hit=drop）："
                    f"rule_id={','.join(scan.hits)} 字符数={len(window)} "
                    f"truncated={scan.truncated}"
                ),
                graph="deep",
                node="research",
            ).warning()
            return (
                "（系统注：该页正文命中注入特征（injection_blocked），已整页拦截——"
                "请跳过并继续研究其他来源。）"
            )
        # 体二次校验（B15 纵深）：入口守卫与快照/登记使用点之间再验——拒收零快照零登记
        rejected_again = await _url_guard_rejection_reason(url, deps)
        if rejected_again is not None:
            recorder.record_error({"stage": "read", "reason": "url_rejected"})
            return (
                f"（系统注：该 URL 二次校验未通过（{rejected_again}），已拦截——"
                "请跳过并继续研究其他来源。）"
            )
        # 读后回写快照（OQ-33 探针建议 2：节点侧登记而非依赖模型自发调用；失败非阻断）
        try:
            meta = await save_snapshot(
                url=url,
                content=text,
                user_id=deps.user_id,
                thread_id=deps.thread_id,
                turn_id=deps.turn_id,
            )
            recorder.record_snapshot(url, meta)
        except (SnapshotError, ValueError) as exc:
            recorder.record_error({"stage": "read", "reason": "snapshot_failed", "url": url})
            AgentLog(
                message=f"快照落盘失败（非阻断继续研究）：url={url} error={type(exc).__name__}",
                graph="deep",
                node="research",
            ).warning()
        recorder.record_visited(url)
        wrapped = _wrap_window(window, url=url, start=start, end=end, total=total)
        if scan is not None and scan.hits:
            recorder.record_error({"stage": "read", "reason": "injection_blocked"})
            AgentLog(
                message=(
                    "子代理读窗注入命中（on_hit=flag 保留留痕）："
                    f"rule_id={','.join(scan.hits)} 字符数={len(window)} "
                    f"truncated={scan.truncated}"
                ),
                graph="deep",
                node="research",
            ).warning()
        return wrapped

    @tool("save_snapshot")
    async def save_snapshot_tool(url: str, content: str) -> str:
        """显式保存网页快照（读取成功时已自动保存；仅补存未快照内容时调用）。"""
        rejected = await _url_guard_rejection_reason(url.strip(), deps)
        if rejected is not None:
            recorder.record_error({"stage": "read", "reason": "url_rejected"})
            return (
                f"（系统注：url 未通过安全校验（{rejected}），快照未保存——"
                "请提供公开可访问的 http/https 页面。）"
            )
        meta = await save_snapshot(
            url=url,
            content=content,
            user_id=deps.user_id,
            thread_id=deps.thread_id,
            turn_id=deps.turn_id,
        )
        recorder.record_snapshot(url.strip(), meta)
        recorder.record_visited(url.strip())
        return (
            f"快照已保存：object_key={meta.get('object_key')} size_bytes={meta.get('size_bytes')}"
            f" reused={meta.get('reused')}"
        )

    return [web_search, firecrawl_read, save_snapshot_tool]


# ---- 装配面（OQ-33 探针钉正形态）----


def _summary_max_tokens(deep_spec: Mapping[str, Any]) -> int:
    try:
        value = int(deep_spec.get("summary_max_tokens", DEFAULT_SUMMARY_MAX_TOKENS))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_SUMMARY_MAX_TOKENS


def _max_subagents(deep_spec: Mapping[str, Any]) -> int:
    try:
        value = int(deep_spec.get("max_subagents", DEFAULT_MAX_SUBAGENTS))
        if value >= 1:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_MAX_SUBAGENTS


def _recursion_limit(deep_spec: Mapping[str, Any]) -> int:
    try:
        value = int(deep_spec.get("recursion_limit", DEFAULT_RECURSION_LIMIT))
        if value >= 1:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_RECURSION_LIMIT


def subagent_spec(
    subtopic: str,
    *,
    index: int,
    model: Any,
    deps: _Deps,
) -> SubAgent:
    """单子课题 SubAgent spec（OQ-33 装配形态单一事实源；测试内省面）。

    - tools=契约三工具（spec["tools"] 出现即覆盖继承缺省——原生收窄）；
    - middleware=只读 FS（泄漏面①防：read_file 必含白名单，卸载取回回路完好）；
    - response_format=SummarySchema（create_sub_agent 转发 create_agent——原生机制）；
    - system_prompt=deep.subagent.system 渲染 + read_min/read_max 配额引导（契约
      占位符集零变更，引导文本节点侧追加——reflect 先例同款）。
    """
    deep_spec = (deps.spec.get("deep") or {}) if isinstance(deps.spec, Mapping) else {}
    deep_spec = deep_spec if isinstance(deep_spec, Mapping) else {}
    summary_tokens = _summary_max_tokens(deep_spec)
    system_prompt = render_prompt(
        resolve_prompt("deep.subagent.system"),
        {
            "subtopic": subtopic,
            "summary_max_tokens": str(summary_tokens),
        },
    ) + _QUOTA_GUIDANCE.format(
        read_min=int(deep_spec.get("read_min", 2) or 0) if isinstance(deep_spec.get("read_min", 2), (int, float)) else 2,
        read_max=int(deep_spec.get("read_max", 5) or 0) if isinstance(deep_spec.get("read_max", 5), (int, float)) else 5,
    )
    return {
        "name": f"research-{index}",
        "description": f"研究子课题：{subtopic}",
        "system_prompt": system_prompt,
        "model": model,
        "tools": _build_contract_tools(deps),
        "middleware": _readonly_fs_middleware(),
        "response_format": SummarySchema,
    }


def _gp_compiled(model: Any) -> Any:
    """GP 顶替体（泄漏面②防）：同名 general-purpose 只读编译体，零检索工具。"""
    spec: SubAgent = {
        "name": GP_SUBAGENT_NAME,
        "description": "保留位子代理（深度研究不派发）",
        "system_prompt": "保留子代理：深度研究流程不使用。",
        "model": model,
        "tools": [],
        "middleware": _readonly_fs_middleware(),
    }
    return create_sub_agent(spec)


class _GatedRunnable(RunnableLambda):
    """门控 RunnableLambda（声明 inner 字段——测试闭包解引 seam 的类型面）。"""

    inner: Any = None


class _SubagentGate:
    """子任务门控（CompiledSubAgent runnable 包装）：信号量并发 + 边界取消 + 进度
    事件 + 回传捕获 + 失败隔离。

    - `inner`：编译子代理（测试内省 seam——RunnableLambda 实例经 .inner 反查）；
    - 边界取消：信号量 acquire 后 pop 进程级取消标志（capture 注入面，不依赖 ambient
      config 合并——确定性）；命中抛 TurnCanceledSignal（穿透 task 工具出节点）；
    - 失败隔离：非取消异常 → 合成 failed:true 报告 + errors 留痕，返回合法 ToolMessage
      形态 dict（主循环存活，其余子任务照常）；
    - 回传捕获：structured_response → SummarySchema；缺失时围栏剥离解析末条 AIMessage。
    """

    def __init__(
        self,
        name: str,
        subtopic: str,
        inner: Any,
        sem: asyncio.Semaphore,
        recorder: _Recorder,
        cancel_flags: dict[str, str] | None,
        turn_id: str | None,
        guards: ContentGuardConfig | None = None,
    ) -> None:
        self.name = name
        self.subtopic = subtopic
        self.inner = inner
        self._sem = sem
        self._recorder = recorder
        self._cancel_flags = cancel_flags
        self._turn_id = turn_id
        self._guards = guards  # B15：报告回传面注入扫描（子代理报告→协调器裸面）

    def _boundary_cancel(self) -> None:
        """子任务边界取消感知（pop 即感知+清理——guards.pop_cancel_flag 同语义）。"""
        if isinstance(self._cancel_flags, dict) and self._turn_id:
            reason = self._cancel_flags.pop(str(self._turn_id), None)
            if reason is not None:
                raise TurnCanceledSignal(reason)

    def _failed_state(self, note: str) -> dict[str, Any]:
        """合成 failed:true 回传终态（ToolMessage 面合法：含 messages + structured_response）。"""
        failed = SummarySchema(subtopic=self.subtopic, summary="", failed=True)
        return {
            "messages": [AIMessage(content=note)],
            "structured_response": failed,
        }

    async def run(self, state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:
        async with self._sem:
            self._boundary_cancel()
            self._recorder.set_status(self.name, "running")
            self._recorder.emit_search_progress()
            try:
                result = await self.inner.ainvoke(state, config)
            except (TurnCanceledSignal, asyncio.CancelledError):
                raise  # 取消非故障：穿透（ToolNode 默认处理器原样重抛；asyncio cancellation 直达）
            except Exception as exc:  # noqa: BLE001 —— 失败隔离（A2 情形 2）
                self._recorder.record_error(
                    {
                        "stage": "research",
                        "reason": "subagent_failed",
                        "subagent": self.name,
                        "error": type(exc).__name__,
                    }
                )
                # 终态由 complete 按报告 failed 派生（failed:true → extra 终帧 failed）
                self._recorder.complete(self.name, _failed_report(self.subtopic))
                AgentLog(
                    message=(
                        f"子代理失败（failed:true 隔离，不阻断其余）：subagent={self.name} "
                        f"error={type(exc).__name__}: {str(exc)[:200]}"
                    ),
                    graph="deep",
                    node="research",
                ).warning()
                return self._failed_state(f"子代理执行失败：{type(exc).__name__}")
            report = _extract_report(result, self.subtopic)
            # B15：报告回传面注入扫描（summary/hard_facts/quotes——子代理报告→协调器
            # 是无包裹裸面）；drop=报告置 failed:true 占位（失败隔离消费——不阻断其余），
            # flag=保留+errors 留痕（rule_id 只进告警——零 payload）
            report, injection_hits = filter_report(report, self._guards)
            if injection_hits:
                self._recorder.record_error(
                    {
                        "stage": "research",
                        "reason": "injection_blocked",
                        "subagent": self.name,
                    }
                )
                if (
                    self._guards is not None
                    and self._guards.injection_filter is not None
                    and self._guards.injection_filter.on_hit == "drop"
                ):
                    report = _failed_report(self.subtopic)
            self._recorder.complete(self.name, report)
            return result

    def runnable(self) -> "_GatedRunnable":
        r = _GatedRunnable(self.run)
        r.inner = self  # 内省 seam（测试闭包解引用）
        return r


def _failed_report(subtopic: str) -> dict[str, Any]:
    return {
        "subtopic": subtopic,
        "summary": "",
        "hard_facts": [],
        "quotes": [],
        "sources": [],
        "failed": True,
    }


def _parse_json_loose(text: str) -> dict[str, Any] | None:
    """围栏剥离 + JSON 子串提取（OQ-34 注②「markdown 围栏/散文包裹可机械修复」）。"""
    if not text:
        return None
    cleaned = text.replace("```json", "```")
    if "```" in cleaned:
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _last_ai_text(result: Mapping[str, Any]) -> str:
    messages = result.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                text = "".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, Mapping)
                )
                if text.strip():
                    return text
    return ""


def _extract_report(result: Any, subtopic: str) -> dict[str, Any]:
    """子代理终态 → SummarySchema 报告 dict（门控捕获面）。

    生效形态（机制增量实测，2026-11-19 真 LLM 探针：chat-standard 命中 ①1/1、
    ②③零触发）：① structured_response（SummarySchema 实例/dict）直取；
    ② 缺失/校验失败 → 围栏剥离解析末条 AIMessage JSON（模型漂移回退层）；
    ③ 皆不达 → failed:true 报告（不终止）。subtopic 以子课题原文兜底（防模型漏填/错位）。
    """
    data: Any = None
    structured = result.get("structured_response") if isinstance(result, Mapping) else None
    if isinstance(structured, SummarySchema):
        data = structured.model_dump()
    elif isinstance(structured, BaseModel):
        data = structured.model_dump()
    elif isinstance(structured, Mapping):
        data = dict(structured)
    if data is not None:
        try:
            report = SummarySchema.model_validate(data).model_dump()
        except ValidationError:
            report = {}
            data = None
        else:
            if not str(report.get("subtopic") or "").strip():
                report["subtopic"] = subtopic
            return report
    parsed = _parse_json_loose(_last_ai_text(result if isinstance(result, Mapping) else {}))
    if parsed is not None:
        try:
            report = SummarySchema.model_validate(parsed).model_dump()
        except ValidationError:
            report = {}
        else:
            if not str(report.get("subtopic") or "").strip():
                report["subtopic"] = subtopic
            return report
    return _failed_report(subtopic)


def _subagent_model(alias: str, index: int) -> Any:
    """子代理模型实例（Standard 档统一——OQ-34 探针推荐倾向；用户终局定档留痕挂
    批次 2 收口，见模块 docstring 模型节）。主循环与摘要同 alias。"""
    return build_chat_model(alias)


# ---- 节点 ----


def _clamp_subtopics(raw: Any, max_subagents: int) -> list[str]:
    """strip + 丢空白 + 保序去重 + ≤max_subagents（deep_planner 夹取同构——防越界）。"""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        text = item.strip() if isinstance(item, str) else ""
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out[: max(1, int(max_subagents))]


def _merge_urls(prior: Any, recorded: dict[str, None]) -> list[str]:
    """既有 state 值 + 本段记录 → 全量并集（普通列表覆盖语义——先 prior 后新增）。"""
    merged: dict[str, None] = {}
    if isinstance(prior, list):
        for item in prior:
            if isinstance(item, str) and item:
                merged.setdefault(item, None)
    for item in recorded:
        merged.setdefault(item, None)
    return list(merged)


def _merge_snapshots(prior: Any, recorded: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    if isinstance(prior, list):
        for item in prior:
            if isinstance(item, Mapping) and isinstance(item.get("url"), str):
                merged.setdefault(item["url"], dict(item))
    for url, meta in recorded.items():
        merged.setdefault(url, meta)
    return list(merged.values())


def _merge_candidates(prior: Any, recorded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    if isinstance(prior, list):
        for item in prior:
            if isinstance(item, Mapping) and isinstance(item.get("url"), str):
                merged.setdefault(item["url"], dict(item))
    for item in recorded:
        url = item.get("url")
        if isinstance(url, str):
            merged.setdefault(url, item)
    return list(merged.values())


async def research(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """research 节点（deep 子图）：子代理并行研究 → 报告/来源/快照回写 DeepState。

    事件面（api.md §7.4 深度主干子集）：stage(search,running,extra.subagents) 推进 +
    stage(read,running/done)；sources 批次由 web_search 工具即时发射。空 subtopics
    静默直通（planner 兜底残缺形态——writer 照常收口）。
    """
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②）

    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    deep_spec = (spec.get("deep") or {}) if isinstance(spec, Mapping) else {}
    deep_spec = deep_spec if isinstance(deep_spec, Mapping) else {}
    subtopics = _clamp_subtopics(state.get("subtopics"), _max_subagents(deep_spec))
    if not subtopics:
        return {}  # 零信号零装配（planner 兜底残缺形态不伪造研究面）

    configurable = (config or {}).get("configurable") or {}
    registry = configurable.get("tools_registry")
    search_chain = configurable.get("search_chain")
    # fail-fast 面 = 冻结搜索链（TurnRunner 轮启动解析、与 registry 同源注入；测试面
    # 仅注链的形态合法——registry 缺席时工具面走引导文本，入口不误拒）：
    if not isinstance(search_chain, SearchChain):
        raise SearchChainUnavailable("搜索链未注入（registry 未启动）——无法执行 research")
    if not search_chain.sources:
        raise SearchChainUnavailable(search_chain.detail or "搜索链不可用")
    read_chain = configurable.get("read_chain")
    read_chain = read_chain if isinstance(read_chain, ReadChain) else None

    cancel_flags = configurable.get("cancel_flags")
    turn_id = str(configurable.get("turn_id") or "") or None

    recorder = _Recorder(subtopics)
    recorder.emit_search_progress()  # 入口帧：全 pending（api.md §7.4 extra.subagents[]）

    alias = resolve_chat_alias(state, config)
    main_model = build_chat_model(alias)
    deps = _Deps(
        registry=registry,
        search_chain=search_chain,
        read_chain=read_chain,
        spec=spec,
        recorder=recorder,
        user_id=str(state.get("user_id") or ""),
        thread_id=str(state.get("thread_id") or ""),
        turn_id=str(state.get("turn_id") or ""),
        time_range=state.get("time_range") if isinstance(state.get("time_range"), str) else None,
        # B15：内容侧护栏轮内装载一次（缺子节回落 defaults 种子=部署即启用）
        guards=load_content_guard_config(spec),
    )

    sem = asyncio.Semaphore(_max_subagents(deep_spec))
    subagents: list[CompiledSubAgent] = []
    for index, subtopic in enumerate(subtopics):
        sub_model = _subagent_model(alias, index)
        spec_dict = subagent_spec(subtopic, index=index, model=sub_model, deps=deps)
        compiled = create_sub_agent(spec_dict)
        gate = _SubagentGate(
            f"research-{index}", subtopic, compiled, sem, recorder, cancel_flags, turn_id,
            guards=deps.guards,
        )
        subagents.append(
            {"name": gate.name, "description": f"研究子课题：{subtopic}", "runnable": gate.runnable()}
        )
    subagents.append(
        {"name": GP_SUBAGENT_NAME, "description": "保留位子代理（深度研究不派发）", "runnable": _gp_compiled(build_chat_model(alias))}
    )

    orchestrator_prompt = _ORCHESTRATOR_TEMPLATE.format(
        lines="\n".join(f"- research-{i}: {topic}" for i, topic in enumerate(subtopics))
    )
    agent = create_deep_agent(
        model=main_model,
        tools=[],  # 主面收敛：task（SubAgentMiddleware 自带）+ 只读文件工具（middleware）
        subagents=subagents,
        system_prompt=orchestrator_prompt,
        middleware=_readonly_fs_middleware(),
    )
    agent_config: RunnableConfig = {**(dict(config) if config else {})}  # type: ignore[typeddict-item]
    agent_config["recursion_limit"] = _recursion_limit(deep_spec)

    dispatch = HumanMessage(
        f"请派发以上 {len(subtopics)} 个子课题的研究任务（task 工具）并在全部回传后汇总。"
    )
    try:
        await agent.ainvoke({"messages": [dispatch]}, config=agent_config)
    except (TurnCanceledSignal, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 —— 主循环失败非终止：已捕获报告保留
        recorder.record_error({"stage": "research", "reason": "research_agent_failed", "error": type(exc).__name__})
        AgentLog(
            message=(
                f"research 主循环失败（部分报告保留，不终止）：error={type(exc).__name__}: "
                f"{str(exc)[:200]}"
            ),
            graph="deep",
            node="research",
        ).warning()

    if recorder.reads:
        emit_stage("read", "done", progress={"current": recorder.reads, "total": recorder.reads})

    update: dict[str, Any] = {
        "subagent_reports": recorder.reports,  # Annotated[add] 增量（批次 2 reducer 改造）
        "visited_urls": _merge_urls(state.get("visited_urls"), recorder.visited),
        "searched_queries": _merge_urls(state.get("searched_queries"), recorder.searched),
        "snapshots": _merge_snapshots(state.get("snapshots"), recorder.snapshots),
        "collected_sources": _merge_candidates(state.get("collected_sources"), recorder.candidates),
    }
    if recorder.errors:
        update["errors"] = recorder.errors  # Annotated[add] 增量
    return update
