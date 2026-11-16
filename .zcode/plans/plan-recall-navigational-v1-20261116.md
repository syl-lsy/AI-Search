# 工单计划：recall 回顾分支 + navigational 题型（v2，2026-11-16）

> v1→v2：吸收业界调研（docs/research/conversation-memory-recall-research-20261116.md）——§3.4 截断策略升级为「首段+标题骨架+近轮放宽」配方（替换前缀硬截）、§3.1 补时效类排除判据、§1 补里程碑裁决（维持原排期）、§6 挂账更新（replay 降级 + 滚动摘要条件工单）、§4/§7 同步。

> wf-level: T2 -- 跨域：Nacos prompts 契约 + 后端图编排 + 契约文档面（前端仅 api.d.ts 枚举小改）
> wf-gate: contract

## 0. 工单定位与根因溯源

**触发**：用户实测同线程问「你之前聊了什么」，模型答「不知道之前说了什么」。

**根因（本会话代码实证）**：历史装配链路存在但只接了一个消费点——
- `turn_service.py:139` 装配最近 6 轮（q + 答案头 200 字）注入 `state.history`；
- `fast_rewrite.py:161` history 进 rewrite 提示词，职责仅为**指代消解**；
- `synthesis.py:262-273` 回答窗口占位符集**无 history**——「复述/回顾上文」类对话元问题无路可走（direct 直答看不到历史；web 检索搜公开网页更搜不到线程私有内容）。

**业界对照（2026-11-16 联网调研，详见 docs/research/conversation-memory-recall-research-20261116.md，35 条分级来源）**：头部产品主流 = 「全量重放 + 窗口溢出平台兜底」（静默截断/自动摘要压缩），本项目「历史不进回答窗」比所有竞品激进——recall = 回归业界默认形态；阈值滚动摘要（0.75~0.85 触发 + 近轮原文保留）是唯一规模验证的摘要形态，每轮即时摘要为公认反模式；「重复提问回放历史答案」零产品先例且被各产品当 bug 修——recall 必须走「用历史重新生成」，不做缓存回放。

**模式**：B9 `search_strategy` 工单同款扩展（rewrite 输出扩容 → 子图内条件边 → synthesis 变体 → 主图拓扑零改动，ADR-012 子图内扩展第二例）。

## 1. 用户裁决记录（2026-11-16，四项）

1. **分层落地**：P0 = recall 保守形态（回顾/复述/重复提问 → synthesis 用历史整理回答，省检索不省生成）；**零生成 replay（历史 answer_md 直接投影成新轮）挂 REQ-046 实验工单**后续做（前置：embedding 相似度基建 + 时敏题排除规则集），不阻塞本批。
2. **排期**：先收 M2 批次 0（用户 git 提交工作区现有改动），本工单独立批次插入，收干净后回 M2 Deep 主线。
3. **navigational 第八题型顺带落地**（清偿 B9 遗留「navigational 题类 M2 前裁决」，含 verify clamp 1 伴生规则）。
4. **契约载体**：`search_strategy` 加第四值 `recall`（业界证据：检索策略与题型正交，B9 调研结论）；navigational 走 `query_type` 第八题型。
5. **里程碑裁决（2026-11-16 业界调研后）**：M5/M6 维持原排期（M3/M4 是记忆价值验证前置，当前痛点 100% 会话内，本工单+滚动摘要条件工单已覆盖）；「轮次滚动摘要」挂 M2 后**条件工单**（三触发条件见 §6）；零生成 replay 实验挂账**降级**（业界证据负面：回放零先例、重复应答当 bug 修）。

## 2. 契约增量清单（architect 定稿 → `wf gate contract --pass`）

| 契约 | 变更 | 判定 |
|---|---|---|
| `nacos-config.md` §3 | `fast.rewrite`：strategy 枚举 +recall 与判定规则段、`common.type_rules` +navigational、`type_query_map` +navigational:1；`fast.synthesis`：+recall 变体段（行级切分，B9 同款机制）+ 新占位符 `history_block` | 升版 |
| `api.md` §7.4 | stage extra `strategy` 枚举 +recall；recall 轮 `sources=[]` 钉文字（direct 零源轮同款钉法） | 升版 |
| `frontend-api.md` | `StageExtra.strategy` 枚举 +recall；recall 轮渲染口径（复用 B9「无来源轮整块不渲染」） | 升版 |
| `frontend/src/types/api.d.ts` | `StageExtra.strategy` 补 `"recall"` | 随批 |
| `ARCHITECTURE.md` §4.0/§4.1/§6.5 | 条件边三目标→四目标（recall→synthesis）；§6.5 历史装配 recall 增强口径；§10 新风险注（recall 误判）；§4.1 检索策略段同步 | 升版 |
| `PRD.md` | REQ-006 验收面补回顾型用例（A 系列增量）+ navigational 题型登记 + replay 实验挂账注 | 升版 |
| `data-model.md` | **零变更判定**（无新表/新列；历史 SQL 不变） | 判定记录 |

SSE 协议/事件/字段/错误码零变更；前端逻辑零变更（仅枚举镜像 + 既有无来源门控复用）。

## 3. 技术方案

### 3.1 rewrite（`app/agent/nodes/fast_rewrite.py`）

- `SearchStrategy = Literal["direct", "verify", "web", "recall"]`；`QueryType` + `"navigational"`。
- 提示词规则（Nacos 键位）：recall 判定 = 回顾复述类（「之前聊了什么」）/ 重复提问类（语义重复，**重新生成不回放**）/ 接续整理类（「接着刚才说」）→ `recall`；**硬规则：时敏/事实新题绝不 recall**（type_rules 判据：weather/news/price 等时效类排除 recall 短路，时效重问 → web 优先）；**护栏：history 为空时 recall 判定回落 direct**（无历史可回顾）。
- 兜底不变：缺省/非法/修复回喂失败 → `web`（`FALLBACK_SEARCH_STRATEGY`，灰度安全口径沿用——存量轮零行为变化）。
- `_aligned_answerable` 扩分支：`recall → answerable=False`（历史 ≠ 知识，不宣称可从知识作答）。
- `_clamp_queries`：`type_query_map` 加 `navigational → 1`（clamp 1 伴生规则，表驱动自动生效）。

### 3.2 条件边（`app/agent/graphs/fast.py`）

- route 映射：`recall` 与 `direct` 同落 synthesis（`→ synthesis` 目标复用，拓扑零改动）。
- 主图零改动（`spec.routing` 不涉——子图内扩展，B9 同款）。

### 3.3 synthesis（`app/agent/nodes/synthesis.py`）

- `_resolve_strategy` 扩 recall → 模板变体段（行级切分，B9 三态变体同款机制）。
- 新占位符 `history_block`：**仅 recall 变体注入**，direct/verify/web 变体恒空串（token 纪律——非 recall 轮零增量）。
- recall 变体段钉文字要点：历史内容不是网页来源、**禁止 [n] 引用标注**、按对话记录整理回答、重复提问优先复用先前答案要点、历史记录若与当前问题无关如实说明。
- 变体段缺省兜底：模板无 recall 段时回落 direct 形态（灰度安全，B9 同款机制）。
- messages 形态不变：`[SystemMessage(模板), HumanMessage(query)]`。

### 3.4 历史装配（`app/services/turn_service.py`）——结构感知截断配方（v2，调研报告 §5/§7 案 A）

- SQL 不变（`history_statement` 已取全文 `answer_md`）。
- 装配双通道（实现常量，评审可调）：
  - `history`（现行不动）：q 全文 + a 头 200 字 → rewrite 消费；
  - `history_recall`（新增）→ synthesis recall 变体消费，每轮 = **问题全文 + 答案首段 + markdown 标题骨架**（H2/H3 标题行 + 每节首句，纯函数解析）；最近 1~2 轮放宽为更大预算（近轮近全文、远轮骨架的简单动态分配）；总软顶 **6000 字符（≈4K token）**，溢出丢最旧。
  - 业界定位如实注记：骨架截断无产品先例（工程创新），按「fixture 测试 + M4 评测探针（回顾类复述完整度）」自证，不引外部数字背书。
- state 契约：`FastState`/`MainState` 投影 + `history_recall`（R14① 投影锁死测试同步）。

### 3.5 观测

ARCH v1.45 检索策略观测三通道沿用：synthesis span metadata / stage extra / AgentLog 携 `strategy=recall`、`query_type=navigational`。

## 4. 测试与门禁（TDD 红绿，失败语义先行）

- **unit**：输出 schema fixture（recall/navigational 合法样例）；判定规则（回顾类→recall、时敏事实类拒 recall、空历史护栏回落 direct）；两兜底→web；`_aligned_answerable` recall 分支；条件边映射四目标；synthesis 变体渲染（history_block 仅 recall 注入、其余恒空、禁引用句在场）；clamp 1；**骨架截断配方 fixture**（首段/标题/每节首句解析、近轮放宽、总软顶溢出丢最旧）。
- **integration**：SSE recall 轮全链路（stage 顺序 = rewrite→synthesis、`sources=[]`、done↔REST 对账）。
- **e2e**：回顾型契约用例（真模型：「你之前聊了什么」→ 答案含历史轮内容关键词、无 [n]、sources 空）；navigational 用例（外网依赖形态同 B9 direct 用例，环境性 skip 口径沿用）。
- **前端**：api.d.ts 枚举镜像 + 无来源轮门控回归（既有测试）。
- 门禁：`make test` / `make typecheck` / `make test-e2e` / `make test-frontend` 全绿，覆盖率 ≥80%。

## 5. 发布次序（B9 硬前置沿用）

1. 契约冻结 → `wf gate contract --pass`；
2. TDD 后端实现全绿（unit→integration→e2e）；
3. **Nacos prompts 先发**（`NACOS_INIT_FORCE=1` + 管理员凭据 `NACOS_INIT_USER/PASS` inline 注入；production L4 回落拒启动教训）；
4. 代码上线（`make dev` 重启）；
5. 活体验证（回顾轮 + navigational 轮真机冒烟 + 用户实测「你之前聊了什么」）；
6. 双评审（code-reviewer + security-auditor）→ 文档巡检（PROGRESS + 七契约）→ 用户 git 提交。

## 6. 挂账（不阻塞本批）

- **滚动摘要条件工单（M2 后观察触发，调研报告 §7 案 B）**：轮结束后异步 job 检查 thread 累计 token，超阈值（0.80~0.85 起步）对旧轮生成 rolling summary 存库，`turns` 原文不动；装配读「摘要 + 最近 N 轮原文」。**三触发条件满足其一才立项**：①fast 多轮长 thread 占比实测上升 ②recall 轮需要 >6 轮历史才能答好（评测探针反馈）③deep 模式上下文压力实测出现。为 M5 长期记忆预铺抽取输入管道（业界短/长期共享组件=摘要管道+存储）。
- **零生成 replay（降级，2026-11-16 调研）**：业界证据负面——「会话内语义命中回放历史答案」零产品先例、重复应答被各产品当 bug 修（报告 §6.2）；REQ-046 实验口径挂账保留但优先级最低，M4 评测线建立后亦不主动立项，除非用户显式裁决。

## 7. 风险

| 风险 | 缓解 |
|---|---|
| recall 误判（该搜的题拿旧历史答 → 答案过时） | 提示词硬规则「时敏/事实新题绝不 recall」+ 空历史护栏 + 兜底 web + M4 评测线观察误判率 |
| 骨架截断丢细节（业界无先例，无外部数据背书） | fixture 锁配方 + 首段保结论先行 + 标题骨架保「知道丢了什么」+ M4 评测探针（回顾类复述完整度）；不足则触发 §6 滚动摘要条件工单 |
| recall 注入 token 膨胀 | 总软顶 6000 字符 + 仅 recall 变体注入 + Nacos 热便可调 |
| recall 轮无 [n] 的契约一致性 | api.md 钉文字 + 前端无来源轮形态复用（B9 已活体验证同形态） |
| navigational 题型误判面 | 兜底 web 灰度安全 + clamp 1 仅表驱动 + 评测线观察 |
