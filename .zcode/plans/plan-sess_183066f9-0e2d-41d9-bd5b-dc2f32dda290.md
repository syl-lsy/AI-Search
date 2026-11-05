wf-level: T3 -- ai-search 契约增量（主图路由编排 ADR-012）+ M1 实施计划 v4 修订
wf-gate: contract

# 主图模式路由编排：契约增量 + M1 计划 v4 修订

## Context

**用户诉求（2026-11-04）**：在两图外层加一个带路由判断的主图，fast/deep 作为子图，路由按模式分发，预留扩展余地。并问建图时机：①前端点击模式时建图，还是 ②提交问题时建图？

**建图时机裁决（两选项不对立，实现上合流）**：LangGraph 图的编译在**服务启动时一次完成**（主图+两子图 compile 后注册 GRAPH_REGISTRY，进程内单例复用）——「点击建图」「提交建图」都不是后端事实。真实时序：
1. 前端点击快速/深度 = 纯前端本地状态（REQ-007：模式对下一轮生效，不调后端）；
2. 提交 = POST /turns 携带 mode（api.md §2.1 必填，fast|deep）→ TurnRunner invoke **主图单例** → 主图路由条件边读 state.mode 分发到子图。

即采用**选项 2 的语义**（提交那一刻按当轮 mode 路由进入子图），点击只是设参数；每轮独立路由——同线程换模式追问（REQ-017/018）天然支持。

**现状空白（探索确认）**：ARCH §6.1:503 只有 `graph.astream(stream_mode=…, subgraphs=(mode=="deep"))`，graph 变量按 mode 从何而来**无规范**（选图逻辑隐式散落）；REQ-017 场景切图（深度后追问构造 Fast 图输入）又是一处应用层 if。主图路由恰好把这个空白收口为显式契约，且 Langfuse trace 获得统一 root run、Studio 获得全景入口。

## 方案定案（主图形态）

- **启动时编译三图**：`graphs/fast.py` / `graphs/deep.py` 子图（现行编译产物与 §4.1/§4.2 拓扑零改动）+ 新增 `graphs/main.py` 主图壳；`graphs/registry.py` 维护 GRAPH_REGISTRY（注册名→编译图）。
- **主图 = 薄壳路由图**：`START → route_by_mode 条件边 → fast|deep 子图节点 → END`。路由函数读 Agent Spec `routing` 映射 ∩ GRAPH_REGISTRY；无 LLM、无 IO、亚毫秒、**不发任何 SSE 事件**。
- **state 契约**：主图 state 为投影薄壳（输入投影 + 输出回填同名键）；FastState/DeepState 子图内部定义**不变**（ARCH §4.1:362 / §4.2:426 现行）；子图以编译产物直挂节点（LangGraph input/output schema 映射）。
- **兜底**：mode 非法已被 API 边界拦截（400001）；routing 映射缺失/读取失败/指向未注册图 → 内置默认表路由 fast + reload 时交集校验告警一次。
- **扩展原则（用户要的扩展余地）**：未来新模式 = ①新子图模块编译 → ②registry 注册 → ③Nacos `spec.routing.modes` 加映射行（热更）→ ④若用户可选则 api.md mode 枚举 + 前端选择器扩展；**主图拓扑零改动**。auto 智能路由（路由器内 LLM 分类）仅作预留扩展点，PRD 未立项不实现。
- **观测**：Langfuse trace root = 主图 run（层级 root→子图→节点更完整），session=thread_id 不变；SSE 事件序列零变化（api.md 10 种事件、stage_key 枚举均不动）。
- **deepagents 兼容**：deep.py（M2）编译产物为 LangGraph 兼容图，注册进 registry 即挂载。
- **Studio**：langgraph.json 主入口改指主图（全景层级），fast/deep 保留为调试直入口。

## 契约增量（architect 执行）

1. **ARCHITECTURE v1.19 → v1.20**（~14 处编辑）：
   - 版本行 prepend v1.20 delta（链式保留历史）
   - §4 标题改「图编排（主图路由 + 两张模式子图）」+ 新增 §4.0 主图编排小节（形态/路由函数/registry/兜底/扩展原则，按上文定案）
   - §3.1 三处：:239 graphs/ 行补 main.py + registry.py；:173 langgraph.json 主图主入口；:297 新增层措辞「两图」→「主图+两子图」
   - §6.1:503 → `main_graph.astream(stream_mode=[…], subgraphs=True)`（主图壳下恒开，子图节点事件冒泡）；:513 解释段同步 + messages 过滤措辞精确化：「主图 synthesis/writer 节点」→「fast 子图 synthesis / deep 子图 writer 节点」（消「主图」歧义）；:454 REQ-017 → TurnRunner 构造主图输入（mode=fast，路由至 fast 子图）
   - §6.2 末补路由兜底一句（routing 读取失败→内置默认表）；§6.6 补 trace root=主图一句
   - §8 工单表：B2「先用桩图」→「桩主图」；B3 行补「主图壳+registry 先行」；B4 行补「deep 子图注册进主图 registry」
   - ADR-012 新增：背景=选图规范空白+用户 2026-11-04 诉求；决策如上；备选=TurnRunner 应用层 if/else 选图（观测缺统一 root、扩展散应用层）、前端按模式分端点（协议分叉）、Agent Server（ADR-001 已排除）；后果含正（单一调用面/统一 trace 根/扩展两步走）负（state 映射+1 层、subgraphs=True 恒开、interrupt/cancel 传播需实测）
   - §10 R14 新增：子图边界三风险——state 投影映射、interrupt/cancel 跨子图传播、subgraphs=True 事件冒泡与 messages 过滤——M1 批次 3/5 显式验证
   - 输入基线登记本轮裁决；日期行 2026-11-04
2. **nacos-config v1.12 → v1.13**（4 处）：§3 spec JSON 顶层新增 `routing` 节（`default_mode:"fast"` + `modes:{"fast":"fast","deep":"deep"}`——deep 行 M2 图注册后生效，未注册按 default 兜底）+ 键位语义注（消费方=主图路由函数/TurnRunner）；§0 总览表 spec 行描述补「与模式路由」；§8:315 init 清单补 v1.13 条目；版本行 delta
3. **api.md / data-model.md：零变更**（显式声明：路由不发事件、mode 枚举不变、无表结构变化——协议契约零漂移）
4. **PRD 就地勘误（不升版）**：§1.2:40「两张独立 LangGraph 图」→「主图模式路由 + 两张模式子图（ARCHITECTURE §4.0/ADR-012）」；版本行同日修订注记录
5. **frontend-api 就地勘误（不升版）**：§1 AnswerView 行请求体显式补 `mode`（现仅由 api.md §2.4 继承表述，防追问漏传——探索发现的文档不对称）
6. **DEVELOPMENT-PROCESS v1.12 → v1.13**（~5 处）：版本行 delta；§2:116 S1 行「B3 主图壳 + fast 图逐节点」；§5.1:276 补「B3 第 0 步：主图壳先行闭环（路由单测），再逐节点注册进 fast 子图」；§5.4 DoD 第 3 条「注册进对应子图（经主图壳可达），Studio 主图入口可单步可重放」；§8 与 :8/:465 ARCHITECTURE v1.19→v1.20 引用、日期行

## M1 实施计划 v4（相对 v3 的批次 delta；批次 0-8 结构、四条执行原则、阶段闭环门禁全部不变）

- **批次 2（B2）**：桩图 → **桩主图**（router 条件边 + 桩 fast 子图秒回），TurnRunner 统一 invoke 主图单例——事件管线验证提前在主图壳下进行
- **批次 3（B3）更名「主图壳 + fast 子图五节点」**：第 0 步主图壳闭环（main.py + registry.py + route_by_mode + config_center 读 spec.routing；单测四条：fast 命中 / 未注册 mode 兜底 default / routing 读取失败回落内置表 / 交集缺失 reload 告警一次）→ 五节点照旧逐个注册进 fast 子图（§5.4 九条 DoD 不变）→ 整图收口 = Studio 主图入口 → fast 子图端到端（真 LLM+真搜索源，含最小检索与降级链）
- **批次 5（S2）新增两条闭环项**：① subgraphs=True 恒开下 messages 过滤规则实测（仅 fast 子图 synthesis 节点产 token/thinking）；② 取消/interrupt 主图壳下透传子图（guards 节点边界感知）实测——R14 对应
- **批次 8**：契约零漂移基线更新为 ARCH v1.20 / nacos v1.13
- **M2 路线注**：deep.py 编译产物注册 registry 即挂载（主图零改动）；REQ-017 追问 = TurnRunner 构造主图输入（mode=fast）

## 执行顺序与 Verification

1. architect 按清单落契约增量（ARCH v1.20 → nacos v1.13 → DEV v1.13 → PRD/frontend-api 就地勘误）
2. 交叉引用复扫：grep「两张独立 LangGraph|fast/deep 两图入口|先用桩图|subgraphs=(mode」确认零残留；三文档版本链与互引一致
3. code-reviewer 分级审查契约 diff，P0-P3 全修后复扫确认
4. M1 计划 v4 落档（新 plans 文件）+ 按《收尾记录协议》落记忆并更新 MEMORY.md 索引
5. 实施按批次 0-8 逐批开工：每批先 `wf declare`、闭环清单不满足不前进、契约零漂移（api.md v1.8 不动）