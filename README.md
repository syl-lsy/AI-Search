# ai-search

类 Perplexity 的自建 AI 搜索引擎：快速模式（多源搜索 → ≤3000 字上限内带引用综合回答，篇幅按题型软引导，P50 ≤30s）+ 深度模式（deepagents 子代理并行研究 → 分节带引用报告）+ 个人知识库融合，全程引用可溯源、成本可观测。

> 当前阶段（2026-11-07）：**批次 0 工程骨架 ✅ + 批次 1 S0 基础设施 ✅ 已闭环**——基建栈（五件套 + 备源）可一键起栈、全部 smoke 通过、六项接入性验证结论落档；下一批 = 批次 2 B1 应用底座。需求 / 架构 / 契约 / 流程以 `docs/` 冻结文档为准（仅本地保留，不入库），工作区约束见 `AGENTS.md`。

## 快速开始

```bash
cp .env.example .env            # 配置见 .env.example；容器侧密钥由 gen_env 生成（见下），上游 API Key 自填
cp .env.test.example .env.test  # integration/e2e 测试凭证（不入库，缺凭证用例按需 skip）
uv sync                         # Python 依赖（uv workspace，Python 3.12）
pnpm install                    # 前端依赖（pnpm workspace）
```

`.env` 中 11 项容器侧密钥（Nacos 鉴权四件套 / LiteLLM 家族 / Langfuse 三密钥 / MinIO root / 沙箱 / crawl4ai）由脚本一次性生成定型：

```bash
python3 scripts/infra_smoke/gen_env.py   # 随机密钥回填 .env（0600，已定型则幂等跳过）
```

7 家上游 API Key（百炼 Token Plan+标准两把 / 智谱 Coding Plan / DeepSeek / Tavily / BoCha / Firecrawl / AnySearch）到各控制台申请后填入 `.env`。

## 基础设施栈（S0 已闭环）

```bash
make infra-up        # 起栈（必须从仓库根跑：--project-directory 已封装）
make preflight       # 起栈前端口占用预检（11 个宿主端口）
make smoke-infra     # 全栈 smoke（postgres/nacos/minio/langfuse/litellm/sandbox/crawl4ai 七组件）
make nacos-init      # 发布五 dataId 初值（需管理员凭据：NACOS_INIT_USER=… NACOS_INIT_PASS=… make nacos-init；
                     #   dataId 已存在且内容不同时默认跳过，确认覆盖加 NACOS_INIT_FORCE=1）
make infra-logs / make infra-ps / make infra-down
```

| 组件 | 宿主地址 | 说明 |
|---|---|---|
| Nacos v3.2.4 | `127.0.0.1:8081`（控制台）/ `8849`（OpenAPI）/ `9849`（gRPC） | 配置面：双账号（管理员 + 只读 `ai-search`）、五 dataId 热更 |
| MinIO | `127.0.0.1:9100`（API）/ `9101`（控制台） | 存储面：三桶（snapshots/kb/langfuse）+ 最小化 SA |
| Langfuse v4 | `127.0.0.1:3100` | 观测面：双 trace（LiteLLM OTel + 应用 SDK）、评测能力已验证 |
| LiteLLM v1.101 | `127.0.0.1:4100`（UI `/ui`） | 模型面：9 别名（三供应商×两档 + light/embedding/rerank）、fallback 链、熔断 |
| OpenSandbox | `127.0.0.1:8801` | 执行面（基建已验，功能闭环到 M4） |
| crawl4ai 0.9.5 | `127.0.0.1:11235` | 备源：Firecrawl 降级备胎（MCP SSE） |
| postgres / redis / clickhouse | `15432` / `16379` / 网内 | 三库（ai_search/langfuse/litellm）+ 索引隔离 + 观测 OLAP |

全部端口绑 `127.0.0.1`；与同机其他课程栈（deal-pilot）并存的宿主端口重映射口径见 `docs/ARCHITECTURE.md` §5。

## 常用命令（make，一律从仓库根跑）

| 命令 | 作用 |
|---|---|
| `make dev` | 后端 uvicorn 热重载 :8300 |
| `make dev-frontend` | 前端 Vite :5173 |
| `make test` | 质量门禁：后端 unit+integration + 前端三层，双端各 80% 覆盖率（e2e 不计） |
| `make test-unit` / `make test-integration` / `make test-e2e` | 后端分层单跑（读 `.env.test`） |
| `make test-frontend` | 前端三层一次跑全（unit/integration/component，含 80% 覆盖率门禁） |
| `make test-frontend-unit` / `-integration` / `-component` | 前端分层单跑 |
| `make test-changed` | 按 git 变更跑关联测试（开发期快速回归，非 git 仓库明确报错） |
| `make typecheck` | 前端 vue-tsc 全量静态检查 + 后端/脚本 compileall（含 `scripts/`） |
| `make db-migrate message="…"` | 生成 alembic 迁移 |
| `make db-upgrade` / `make db-downgrade version=-1` | 迁移升级 / 降级 |
| `make infra-up` / `infra-down` / `infra-ps` / `infra-logs` | 基建栈生命周期 |
| `make preflight` / `smoke-infra` / `nacos-init` | 基建预检 / 全栈 smoke / 五 dataId 发布 |

## 里程碑路线

M1 = 快速模式端到端可用（当前：M1 计划 v4 执行中，批次 0~1 已闭环）→ M2 深度模式 → M3 知识库 → M4 沙箱/评测/多模态 → M5/M6 记忆。批次级进度见 `docs/PROGRESS.md`（本地）。
