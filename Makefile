# ai-search — 前后端命令族唯一入口（docs/ARCHITECTURE.md §3.1 根 Makefile 目标族表）
# 命令一律从仓库根执行：.env 的 env_file 相对运行时 cwd 解析，禁止换目录裸跑。

.PHONY: dev dev-frontend studio test test-frontend test-frontend-unit test-frontend-integration \
        test-frontend-component test-unit test-integration test-e2e test-changed typecheck \
        typecheck-backend \
        build-frontend audit \
        db-migrate db-current db-upgrade db-downgrade \
        preflight infra-up infra-down infra-ps infra-logs nacos-init smoke-infra

# ── S0 基础设施（deploy/docker-compose.yml）────────────────────────────
# compose 相对路径与 .env 都按 --project-directory 解析：必须从仓库根跑并显式指回仓库根
# （compose 默认从 compose 文件所在目录 deploy/ 找 .env 与解析相对路径，会错位）。
COMPOSE := docker compose --project-directory . -f deploy/docker-compose.yml

# 起栈前置检查（端口空闲等）——独立目标，仅起栈前手动使用；不属 smoke 套件
# （preflight 查「端口应空闲」，起栈后跑必假阳性，严禁混入 smoke-infra 循环）
preflight:
	python3 scripts/infra_smoke/preflight.py

# 起 S0 基建栈（10 服务后台拉起；前置：make preflight 端口空闲 +
# python3 scripts/infra_smoke/gen_env.py 生成 .env 容器侧密钥 + 上游密钥人工填好）
infra-up:
	$(COMPOSE) up -d

# 停栈（保留数据卷——删卷=删库，须显式自行 `$(COMPOSE) down -v` 自担）
infra-down:
	$(COMPOSE) down

infra-ps:
	$(COMPOSE) ps

infra-logs:
	$(COMPOSE) logs -f --tail=100

# Nacos 五 dataId 初始化（写操作，管理员凭据经环境变量注入，AI 不经手——nacos-config.md §8）：
#   NACOS_INIT_USER=<管理员用户名> NACOS_INIT_PASS=<管理员密码> make nacos-init
#   或：make NACOS_INIT_USER=... NACOS_INIT_PASS=...
#   两种形态均正确透传——recipe 内 export 直取 make 变量（env 注入与命令行注入都成立），
#   值不再经 shell 双引号重解释（密码含 $、反引号等特殊字符也安全）。
# 前置：nacos 容器 healthy + 管理员改密 + namespace（名=ID=ai-search-dev）已建。
# 覆写保护：已存在且与初值不同的 dataId 默认跳过（防线上热更值回滚）；确认覆盖加 NACOS_INIT_FORCE=1
nacos-init:
	export NACOS_INIT_USER NACOS_INIT_PASS; \
		python3 scripts/nacos_init.py

# S0 smoke 套件：起栈后七组件逐一跑并汇总 PASS/FAIL（任一 FAIL 整体非零退出；判定只基于七件）。
# preflight.py（查端口空闲，起栈后跑必假阳性）与 gen_env.py 都是起栈前工具、不是 smoke——
# 不进本循环：起栈前单独跑 make preflight + python3 scripts/infra_smoke/gen_env.py
smoke-infra:
	@overall=0; \
	for s in postgres nacos minio langfuse litellm sandbox crawl4ai; do \
		printf '== %s ==\n' "$$s"; \
		if python3 scripts/infra_smoke/$${s}_smoke.py; then printf '%s: PASS\n' "$$s"; \
		else printf '%s: FAIL\n' "$$s"; overall=1; fi; \
	done; \
	if [ $$overall -eq 0 ]; then printf 'smoke-infra: ALL PASS\n'; else printf 'smoke-infra: HAS FAIL\n'; fi; \
	exit $$overall

# ── 开发 ─────────────────────────────────────────────────────────────

# 后端 dev：uvicorn 热重载 :8300（§7 API_HOST/API_PORT；host 默认 127.0.0.1）
dev:
	uv run --package search-api uvicorn app.main:app --reload --port 8300

# 前端 dev：Vite :5173（vite proxy /api → 127.0.0.1:8300，SSE compress:false）
dev-frontend:
	pnpm --filter frontend dev

# LangGraph Studio：开发期图观测（langgraph dev :2024；TRACING=false 显式导出=防御性
# 执行不出域裁决——不给 Smith 上报任何 trace，纯本地 in-mem 观测）
studio:
	LANGSMITH_TRACING=false uv run --package search-api langgraph dev --config apps/search-api/langgraph.json --no-browser

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

# -rP：passed 用例的捕获输出随报告显示——trace_cleanup 删除滞后观察数据（[lag] 行）得以可见
# （langfuse#8834 删除异步收敛口径，ARCH §7 滞后观察）
test-integration:
	uv run pytest apps/search-api/test/integration/ -q -rP

test-e2e:
	uv run pytest apps/search-api/test/e2e/ -q

# 按 git 变更跑关联测试（staged + unstaged + untracked ∩ [tool.test-runner] test_dirs，
# 逐文件串行；非 git 仓库明确报错）。定位是开发期快速回归，替代不了 make test 的覆盖率门禁
test-changed:
	python scripts/run_changed_tests.py

# ── 类型检查 ─────────────────────────────────────────────────────────

# 后端正式门禁（B1 工具链钉版，替换骨架期 compileall 语法级检查）：pyright basic
# （类型）+ ruff（lint）；范围 apps/search-api/app + scripts/（运维/烟测脚本同为
# Python 交付物，一并入门禁）；规则与范围配置在仓库根 pyproject
# [tool.pyright]/[tool.ruff]，工具本体在 apps/search-api dev 组 == 钉版
typecheck-backend:
	uv run --package search-api pyright apps/search-api/app scripts
	uv run --package search-api ruff check apps/search-api/app scripts

# 前端：vue-tsc --noEmit 全量静态检查（含 .vue 模板表达式）；后端段调 typecheck-backend
typecheck:
	pnpm --filter frontend exec vue-tsc --noEmit
	$(MAKE) typecheck-backend

# ── 构建与依赖审计 ───────────────────────────────────────────────────

# 前端生产构建（vue-tsc --noEmit + vite build → frontend/dist/）。生产 CSP 形态核对
# （security-audit-m1-batch8.md R2）：build 产物 script 标签应全为 src 外链（无 inline script，
# 符合 script-src 'self'）——grep dist/index.html 取证
build-frontend:
	pnpm --filter frontend build

# 依赖 CVE 机扫通道（security-audit-m1-batch8.md R4 落地，2026-11-13）：
#   前端 = pnpm audit（官方源 registry.npmjs.org——npmmirror 无 audit API 端点）
#   后端 = pip-audit 环境审计（apps/search-api dev 组 == 钉版，枚举 venv 已装发行版比对
#          PyPI Advisory DB；传递依赖自带 pip 供环境枚举）
# 任一通道非零退出即整体 FAIL，不吞错——审计信号缺失必须显性暴露（网络不可达时如实红）。
# 首跑（2026-11-13）= 周期性手动门禁；接 CI 周期跑属后续工程化事项
#
# 已知接受（2026-11-13 批次 9 终审留档，证据与升级计划见
# security-audit-m1-batch8.md §五.4 处置小节）——后端通道在下列两条修复前会预期 FAIL：
#   ① cryptography 44.0.3（运行时传递：nacos-sdk-python==3.2.0 → alibabacloud-* / google-auth）
#      修复线 ≥50.0.0（最低档 46.0.5 起分档收敛，全部清空需 50.0.0）；
#   ② langgraph-api 0.5.42（dev 工具链传递：langgraph-cli[inmem]==0.4.9）
#      修复线 ≥0.15.0（PYSEC-2026-3857/3858 + CVE-2026-118668）。
#   二者相互咬合、受 langgraph-cli==0.4.9 的 inmem 上界 <0.6.0 锁死：该区间内所有
#   langgraph-api 0.5.x 反钉 cryptography<45.0，故单独升 cryptography 必然解析失败。
#   升级路径 = 同批抬 langgraph-cli ≥0.4.43（inmem 上界放宽到 <1.0.0）+ langgraph-api==0.18.0
#   + cryptography==50.0.2（后者会引入 pre-release 的 langgraph-runtime-inmem，须单独评审）。
# 前端 2 条 moderate（glob/minimatch，均 @vue/test-utils>js-beautify dev 链）已用
# package.json pnpm.overrides 升至修复线（glob 14.0.6 / minimatch 11.0.8），通道现为绿。
audit:
	@overall=0; \
	if pnpm --dir frontend audit --registry=https://registry.npmjs.org; then \
		printf 'audit(frontend): OK\n'; \
	else printf 'audit(frontend): FAIL\n'; overall=1; fi; \
	if uv run --package search-api pip-audit; then \
		printf 'audit(python): OK\n'; \
	else printf 'audit(python): FAIL\n'; overall=1; fi; \
	if [ $$overall -eq 0 ]; then printf 'audit: ALL PASS\n'; else printf 'audit: HAS FAIL\n'; fi; \
	exit $$overall

# ── 数据库迁移 ───────────────────────────────────────────────────────
# alembic 服务自管：apps/search-api/alembic.ini（script_location = %(here)s/migrations）

# 生成迁移：make db-migrate message="add user table"（autogenerate 对比 Base.metadata 与库；
# 产物须人工核对契约 DDL——DESC/部分索引、seed、扩展等 metadata 外语义手补）
db-migrate:
	uv run --package search-api alembic -c apps/search-api/alembic.ini revision --autogenerate -m "$(message)"

# 查看当前库所处 revision（data-model.md §4：增补迁移开工前「以 make db-current
# 核实链尾」——0008/0009 行均引用此口径）
db-current:
	uv run --package search-api alembic -c apps/search-api/alembic.ini current

db-upgrade:
	uv run --package search-api alembic -c apps/search-api/alembic.ini upgrade head

# 降级：make db-downgrade version=-1（或目标 revision 号）
db-downgrade:
	uv run --package search-api alembic -c apps/search-api/alembic.ini downgrade $(version)
