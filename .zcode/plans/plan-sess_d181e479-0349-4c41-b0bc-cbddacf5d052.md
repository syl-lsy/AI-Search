wf-level: T3 -- ai-search M1 批次 0 工程骨架 + git init（契约基线已冻结：ARCH v1.21 / api v1.9 / data-model v1.10 / frontend-api v1.11 / nacos v1.14 / PRD v1.11 / DEV v1.14）
wf-gate: contract

# 批次 0 执行计划：工程骨架 + git init（M1 计划 v4 之批次 0，用户裁决范围仅此批）

## Context

M1 实施计划 v4 已获批，批次 0 是开工第一步（用户裁决"骨架先行"）。现状：仓库仅 docs/ + AGENTS.md，未 git 化、零代码。本批产出完整工程骨架与 git 首提交，为批次 1（S0 基础设施）铺路。**本批无任何外部依赖（无需密钥/Docker），全自动可闭环**；用户自行将 `.env.example` 复制为 `.env` 填入 7 家上游密钥（见下表）。

用户要求的 7 家密钥（占位写入 .env.example，用户填 .env）：
`DASHSCOPE_API_KEY`(百炼 qwen+embedding+rerank) / `ZHIPU_API_KEY`(智谱) / `DEEPSEEK_API_KEY`(DeepSeek) / `TAVILY_API_KEY`(搜索主源) / `BOCHA_API_KEY`(中文搜索) / `FIRECRAWL_API_KEY`(网页读取) / `ANYSEARCH_API_KEY`(三级兜底)；另 `CRAWL4AI_API_TOKEN` 自设（openssl rand -hex 32）。

## 冻结依据（唯一事实源，冲突时停下回报）

- 目录结构与 Makefile 目标族：`docs/ARCHITECTURE.md` §3.1/§3.2（行 134~337）
- .env 变量清单：ARCH §7（行 633~760）——**closure ④ 的对照基准**
- 端口/服务拓扑（仅占位注释引用，本批不起栈）：ARCH §5
- 脚手架：全局技能 `python-backend-base`（`scripts/scaffold.sh` + `scripts/extend-ai.sh`），M1 v4 要求先加载该技能再动手

## 交付物 A：仓库根

| 文件 | 要点 |
|---|---|
| `pyproject.toml` | **仅 workspace 定义**：`[tool.uv.workspace] members=["apps/*"]`（**无 packages/**）+ `[tool.test-runner]`（供 scripts/run_changed_tests.py）；**禁止声明 [project]**（声明后 uv sync 不装成员——基座踩坑结论，ARCH §3.1 行 140） |
| `pnpm-workspace.yaml` | `packages: ["frontend"]` |
| `package.json` | 根 private 包，无依赖，仅 workspace 入口 |
| `.python-version` | **3.12**（scaffold 默认 3.14，必须改——ARCH 行 152 明确 3.12） |
| `Makefile` | ARCH §3.1 表全 13 目标：dev / dev-frontend / test / test-frontend / test-frontend-{unit,integration,component} / test-{unit,integration,e2e} / test-changed / typecheck / db-migrate / db-upgrade / db-downgrade；命令体按表写真实形态（如 dev=`uv run --package search-api uvicorn app.main:app --reload --port 8300`）；test/typecheck 族骨架期不要求跑绿（B1/F1 补齐），`make -n` 可解析即达标 |
| `.gitignore` | `.env`/`.env.test`/`data/`/`logs/`/`.mimosa/`/`__pycache__`/`.venv`/`node_modules`/`dist`/`coverage` 等；**忽略 `.zcode/` 但例外保留 `.zcode/plans/`**（实施计划是项目资产）；基座模板 gitignore 为底 |
| `README.md` | 占位（一句话项目定位 + make 命令速查表） |

## 交付物 B：apps/search-api 骨架（scaffold 生成 → 按冻结架构改装）

生成方式：`scaffold.sh ai-search search-api /tmp/ai-search-skel` + `extend-ai.sh search-api /tmp/ai-search-skel` → **合并进仓库根并执行改装清单**（脚本不能直跑仓库根——根目录已非空）：

1. `app/schema/` → **改名 `app/schemas/`**（AI Agent 模板口径，ARCH §3.1）
2. `app/model/` → **改名 `app/db/`**：engine.py → `db/session.py`（get_engine 懒加载单例 + 慢查询钩子骨架），新建 `db/base.py`（DeclarativeBase）、`db/models/` 空包
3. **全局修正改装引发的 import**（基座模板内部引用 model.engine / schema.* 处 → db.session / schemas.*；含 main.py、services/base.py、测试模板）
4. `extend-ai.sh` 的 `app/agent/graph.py` 单文件 → **改为 `app/agent/graphs/` 包**：`__init__.py`（导出编译图占位）+ `main.py`/`registry.py`/`fast.py`/`deep.py` 占位（ARCH §4.0/ADR-012 树锚点；B3 实装）+ `agent/state.py`/`nodes/`/`tools/`/`prompts/`/`guards.py` 占位
5. 新增 ARCH 特有层：`app/config_center/`、`app/crons/`（仅 `__init__.py` 注册表占位；trace_cleanup.py 随 B1）、`app/dependencies.py` 占位
6. `main.py` 用基座版（setup_logging + middleware 注册表 + 四处异常处理器 + openapi 同步 + /health），B1 再补启动钩子/crons 注册
7. `.python-version` 3.12；服务 pyproject `requires-python >=3.12`
8. `apps/search-api/pyproject.toml` 依赖（运行依赖 `==` 锁当日稳定版，R8 三件锁版本+护栏留 B1）：
   - runtime：fastapi、uvicorn[standard]、sqlalchemy[asyncio]、asyncpg、alembic、pydantic、pydantic-settings、loguru、httpx
   - AI 栈：langgraph（锁 ≥1.1 的当日稳定版）、langchain、langchain-openai、langchain-mcp-adapters、deepagents（≥0.5.3 锁定）
   - dev 组 `[dependency-groups] dev`：pytest、pytest-asyncio、pytest-cov、debuggy、psycopg2-binary（ARCH 服务 pyproject 注）
   - **兜底**：若 AI 栈解析冲突卡住 → 先以 web 栈 + langgraph/langchain 最小集闭环本批，deepagents/MCP adapters 报告用户后延至 B3 前补装（非契约变更）
9. `alembic.ini`（`script_location = %(here)s/migrations`）+ `migrations/env.py` 最小骨架（async engine、DB url 从 settings 读、target_metadata 占位）+ `migrations/versions/.gitkeep`
10. `langgraph.json` 占位（合法 JSON，graphs 指向 app/agent/graphs；B3 定稿主图入口）

## 交付物 C：占位目录

- `frontend/.gitkeep`（**不放 package.json**——F1 用 frontend-web-base 脚手架生成，避免冲突；pnpm install 仅管理根）
- `scripts/`：`run_changed_tests.py`（基座复制）+ `nacos_init.py` 占位（批次 1 实装）+ `eval/` 目录占位
- `deploy/`：`docker-compose.yml` 与 `litellm/config.yaml` 占位文件（头部注释写明"批次 1 实装，拓扑见 ARCH §5/§7"）
- `data/`：不建目录，仅 .gitignore 条目（运行期生成）

## 交付物 D：.env.example / .env.test.example（closure ④ 对照 ARCH §7）

`.env.example` **逐组全量**（注释沿用 §7 语义；密钥一律 `<secret>` 占位；compose 消费型变量注明"应用不读取"）：
- 通用：APP_ENV / API_HOST / API_PORT / CORS_ORIGINS / DEFAULT_USER_ID / DEFAULT_USER_NAME
- LOG_：LOG_LEVEL / LOG_FILE_DIR / LOG_SLOW_QUERY_THRESHOLD
- DB_：HOST/PORT(15432)/USER/PASSWORD/NAME/POOL_SIZE/MAX_OVERFLOW
- MINIO_：ENDPOINT/ACCESS_KEY/SECRET_KEY/SECURE/BUCKET_SNAPSHOT/BUCKET_KB
- NACOS_：SERVER_ADDR/NAMESPACE/GROUP/USERNAME/PASSWORD/STARTUP_TIMEOUT_S/SNAPSHOT_DIR + 容器鉴权 4 变量（AUTH_ENABLE/TOKEN/IDENTITY_KEY/IDENTITY_VALUE，注明 compose 消费）
- LITELLM_：BASE_URL/API_KEY/MASTER_KEY(注明同值+compose 消费) + 上游 **ZHIPU_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY**（注释写清每家申请渠道与用途）+ OLLAMA_API_BASE
- MCP_：TAVILY_URL/**TAVILY_API_KEY** / BOCHA_API_BASE/**BOCHA_API_KEY**/BOCHA_MCP_TRANSPORT/COMMAND/ARGS / FIRECRAWL_MCP_URL/**FIRECRAWL_API_KEY** / ANYSEARCH_URL/**ANYSEARCH_API_KEY** / CRAWL4AI_URL/**CRAWL4AI_API_TOKEN**
- LANGFUSE_：HOST/PUBLIC_KEY/SECRET_KEY/TRACE_RETENTION_DAYS/WEBHOOK_ALERT_SECRET/WEBHOOK_EXPERIMENT_SECRET + S3_* 注释块（批次 1 对照官方校对）
- SANDBOX_（M4）：ENABLED/API_BASE_URL/ARTIFACTS_DIR
- SSE_：EVENT_TTL_MINUTES/HEARTBEAT_SECONDS/BUFFER_MAX_EVENTS
- KB_：MAX_FILE_MB/ALLOWED_TYPES/EMBEDDING_DIM/CHUNK_SIZE/CHUNK_OVERLAP/TOP_K_PER_QUERY

`.env.test.example`：基座模板为底 + 本项目变量名（测试库 DB_* + integration 所需 LITELLM_/TAVILY_/BOCHA_ 键占位；注明不入库、缺凭证按需 skip）。

## 执行步骤（派单序列）

1. 主会话 `wf declare T3 --reason "M1 批次 0 工程骨架（契约已冻结过 gate）"`
2. **派 backend-developer**（执行强制：实现由领域专家做）执行交付物 A~D 全部内容 + 骨架自检
3. 主会话只做：验收闭环 6 条（只读检查）、git init + 首提交、落档

## 闭环条件（M1 v4 批次 0 原文 6 条，逐条留证）

1. **目录树逐项对照 ARCH §3.1 一致**——主会话产出对照 checklist（树逐项 ✓）写入交付说明
2. **`uv sync` 与 `pnpm install` 仓库根成功**——两条命令退出码 0 的输出留档
3. **Makefile 命令骨架可调用**——`make -n` 覆盖全部 13 目标无解析错误；附加冒烟：`uv run --package search-api python -c "import app.main"` 成功
4. **.env 模板变量与 ARCH §7 一一对应无缺漏**——按交付物 D 清单逐组核对（多变量需注释溯源 §7 行号）
5. **`.gitignore` 生效**——向 data/ 与 .env 写测试文件后 `git status` 不出现
6. **git 首提交完成**——`git log` 含骨架+docs/+AGENTS.md+.zcode/plans/ 的初始提交，`git status` 干净

测试证据（Stop 门禁）：基座自带 unit 冒烟用例 `uv run --package search-api pytest test/unit -q` 跑绿；改装若破坏模板用例，修到绿（这就是本批测试证据）。

## 边界（本批不做）

- 不起 docker/compose、不写任何业务代码与迁移 0001（B1）、不实装 Makefile test 族的目标体语义（B1/F1）、frontend 不脚手架（F1）、不动 docs/ 契约（零契约变更）
- 后端 typecheck 工具未冻结（pyright=用户口径待 B1 钉死）→ Makefile typecheck 后端段占位注释

## 落档（收尾）

按收尾记录协议 T1 档：全局记忆一条（骨架落地 + scaffold 改装决策）+ 踩坑一条（scaffold 与冻结架构的差异点，供后续批次复用）；进度=git 首提交。