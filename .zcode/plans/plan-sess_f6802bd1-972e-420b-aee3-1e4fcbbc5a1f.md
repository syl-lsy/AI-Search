wf-level: T2 -- 理由：跨前后端两域（后端 rerank 语义/synthesis 调用面 + 前端 CSP/构建基建/Makefile）；契约零变更（rerank 归位走 ARCH v1.41 已钉二分口径；CSP 放宽对齐 PRD 现行 http/https 白名单——用户已裁决方向）

# 批次 9「M1 后专项」计划（四条一次性收口）

## Context

M1 已带注记达成退出，退出时挂了 4 条专项 + 1 条终审 P2（fast 非空答案契约零活跃 e2e 覆盖——后端 e2e 三契约用例全 skip 挂牌，`make test-e2e` 字面绿≠全链路）。本批一次性收口：**核心目标 = 后端 e2e 三契约用例摘牌复跑全绿 + 性能保持 P50≤30s 且慢轮收敛 + CSP 对齐 + 依赖 CVE 机扫入口建立**。

## 专项② rerank 兜底 degraded 语义归位（后端，先行，最小）

- 依据：ARCH v1.41 §6.2 已钉二分口径（枚举内降级两侧可见；非枚举机制兜底只落 stage extra + errors[] + AgentLog，**不置 degraded**）——rerank 兜底当前违反②口径：`fuse_rerank.py:271` `degraded=True` → `turn_service.py:856` 派生 `source_failure` 误标。
- 改动：rerank except 段（`fuse_rerank.py:263-273`）移除 degraded 置位，保留 `errors[].reason="rerank_fallback"` + stage(search,done,extra) 留痕 + 补 AgentLog 埋点（对齐 rewrite 先例）；顺手修 `state.py:137-138` 注释枚举错误（`"timeout|budget|canceled"` → `"timeout|budget|source_failure"`）。
- TDD：红测=rerank 兜底触发（stub 注入失败）时 `state.degraded=False`、errors/stage extra 留痕、turn_service 不派生 degraded_reason。
- 契约零变更（ARCH v1.41 注明「api.md §7.2 枚举与事件面零变更」）。

## 专项③ CSP 对齐 + 生产 build 验证（前端+基建）

- `frontend/index.html:15` CSP `img-src 'self' https:` → `'self' http: https:`（用户已裁决方向；`index.html:6-12` 设计注释同步更新），零契约变更（PRD:165/5.3.4 现行「仅允许 http/https」）。
- Makefile 新增 `build-frontend` target（`pnpm --filter frontend build`——scripts 已存在 `build: vue-tsc --noEmit && vite build`）。
- 生产验证（闭环安全审计 R2）：build 后核对 `dist/index.html` script 标签全为 src 外链（无 inline）→ `vite preview` 起服 + playwright 一条冒烟（页面渲染正常 + console 无 CSP violation 报告）→ 结论回写 `docs/security-audit-m1-batch8.md` R1/R2 状态。
- 顺手勘误：该审计文档「ALLOWED_TAGS 31 标签」→ 实测 37 标签（`renderer.ts:43-47`）。

## 专项④ 安全 P3 跟踪闭环（基建+文档，无行为代码）

- Makefile 新增 `audit` target：前端 `pnpm --dir frontend audit --registry=https://registry.npmjs.org`（npmmirror 无 audit 端点，官方源直连）；Python 侧 dev 组加 `pip-audit` + `uv run --package search-api pip-audit`。
- 首跑基线结果回写 `docs/security-audit-m1-batch8.md` R4 更新节；官方源网络不可达时如实记录（钉版+lockfile 缓解面不变，机扫通道建立为目的）。
- R3 认证触发器留档补强：AGENTS.md 遗留与 m1-exit §后续动作标明「M2+/公网/多用户部署前必办阻断项，单点替换 `app/dependencies.py:75-82` get_current_user」（本批不加认证代码）。

## 专项① synthesis 提速 → 摘牌复跑（后端+提示词，压轴，核心）

杠杆组合（**计划批准即授权**；均不动模型映射与 `fast.turn_timeout_s=60` 预算）：
1. **关 thinking**：`synthesis.py:55-69` build_chat_model 调用面按 rewrite 先例（`fast_rewrite.py:112-115`）补 `extra_body={"think": false}`——chat-standard 通道（云端 qwen3.7-plus）的生效键形态需探针实测（`think` vs `enable_thinking`，生效证据=Langfuse thinking 事件消失）；改动前先全量跑 R8 护栏（`test_reasoning_chat_model.py` 17 条）。
   - 依据：perf 报告实证 thinking 约占 synthesis 段 74%、慢轮 top5 100% 由 synthesis 驱动；§6.2 组合预期「关 thinking 档 P50 估 21~25s」。
2. **篇幅软引导收紧**：Nacos `fast.synthesis` 热更——事实型「从简直答」篇幅指引收紧（给目标字数带）；**双源同步**改内置默认 `app/agent/prompts/fast/synthesis.txt` + `defaults.py:30`（`test_prompts.py:63-66` 逐字节一致锁会红，必须同步）；六占位符变量集不变（`test_prompts.py:50-54` 锁）。
3. **复测采样**：A+B ≥20/场景 + C 多面 5 次（方法同 `docs/perf-sampling-batch8.md` §1），报告落 `docs/perf-sampling-m15.md`（含与批次 8 复测对照）。目标：主口径 P50 维持 ≤30s（预期大幅余量）+ 慢轮 max 显著下降。
4. **摘牌复跑**：逐条删 `test_fast_round_contract.py` 三处 skip（`:289/:304/:322`，断言体与题面原样保留不得放宽）→ `make test-e2e` 全绿。个别用例仍红时：单条复跑甄别，确属 synthesis 残余的改回挂牌并记录（不放宽断言）；多面题若仍熔断→如实记录（后续再议，预算与断言都不动）。
5. 注记：关 thinking 后 thinking 事件消失 = 契约设计内（api.md §7.2 产生条件不满足，前端已按「无事件不渲染」实现）；答案质量归 M4 评测线观察（遗留⑥观测点）。

## 执行形态与门禁

- 动态工作流单批次（批次 9「M1 后专项」）：backend-developer（②①代码面+探针）、frontend-developer（③）、test-engineer（复测采样+摘牌判定+门禁）、code-reviewer 收尾；契约/预算敏感点 escalation 纪律照旧。
- 终局门禁全绿：`make test` / `make test-e2e`（**3 契约用例活跃全绿**）/ `make typecheck` / `make test-frontend` / `make build-frontend`（新）。
- 巡检：PROGRESS 批次 9 行；ARCH v1.41「残留观察」句→已归位核验注（无语义增量则就地勘误不 bump）；m1-exit-batch8.md §5.3 摘牌专项状态更新；README 补 build-frontend/audit 命令；AGENTS.md 专项清单转完成态。

## 验证（端到端）

1. `make test-e2e`：三条契约用例活跃全绿（multifacet/factoid/en_language_follow，断言面=stage 顺序/[n]↔sources/≤3000 字/语言跟随）。
2. `docs/perf-sampling-m15.md`：主口径 P50≤30s 维持 + 与批次 8 复测对照表。
3. `make build-frontend` 产物 script 全外链 + preview 冒烟无 CSP violation。
4. `make audit` 双端输出留档（含不可达时的如实记录）。
5. 全量门禁五命令绿 + 双评审收口。