# Decision Model 意图识别 + LiteLLM 升级 立项计划（2026-11-18）

wf-level: T2 -- 跨域工单（基建/网关 + 后端图编排 + Nacos 契约 + 评测线），含契约增量预清单
wf-gate: contract

> **执行状态**：批次 0 已闭环（2026-11-18，dwfrun-b0565119，四件套全过，落档 docs/litellm-upgrade-batch0-20261118.md v1.2）；批次 1 R19 探针已 PASS（2026-11-18，报告 docs/research/decision-model-r19-probe-20261118.md——**端点定案=token-plan 同域同 Key，无需 Workspace ID，.env 预期零新增**，§10 用户参与点②③相应作废）；批次 2 待开工。

## 0. 背景与动机

用户提议：在 LiteLLM 新增「Decision Model」分组（百炼 `decision-model-preview`，限时免费），
用专用决策模型接管主图意图识别（现由 rewrite 节点 light 模型全职责承担），并建立
「数据集 + 评估器 + 热更迭代」的命中率评测闭环（目标 ≥90%~100%）。

调研结论（2026-11-18 本会话核查，Aliyun 官方文档 + LiteLLM 官方文档）：
- `decision-model-preview`：TypeSafe System One 协议（`POST /compatible-mode/v1/systemone`），
  一次前向并行返回多题结果（choice/noul/score），带完整概率分布 + confidence，延迟 ~53ms 级，
  输入 65,536 tok、输出长度 0（不生成文本）、RPM 1200 / TPM 2M、限时免费。
  官方适用场景含「智能体路由」「内容审核」——与本工单场景直接对口。
- LiteLLM v1.104.2 起 decisions 路由可用（`/v1/systemone` + `/v1/decisions` 双端点、两格式互转），
  v1.106.0 起 TypeSafe provider 增强；`typesafe/` provider 支持自定义 `api_base`，可指向百炼端点。
- 现网 litellm `v1.101.0`（DB 模式 ADR-014）无此能力，须升级。
- ⚠️ LiteLLM 2026-10-21 供应链投毒事件：v1.107.0 的 PyPI wheel 与 Docker 镜像（含 GHCR
  `litellm-database` 变体）被注入恶意钩子窃取凭据。**v1.107.0 之前所有版本安全**（现网
  v1.101.0 未受影响）；升级必须走 digest 钉版 + cosign 验签门禁（详见批次 0）。

## 1. 已定裁决（2026-11-18 用户确认）

| # | 裁决点 | 结论 |
|---|---|---|
| 1 | 网关形态 | litellm 1.106.4 `typesafe/` provider 声明 + app 侧薄客户端打网关 `/v1/systemone`；不自研 litellm handler、不 app 直连百炼 |
| 2 | 上游 Key | 复用 `DASHSCOPE_TOKEN_PLAN_API_KEY`（sk-sp-，Token Plan 专属）；`.env` 密钥零新增，仅新增端点变量 |
| 3 | 编排形态 | **串行先判**：rewrite 节点内前置 decision 步骤；strategy ∈ {direct, recall} 免 light 调用直达 synthesis；verify/web 轮 light 做瘦身版改写 |
| 4 | 命中率门禁 | golden set 总体 ≥90% 且 search_strategy 单类 ≥95%、无单类 <80% 塌方；shadow 双跑 agreement ≥90% 才全量切换 |
| 5 | 排期 | 两件套一起立项；升级批先行，意图识别批随后串行开工（safety 批 v1.32 在途落地提交之后） |
| 6 | 迭代闭环 | decision 题面（instructions/criteria）进 Nacos prompts dataId 热更；评估器长期在线，按 type_rules 调优先例持续迭代 |

## 2. 总体设计

### 2.1 拓扑零改动（对齐 ADR-012 哲学）

fast 子图 `rewrite → search_fanout` 条件边现状已按 `search_strategy` 四值路由
（direct/recall → synthesis 免检索；verify/web → 检索段）。本工单**不改图拓扑**：
rewrite 节点内部前置 decision 步骤，输出字段语义不变，条件边原样生效。

### 2.2 RewriteOutput 七字段的职责再分配

| 字段 | 归属 | 说明 |
|---|---|---|
| `query_type` | decision（choice，8 选项照 QueryType 枚举） | — |
| `answerable_from_knowledge` | decision（noul，P(yes)） | — |
| `search_strategy` | decision（choice，4 选项） | confidence < 阈值 → 回落 `web`（R17 第三重压缩数值化） |
| `safety` | decision（choice，5 值 none/danger/profanity/privacy/politics） | **architect 评审确认点**：B13 Layer 2 判定面从 light 迁 decision；语义不变（缺省 none 灰度、politics 一律 reject、仅成功路径判读），须 security-auditor 过 + 评测加 safety 维度。保守备选：首版 safety 留 light（direct/recall 轮 Layer 2 缺位靠 Layer 1 词表兜底） |
| `queries` / `time_range` / `memory_directive` | 留 light（文本生成面） | decision 输出长度 0，天然不可承接 |

### 2.3 decision 题表（System One questions）

四题一次前向并行返回：`query_type`(choice) + `search_strategy`(choice) + `answerable`(noul) +
`safety`(choice)。题面文本 = 现 type_rules 判据的迁移载体（R17 第一重压缩从 light 提示词迁至
decision criteria，机制不变：Nacos 热更 + fixture 测试 + 评测对比）。附加 `other` 兜底选项
（百炼官方建议）。

### 2.4 失败语义与兜底（不劣于现状）

decision 调用失败/超时/结构异常 → **回退 light 全职责路径**（含分类+safety），行为等同现状，
零能力回退；stage done.extra 留痕 `decision_source=decision|light_fallback`；不置 degraded
（对齐 rewrite 兜底口径，api.md §7.2 枚举外语义不入 degraded_reason）。网关侧单上游不配
fallback（对齐 embedding/rerank 处理）。Kill switch = Nacos `model.mapping.decision` 键置空/
删除 → 全量走 light 路径（灰度安全：缺省禁用，存量轮零行为变化，沿 search_strategy v1.20 先例）。

### 2.5 观测与成本

- Langfuse：decision 调用挂独立 generation（model=decision-model、usage.input_tokens、
  latency_ms 响应自带、probabilities/confidence 落 metadata）；shadow 阶段双结果对比落 metadata。
- 成本：litellm spend logs + `x-litellm-response-cost`；标价 0（限时免费口径注记），
  转收费前复核套餐覆盖面（挂成本观测）。
- P50 影响：direct/recall 轮预期提速（省 1.5~6s light 调用）；verify/web 轮 +~50ms。
  切换后按性能铁律跑 P50/P90 基线复测留档。

## 3. 批次 0：LiteLLM 升级批（T1 基建批，先行独立落地）

**范围**：`deploy/docker-compose.yml` image `litellm/litellm-database:v1.101.0` → `v1.106.4`。

硬性安全门禁（供应链事件背景，security-auditor 必过）：
1. digest 钉版：image 带 `@sha256:...`，与官方 release 公示 digest 比对；
2. cosign 验签：新公钥指纹 `e3fe3e08c44b16407fe0b37c43b4f8b603b3a0f4ee74c29e647bc022d762c2c0`
   （10-23 换钥后批次；11-15 起 keyless cosign 按官方 verify 命令）；
3. 升级前备份 litellm 库（postgres 第三库；DB 模式 alembic 迁移只进不退，回退预案写明处置）；
4. 升级后冒烟四面：`make smoke-infra` litellm smoke + 9 别名 fallbacks 链 + 虚拟 key +
   OTel→Langfuse span + session_affinity pre-call check。

回退预案：image tag 回 `v1.101.0`；若迁移已执行且不可逆，按备份恢复库。
顺手项：扫描 1.101→1.106.4 changelog 的 breaking changes 清单留档。

**DoD**：冒烟四面全绿 + 双评审（code-reviewer + security-auditor）零 P0/P1 + 文档巡检
（ARCH §3.1/§6.7 版本行、DEV、PROGRESS、compose 头注）。

## 4. 批次 1：Phase 0 接入性验证（R19 探针，不动契约）

新增 R19 探针清单（结论落档 `docs/research/decision-model-r19-probe-20261118.md`，
verdict=pass 才进批次 2）：
1. litellm `typesafe/` + 自定义 api_base 对百炼 systemone 端点连通性与鉴权（Token Plan Key）；
2. `.env` 新增 `DASHSCOPE_DECISION_BASE_URL`（workspace id 用户从百炼控制台取）；虚拟 key
   `decision-model` 白名单（litellm UI 建，用户操作项，沿 Halo 先例）；
3. 中文分类小样本：20~30 条 golden 起步（含 recall/navigational 薄类 + safety 正负例），
   对照现 light 路径判定，粗估对齐度与 confidence 分布；
4. 延迟分布（P50/P95）与 65,536 tok 长输入行为；
5. litellm 方言 × 百炼原生 schema 差异面实测（题数/选项数/透传无损性；box/images 等百炼
   没有的类型不在用例面）；
6. 网关 OTel callback 对 decisions 路由的 span 覆盖确认（应用侧 SDK generation 兜底不受影响）。

## 5. 批次 2：接线 + Shadow 双跑 + 评测门禁（T2 主体）

### 5.1 后端接线（契约增量产出并过 `wf gate contract --pass` 后开工）

- `app/agent/decision_client.py` 薄客户端（httpx 打网关 `/v1/systemone`，超时/失败分类对齐
  guards 语义，deadline-aware 沿 rewrite 现口径）；
- rewrite 节点前置 decision 步骤 + confidence 阈值闸（阈值进 Nacos `fast.decision.*`）+
  light 兜底路径 + `decision_source` 留痕；
- light 瘦身：type_rules 判据文本迁出（prompts `fast.rewrite` 减面 + `fast.decision.*`
  增面双源同步）；strategy ∈ {direct, recall} 时跳过 light（省调用）；
- Langfuse generation 接入 + shadow 双跑开关（Nacos 单键：shadow 模式下 decision 只观测
  不生效，双结果落 trace metadata）；
- 发布次序硬前置（B12 教训）：**先发 Nacos（model.mapping.decision 键 + prompts
  fast.decision.* 题面键）再上代码**。

### 5.2 评测线（eval-engineer，与 5.1 并行）

- golden set：≥100 条起步（题型覆盖 ≥10/类、safety 正负例 ≥20、recall/navigation 薄类足额），
  来源 = 线上 trace 回捞（metadata 已挂 search_strategy/query_type）+ 离线构造（B9/B12 判据
  + A32 题型分布 + safety 词表正负例）；四维标注 expected（含 safety）；
- 离线判分：自有进程 exact-match（A' 契约形态 a）+ 分题型混淆矩阵留档；
- 线上评估器：复用 strategy_fitness / query_type_fitness；shadow agreement 观测；
  safety 维度评估器视 B13 现状补齐；
- **切换门禁（裁决 4 数值）**：总体 ≥90% 且 search_strategy ≥95%、无单类 <80%、
  shadow agreement ≥90% → 才允许批次 3 灰度。

**DoD**：`make test` 全绿 + 覆盖率 ≥80% + `make typecheck` 0 + e2e 全量；评测门禁报告落档
`docs/`；双评审（code-reviewer + security-auditor[safety 迁移面]）零 P0/P1；契约六件升版
同步（见 §7）。

## 6. 批次 3：灰度切换 + 性能复测

- Nacos `model.mapping.decision` 键启用（热更，不发版）→ 线上观察命中率/agreement/兜底吞噬率
  （decision 失败率目标 <1%，吞噬率挂 M4 观察）；
- P50/P90 基线复测留档（性能铁律：基线→优化→复测；预期 direct/recall 轮提速、整体不劣化）；
- 观察窗通过 → 全量；异常 → kill switch 单键回退（零发版）。

## 7. 契约触点预清单（批次 2 实施时产出增量 diff，禁止私改冻结字段）

| 文件 | 增量 | 备注 |
|---|---|---|
| `deploy/litellm/config.yaml` | decision-model 条目 + 头注别名清单 9→10 | 批次 0/2 两段 |
| `.env` / `.env.example` | +`DASHSCOPE_DECISION_BASE_URL` | 密钥零新增 |
| `docs/contract/nacos-config.md` | model.mapping +`decision` 键；prompts +`fast.decision.*` 题面键；§5 别名矩阵 | 发布次序注 |
| `docs/ARCHITECTURE.md` | §4.1 rewrite 行（decision 前置步骤）、§6.7 网关、R17 注（判据载体迁移机制不变）、新 R19 风险行、ADR-015（决策模型接入，拓扑零改动第四例） | — |
| `docs/DEVELOPMENT-PROCESS.md` | §3.3 R19 探针条 + 工单编号 | — |
| `docs/contract/api.md` | 预期零变更；stage extra 若新增 `decision_source` 观测字段则钉文字 | 前端 stage_key 面不变 |
| `frontend-api.md` / `api.d.ts` | 预期零变更（前端零改动） | 判定后记「无需更新」+理由 |
| `data-model.md` | 零 DDL 预期 | 判定后记「无需更新」+理由 |
| `docs/PRD.md` | 视需要挂 OQ（兜底吞噬率观察等归 M4 评测线） | architect 定 |
| `README.md` / `docs/PROGRESS.md` | 收尾巡检 | §10.1 硬规范 |

## 8. 风险与护栏

| 风险 | 护栏 |
|---|---|
| litellm 供应链（10-21 投毒事件，第二次） | 批次 0 四件套硬门禁 + security-auditor；此后升级一律 digest+cosign |
| preview 限时免费可能转收费/下线/协议变更（R8/R16 同型） | kill switch 单键回退 + 成本观测 + 转收费前套餐覆盖复核 |
| safety 判定面迁移（B13 新面，刚调优过） | architect 评审 + security-auditor 必过 + 评测加 safety 维度 + 保守备选（留 light） |
| 中文分类质量未实测 | 批次 1 探针先行，verdict=pass 才接线 |
| recall 误判（R17） | 判据载体迁移机制不变：criteria 热更 + 空历史护栏（代码不变）+ confidence<阈值回落 web（数值化）+ M4 误判探针 |
| direct/recall 误判直答风险高于误检索 | search_strategy 单类门禁 ≥95%（最高）；置信阈值初值批次 2 实测定 |
| 依赖升级面 | langgraph/langchain 等应用依赖零变更（升级仅网关容器镜像） |

## 9. 排期与依赖

```
safety 批 v1.32 落地提交（在途，前置）
  → 批次 0 升级批（T1，devops-engineer + security-auditor）
  → 批次 1 R19 探针（调研批，不动契约）
  → 批次 2 接线+评测（T2 主体，architect 出契约 → wf gate contract --pass → 前后端…本单无前端 → 后端+评测线并行）
  → 批次 3 灰度+性能复测 → 收尾巡检
批次 4（可选扩面，另行裁决）：deep planner 顺带分类迁 decision / safety 题表扩容 /
  decision model 微调（百炼支持）/ /v1/decisions 统一格式面评估
```

## 10. 用户参与点清单

1. 批次 0：无（全自动）；升级窗口避开 make dev 运行时段；
2. 批次 1：提供百炼控制台 workspace id；litellm UI 建虚拟 key `decision-model`；
3. 批次 2：Nacos 发布凭据注入（沿 nacos_prompts_hotswap.py 先例）；golden set 抽查复核；
4. 批次 3：灰度启用与全量切换两次确认；git 提交（AI 零写操作，纪律不变）。
