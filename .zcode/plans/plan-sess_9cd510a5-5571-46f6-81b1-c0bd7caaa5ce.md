wf-level: T3 -- 新项目从零实施，契约套件已冻结（ARCH v1.12 / api v1.6 / data-model v1.8 / frontend-api v1.8 / nacos-config v1.8 / PRD v1.8 / 流程 v1.6）
wf-gate: contract

# ai-search 项目实施计划（骨架先行 + 后端先行 + 阶段闭环门禁，范围 = 骨架 + S0 + M1 全链路含前端）

## Context

ai-search 是类 Perplexity 的双模式搜索 Agent（T3 新项目）。现状为**纯文档绿地**：契约/PRD/架构/开发流程/设计稿全部定稿（2026-11-03 最新基线），零代码、非 git 仓库。用户已裁决四条执行原则：

1. **工程骨架先行**——第一步按 ARCHITECTURE §3 搭工程骨架，再进 S0 基础设施；
2. **后端先行**——后端链路完成后再派前端；
3. **前端契约已闭环**——用户补充的边界情况已落 frontend-api v1.8（滚动交互三件套 REQ-043~045 / A26~A28，设计稿 v1.1 已确认），F1 无额外门禁、按本计划正常开工；
4. **阶段闭环门禁**——每阶段完成做严格闭环测试：链路端到端真实跑通 + 逐条满足事前冻结的闭环条件清单；**不满足即未完成，禁止强制闭环**（不降级、不放宽、不带病前进），修复后重验同一清单，卡住停下交用户裁决，改条件须用户明示并落档。

M1 范围（PRD v1.8 §4.0）：REQ-001~011 + 横切 REQ-028/029 + REQ-042（P1，并发四策略）+ REQ-043~045（P1，滚动交互三件套）。

## 阶段闭环验收机制（每批次强制）

- **闭环 = 该阶段声称打通的链路，在真实依赖（真库/真 LLM/真搜索源/真事件流）上端到端跑通，且逐条满足事前冻结的闭环条件清单**。
- 条件事前冻结（各批次清单见下，增补须当批开工时定稿）；每条对应可复查证据（测试输出、curl 记录、Studio 截图、Langfuse trace 链接、迁移日志）写入当批交付说明。
- 不闭环 → 本批未完成、不进下一批；修复后重验**同一份清单**；无法修复 → 停下报告根因与选项，由用户裁决。
- 与 DEVELOPMENT-PROCESS v1.6 各 DoD（§3.3 / §5.4 / §7 / §8）合并执行，二者取严。

## 用户前置项（批次 1 前备齐）

- API 密钥：阿里云百炼（qwen chat + text-embedding-v4 + qwen3-rerank）、智谱 GLM（Standard/Speed 档；Coding Plan 走 R11 核验）、DeepSeek（**flash 档位名按控制台逐字核对**）、Tavily、BoCha、Firecrawl、AnySearch（免费档，配额观测）
- 自部署 crawl4ai 的 `CRAWL4AI_API_TOKEN`（自设）；本地 Ollama **可选**（已降级为离线备选，非必装）
- Docker Desktop 可用；端口全部绑 127.0.0.1（新增 11235 crawl4ai）

## 批次总览

| # | 批次 | 执行专家 | 闭环链路 |
|---|---|---|---|
| 0 | 工程骨架 + git init | backend-developer | 目录结构 → 工具链 → 依赖安装 → 骨架自检 |
| 1 | S0 基础设施 + 接入性验证 | devops-engineer + harness-engineer | compose 起依赖 → 健康 → 初始化 → 四类模型 smoke → Langfuse 双 trace → R1/R7/R9/R11/R12 |
| 2 | B1 底座 | backend-developer | 迁移回滚链 → 双探针 → EventBus 收发 → 全量回归 |
| 3 | B2 线程轮次 | backend-developer + harness-engineer | 桩图轮次状态机 → REST 对账 → 取消/四策略 → A25 reject 回归 |
| 4 | B3 fast 图五节点 | harness-engineer + prompt-engineer | 逐节点红→绿→接图→Studio→Langfuse→九条 DoD→全量回归；末尾整图端到端 |
| 5 | S2 SSE 联调 | harness-engineer + backend-developer | POST → SSE 全事件序列 → done 对账 → 断线补帧 → 取消链路 |
| 6 | 后端综合验证 | test-engineer | 全量测试矩阵 + API 可判定验收项预检 |
| 7 | 前端 F1（含滚动三件套） | frontend-developer | fixture→真服务切换 → 四页主链路 → A13/A26~A28 浏览器判定 |
| 8 | M1 综合测试与退出 | test-engineer + code-reviewer + security-auditor | 全链路综合 + A 编号逐条 + 性能抽样 + 审查落档 |

**批次节奏铁律**：一次只开一个工单/节点；上一批闭环清单未全勾不开下一批；每批先写测试（红）再实现；每批开工执行 DEVELOPMENT-PROCESS §11 开工 checklist。

## 各批次要点与闭环条件

### 批次 0：工程骨架（第一步）

按 ARCHITECTURE v1.12 §3（目录与 Makefile 目标族）+ ADR-007（uv + pnpm 双 workspace），**先加载 `python-backend-base` 技能**（AI Agent 扩展形态见其 `references/ai-agent.md` + `scripts/extend-ai.sh`），不手搓：

- `git init` + `.gitignore` + README 占位；根 `pyproject.toml` 仅 workspace 定义（**不声明 `[project]`**），members + `[tool.test-runner]`；`.python-version`
- `pnpm-workspace.yaml`；根 `Makefile` 按 ARCH §3 目标族（dev/test 族 + db 迁移族，目标体随后续批次补齐）
- `apps/search-api/` 完整目录树（core/api/schemas/db/services/agent/config_center/exception/utils + test 三层 + alembic.ini + migrations/ + langgraph.json 占位）
- `frontend/`、`scripts/`、`deploy/` 目录占位；`.env.example` / `.env.test.example` 模板（变量清单照抄 ARCH §7，含 MCP_ 组 +4 备源变量与 CRAWL4AI_API_TOKEN）

**闭环条件**：① 目录树逐项对照 ARCH §3 一致；② `uv sync` 与 `pnpm install` 仓库根成功；③ Makefile 命令骨架可调用；④ .env 模板变量与 ARCH §7 清单一一对应无缺漏；⑤ `.gitignore` 生效；⑥ git 首提交完成。

### 批次 1：S0 基础设施先行（流程铁律第 1 条）

顺序纪律：**先起依赖 → 看健康 → 再初始化**（流程 §3）。devops-engineer 部署，harness-engineer 做接入性验证：

- `deploy/docker-compose.yml`：postgres17+pgvector（ai_search/langfuse 双库）、MinIO 三桶、Nacos standalone、LiteLLM 网关、Langfuse 栈、**crawl4ai 常驻（:11235，token 鉴权，MCP `/mcp/sse`）**；`deploy/litellm/config.yaml`：**6 个 chat 别名**（chat-standard×3 / chat-speed×3，供应商优先序百炼→智谱→DeepSeek，每档三节 fallback 链，`num_retries=0`）+ embedding/rerank；`scripts/nacos_init.py` 写五 dataId 初值（含 v1.7 模型矩阵 + v1.8 备源 anysearch/crawl4ai 条目 + `turn.default_on_conflict=reject`）
- 接入性验证**五项**写回 ARCH §10（作为 S1 rewrite 前置输入）：R1 BoCha MCP stdio 装载；**R7 chat-speed 三家 flash 逐家**结构化输出合法率 ≥90% → 选定 Speed 档主力；R9 会话亲和（含主力冷却→fallback 切换→冷却恢复→粘黏回归增验路径）；R11 智谱 Coding Plan 产品化适配（端点/并发限流/公平条款）+ qwen `reasoning_content` 字段形态同批核验；R12 AnySearch 连通+单查询+配额观测、crawl4ai 端点形态 + 实抓一页对照 `/md`

**闭环条件**（= 流程 §3.3 五条 + crawl4ai 行）：① §3.1 第 1~8、10 项全过，`docker compose ps` 全 healthy；② 五 dataId 可读且为**现行版本**契约初值（含模型矩阵与备源条目）；③ LiteLLM 四类 smoke 过（chat-standard / chat-speed / embedding=1024 维 / rerank 降序）；④ Langfuse 网关侧+应用侧 trace 可见、session=thread_id 可聚合；⑤ R1/R7/R9/R11/R12 五项结论落档。

### 批次 2：B1 底座

契约：data-model v1.8 迁移 0001、ADR-007。TDD。core 四件套（config 按前缀分组 / loguru 日志 / middleware 注册表 / openapi 信封同步）+ exception 体系 + `db/session.py`（懒加载单例+慢查询钩子）+ ORM 模型（users/threads/turns/turn_events/sources）+ 迁移 0001 + `/health` `/health/ready` 双探针 + `services/event_bus.py`（线程级 seq、内存缓冲）。

**闭环条件**：① 迁移链 upgrade→downgrade→upgrade 真库回滚成功；② `/health` 存活 + `/health/ready` 真连库；③ EventBus 发布→订阅→seq 单调单测绿；④ 全量 `make test` 绿且后端覆盖率 ≥80%。

### 批次 3：B2 线程轮次

契约：api.md v1.6 端点 3-10、迁移 0008、ADR-010。threads/turns REST + schemas DTO + TurnRunner 骨架（**桩图**：秒回固定答案走完整事件管线）+ ThreadScheduler 四策略（reject/enqueue/interrupt/rollback，缺省取 Nacos；409001 语义）+ 迁移 0008。

**闭环条件**：① POST /threads 真实建线程并跑完一轮桩图（pending→running→completed）；② GET /turns 终态与 turn_events 落库一致；③ 追问、cancel（running/queued 两态）真实走通；④ 四策略逐一真实触发（reject=409001 / enqueue 按序执行 / interrupt·rollback 的 cancel_reason 落库且 interrupt 不产生 error 事件）；⑤ api.md 契约测试逐端点绿 + 全量回归绿。

### 批次 4：B3 fast 图五节点逐个闭环（核心批次）

契约：ARCHITECTURE §4.1/§5.2、nacos-config v1.8 键位；加载 `langgraph`/`langchain` 技能。**一次只开一个节点**：

1. `rewrite`（R7 结论为前置）：chat-speed 结构化输出 + 夹取 1~4 + 失败重试→factoid 单发兜底
2. `merge_dedupe`（纯函数，可与相邻节点同批测试，仍单独勾 DoD）
3. `web_search`（R1/R12 结论为前置）：Send 扇出；zh→Tavily+BoCha、非 zh→Tavily；schema_guard 双向校验；**链式降级 = BoCha↔Tavily 互兜底 → 双断条件装载 AnySearch → 三断该轮 search 失败**（stage detail 标注降级）
4. `fuse_rerank`：KB 命中才 rerank top10，失败按 origin 得分兜底
5. `synthesis`：token 流式 ≤300 字、`[n]` 引用；最小检索路径作为 rewrite/synthesis 测试分支，不单列闭环

提示词由 prompt-engineer 出初稿进 Nacos prompts dataId（键位 `fast.{node}`），内置默认回落 + 占位符变量集单测锁死。

**闭环条件（每节点）** = 流程 §5.4 九条 DoD（unit 红绿留痕 / integration 真 LLM 断 structure / 注册进图 Studio 可单步可重放 / state 与 `agent/state.py` 一致 / 失败语义符合 §4.1 / Nacos 键位+内置默认+占位符单测 / Langfuse 节点级 span 独立可识别 / 事件符合 api.md §7.2 / 全量回归绿）。
**整图收口**：五节点全闭环后 Studio 端到端跑通 fast 图（真 LLM+真搜索源，含最小检索分支与降级链演练），Langfuse 完整 trace 覆盖五节点。

### 批次 5：S2 SSE 联调

ADR-003 + api.md §7：TurnRunner 事件翻译 → EventBus → `GET /turns/{id}/events`；seq 单调、15s 心跳、Last-Event-ID 重放（TTL 30 分钟滑动续期、运行中缓冲永不过期）、done 后 REST 对账、取消链路、四策略 SSE 表现。加载 `streaming-buffer` 技能。

**闭环条件**：① 真实首问 SSE 事件序列与 api.md §7.4 快速序列逐事件比对一致；② done 与 GET /turns 终态对账一致；③ 断线重连 Last-Event-ID 补齐缺帧（A13）；④ 心跳 15s 出现且不占 seq；⑤ 取消链路真实走通；⑥ turn_events 落库子集逐字段核对；⑦ 全量回归绿。

### 批次 6：后端综合验证

test-engineer：全量 `make test` + `make test-e2e`（真服务真库真 LLM；硬约束：≤300 字、`[n]` 与 sources 一一对应、stage 顺序、语言跟随）+ `make typecheck`。

**闭环条件**：三命令全绿；M1 验收项中可 API 判定部分预检留档（UI 侧判定留批次 8）。

### 批次 7：前端 F1（契约已闭环，无额外门禁）

frontend-developer：加载 `frontend-web-base` + `vue-best-practices` + `streaming-buffer`。按 frontend-api v1.8 + 设计稿 v1.1 + tokens.css：

- 四页 + SSE 客户端 `src/sse/turnStream.ts`（连接生命周期 12 条）+ Pinia setup stores + `types/api.d.ts` 镜像 + i18n + Tailwind↔tokens 映射
- **滚动交互三件套（REQ-043~045）**：AnswerScrollController（贴底 <40px 自动跟随 / 上滚 >8px 接管且流式不中断 / 恢复三途径，恢复仅认接管后用户主动下滚进贴底区）、OutlineAnchor（右缘锚点，a11y 硬性，<720px 隐藏）、ScrollToBottomFab（离底约一屏常驻、贴底淡出）
- 四层测试落位（流程 §7）：滚动状态机阈值判定→unit；锚点/箭头渲染与 a11y→组件层；流式中接管与恢复→integration；A26~A28 浏览器判定 + **A27 与 DeepSeek 实测参照逐项比对截图留档**→e2e GUI 黑盒

**闭环条件**：① fixture 全量替换为真服务；② 四页主链路 + A13 浏览器实测通过；③ A26~A28 逐条判定（P1 可短期降级但须记录；A27 深度拆分形态随 M2）；④ 前端三层覆盖率 ≥80%；⑤ `make typecheck` 绿；⑥ DOMPurify 白名单等渲染安全过 security-auditor。

### 批次 8：M1 综合测试与退出

- test-engineer：e2e 全链路 + 三项全量回归 + **A1/A6/A8/A9/A10 必过；A25、A26~A28 P1 可降级须记录**逐条对照 + 性能抽样（每场景 ≥20 次，P50≤8s / P90≤15s）
- code-reviewer 全量 diff；security-auditor 审 M1 安全面（密钥配置、输入校验、SSE 协议、渲染安全）
- 收尾落档：进度/踩坑按《收尾记录协议》落全局记忆；契约同步检查

**闭环条件**：A 编号逐条全过（P1 降级项须留记录）+ 性能抽样报告留档 + 审查无未处理 P0/P1 + 契约零漂移（api.md 与实现一致）+ 落档完成。

## M1 之后的路线（本轮不展开）

M2 Deep = S5 复用同流程（B4 深度图 + degraded_writer + 快照链 Firecrawl→Crawl4AI，加载 `deep-agents` 技能，A27 深度拆分形态一并核验）→ A2/A3/A4；M3 KB（B5）→ M4 沙箱+评测（B6 归 eval-engineer，A' 方案）→ M5/M6 记忆（MB1-MB4）。每里程碑同样执行"阶段闭环门禁"。

## 通用纪律（每批次适用）

- 开工先 `wf declare <级别> --reason`；契约已过 gate
- TDD 红绿重构；unit 全 mock 零网络，integration 真 LiteLLM（`requires_llm`），凭证读 `.env.test`
- Langfuse 节点 span 独立可识别才算观察通过；Studio 不经 TurnRunner，节点不依赖运行时设施
- 契约变更走增量 diff 同步全部受影响方；安全敏感改动过 security-auditor
- 测试/lint/typecheck 证据是收尾硬门禁；每批收尾落档；**闭环未过不前进，不强制闭环**

## Verification（怎么确认做对了）

1. 批次 0：目录树逐项勾选；`uv sync`/`pnpm install` 成功；git 首提交入库
2. 批次 1：`docker compose ps` 全 healthy（含 crawl4ai）；五 dataId = 现行契约初值；四类 smoke 通过；R1/R7/R9/R11/R12 落档
3. 批次 2-6：每批闭环清单逐条留证勾选；`langgraph dev` 后 Studio 单步各节点；Langfuse 按 session=thread_id 见节点级 span
4. 批次 5：curl 断线重连带 Last-Event-ID 补帧；同线程第二问 reject 返 409001
5. 批次 7：浏览器实测四页 + A13 + A26~A28（截图留档）
6. 批次 8：A 编号逐条勾选 + 性能抽样报告留档