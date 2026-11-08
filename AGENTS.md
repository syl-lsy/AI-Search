# AGENTS.md — AI Search 项目工作区指引

类 Perplexity 的自建 AI 搜索引擎：快速模式（多源搜索 → ≤3000 字上限内带引用综合回答，篇幅按题型软引导，P50 ≤30s）+ 深度模式（deepagents 子代理并行研究 → 分节带引用报告）+ 个人知识库融合，全程引用可溯源、成本可观测。

**当前阶段（2026-11-07）**：M1 实施计划 v4 **已获批**（`.zcode/plans/plan-m1-v4-20261104.md`），**批次 0 + 批次 1 S0 + 批次 2 B1 底座均已闭环**（批次 2 于 2026-11-07：config 分组/settings 单例、CORS、db 会话、五表 ORM+迁移 0001（含默认用户 seed）、config_center 三层、EventBus（线程级 seq）、双探针（ready 四组件 503 口径）、crons+trace_cleanup、ReasoningChatModel 护栏（R8）、pyright+ruff 钉版（Makefile `typecheck-backend`）；门禁绿：unit 320 passed + integration 4 passed/5 skipped（NACOS_/LANGFUSE_ 凭证门控）+ 后端覆盖率 97.78%（≥80%）+ typecheck-backend 零告警；§10.1 巡检落档）。下一批 = **批次 3 B2 线程轮次（桩主图）**（threads/turns REST + TurnRunner 骨架——桩主图走完整事件管线、TurnRunner 自此统一 invoke 主图单例 + ThreadScheduler 四策略（409001）+ 迁移 0008；B1 遗留接线项随 B2 同批：main.py lifespan 接 config_center/EventBus、/health/ready 加 nacos degraded 位）。**每环节闭环后执行文档巡检并更新 `docs/PROGRESS.md`（DEV §10.1）**。进度实况见 `docs/PROGRESS.md`。

## 文档地图（改任何东西前先读对应文档）

| 文档 | 管什么 |
|---|---|
| `docs/PRD.md`（v1.15） | 做什么：REQ-001~047、验收 A1~A33、里程碑 M1~M6（实验/可选功能 REQ-046/047 带 P2 降级口径；fast 答案 ≤3000 字硬上限、P50 ≤30s；评测面三层扩展——A31 维度集 / A32 数据集增量 / A33 第三增量） |
| `docs/ARCHITECTURE.md`（v1.33） | 怎么实现：图编排=主图路由+两张模式子图（§4.0/ADR-012）、目录结构、端口（Nacos v3.2.4 三端口，ADR-013）、工单 B1~B8/F1、ADR-001~014、风险 R1~R15 |
| `docs/DEVELOPMENT-PROCESS.md`（v1.21） | 按什么顺序做：S0~S5 流程、TDD 规范、节点五步闭环、门禁与 DoD、环节闭环文档巡检（§10.1）。**开发会话开工前必读全文 + §11 checklist** |
| `docs/PROGRESS.md` | 进度总览活文档：M1 完成流程图、批次状态表、每环节文档巡检记录。**每闭环一个环节必须更新**（巡检规则见 DEV §10.1） |
| `docs/contract/api.md`、`data-model.md`、`nacos-config.md`、`frontend-api.md`、`memory-api.md`、`memory-data-model.md` | 冻结契约：REST+SSE 协议、DDL 与迁移、Nacos 五 dataId、前端映射。**字段/事件/表结构以此为准，禁止私改** |
| `docs/design/`（4 个 HTML + `tokens.css` + `设计说明.md` + `reference/` 对照截图） | 已确认的视觉基准，前端样式必须用 tokens |
| `docs/research/` | 调研报告（记忆/上下文压缩/评测工具链/dsh-superagent 适配性），是若干契约默认值的溯源依据；dsh 报告结论=不采用、仅借鉴「3+2 思想」落实现层 |

**冲突优先级**：PRD 验收口径与 contract 冻结字段 > ARCHITECTURE 结构决策 > DEVELOPMENT-PROCESS 流程细节。发现引用不一致时停下回报待人工确认，不自行选边、不擅自改契约。契约变更必须走增量 diff 并同步所有受影响方（含 `frontend/src/types/api.d.ts` 镜像）。

## 计划中的仓库布局与命令（开工时按此脚手架，勿自创）

- 双 workspace：根 `pyproject.toml` **仅 workspace 定义**（`members=["apps/*"]`，虚拟根不声明 `[project]`——声明后 `uv sync` 不装成员依赖）+ `pnpm-workspace.yaml`（`packages=["frontend"]`）。后端服务 `apps/search-api/`（导入名 `search_api`），前端 `frontend/`。
- 全部命令从仓库根跑（`env_file` 相对运行时 cwd 解析，禁止换目录裸跑）：`make dev`（后端 :8300）/ `make dev-frontend`（:5173）/ `make test` / `make test-unit` / `make test-integration` / `make test-e2e` / `make test-frontend*` / `make typecheck` / `make db-migrate|db-upgrade|db-downgrade`。
- 分层铁律：`api/` 只做路由与校验，业务在 `services/`，**API 层不直接调 LangGraph**；`schemas/`（DTO）≠ `db/models/`（ORM）；service 只 flush 不 commit（事务边界在 `get_db`；TurnRunner/KB worker 为短事务例外）；图节点保持纯函数，持久化由 TurnRunner 消费事件流完成。
- 依赖方向：`api/ → services/ → agent/ → config_center/`；agent 永不 import api；core/ 横切。

## 开发规范速查（判据式）

### 配置三分法（变量禁止写死）

| 落点 | 判据 | 典型项 |
|---|---|---|
| `.env`（后端经 `core/config.py` settings 单例读；前端只读 `import.meta.env.VITE_*`） | 静态、敏感或部署环境绑定，改动需重启 | 密钥、连接串、端口、桶名、`VITE_API_BASE_URL` |
| Nacos 五 dataId | AI 行为类、运行时热更（改了不重启即生效） | 提示词、模型映射、Agent Spec（含 `spec.routing`）、功能开关、热门池 |
| 代码常量 | 与环境无关的纯逻辑值，语义化命名收敛到常量模块 | 枚举、纯前端阈值（有 Nacos 键的阈值除外） |

一句话判据：**改了要重启 → `.env`；改了要不重启生效 → Nacos；敏感 → 永远 `.env`**（Nacos 拒收疑似密钥字段）。代码里出现裸密钥/裸端口/裸提示词/裸模型名 = 违规。

### 前端类型防线（vue-tsc 的边界）

- `vue-tsc --noEmit` 是**静态全量**检查（含 `.vue` 模板表达式），但类型 ≠ 运行时正确——`api.d.ts` 是静态镜像、非运行时校验器；后端字段变更 vue-tsc 不报错，防线=契约同步镜像 + fixture 测试 + 评审 diff 核对。
- 盲区纪律（**已升冻结契约**：`frontend-api.md` §8 第 9 条「类型盲区纪律」，v1.11，评审与 CI 依据）：禁 `as` 双重断言与 `@ts-ignore`（确需豁免用 `@ts-expect-error` + 行注释理由）；无类型边界产物先经类型守卫收窄再用；`defineProps`/`defineEmits` 一律类型式声明；第三方库无类型时写本地 `.d.ts`。

### 规范出处（语义索引，出处为冻结文档时以文档为准）

| 域 | 出处 |
|---|---|
| 前端工程约定（Pinia setup store / 请求唯一出口 / 路由懒加载 / UI 自绘 / 流式解耦 / TS 严格） | `frontend-api.md` §8 |
| 后端分层 / 异常 / 日志 / 事务 | 本文件「仓库布局」分层铁律 + 全局技能 `python-backend-base` |
| 图节点 / 提示词 / 热更键位 | ARCHITECTURE §4（ADR-012）+ `nacos-config.md` + DEVELOPMENT-PROCESS §4~5 |
| TDD 规范（全栈适用：前端 / 后端+AI） | `DEVELOPMENT-PROCESS.md` §4（红绿循环）+ §5.4（节点五步闭环 DoD） |
| SSE 流式渲染缓冲（合帧/flush/断线续传/重连退避） | `streaming-buffer` 技能（流式实现统一规范） |
| 安全（外部内容包裹 / DOMPurify / 上传白名单） | 本文件「关键约束」安全铁律 |

## 测试与质量门禁（"完成"的必要条件）

- **全栈 TDD（用户裁决 2026-11-04，前端 / 后端+AI 一律适用）**：先写测试再实现（红→绿→重构），失败语义与边界先行出红测试，禁止"先实现后补测试"；后端与 AI 图节点走 DEVELOPMENT-PROCESS §4 红绿循环 + §5.4 节点五步闭环（写测试→实现→接入图→Studio/Langfuse 观察→验收，禁止批量写节点）；前端同样红绿循环（unit 纯逻辑先行 → 最小实现 → 组件/integration 补齐）。
- 后端三层（unit 全 mock 零网络 / integration 真依赖读 `.env.test` / e2e）+ 前端四层（unit 纯逻辑 / integration 联装 / component 渲染 / e2e GUI 黑盒）。覆盖率门禁双端 80%：后端计 unit+integration，前端计前三层；e2e 两端均不计入。
- AI 图节点**一个一个闭环**（写测试→实现→接入图→Studio/Langfuse 观察→验收，§5.4 九条 DoD），**禁止批量写节点**；失败语义先写红测试。
- unit 层 LLM 全 mock（结构化输出用 fixture）；integration 断言结构不断言文案；回答质量走评测线（M4 四套基线，eval-engineer），与确定性测试互不充当证据。
- 全量 `make test` + `make typecheck` 绿才算完成；不接受只跑增量或 `--last-failed`。

## Git 提交分工（2026-11-05 用户裁决，硬规范）

- **AI 不执行任何 git 写操作**（commit / push / stash / 历史重写 / branch 管理等）：任务完成后停在工作区改动状态，向用户报告改动文件清单与内容摘要，提交由用户自行 add/commit/push。
- git 仅做只读诊断（status / log / diff 等，且仅在用户问起 git 问题时）；唯一例外 = 用户当次明确指派执行某个具体 git 操作。

## 关键约束与已知坑

- **密钥只进 `.env`**（Nacos 拒收疑似密钥字段）；禁止把密钥/凭据内容读入上下文、展示或写入任何对外产物；全部 host 端口绑 127.0.0.1。端口：backend 8300、frontend 5173、postgres **15432**（规避本机 5432）、minio 9100/9101、nacos 8081/8849/9849、litellm 4100、langfuse 3100、redis **16379**（6379+10000 惯例；本机 6379 被原生 redis 占用）、sandbox 8801（M4；2026-11-06 宿主端口重映射，与同机 deal-pilot 栈并存，ARCH v1.24）、crawl4ai **11235**（MCP 备源，compose 常驻）、searxng **18080**（搜索兜底链首，B3 compose 落地）。
- **SSE 协议**：POST 建轮 + GET 订阅两段式（原生 EventSource 自动重连带 Last-Event-ID）；事件带线程级单调 `seq`；token 增量不落库；前端流式渲染遵守 streaming-buffer 规范。
- **图编排（ADR-012）**：fast/deep 是**子图**；外层主图壳 `graphs/main.py` + `registry.py` 启动时一次编译三图注册 `GRAPH_REGISTRY`，TurnRunner 统一 `astream(subgraphs=True)`，`route_by_mode` 按 `state.mode` 读 Agent Spec `routing` ∩ registry 分发（亚毫秒/零 SSE 事件/无 LLM·IO）；扩展=新子图注册 + `spec.routing` 热更两步，主图拓扑零改动；前端点模式仅为本地状态（REQ-007）。子图边界三风险（state 投影映射/interrupt·cancel 传播/事件冒泡过滤）见 R14——B3 第 0 步主图壳先行 + 路由单测四条先闭环。
- **图外功能不进节点五步闭环**：followups（REQ-046）与首页推荐池（REQ-047）按 §4 普通 TDD 交付；实验功能硬口径=默认关闭/失败静默/不计入 P50/评测验证后定转正或下线。crons：`trace_cleanup.py` 随 B1 落地，`memory_ttl_sweeper.py` 归 M5。
- **安全铁律**：外部内容进上下文一律 `<external_content>` 包裹（提示注入隔离）；前端 markdown 过 DOMPurify、协议白名单 http/https；上传类型白名单 + 20MB 上限。
- **版本锁定例外**：`deepagents>=0.5.3`、`langgraph>=1.1`、`langchain-openai` 须**锁版本 + 单测护栏**（ReasoningChatModel 覆写私有 API，升级前必须先跑护栏，R8）。
- **模型映射现行值**：`model.mapping.light` = 本地 `qwen3.5-4b-ollama`（宿主 Ollama 为**默认运行时依赖**，litellm 静态 fallback→`chat-speed`）。备源降级链（v1.32 两级链）：搜索兜底 = SearXNG（自部署，`fallback_search` 链首）→ AnySearch（`fallback_search_secondary` 次级）+ 读取备胎 = 自部署 Crawl4AI（`url_reader_backup`，`/mcp/sse`），备位**条件装载**（语言门控后主位全断才按链序临时顶替，R12；SearXNG compose 与接入性验证归 B3 第 0 步，验证不过回退 AnySearch 独担）。
- **M1 开工前接入性验证六项（DEV §3.3）**：R1（BoCha stdio MCP，官方仓库 `github.com/BoChaAI/bocha-search-mcp`）、R7（本地 qwen3.5:4b 结构化输出，20 条合法率 ≥90%）、R9（LiteLLM 会话粘黏）、R11（智谱 Coding Plan 适配）、R12（AnySearch 配额 + crawl4ai 端点形态）、R15（nacos-sdk-python × Nacos v3.2.4 三链路，不通过=改自封装 httpx v3 client）——结论落档后才开 S1。
- **S0 基建 UI 初始化 checklist（DEV §3.2）**：compose 起栈后、S1 开工前执行，顺序 **Nacos→MinIO→Langfuse→LiteLLM**（Nacos 先鉴权后操作：管理员改密、建只读账号 `ai-search-ro`、发布 403 实证，再初始化五 dataId）。课程仓库（ai-infrastructure / ai-eval）只是教学参考：**提示词走 Nacos，不用 Langfuse Prompt Management；M4 评测以 A' 契约为准**，教学端口/口令勿照抄。
- 提示词/模型映射/Agent Spec 全部走 Nacos 热更（五 dataId，键位见 nacos-config.md），改提示词先补 fixture 测试与评测对比。
- 收尾三底线：测试证据、安全敏感改动过 security-auditor、契约变更同步受影响方并更新 `docs/contract/*` 并升版本。
- **环节闭环文档巡检（2026-11-05 用户裁决，DEV §10.1）**：每闭环一个环节（批次/阶段/工单）必做——盘点受影响文档 → 逐份判定需不需要更新（实质语义变更当批更新升版并同步引用方；无变更记「无需更新」+理由，**禁止机械 bump 版本**）→ 需更新者当批更新完、不留待办到下一环节 → 留痕（交付说明 + `docs/PROGRESS.md` 巡检记录与状态推进）。**巡检未做不算环节收尾完成**；巡检更新 ≠ 私改契约（契约变更仍走增量 diff）。
