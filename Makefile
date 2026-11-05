# ai-search — 前后端命令族唯一入口（docs/ARCHITECTURE.md §3.1 根 Makefile 目标族表）
# 命令一律从仓库根执行：.env 的 env_file 相对运行时 cwd 解析，禁止换目录裸跑。

.PHONY: dev dev-frontend test test-frontend test-frontend-unit test-frontend-integration \
        test-frontend-component test-unit test-integration test-e2e test-changed typecheck \
        db-migrate db-upgrade db-downgrade

# ── 开发 ─────────────────────────────────────────────────────────────

# 后端 dev：uvicorn 热重载 :8300（§7 API_HOST/API_PORT；host 默认 127.0.0.1）
dev:
	uv run --package search-api uvicorn app.main:app --reload --port 8300

# 前端 dev：Vite :5173（vite proxy /api → 127.0.0.1:8300，SSE compress:false）
dev-frontend:
	pnpm --filter frontend dev

# ── 测试 ─────────────────────────────────────────────────────────────

# 质量门禁（"完成"的必要条件）：后端 unit+integration + 前端 unit/integration/component，
# 双端各 80% 覆盖率门禁（后端 --cov-fail-under=80；前端 80% 阈值由 vitest coverage 配置承担）；
# e2e 两端均不计入
test:
	uv run pytest apps/search-api/test/unit/ apps/search-api/test/integration/ -q \
		--cov=app --cov-report=term-missing --cov-fail-under=80
	pnpm --filter frontend exec vitest run --coverage test/unit test/integration test/component

# 前端三层一次跑全（unit + integration + component，Vitest + 80% 覆盖率门禁）
test-frontend:
	pnpm --filter frontend exec vitest run --coverage test/unit test/integration test/component

# 前端分层单跑（Vitest 按目录过滤；GUI 黑盒 e2e 不在其中、不计覆盖率）
test-frontend-unit:
	pnpm --filter frontend exec vitest run test/unit

test-frontend-integration:
	pnpm --filter frontend exec vitest run test/integration

test-frontend-component:
	pnpm --filter frontend exec vitest run test/component

# 后端分层单跑（integration/e2e 读仓库根 .env.test，缺凭证用例按需 skip）
test-unit:
	uv run pytest apps/search-api/test/unit/ -q

test-integration:
	uv run pytest apps/search-api/test/integration/ -q

test-e2e:
	uv run pytest apps/search-api/test/e2e/ -q

# 按 git 变更跑关联测试（staged + unstaged + untracked ∩ [tool.test-runner] test_dirs，
# 逐文件串行；非 git 仓库明确报错）。定位是开发期快速回归，替代不了 make test 的覆盖率门禁
test-changed:
	python scripts/run_changed_tests.py

# ── 类型检查 ─────────────────────────────────────────────────────────

# 前端：vue-tsc --noEmit 全量静态检查（含 .vue 模板表达式）
# 后端段：骨架期先以 compileall 做语法级检查（真实命令形态，零新增依赖）；
#   lint/类型检查工具随 B1 钉死后替换为正式命令
typecheck:
	pnpm --filter frontend exec vue-tsc --noEmit
	uv run --package search-api python -m compileall -q apps/search-api/app

# ── 数据库迁移 ───────────────────────────────────────────────────────
# alembic 服务自管：apps/search-api/alembic.ini（script_location = %(here)s/migrations）

# 生成迁移：make db-migrate message="add user table"
db-migrate:
	uv run --package search-api alembic -c apps/search-api/alembic.ini revision -m "$(message)"

db-upgrade:
	uv run --package search-api alembic -c apps/search-api/alembic.ini upgrade head

# 降级：make db-downgrade version=-1（或目标 revision 号）
db-downgrade:
	uv run --package search-api alembic -c apps/search-api/alembic.ini downgrade $(version)
