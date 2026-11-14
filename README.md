# ai-search

类 Perplexity 的自建 AI 搜索引擎：快速模式（多源搜索 → ≤3000 字上限内带引用综合回答，篇幅按题型软引导，P50 ≤30s）+ 深度模式（deepagents 子代理并行研究 → 分节带引用报告），全程引用可溯源、成本可观测。

> 当前阶段（2026-11-13）：**M1（快速模式）已退出，M1 后专项（批次 9）已落地**——批次 0~8 全部闭环（工程骨架 → S0 基础设施 → B1 底座 → B2 线程轮次 → B3 主图壳+fast 五节点 → S2 SSE 联调 → 后端综合验证 → 前端 F1 → M1 综合测试与退出），M1 退出判定为**带注记达成**（判定见 `docs/m1-exit-batch8.md`）。批次 9 一次性收口 M1 后专项四条：①synthesis 提速（关 thinking + 事实型篇幅收紧，复测 **P50 15.43s / 慢轮 max 24.27s / 多面题熔断 0/5**，报告 `docs/perf-sampling-m15.md`）+ 后端 e2e 三契约用例摘牌（断言与题面原样，复跑留档待补）；②rerank 兜底 degraded 语义归位（对齐 ARCHITECTURE §6.2 二分口径）；③CSP `img-src` 对齐契约 + 生产 build/preview 冒烟验证 + `make build-frontend` / `make audit` 依赖 CVE 机扫通道（首跑结果见 `docs/security-audit-m1-batch8.md` §五）；④渲染安全专项审计回写收口。下一步 = M2（深度模式）。需求 / 架构 / 契约 / 流程以 `docs/` 冻结文档为准（docs/ 仅本地保留不入库），进度实况见 `docs/PROGRESS.md`，工作区约束见 `AGENTS.md`。

## 快速开始

```bash
cp .env.example .env            # 配置见 .env.example；容器侧密钥由 gen_env 生成（见下），上游 API Key 自填
cp .env.test.example .env.test  # integration/e2e 测试凭证（不入库，缺凭证用例按需 skip）
uv sync                         # Python 依赖（uv workspace，Python 3.12）
pnpm install                    # 前端依赖（pnpm workspace；前端批次 7 已落位，make dev-frontend 前必跑）
```

## 前端（批次 7 已落位）

```bash
cp frontend/.env.example frontend/.env   # 可选：模板默认直连后端 http://127.0.0.1:8300（CORS 已放行 :5173 两个 origin）
make dev-frontend                        # Vite :5173（/api 代理 → http://127.0.0.1:8300）
```

- `frontend/.env` 的 `VITE_API_BASE_URL`：未设/留空 = 同源相对路径走 Vite dev proxy（推荐 dev 形态，无需 CORS）；直连 = 填后端 origin，改端口须同步后端 `CORS_ORIGINS`。仅 `VITE_` 前缀变量暴露给客户端（唯一读取处 `frontend/src/api/client.ts`）。
- 前端测试：`make test-frontend`（unit/integration/component 三层 + 80% 覆盖率门禁）；e2e（playwright，真服务）运行期间 `make dev` + `make dev-frontend` 双服务须在跑。**流式 e2e 已于批次 8 摘牌复跑全绿**（2026-11-13，四页主链路 + A13/A26/A27/A28 全过，判定截图见 `docs/design/e2e-batch8/`）——此前 skip 挂牌的根因是本机系统代理未对 127.0.0.1/localhost 加 bypass，非项目缺陷；本机若复现同症状，先查代理 bypass 再怀疑链路。

## S0 基础设施栈（批次 1 已闭环）

`deploy/docker-compose.yml`，11 个服务（含 searxng，见下方备源组件）全部绑 127.0.0.1：

| 组件 | 宿主端口 | 说明 |
|---|---|---|
| postgres(pgvector) | 15432 | 三库 ai_search / langfuse / litellm（`deploy/postgres/init.sh`） |
| nacos v3.2.4 | 8081 / 8849 / 9849 | 控制台 http://127.0.0.1:8081/（无 /nacos 路径，ADR-013）/ HTTP OpenAPI / gRPC；五 dataId 配置中心 |
| minio | 9100 / 9101 | S3 API / 控制台 http://127.0.0.1:9101（三桶 + SA） |
| langfuse（web + worker + clickhouse） | 3100 | http://127.0.0.1:3100（LLM 观测与 M4 评测载体） |
| litellm | 4100 | 模型网关（DB 模式，九别名 + 定价 / fallback 链 / OTel） |
| redis | 16379 | 共享缓存（litellm `REDIS_DB=1` 索引隔离复用） |
| crawl4ai | 11235 | 自部署读取备胎（`url_reader_backup`，MCP `/mcp/sse`） |
| opensandbox | 8801 | 代码沙箱（M4 启用） |

备源组件（批次 4 已落地）：SearXNG :18080（镜像锁 tag `2026.9.8-3fdc6d753`；`make infra-up` 起栈自启，需 .env `SEARXNG_SECRET`——该版本镜像对默认 secret_key 硬拒绝启动）；接入性验证 PASS（延迟 P50 1.32s / P95 3.02s，显著优于 AnySearch 实测 5.9~7.9s；上游引擎 CAPTCHA/限流残余风险与回退门 `searxng.enabled=false` 见 ARCHITECTURE §10 R12）。批次 5（2026-11-11）起标准 `make dev` 形态下链首**真装载**（`.env` 的 `SEARXNG_*` 经 `McpCredentialSettings` 生效——此前后端未声明三字段导致 `.env` 有值但 wrapper 装不上，降级链退化为 AnySearch 独担；缺口已修），且 Nacos 运行时 mcp.json 已发布 v1.18（六源含 searxng、anysearch 降为次级）。

### 起栈流程（顺序执行，全部从仓库根跑）

```bash
make preflight                            # 端口空闲前置检查（仅起栈前跑；起栈后跑必假阳性）
python3 scripts/infra_smoke/gen_env.py    # 生成 .env 容器侧密钥（自动回填 <GEN> 占位）
# 上游密钥人工填 .env 的 <secret> 占位（Tavily / BoCha / AnySearch、智谱 / 百炼等，脚本不代填）
make infra-up                             # 起栈（compose --project-directory . -f deploy/docker-compose.yml）
NACOS_INIT_USER=<管理员> NACOS_INIT_PASS=<密码> make nacos-init   # 五 dataId 初始化（覆写保护默认开，AI 不经手）
make smoke-infra                          # 七组件逐一 smoke（postgres/nacos/minio/langfuse/litellm/sandbox/crawl4ai）
```

初始化细节（Nacos 鉴权与只读账号、dataId 顺序）见 `AGENTS.md`「S0 基建 UI 初始化 checklist」与 `docs/s0-batch1-handoff.md`。

> **升级既有部署（非首次起栈）的部署次序【B9 起硬前置】**：新代码的 prompts 校验会拒收 Nacos 上的旧版（v1.19 及更早）`ai_search.agent.prompts` 文本——dev 环境回落 L4 内置 v1.20 正文、代码部署即启用免检索直答（direct）；**production 环境 L4 回落触发 `_enforce_production_floor` 拒绝启动**。故生产升级须**先向 Nacos 发布含 v1.20 内容的 prompts（`NACOS_INIT_FORCE=1` + 管理员凭据）再部署代码**（反向无害：旧代码 + 新 prompts 忽略未知字段、行为不变）。机制与回退门见 `docs/contract/nacos-config.md` v1.21 §1/§8。

## 常用命令（make，一律从仓库根跑）

| 命令 | 作用 |
|---|---|
| `make dev` | 后端 uvicorn 热重载 :8300 |
| `make dev-frontend` | 前端 Vite :5173（批次 7 已落位；启动与环境变量说明见上方「前端」节） |
| `make studio` | LangGraph Studio 开发期图观测（langgraph dev :2024；`LANGSMITH_TRACING=false` 显式导出——trace 不出本机，观测主通道为自托管 Langfuse；图调试流程见 `docs/DEVELOPMENT-PROCESS.md` §5.3） |
| `make test` | 质量门禁：后端 unit+integration + 前端三层，双端各 80% 覆盖率（e2e 不计） |
| `make test-unit` / `make test-integration` / `make test-e2e` | 后端分层单跑（读 `.env.test`，缺凭证用例按需 skip） |
| `make test-frontend` | 前端三层一次跑全（unit/integration/component，含 80% 覆盖率门禁） |
| `make test-frontend-unit` / `-integration` / `-component` | 前端分层单跑 |
| `make test-changed` | 按 git 变更跑关联测试（开发期快速回归，非 git 仓库明确报错） |
| `make typecheck` | 前端 vue-tsc 全量静态检查 + 后端段（链式调 `typecheck-backend`） |
| `make typecheck-backend` | 后端正式门禁：pyright basic + ruff（B1 钉版 pyright==1.1.414 / ruff==0.17.4） |
| `make build-frontend` | 前端生产构建（vue-tsc + vite build → `frontend/dist/`；批次 9 安全审计 R2 生产 CSP 形态核对入口——产物 script 应全为 src 外链） |
| `make audit` | 依赖 CVE 机扫双通道（前端 `pnpm audit` 官方源 + 后端 pip-audit 环境审计；任一通道非零退出即整体 FAIL 不吞错——周期性手动门禁，首跑基线见 `docs/security-audit-m1-batch8.md` §五.4） |
| `make db-migrate message="…"` | 生成 alembic 迁移（autogenerate；产物人工核对契约 DDL） |
| `make db-upgrade` / `make db-downgrade version=-1` | 迁移升级 / 降级 |
| `make infra-up` / `infra-down` / `infra-ps` / `infra-logs` | S0 基建栈起停 / 状态 / 日志（停栈保留数据卷） |
| `make preflight` / `make smoke-infra` / `make nacos-init` | 起栈前置检查 / 七组件 smoke / 五 dataId 初始化 |

## 里程碑路线（M1，状态见 `docs/PROGRESS.md`）

批次 0 工程骨架 ✅ → 批次 1 S0 基建+接入性验证 ✅ → 批次 2 B1 底座 ✅ → 批次 3 B2 线程轮次（桩主图）✅ → 批次 4 B3 主图壳+fast 五节点 ✅ → 批次 5 S2 SSE 联调 ✅ → 批次 6 后端综合验证 ✅ → 批次 7 前端 F1 ✅ → 批次 8 M1 综合测试与退出 ✅ → **M1（快速模式）已退出**（带注记达成，判定见 `docs/m1-exit-batch8.md`）→ 批次 9 M1 后专项（synthesis 提速+e2e 摘牌 / rerank 兜底语义归位 / CSP 对齐+依赖机扫通道）✅；之后 M2 深度模式 / M3 知识库 / M4 沙箱·评测·多模态 / M5·M6 记忆。
