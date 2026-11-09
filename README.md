# ai-search

类 Perplexity 的自建 AI 搜索引擎：快速模式（多源搜索 → ≤3000 字上限内带引用综合回答，篇幅按题型软引导，P50 ≤30s）+ 深度模式（deepagents 子代理并行研究 → 分节带引用报告），全程引用可溯源、成本可观测。

> 当前阶段（2026-11-08）：**批次 0（工程骨架）+ 批次 1（S0 基础设施）+ 批次 2（B1 底座）+ 批次 3（B2 线程轮次·桩主图）已闭环**，下一批 = 批次 4（B3 主图壳+fast 五节点）。需求 / 架构 / 契约 / 流程以 `docs/` 冻结文档为准（docs/ 仅本地保留不入库），进度实况见 `docs/PROGRESS.md`，工作区约束见 `AGENTS.md`。

## 快速开始

```bash
cp .env.example .env            # 配置见 .env.example，密钥自填 .env（不入库）
cp .env.test.example .env.test  # integration/e2e 测试凭证（不入库，缺凭证用例按需 skip）
uv sync                         # Python 依赖（uv workspace，Python 3.12）
pnpm install                    # 前端依赖（pnpm workspace；前端批次 7 落位，当前可跳过）
```

## S0 基础设施栈（批次 1 已闭环）

`deploy/docker-compose.yml`，10 服务全部绑 127.0.0.1：

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

备源组件（计划内）：SearXNG :18080 随批次 4（B3）第 0 步 compose 落地并做接入性验证，验证不过经 Nacos `searxng.enabled=false` 回退 AnySearch 独担（见 ARCHITECTURE §8）。

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

## 常用命令（make，一律从仓库根跑）

| 命令 | 作用 |
|---|---|
| `make dev` | 后端 uvicorn 热重载 :8300 |
| `make dev-frontend` | 前端 Vite :5173（前端批次 7 落位） |
| `make test` | 质量门禁：后端 unit+integration + 前端三层，双端各 80% 覆盖率（e2e 不计） |
| `make test-unit` / `make test-integration` / `make test-e2e` | 后端分层单跑（读 `.env.test`，缺凭证用例按需 skip） |
| `make test-frontend` | 前端三层一次跑全（unit/integration/component，含 80% 覆盖率门禁） |
| `make test-frontend-unit` / `-integration` / `-component` | 前端分层单跑 |
| `make test-changed` | 按 git 变更跑关联测试（开发期快速回归，非 git 仓库明确报错） |
| `make typecheck` | 前端 vue-tsc 全量静态检查 + 后端段（链式调 `typecheck-backend`） |
| `make typecheck-backend` | 后端正式门禁：pyright basic + ruff（B1 钉版 pyright==1.1.414 / ruff==0.17.4） |
| `make db-migrate message="…"` | 生成 alembic 迁移（autogenerate；产物人工核对契约 DDL） |
| `make db-upgrade` / `make db-downgrade version=-1` | 迁移升级 / 降级 |
| `make infra-up` / `infra-down` / `infra-ps` / `infra-logs` | S0 基建栈起停 / 状态 / 日志（停栈保留数据卷） |
| `make preflight` / `make smoke-infra` / `make nacos-init` | 起栈前置检查 / 七组件 smoke / 五 dataId 初始化 |

## 里程碑路线（M1，状态见 `docs/PROGRESS.md`）

批次 0 工程骨架 ✅ → 批次 1 S0 基建+接入性验证 ✅ → 批次 2 B1 底座 ✅ → 批次 3 B2 线程轮次（桩主图）→ 批次 4 B3 主图壳+fast 五节点 → 批次 5 S2 SSE 联调 → 批次 6 后端综合验证 → 批次 7 前端 F1 → 批次 8 M1 综合测试与退出；之后 M2 深度模式 / M3 知识库 / M4 沙箱·评测·多模态 / M5·M6 记忆。
