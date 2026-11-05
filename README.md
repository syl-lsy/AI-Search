# ai-search

类 Perplexity 的自建 AI 搜索引擎：快速模式（多源搜索 → ≤3000 字上限内带引用综合回答，P50 ≤30s）+ 深度模式（deepagents 子代理并行研究 → 分节带引用报告），全程引用可溯源、成本可观测。

> 当前阶段：契约与设计已冻结（见 `docs/`），代码骨架施工中。需求 / 架构 / 契约 / 流程以 `docs/` 冻结文档为准，工作区约束见 `AGENTS.md`。

## 快速开始

```bash
cp .env.example .env            # 配置见 .env.example，密钥自填 .env（不入库）
cp .env.test.example .env.test  # integration/e2e 测试凭证（不入库，缺凭证用例按需 skip）
uv sync                         # Python 依赖（uv workspace，Python 3.12）
pnpm install                    # 前端依赖（pnpm workspace）
```

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
| `make typecheck` | 前端 vue-tsc 全量静态检查（后端段 B1 钉死工具后补齐） |
| `make db-migrate message="…"` | 生成 alembic 迁移 |
| `make db-upgrade` / `make db-downgrade version=-1` | 迁移升级 / 降级 |
