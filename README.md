# ai-search

类 Perplexity 的自建 AI 搜索引擎：快速模式（多源搜索 → ≤3000 字上限内带引用综合回答，篇幅按题型软引导，P50 ≤30s）+ 深度模式（deepagents 子代理并行研究 → 分节带引用报告），全程引用可溯源、成本可观测。

> 当前阶段（2026-11-22）：**M1（快速模式）已退出（带注记达成，判定见 `docs/m1-exit-batch8.md`）；M2（深度模式）已退出（带注记达成，判定见 `docs/m2-exit-batch7-20261121.md`）**——M1 批次 0~8 + 批次 9 后专项全部闭环；M2 七批全部闭环：批次 0 基线收口与版本护栏 → 批次 1 B4 底座（持久化+状态投影+TurnRunner deep 化+工具面+模型工厂+deep 提示词骨架）→ 批次 2 planner+research 逐节点 → 批次 3 reflect+supplement+guards → 批次 4 writer/finalize/degraded_writer+导出链+deep 整图收口 → 批次 5 REQ-017 深度后追问+deep regenerate+deep SSE 联调 → 批次 6 前端 deep 交付（导出报告消费面+SnapshotModal+deep 刷新恢复+报告渲染回归）→ 批次 7 M2 综合测试与退出（e2e deep 契约套件 + 双评审 P0/P1=0 + 性能正式抽样 40 轮双口径 8/8 类达标〔用户钉定 P95 ≤360s + 360s 内完成率 ≥80%，按实测校准，报告 `docs/perf-sampling-m2.md`〕+ 契约零漂移九项终检 + 退出判定留档）；B12 recall 回顾分支+navigational 题型、B13 输入安全检查、B14 历史装配脱敏、B15 内容侧安全护栏四独立工单插队闭环。下一步 = **M3 知识库（已立项+第一阶段契约包〔api v1.31 等〕+第二阶段全闭环 2026-11-23：UI 设计稿用户过审〔`docs/design/05-library-m3.html`〕+ 基建验证〔归一化组件 PASS·OCR 钉 PaddleOCR / ES·jieba 可用带保留 / 飞书 V1~V6 全实证，验证汇总 `docs/research/m3-phase2-verification-20261122.md`〕；**批次 1（B16）✅ 2026-11-23**（迁移 0013+ORM+IR/装配器+storage，分层门禁全绿）；**批次 2（B17）✅ 2026-11-23**（kb 13 键三源同步〔ocr_engine 钉 paddleocr〕+normalizers+chunker 逐级切分+上传流水线六态+REST §4.1~4.5，分层门禁全绿+双评审 pass，交付说明 `docs/m3-b2-delivery-20261123.md`）。需求 / 架构 / 契约 / 流程以 `docs/` 冻结文档为准（docs/ 仅本地保留不入库），进度实况见 `docs/PROGRESS.md`，工作区约束见 `AGENTS.md`。

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
| `make studio` | LangGraph Studio 开发期图观测（langgraph dev :2024；`STUDIO_LANGFUSE=1` 门控下 run 落自托管 Langfuse 散点 trace〔无 session/thread 关联，仅调试观测〕；`LANGSMITH_TRACING=false` 显式导出——不上报 LangSmith；图调试流程见 `docs/DEVELOPMENT-PROCESS.md` §5.3） |
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

## 里程碑路线（M1/M2，状态见 `docs/PROGRESS.md`）

批次 0 工程骨架 ✅ → 批次 1 S0 基建+接入性验证 ✅ → 批次 2 B1 底座 ✅ → 批次 3 B2 线程轮次（桩主图）✅ → 批次 4 B3 主图壳+fast 五节点 ✅ → 批次 5 S2 SSE 联调 ✅ → 批次 6 后端综合验证 ✅ → 批次 7 前端 F1 ✅ → 批次 8 M1 综合测试与退出 ✅ → **M1（快速模式）已退出**（带注记达成，判定见 `docs/m1-exit-batch8.md`）→ 批次 9 M1 后专项（synthesis 提速+e2e 摘牌 / rerank 兜底语义归位 / CSP 对齐+依赖机扫通道）✅；M2 深度模式：批次 0 基线收口与版本护栏 ✅ → 批次 1 B4 底座 ✅ → 批次 2 planner+research ✅ → 批次 3 reflect+supplement+guards ✅ → 批次 4 writer/finalize/degraded_writer+导出链+deep 整图收口 ✅ → 批次 5 REQ-017 深度后追问+deep regenerate+deep SSE 联调 ✅ → 批次 6 前端 deep 交付 ✅（B12 recall 工单 ✅ / B13 输入安全 ✅ / B14 历史装配脱敏 ✅ / B15 安全护栏 ✅ 独立插队）→ **批次 7 M2 综合测试与退出 ✅ → M2（深度模式）已退出**（带注记达成，判定见 `docs/m2-exit-batch7-20261121.md`）；之后 M3 知识库（**已立项+第一阶段+第二阶段全闭环 2026-11-23**〔设计稿过审+基建验证，报告 `docs/research/m3-phase2-verification-20261122.md`〕，批次 1 B16 待启动）/ M4 沙箱·评测·多模态 / M5·M6 记忆。
