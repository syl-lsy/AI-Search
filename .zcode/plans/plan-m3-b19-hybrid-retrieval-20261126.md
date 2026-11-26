# M3 批次 5 工单 B19 实施设计：混合检索 kb_search 接真 + 索引语义升级（plan-m3-b19-hybrid-retrieval-20261126.md）

> wf-level: T2 -- 跨域工单（索引面 rag/ + 检索面 agent/ + REST 溯源端点 + 契约三件增量），计划已含契约增量
> wf-gate: contract
> 产出日期：2026-11-26。状态：设计钉版待实施（契约增量已随本批落地：data-model v1.21 / nacos-config v1.46 / api.md 零面判定行；ARCHITECTURE/PROGRESS/README 本批不动，归收官主会话巡检）。
> **2026-11-26 增补：英文词法加固（content.en multi-fields 双查）经用户裁决并入本批实施范围——见 §11 增补节；A7 / B1 / §6 / §7 / §8.1 / §9 / §10 已同步修订，实施与门禁以增补后为准。**

---

## 0. 输入与现状核实（设计证据基线）

### 0.1 ask 指定输入（全部已读）

| 输入 | 关键事实（本设计依赖） |
|---|---|
| `docs/ARCHITECTURE.md`（v1.79 现行） | §8 B19 行（:1097）：Send 扇出每 query ES BM25‖pgvector top30 并行 → RRF(k=60) → kb_fuse_top_k=20 → rerank ≤30 上界（行为增量随 TDD 显式测试）→ 可观测留痕（A35）→ ES 熔断降级单通道 → deep 子代理检索等价升级 → search_service 融合收口 → **KB chunk 进上下文安全接线实装（`<external_content>` 包裹+injection_filter，顺序锁=过滤先于包裹，§6.4 第 10 条）** → 溯源下钻端点后端（REQ-053/A36，ir.json children 序重建 prev/next）→ **检索架构增强包三件**（kb_route 三档〔rewrite 扩展字段+条件边第四例；KB 语料空/未建索引强制回 hybrid〕/ fast 有界纠错轮〔轮帽 1 次〕/ deep reflect 置信度门控〔确定性信号 gate 零 LLM〕）——全部灰度默认关+确定性信号触发+Nacos 热更阈值+M4 评测把关后逐档放开；**kb_search 节点走 DEV §5.4 五步闭环、溯源 REST 端点为普通 TDD 面**（双 DoD 拆注） |
| `docs/ARCHITECTURE.md` §4.1（:479） | 漏斗已钉参数（不造新值）：每 query **ES BM25 top30 ‖ pgvector top30**（`kb.per_query_top_k=30`）→ **RRF(k=60)** 跨 query×通道合并+同章节归并 → **KB 份额 kb_fuse_top_k=20** → 合并基数超 30 按 origin/RRF 得分截取至 30（`kb.max_rerank_candidates`）→ qwen3-rerank（≤2s，仅 KB 命中门控不变）→ topN（`fast.fuse_top_n`/`deep.fuse_top_n` 既有键承载）→ synthesis；题型权重 `kb.type_weights` 8 值；降级链三级（KB 空→纯网络 / ES 不可用·熔断·es_enabled=false→pgvector 单通道 / 双通道全断不影响网络侧）；预算 `kb.dual_channel_timeout_s=3`；检索通道可替换接口封装 `rag/retrieval.py`（决策⑨，供 fast/deep 复用） |
| `docs/ARCHITECTURE.md` ADR-017（:1246）/ §6.4 第 10 条（:688） | ADR-017 决策①~⑨全量钉定；第 10 条：KB chunk 进 fast synthesis + deep 子代理读窗**两形态**经 `<external_content>` 包裹 + injection_filter，**顺序锁=过滤先于转义包裹**，flag/drop 同判据、drop 不占 ref_no，归一化产物不直接进上下文、接线收敛在 kb_search 单点不做事后接线；溯源预览仅进 UI 不进 LLM |
| `docs/contract/api.md`（v1.33 现行） | §4.6 溯源下钻**已冻结契约**（:574-601——六必选字段 chunk_id/document_id/document_title/content/heading_path/page/paragraph + 两可选 node_token/node_id 仅飞书；非 KB 来源 400001；不存在/越权/已删除 404001 单码；溯源内容仅进 UI 不进 LLM、前端 DOMPurify）——本批**纯实现**；§2.5/§7.2 sources 条目 `kb_chunk_id`（v1.30 已批）；§4.1~4.5 KB 端点现行 |
| `docs/contract/data-model.md`（v1.20 现行） | §1.7 content_hash（:272——正文 sha256，方案 A 语义注待升版〔本批落 v1.21〕）；§1.8 kb_chunks embedding vector(1024) NOT NULL + meta 七键 + 检索语句模板（:341-348，参数绑定禁拼接）；§1.18 IR ID 前缀四值+heading_path 物化细则；§1.19 ES mapping 要点（heading_path 独立字段 multi_match 权重高于正文——**权重数值契约未钉**，基线探针固定 `^2` 一次性钉定先例） |
| `docs/contract/nacos-config.md`（v1.45 现行） | kb 节 13 键现行形态（:290 示例 + :337-343 键注——per_query_top_k/rrf_k/kb_fuse_top_k/max_rerank_candidates/dual_channel_timeout_s/es_enabled/type_weights/chunking/normalizer 全在位）；§5 model.mapping `embedding: "text-embedding-v4"`（:379）；§6 热更边界（OQ-11 快照） |
| `docs/research/m3-b18-golden-bm25-baseline-20261125.md` | BM25 双口径基线 100 题：锚点 97/98/98、摘录 99/100/100；唯一真排序 miss=G059 同标题兄弟块 rank2 分差 2.13%（向量+rerank 治理面）；G044/G076 锚点不在库（98/100 对齐率天花板）；**multi_match 默认 OR 语义注记**（解读前提）；复测口径冻结面=§3.1 查询形态/§3.2 判定/§4 指标 |
| `docs/m3-b4-delivery-20261126.md` §三/§六 | P3 留档：#3 bulk-item 429/503（**B19 前裁决**）、#4 删除腿 404 短路、#5 回填连接异常隔离、#6 openpyxl dev 组声明；§六 P3：ai_search_app 最小权限角色（**B19 前**）、elastic_transport TRACE 护栏、test_config.py docstring 过期、`.env.example :8` 组划分行；决策点 A（es_enabled 翻 true 建议批次 5 消费面就绪后一并翻）+ 决策点 B（content_hash 方案 A **已裁决采纳，实现随批次 5**） |
| `.zcode/plans/plan-m3-b18-es-write-20261125.md` | 先例形态：§必答项带建议值 / §触点清单 / §TDD 用例 / §验收门禁 / §11 实施序 / §13 偏离表纪律 |
| 门禁级前置事实（ask 给定） | kb_chunks 25979 行 embedding 全零向量占位——pgvector 检索腿为退化通道；向量通道真实化是「效果口径」先决条件；ES BM25 腿（25979 chunks 已回填）不受影响 |

### 0.2 本会话实测（检查已跑，非转述）

| 检查 | 命令 | 结果 |
|---|---|---|
| 零向量占位复核 | `docker exec ai-search-postgres-1 psql -U ai_search -d ai_search -t -A -c "select count(*) total, count(*) filter (where embedding = zero-vector) from kb_chunks"` | **25979 \| 25979**——全量零向量占位（与 ask 门禁级前置事实一致） |
| embedding 唯一写入口 | `grep -rn "embedding=" apps/search-api/app/ --include="*.py"`（排除 test/__pycache__） | **唯一命中 `app/rag/pipeline.py:289 embedding=embedding_placeholder()`**——全仓无真实 embedding 客户端调用 |
| pgvector 检索腿零实现 | `grep -rn '<=>\|cosine_distance\|max_inner_product' apps/search-api/app/ --include="*.py"`（排除 test） | **零命中**——pgvector 距离算子全仓零消费，检索腿实现面全新 |
| content_hash 现行落点 | `apps/search-api/app/rag/chunker.py:1041` | `document_content_hash=content_hash(document_text)`——方案 A 单点改动位置 |
| fuse_rerank 现行基数 | `apps/search-api/app/agent/nodes/fuse_rerank.py:240-322` | rerank 输入=state.fused 全量无上界（ADR-017 决策④「行为增量」实证）；`_source_item`（:153-166）硬编码 `source_type:"web"` + `kb_doc_id:None`——KB 卡扩展面 |
| Source 落库缺 KB 列接线 | `apps/search-api/app/services/turn_service.py:1773-1785` | `Source(...)` 构造不写 kb_doc_id/kb_chunk_ids——列已在 0013 迁移（db/models/source.py:45-47），消费面接线归本批 |
| merge_dedupe 丢无 url 项 | `apps/search-api/app/agent/nodes/merge_dedupe.py:37-38` | `merge_candidates` 对缺 url 项静默丢弃——KB 候选（无 URL）不能走现行 URL 归并主路径，需 RRF 旁路 |
| search_fanout 现行 | `apps/search-api/app/agent/nodes/web_search.py:52-88` | 四目标判定序 ①direct/recall→synthesis ②空 queries→merge_dedupe ③Send("web_search")×N——kb_route 扩展位在 ③ 分支内 |
| fast 拓扑 | `apps/search-api/app/agent/graphs/fast.py:40,55-58` | `search_fanout_targets=["web_search","merge_dedupe","synthesis"]`；kb_search 插入位=Send 扇出目标集扩展（ADR-012 子图内改） |
| reflect 入口 | `apps/search-api/app/agent/nodes/deep_reflect.py:355-375` | `trip = _pre_trip(...)` 判定后进 LLM——置信度门控插入位在 trip 判定后、LLM 调用前（节点内步骤级扩展） |
| content_guard 原语 | `apps/search-api/app/agent/safety/content_guard.py:135-178` | `filter_candidates(candidates, guards, stage=)` 现成接线原语——kb_search/deep 工具面直接复用 |
| external_content 包裹 | `apps/search-api/app/agent/external_content.py:21-30` | 共享 helper 单一实现——KB 卡包裹复用（kind="kb"） |
| 溯源 O(1) 物化 | `apps/search-api/app/rag/storage.py:154 get_ir_json`（内置 rebuild_siblings） | §4.6 端点走 kb_chunks.meta 物化字段（契约 §1.8/§1.18），不拉 ir.json；prev/next 重建已在 storage 层（契约响应无 prev/next 字段——端点按冻结契约实现零扩展） |
| ES indexer 现状 | `apps/search-api/app/rag/indexers/es_indexer.py:116-126` | bulk 逐 item 失败聚合成 `EsIndexError`（无 status）→ `is_retryable_es_error=False` → pipeline 不重试——必答项③现状基线 |
| defaults kb 节 | `apps/search-api/app/config_center/defaults.py:304` | 13 键与 nacos-config §3 示例逐字同构（parity 先例） |
| validator KbSpec | `apps/search-api/app/config_center/validator.py:1104-1263` | kb 节四源校验在位（交叉约束 max_rerank_candidates≥kb_fuse_top_k 等）——新键随批扩 |

### 0.3 契约锚点（冻结方向，本设计不得冲突）

- **ADR-017 漏斗三数与降级链**（本计划零改动——检索面全部消费已钉键值）；
- **§6.4 第 10 条顺序锁**（过滤先于包裹）与 B15 判据（flag/drop、errors 留痕不置 degraded）；
- **api.md §4.6**（本批纯实现，零契约变更）；**§7.2/§7.4**（stage_key 枚举零扩容；extra 为通用 dict——扩键沿 extra.strategy〔v1.14〕/decision_source〔v1.26〕先例）；
- **data-model §1.7**（content_hash 方案 A 语义注——本批升版 v1.21 落字）；**§1.8**（meta 七键投影零扩展；检索语句参数绑定）；
- **批次 5 终审 P0 教训（v1.70 供给面注）**：引用供给编号恒等——KB 卡随 fused 序全量供给与落库，编号映射只由 synthesis 供给面承担，禁止「命中才复用」紧缩。

---

## 1. 范围边界（先钉边界再钉决策）

**本计划 = B19 混合检索 kb_search 接真 + 索引语义升级**。含三面：
1. **索引面**：content_hash 方案 A 落地（chunker 单点+fixture 锁定）、heading_path 前置进 embedding 输入（护栏 ≤200 字符保叶端）、embedding 客户端接线（`rag/embedding.py` 新模块）、存量 content_hash 基线重算（必答项①）、存量向量真实化回填（必答项②）；
2. **检索面**：kb_search 节点（双通道 Send 扇出）、rag/retrieval.py 通道封装、merge_dedupe RRF 融合收口、fuse_rerank 基数上界截取+KB 来源卡、kb_route 三档+fast 有界纠错轮、deep reflect 置信度门控、deep 子代理 kb_search 工具、溯源端点后端、KB chunk 进上下文安全接线、TurnRunner Source 落库 KB 列接线；
3. **P3 随手清单**（七项，见 §2.4）。

**不含（如实列出，防静默缩窄）**：
- **前端消费面**（frontend-api.md / api.d.ts StageExtra 扩键镜像 / F6 溯源 UI）——契约面零变更判定成立（extra 通用 dict 先例），前端镜像归实施批评审后随 F6 工单；溯源端点前端组件已是 F6 范围（ARCH §8）；
- **ARCHITECTURE / PROGRESS / README / DEV 巡检写回**——收官主会话（ask 纪律）；
- **es_enabled / embedding_enabled 运行态翻键**——必答项⑤裁决为用户收官动作，工作流不自动执行；
- **rerank 模型/超时面**——既有 `kb.rerank_timeout_s`/`model.mapping.rerank` 消费零改动；
- **deep 图拓扑**——reflect 门控为节点内步骤级扩展、deep 子代理工具为工具面新增，deep 子图编译结构零触碰。

---

## 2. 工单分解与实施序

### 2.1 第一线：索引面（可先行独立闭环，先于检索面）

| 步 | 内容 | TDD 载体 |
|---|---|---|
| A1 | `rag/embedding.py` 新模块：`build_embedding_input`（纯函数：标题轨迹前置+200 字符护栏保叶端）+ `embed_texts`（LiteLLM 网关 /embeddings，model 别名读轮内映射快照 `embedding` 键缺省回落内置 text-embedding-v4；批窗切、失败重试 1 次、失败异常上抛） | unit 全 mock（httpx MockTransport 先例）红→绿 |
| A2 | content_hash 方案 A：`chunker.py:1041` hash 输入 `document_text + "\n" + 标题结构清单`（标题结构清单=单趟 DFS 中 heading_enter 时收集的全量 heading 轨迹 join 串按序拼接） | fixture 锁定六态红→绿（§7 T1 组） |
| A3 | pipeline chunking 段接 embedding：`_run_chunking` 内 replace_chunks 前按 `kb.embedding_enabled` 分支（true=embed 全量 chunks 真向量入库；false=零向量占位**逐字节现状**）；embed 失败 → failed（`chunking: embedding 写入失败: {类型名}`，重试 1 次后耗尽） | unit 哨兵+失败语义红→绿 |
| A4 | 存量 content_hash 基线重算脚本模式（必答项①建议形态）：`scripts/kb_es_backfill.py --mode hash-realign`（遍历 ready 文档 → ir.json → chunk_forest 重算新口径 hash → UPDATE kb_documents.content_hash；零重导零重嵌） | 脚本 audit/hash-realign 只读面 unit |
| A5 | 存量向量真实化回填脚本 `scripts/kb_embedding_backfill.py`（必答项②建议形态）：backfill（行级零向量判定→分批 embed→行级 UPDATE；幂等=非零跳过）/ audit（零向量统计对账）；连接异常 per-文档隔离记账续跑（P3#5 同款教训） | unit 全 mock 红→绿 |
| A6 | data-engineer 估时先行：小批量（10 文档）真调用实测 per-文档耗时与 token 成本 → 推全量报告 → 用户确认后全量回填 | 估时报告落 `docs/research/`（实施批产出） |
| A7 | **英文词法加固（§11 增补，2026-11-26 用户裁决）**：`es_mapping.py` content 增 `fields.en` multi-field（内置 `english` analyzer=standard tokenizer+lowercase+porter 词干化，零新依赖）→ `make kb-es-backfill` 全量重建（25979 chunks；es_enabled=false 灰度期零生产读，重建零生产影响）→ 新形态 golden 复测（§8.1 增补门）；附属项=jieba 噪声 token 探针（临时索引验证自定义 analyzer 可过滤性，可行且无回归才换装、不可行留档） | unit：mapping 断言 `fields.en` 形态（analyzer=english），红→绿 |

### 2.2 第二线：检索面（索引面 A1~A3 后并行开工；kb_search 节点走 DEV §5.4 五步闭环）

| 步 | 内容 | TDD 载体 |
|---|---|---|
| B1 | `rag/retrieval.py` 通道封装（ADR-017 决策⑨）：`KbRetriever` 协议——`bm25_search(query, user_id, k)`（ES multi_match **content+heading_path^2+content.en**〔§11 增补：content.en 经内置 english analyzer 独立分词——ES multi_match 逐字段用各自 search_analyzer 分析查询，同份 query 天然双路；查询形态沿基线探针 §3.1 版本化 bump〕；user_id term 恒携带）+ `vector_search(query_text, user_id, k)`（查询文本自身 embed→pgvector cosine top-k，参数绑定）+ `kb_available()`（es_enabled ∧ embedding_enabled 通道判定）；生产装配 `get_kb_retriever()`，configurable 注入缝 | unit 全 mock（retriever 协议假体）红→绿 |
| B2 | `agent/nodes/kb_search.py` 节点（五步闭环第 1~3 步）：Send 扇出并行副本同构 web_search——每 query 双通道 asyncio.gather 并行（`kb.dual_channel_timeout_s=3` 包络）→ 逐候选 filter_candidates（B15 顺序锁第一半：**过滤先于写 candidates/ref_no 冻结**）→ state.candidates（add reducer 与 web_search 同键）+ errors 留痕不置 degraded；es_enabled=false → **零 ES 调用**（分支先于客户端）；embedding_enabled=false → 向量腿短路零 pg 调用；双通道全断 → KB 零候选自然回退纯网络（降级链第一级） | unit 哨兵+降级红→绿；integration 真 ES+pg |
| B3 | merge_dedupe 扩展（RRF 融合收口落点）：KB 候选旁路现行 URL 归并（无 url 不再静默丢）→ 新纯函数 `rrf_fuse_kb`：跨 query×通道 RRF(k=60) 同 chunk 累计去重 + type_weights 题型权重 + 同章节归并（同 kb_doc_id+heading_path 前缀兄弟块留最高分）+ kb_fuse_top_k=20 截取 → KB 卡并入 fuse 输入流；web URL 归并行为**逐字节不变**（负例不变式锁死） | unit 纯函数红→绿 |
| B4 | fuse_rerank 扩展：合并基数截取 `kb.max_rerank_candidates=30`（合并超限按 origin/RRF 得分截取，被截者不进 rerank 不丢来源池——行为增量契约钉定显式测试）+ KB 来源卡 `_source_item` 扩展（source_type="kb"、kb_doc_id/kb_chunk_id 恒带、url=None、snippet=chunk.content 截 500、title=heading_path 尾段‖doc title）+ rerank 仅 KB 命中门控不变 | unit 红→绿（基数上界 31 候选断言 rerank 恰 30） |
| B5 | TurnRunner Source 落库接线：`_publish_and_persist` Source 构造补读 batch item `kb_doc_id`/`kb_chunk_id` → 列 kb_doc_id/kb_chunk_ids（D2-01 截断护栏兼容——kb_doc_id 为裸 uuid 列非限长 varchar） | unit 红→绿 |
| B6 | kb_route 三档 + fast 有界纠错轮（五步闭环；语义与阈值=必答项④建议值）：rewrite 结构化输出 +`kb_route` 字段（缺省 hybrid 灰度；decision/light 两路径兜底同落 hybrid）；search_fanout 判定序扩展（①direct/recall 不动 ②kb_only/kb_first → Send 仅 kb_search ③hybrid/缺省/非法 → web+kb 双 Send）；fuse_rerank 后确定性信号 gate → 纠错回边 rewrite 一次（轮帽 1 次，子图内条件边） | unit 红→绿（路由矩阵+回边轮帽） |
| B7 | deep reflect 置信度门控：`reflect` 节点 trip 判定后插确定性信号 gate（零 LLM）——全绿直接 sufficient 走 done 出口（拓扑零改动）；有红 → 现状 LLM 深评 | unit 红→绿 |
| B8 | deep 子代理 kb_search 工具（检索等价升级）：`deep_tools.py` 增 `kb_search(query)` @tool——经同一 KbRetriever + filter_candidates + external_content 包裹（kind="kb"）返回（deep web_search 工具先例同构，deep_research.py:464-511 形态） | unit 全 mock 红→绿 |
| B9 | 溯源端点后端（普通 TDD 面）：`KbService.get_chunk_drilldown` + `api/v1/kb.py` GET `/sources/{source_id}/kb-chunk` + `schemas/kb.py` KbChunkDetail DTO——按 §4.6 冻结契约六必选+两可选实现；404001 已删除/越权/不存在单码；非 KB 来源 400001 | unit+API 测试红→绿 |
| B10 | fast 有界纠错轮 SSE 留痕：stage(rewrite,done) extra `kb_route`（+纠错轮 `correction:true`）键可省——extra 通用 dict 扩键（api.md 零面判定行的实现载体） | unit 断言 extra 形态 |

### 2.3 第三线：效果口径验收（依赖 A5~A6 回填 + B 线全绿）

- 黄金集混合口径复测（§8 验收门禁）→ 必答项⑤收官翻键（用户执行）。

### 2.4 P3 随手清单（ask 点名七项，随对应步骤顺手清）

| # | 项 | 去向 |
|---|---|---|
| 1 | bulk-item 429/503 分型重试裁决（B19 前必答） | **必答项③**，本批落地（§4.3 建议=分型 retryable）——落 `es_indexer.index_chunks` item-status 解析 + `is_retryable_es_error` 扩展 + 回归单测 |
| 2 | 删除腿 404 短路 | B1 顺手：`es_indexer.delete_document` 捕获 404 index_not_found → 静默返回（灰度期 WARN 噪音消除） |
| 3 | openpyxl dev 组显式声明 | 随 A6 估时批：`apps/search-api/pyproject.toml [dependency-groups] dev` 加 `openpyxl==3.1.5`（probe 直依赖） |
| 4 | 回填脚本连接异常隔离 | A5 同款纪律：kb_es_backfill per-文档 try 续跑（P3#5） |
| 5 | test_config.py docstring 过期表述 | 顺手清：「零 .env 可跑：空 auth 零鉴权形态」→ ES_PASSWORD 必填形态（b4 §6.5 留档） |
| 6 | elastic_transport TRACE 护栏单测 | 顺手加：断言 `elastic_transport.trace` logger 未置 TRACE 级（b4 §6.4 之 5——TRACE 打印请求体） |
| 7 | `.env.example :8` ES 组划分行 | 顺手补：组划分注释行补 `ES_`/`FEISHU_`（b4 轻审留档；注释级零变量改动） |

另收编：**ai_search_app 最小权限角色**（b4 §6.4 之 2，B19 前）——归实施批 devops 步（角色 read/write/manage on `ai_search_*`；应用侧零改码只换 .env 两键；compose ELASTIC_PASSWORD 保持超管；**凭据零打印**，.env 由用户自改）。

---

## 3. 决策一：索引面（钉死）

### 3.1 content_hash 方案 A（用户已裁决 2026-11-26，不得重开）

- **hash 输入** = `document_text + "\n" + 标题结构清单`；标题结构清单 = IR 全部 heading 节点的完整 `heading_path` join 串（" > "）按 DFS 序逐行拼接（与 chunk 单轨迹同一物化口径——含目录链/接缝去重/回显剔除/星号剥离全部既有规则，复用 `_DocState.heading_path` 同源实现）。
- **落点**：`chunk_document` 单趟 DFS 内 `_DocState` 增结构收集器（heading_enter 时 append）；`chunker.py:1041` 改 hash 输入。**单点改动**——chunk 级 content_hash（chunker.py:963，不入库）与 document_text 语义零变化。
- **负例不变式（fixture 锁死）**：无 heading 文档 → 结构清单空串 → hash 输入与现行逐字节一致（hash 值不变）。
- **效果传导链**：标题改名/增删/移动 → document_content_hash 变 → 飞书同步 diff 判定序④（obj_edit_time 缺失/0 时）content_hash 比对判变更 → 整文档单元三存储级联重导 → 重切 + **重嵌**（3.2 真向量）+ ES 重写（B18 已幂等）——向量与轨迹永不陈旧。判定序②（catalog_path 全串）已有通道不变。
- **契约落字**：data-model §1.7 语义注升版（本批已落 v1.21，见 §5.1）。

### 3.2 heading_path 前置进 embedding 输入（用户裁决带护栏）

- **输入文本** = `标题轨迹：{" > ".join(heading_path)}\n{content}`；
- **护栏（代码常量）**：轨迹串截断 ≤ `EMBEDDING_TRAJECTORY_MAX_CHARS = 200` 字符**保叶端**（leaf 侧语义最具体；root 侧文档题名已由 doc 级承担——B18 plan §9① 原裁决护栏字面）；常量落 `rag/embedding.py` 模块头（配置三分法第三行——纯逻辑防御值不入 Nacos，沿 IMAGES_MAX_COUNT 判例）；
- **消费点**：pipeline `_run_chunking`（3.3）与 A5 回填脚本与 deep/kb 查询向量化（B1 `vector_search` 查询侧——查询文本无轨迹，仅内容侧 embed，零前缀）——同一 `build_embedding_input` 单一事实源。

### 3.3 pipeline chunking 段 embedding 接线（钉死）

| 项 | 钉定 | 理由（含被否备选） |
|---|---|---|
| 落点 | `_run_chunking` 内、`replace_chunks` 调用前：`kb.embedding_enabled=true` → `embed_texts([build_embedding_input(c.content, c.heading_path) for c in chunks])` → Chunk 带真向量 → replace_chunks 入库；false → 现行 `embedding_placeholder()` 逐字节不变 | ①向量与 chunk 行同批写入原子（无中间态窗口）；②embed 是外部 SDK 调用，发生在 replace_chunks 短事务之前——不占库连接（service 层短事务纪律）；③resume-from-chunking 幂等（先删后插，代价=重复 embed 调用）。**被否**：indexing 段 UPDATE 补写——两段动 pg 行、零向量中间态窗口、resume 语义复杂化 |
| 失败语义 | embed 失败重试 1 次（连接类）→ 耗尽 `mark_failed("chunking: embedding 写入失败: {类型名}")` 不静默 ready——对齐 B18 ES 失败口径（契约 §1.19 不静默 ready 精神）；重试中状态仍 chunking | 零向量静默入库会让「混合达标」静默退化（ask 门禁级前置事实教训）——失败必须显性化 |
| 开关 | `kb.embedding_enabled`（严格布尔，缺省 **false 灰度安全**——零向量=现状逐字节不变哨兵；翻 true=用户收官动作，与必答项⑤ es_enabled 同窗） | 沿 es_enabled v1.43 灰度先例；「存量轮零行为变化」纪律 |
| 模型与维度 | 别名=轮内映射快照 `model.mapping.embedding`（缺省回落内置 text-embedding-v4——nacos §5 :379 已钉）；维度=1024（`kb_settings.embedding_dim`——data-model §1.8:316 / config.py:231 已钉）；响应维度≠1024 → 异常 failed（防静默脏向量） | ask 门禁级前置事实钉定口径：模型与维度按契约/defaults 已钉原文核对一致；OQ-32（本地 CPU 进程内）只涉归一化模型（PaddleOCR），embedding 走网关不适用。**2026-11-26 用户裁决：维度评测提案议后维持 1024 不评测不翻档**（探针实证：DashScope 原生支持 dimensions 64~2048 直连生效；**litellm 网关 embedding 路由丢 dimensions 参数恒回 1024**——换档工程线=网关透传修复+embed_texts 参数+DDL vector(N)+全量重嵌二次计费，后续有需要再议，本批零新增工序） |
| 通道退化联动 | embedding_enabled=false 时检索侧向量腿短路（B2）——零向量行不得进 cosine 查询（零向量得分同质=噪声候选污染融合） | 必答项②时序钉定：回填完成 → 用户翻键 → 向量腿才启用 |

### 3.4 存量处置（必答项①②建议形态，裁决后实施）

- **①存量 content_hash 基线重算**（§2.1 A4）：1022 ready 文档均有 ir.json（B16 起落桶）——重算走 `chunk_forest` 同一代码路径（零算法复制，沿 backfill 复用 `build_es_doc_meta` 先例），UPDATE kb_documents.content_hash 到新口径基线；零重导零重嵌。不迁移的后果=飞书定时 diff 判定序④对存量**恒判变更**→ 首轮全量误重导+重嵌（1022 文档 embedding 计费）——重算成本（纯 MinIO GET+hash）远低于误重导，推荐 (a)。
- **②存量向量真实化回填**（§2.1 A5~A6）：独立脚本（语义与 ES backfill 不同——外部 API 计费、失败面不同）；行级判据=零向量（本会话 SQL 实测形态）；幂等=非零跳过；**回填输入=新口径（heading_path 前置轨迹）**——与 3.2 前置后的新块向量输入同构，混用两口径向量会撕裂向量空间（ask 必答项②钉「回填输入形态一并钉」）；小批量估时先行再全量（data-engineer 纪律）；完成后与 es_enabled 同窗翻 `kb.embedding_enabled=true`。

---

## 4. 决策二：检索面（钉死）

### 4.1 kb_search 节点（五步闭环；ADR-017 决策①⑦⑧）

- **形态**：`agent/nodes/kb_search.py`，与 web_search 同构 Send 扇出并行副本（fast.py `search_fanout_targets` 加 `"kb_search"`；`search_fanout` ③ 分支双 Send——web_search×N + kb_search×N）；节点经 configurable 注入 KbRetriever（registry/chain 同款注入纪律，节点纯函数零全局单例直连）。
- **每 query 副本**：`asyncio.gather(bm25_search, vector_search)` + `asyncio.timeout(kb.dual_channel_timeout_s=3)` 包络 → KB 候选条目 `{chunk_id, kb_doc_id, title, snippet=content 截断, score, origin:"kb", node_id, heading_path}` 写 state.candidates（add reducer）。
- **降级链三级（契约钉定逐字）**：KB 空/无命中→纯网络（既有）→ ES 不可用/熔断/es_enabled=false→pgvector 单通道（errors 留痕 `kb_es_unavailable`）→ embedding_enabled=false 向量腿短路（errors 留痕 `kb_vector_unavailable`）→ 双通道全断 KB 零候选不影响网络侧。**errors[]+stage(search,done) extra 留痕不置 degraded**（§6.2 v1.41 二分口径②——degraded_reason 枚举封闭面零触碰）。
- **B15 顺序锁第一半**：写 candidates 前 `filter_candidates`（title+snippet+content，guards 经 Send 输入投影进节点——web_search.py:80-85 先例同构）；包裹在 synthesis/_sources_block 与 deep 工具返回处（B15 判据「过滤先于转义包裹」天然满足）。
- **哨兵**：es_enabled=false + embedding_enabled=false → 零 ES 零 pg 调用 + 轮行为与现状逐字节一致（六态 IN 列表逐字断言保持绿）。

### 4.2 RRF 融合收口落 merge_dedupe（钉死）

- **落点选择**：merge_dedupe 是 fast 子图唯一汇聚节点（WS→MG←KS 图拓扑既有）——RRF 跨 query×通道合并天然在此收口（ADR-017 ②字面「跨 query×通道合并」要求全副本完成后的单点）。**被否**：kb_search 副本内 RRF（跨 query 语义不成立）；新独立节点（fast 拓扑加节点=超 ADR-012 子图内改边界，且 ARCH mermaid 拓扑零新节点）。
- **`rrf_fuse_kb` 纯函数**（merge_dedupe.py 模块级，unit 直驱）：
  1. KB 候选按 (kb_doc_id, chunk_id) 归并——跨 query×通道同 chunk 累计 `score = Σ_channels Σ_queries w_channel × 1/(rrf_k + rank_i)`（rrf_k=60 spec 读；w_channel=type_weights[query_type] 的 [bm25_w, vec_w] 分轴权重）；
  2. 同章节归并：同 kb_doc_id 且 heading_path 去叶端同前缀的兄弟块仅留最高分（调研 §13.6③「同章节归并去重」——G059 形态的兄弟块收敛面）；
  3. 截取 kb_fuse_top_k=20 进融合段。
- **web 侧 URL 归并逐字节不变**（负例不变式：零 KB 候选轮 merge_dedupe 输出与现状全等）。

### 4.3 fuse_rerank 扩展（钉死）

- **基数上界**：合并（web URL 归并 + KB RRF 输出）后、rerank 前全局截取 `kb.max_rerank_candidates=30`——超限按 origin/RRF 得分降序截取；**被截者不进 rerank 不丢来源池**（来源池=candidates 全量；sources 事件只发 fused topN——供给面语义不变）；**行为增量契约钉定随 TDD 显式测试**（R27），不做静默改。
- **rerank 门控不变**：仅 KB 命中（has_kb_hit）时调用（fuse_rerank.py:262 现状）；失败兜底按 origin 得分序（现状）。
- **KB 来源卡**：`_source_item` 扩展——`source_type="kb"`、`url=None`、`kb_doc_id`/`kb_chunk_id` 恒带、`title`=heading_path 尾段‖kb_documents.title 投影、`snippet`=chunk.content 截 SNIPPET_MAX_CHARS=500、`score`=RRF 分；`snapshot_available=False`。**引用供给编号恒等**（批次 5 终审 P0 教训）：KB 卡随 fused 序 enumerate 供给 [n]、全量落库（B5）——禁止紧缩。
- **综合上下文注入**：KB 卡经 `_sources_block` 既有转义包裹——`source="kb:{doc_id}"` + `type="kb"`（§6.4.3 铁律原文已含 `kb:doc_id` 形态；external_content 共享 helper kind 参数承载）；顺序锁由 4.1 前置过滤满足。

### 4.4 kb_route 三档 + fast 有界纠错轮（必答项④建议语义——裁决后按此实施）

**路由矩阵（search_fanout 判定序扩展；①direct/recall→synthesis 与 ②空 queries→merge_dedupe 两级不动）**：

| kb_route（rewrite 输出，缺省/非法/两兜底路径一律 `hybrid`） | Send 形态 | 后续 |
|---|---|---|
| `hybrid`（缺省灰度） | web_search×N + kb_search×N 双 Send | fuse_rerank → synthesis（纠错轮关闭时=现状+KB 增量，行为面只有 RRF 增量） |
| `kb_first`（先 KB 判足不足再 web） | 仅 kb_search×N | fuse_rerank 后确定性信号 gate：**足** → synthesis；**不足** → 纠错回边一次（带反馈重写 → 强制 kb_route=hybrid 第二趟 web+kb 并行）→ synthesis |
| `kb_only`（显式内部知识信号只查 KB） | 仅 kb_search×N | fuse_rerank → synthesis；**KB 零候选兜底 = 转纠错回边一次补 web**（errors 留痕 `kb_only_fallback_web`——零源 500101 对用户显式指令不友好，兜底转 web 沿「拿不准一律 web」安全侧） |

**确定性信号 gate（纯函数，零 LLM）**——判定输入=fused KB 卡集：零 KB 命中 ∨ top1 KB RRF 分 < `min_top1_score` ∨ KB 来源数 < `min_kb_sources` → 不足；反之为足。**轮帽 1 次**：纠错回边经 state `correction_round` 标记，第二次 gate 不足不再回边直达 synthesis（兜底产出语义 v1.25 既有面）。

**灰度安全**：`kb.route.default="hybrid"` 缺省 + rewrite 输出 `kb_route` 缺省/非法/decision 与 light 两兜底路径一律 hybrid = 存量轮行为面仅增 KB 候选（RRF 增量），纠错轮 `correction_enabled` 缺省 **false**（kb_first/kb_only 信号不足直达 synthesis=现状路径）；M4 评测把关后逐档放开（ARCH B19 行「全部灰度默认关」字面）。**强制回 hybrid**：retriever 不可用（es_enabled=false ∧ embedding_enabled=false）→ kb_only/kb_first 路由降级为 hybrid Send 形态（web 照跑）。

**SSE 面（api.md 零面判定行的依据）**：纠错轮 = stage(rewrite,running/done)+stage(search,…) **既有事件重复发射**（§7.2 stage 条「同 key 多次出现合法——前端按 key 就地更新」既有字面，web_search docstring 同源）；kb_route/correction 走 `stage(rewrite,done).extra` 通用 dict 扩键（extra.strategy v1.14 / decision_source v1.26 先例同款——键可省、不置 null、前端零渲染义务）；`kb_funnel` 观测投影（per_query_top_k/rrf_k/kb_fuse_top_k/es_hits/vec_hits/type_weights）走 stage(search,done) extra 可选键（A35 可观测留痕）。**零新事件类型、零新字段 schema、零 stage_key 扩容**。

### 4.5 deep reflect 置信度门控（钉死）

- **插入位**：`reflect` 节点 `_pre_trip` 判定后、LLM 调用前（deep_reflect.py:365 之后）——**节点内步骤级扩展，拓扑零改动**（ADR-016 decision 前置同款先例）。
- **gate（零 LLM，纯确定性）**：`len(collected_sources) ≥ min_sources` ∧ `top1 score ≥ min_top1_score` ∧ `failed 比例 ≤ max_failed_ratio` ∧ `outline 要点覆盖率 ≥ min_outline_coverage` 全绿 → 直接 sufficient（`gaps=[]` + 走既有 done 出口——跳过 LLM 判定）；任一红 → 现状 LLM 深评（零行为变化）。**M4 先做历史 trace gate 绿率离线分析再定默认档**（ARCH B19 行③字面）——`deep.reflect_gate.enabled` 缺省 false 灰度。
- **键位**：`deep.reflect_gate` 子节（归 deep 节——信号跨 web/kb、消费面在 deep 图；阈值组 {enabled, min_sources, min_top1_score, max_failed_ratio, min_outline_coverage} 建议值见 §5 必答项④）。

### 4.6 溯源端点后端（普通 TDD 面；api.md §4.6 纯实现零契约变更）

- **链路**：sources 行（id+user_id 归属校验）→ source_type≠"kb" → 400001 → kb_chunk_id（§2.5 来源行）→ kb_chunks 行（user_id 双过滤）→ kb_documents 行——任一缺失=已删除/越权/不存在 → **404001 单码**（不泄漏存在性，契约字面）。
- **投影**：六必选（chunk_id/document_id/document_title/content/heading_path 数组/page/paragraph——meta.heading_path jsonb list 直投、meta.loc {page?, para?} 缺则 null 键恒出现）+ 两可选（node_token/node_id——`meta.node_id` 形态 `fs:node:{token}` 解析 token；`fs:blk:` 前缀无 token 或 `loc:` 前缀 → 两键省略，沿 v1.12 images 可选键先例不置 null）。**不拉 ir.json**（溯源 O(1) 契约走 meta 物化字段；get_ir_json 的 rebuild_siblings 是下钻 UI 预留、契约响应无 prev/next 字段——端点零扩展）。
- **落点**：service `KbService.get_chunk_drilldown`（DTO 进 DTO 出）+ 路由 + `schemas/kb.py` KbChunkDetail + OpenAPI 信封组件同步（§0 信封纪律）。

### 4.7 deep 子代理检索等价升级（钉死）

- `deep_tools.py` 增 `kb_search(query)` @tool：经 KbRetriever（deps 注入）→ filter_candidates（drop 不占 ref_no——add_candidates 前过滤，deep_research.py:493 先例同构）→ 命中行 `[ref_no] title（score）` + `<external_content source="kb:{doc_id}" type="kb">` 包裹返回；零命中返回系统注引导（web_search 工具 :511 空结果字面先例）。子代理工具描述最小权限面（批次 7 安全审计同族）。

---

## 5. 必答项清单（每项带建议默认值+理由，实施按建议值落地；用户可在线热更调整）

| # | 问题 | 建议默认值 | 理由 |
|---|---|---|---|
| ① | 存量 content_hash 迁移形态（1022 ready 行为旧口径 hash——不迁移则飞书判定序④对存量恒判变更首轮全量误重导） | **`kb_es_backfill.py --mode hash-realign` 一次性重算基线**（ir.json→chunk_forest 同代码路径重算→UPDATE content_hash；零重导零重嵌；只读 ir.json 非幂等问题、可重复跑） | 重算成本=纯 MinIO GET+hash（分钟级）；误重导成本=1022 文档三存储级联+embedding 计费；重算后新旧判定序④口径一致 |
| ② | 存量向量真实化回填形态（25979/25979 零向量占位——本会话 SQL 实测复核） | **`kb.embedding_enabled` 缺省 false 灰度**（零向量=现状逐字节不变哨兵）+ **独立回填脚本 `kb_embedding_backfill.py`**（行级零向量判定→分批 embed→行级 UPDATE；幂等非零跳过；连接异常 per-文档隔离）+ **回填输入=新口径（heading_path 前置轨迹，与 3.2 同一 build_embedding_input）** + **小批量（10 文档）估时先行→推全量报告→用户确认→全量回填**；回填完成前向量腿短路（`kb_vector_unavailable` 留痕）、完成后与 es_enabled 同窗翻 true | 零向量行进 cosine 查询=全 0 同质得分噪声候选——通道级短路而非行级过滤（全表零向量过滤无索引可用）；混用两口径输入撕裂向量空间；灰度缺省保「存量轮零行为变化」；模型=text-embedding-v4（nacos §5 已钉）/维度=1024（§1.8 已钉）核对一致；OQ-32 本地 CPU 裁决只涉归一化模型不适用 embedding |
| ③ | bulk-item 429/503 处置（现状聚合 EsIndexError 无 status → 不可重试直 failed——es_indexer.py:116-126 实测） | **分型 retryable**：index_chunks 解析失败 item status——全部 ∈ {429, 502, 503, 504} → 抛带 status 的 EsIndexError（`is_retryable_es_error` 识别为 retryable，复用 pipeline 既有 1s/3s 退避预算）；含其他 4xx（400/413 等）→ 不可重试立即 failed；**不逐 item 重投**（整文档重发幂等——先删后 bulk+确定性 _id，逐 item 重投复杂度不抵收益） | 429/503 与连接类同属瞬态（ES 索引段重试预算已在位零新增）；b4 P3#3 test-engineer 定性「半覆盖真实缺口」；单节点低并发概率低但失败语义应与异常级一致 |
| ④ | kb_route 三档判定语义与阈值键位（ARCH B19 行钉机制未钉值） | **语义**：hybrid 缺省灰度（rewrite 输出缺省/非法/两兜底一律 hybrid）；kb_first=先 KB 信号 gate 足→直达/不足→纠错回边一次（强制 hybrid 第二趟 web+kb）；kb_only=只查 KB、零候选兜底纠错回边补 web；纠错轮帽 1 次、`kb.route.correction_enabled` 缺省 false（三档均不纠错=现状路径，M4 把关后放开）。**键位与建议值**：`kb.route` 子节 {default:"hybrid", correction_enabled:false, min_kb_sources:2, min_top1_score:0.05（RRF 归一量级——1/(60+1)≈0.0164/通道，双通道命中 ≈0.03+，0.05=双通道同命阈值上浮）}；`deep.reflect_gate` 子节 {enabled:false, min_sources:5, min_top1_score:0.3（rerank relevance 量级）, max_failed_ratio:0.2, min_outline_coverage:0.6}——全部【M4 评测后调】不冻结；单键热更零重启 | 机制=ARCH B19 行已钉字面（零命中/top1 分数/来源数三类确定性信号）；RRF 分数量级推导自 k=60 公式；阈值属运行调优面（配置三分法第二行=Nacos）；灰度缺省保存量轮零行为变化；kb_only 零源 500101 对显式指令不友好→安全侧兜底转 web |
| ⑤ | es_enabled 翻 true 时点 | **B19 门禁全绿 + 黄金基线不低于纯 BM25 基线（锚点 @1≥97/@5≥98/@10≥98、摘录 @1≥99/@5=100/@10=100）后，收官由用户 Nacos 单键翻 `kb.es_enabled=true` + `kb.embedding_enabled=true`（同窗两键）；工作流不自动执行翻键**；翻键前「只写不读窗口」由回填先行消除（B18 已回填 25979 chunks） | b4 决策点 A 建议「批次 5 消费面就绪后一并翻，避免只写不读窗口」；ask 必答项⑤建议原文钉定；运行态灰度缺省双 false=存量零行为变化 |

---

## 6. 契约增量（本批已落地——三件升版纪律=版本格 bump+判定理由）

| 文件 | 升版 | 增量 |
|---|---|---|
| `docs/contract/data-model.md` | **v1.20 → v1.21** | §1.7 content_hash 语义注升版（方案 A：hash 输入=document_text+标题结构清单；标题改名/增删/移动→整文档重导+重嵌；存量基线重算挂必答项①；零 DDL）+ §1.8 检索注补向量通道真实化时序注（embedding_enabled 通道级判定/零向量行不进查询）——**零 DDL 零迁移零列变更** |
| `docs/contract/nacos-config.md` | **v1.45 → v1.46** | kb 节新键组：`kb.embedding_enabled`（严格布尔缺省 false——索引面开关）+ `kb.route` 子节（default/correction_enabled/min_kb_sources/min_top1_score——kb_route 三档与纠错轮阈值）+ `deep.reflect_gate` 子节（enabled/min_sources/min_top1_score/max_failed_ratio/min_outline_coverage）——全部建议默认值标【M4 评测后调】+ 校验规则行同步 + 回退门行同步（correction_enabled=false / reflect_gate.enabled=false / route.default=hybrid 三重灰度回退门）——**dataId 数量零变更** |
| `docs/contract/api.md` | **版本维持 v1.33（零面判定行入版本格）** | 端点/事件/字段/错误码零新增零变更判定：kb_route/correction/kb_funnel 走 stage extra 通用 dict 扩键（extra.strategy/decision_source 先例）；纠错轮=既有 stage 事件重复发射（同 key 多次合法既有字面）；KB 来源卡承载面=§2.5/§7.2 sources 既有字段（source_type 双值 + kb_chunk_id v1.30 已批 + kb_doc_id 既有）；§4.6 溯源端点为已冻结契约的纯实现批 |
| `docs/contract/data-model.md`（**§11 增补**） | **v1.21 → v1.22（随实施批落字）** | §1.19 ES mapping 要点增行：content.en multi-field（内置 english analyzer）+ BM25 查询形态 fields 三字段（content / heading_path^2 / content.en）+ 复测口径冻结面版本化注——ES 侧非 DDL 面，mapping 事实源仍=`es_mapping.py`（§1.19 既有事实源分层口径） |

**受影响方同步判定**：frontend-api.md / `types/api.d.ts`——零类型形状变更（extra 通用 dict + sources 既有字段），StageExtra 扩键镜像（`kb_route?/correction?/kb_funnel?` 注释级）归实施批评审轮随批落，不升版；PRD——REQ-051 漏斗四键+既有键承载口径 v1.33 已钉，零变更；DEV——零变更。**§11 增补追加判定**：nacos-config——零变更（mapping 形态属实现结构无新键，通道开关既有 `es_enabled`/`embedding_enabled` 承载）；api.md / frontend-api.md——零变更（BM25 通道内部形态零 REST/SSE 面）。

---

## 7. TDD 用例清单（先红后绿；红期记录沿 B18 §13 纪律）

### T1 索引面（unit 全 mock 零网络）

| 用例组 | 断言面 |
|---|---|
| `test_kb_chunker.py`（扩展）content_hash 方案 A | ①标题改名→hash 变；②标题增删→hash 变；③标题移动（同级换位/跨级）→hash 变；④正文变→hash 变；⑤正文+标题均不变→hash 逐字节不变（回归）；⑥**无 heading 文档→hash 与旧口径逐字节一致**（负例不变式）；⑦结构清单含目录链前缀（heading_path 同源物化口径） |
| `test_kb_embedding.py`（新） | `build_embedding_input`：①`标题轨迹：{t}\n{content}` 逐字节；②轨迹 >200 截断保叶端；③空 heading_path→仅 content（零前缀）。`embed_texts`：①请求形态（model/documents）；②批窗切（≤10 代码常量）；③429/5xx 重试恰 1 次；④维度≠1024 异常；⑤失败异常上抛（消息零 content 回显） |
| `test_kb_pipeline.py`（扩展）chunking 段接线 | ①embedding_enabled=true → embed 恰调一次、行带真向量；②**false → embedding 零调用 + 行 embedding 与现状零向量逐字段相同**（字节级不变哨兵）；③embed 失败重试 1 次后 failed、前缀 `chunking: embedding 写入失败:`；④resume-from-chunking 重入队幂等；⑤REQUEUE_STATUSES 六态 IN 逐字断言保持绿 |
| `test_kb_hash_realign.py`（新） | hash-realign 模式：ready 文档重算→UPDATE 调用参数断言；非 ready 跳过；ir 缺失记账续跑 |

### T2 检索面（unit 全 mock + integration 真 ES/pg）

| 用例组 | 断言面 |
|---|---|
| `test_kb_retrieval.py`（新） | bm25_search 查询体（multi_match **content+heading_path^2+content.en**〔§11 增补〕+ term user_id 恒携带）；vector_search 参数绑定+user 过滤；kb_available 通道判定矩阵（es×embedding 四态） |
| `test_kb_search_node.py`（新）五步闭环 | ①双通道并行各恰调一次+user_id 断言；②**es_enabled=false → 零 ES 调用**（哨兵）；③embedding_enabled=false → 向量腿短路零 pg 调用；④ES 超时 → 单通道降级+errors `kb_es_unavailable` 不置 degraded；⑤filter_candidates 先于写 candidates（drop 不进、不占 ref_no）；⑥双灰度 → KB 零候选+轮行为现状一致；⑦stage(search,done) extra.kb_funnel 可选键形态 |
| `test_merge_dedupe.py`（扩展）rrf_fuse_kb | ①RRF k=60 累计公式精确值断言；②跨 query×通道同 chunk 累计去重；③type_weights 权重分轴应用；④同章节归并留最高分；⑤kb_fuse_top_k=20 截取；⑥**零 KB 候选 → web URL 归并输出与现状全等**（负例不变式）；⑦无 url KB 候选不再静默丢 |
| `test_fuse_rerank.py`（扩展） | ①31 候选 → rerank documents 恰 30（基数上界行为增量显式测试——R27）；②被截者不进 rerank 不丢 candidates 来源池；③KB 卡 _source_item（source_type=kb/kb_doc_id/kb_chunk_id 恒带/url=None）；④rerank 仅 KB 命中门控保持（纯 web 轮零调用回归）；⑤KB 卡经 external_content kind="kb" 包裹形态 |
| `test_search_fanout.py`（扩展）kb_route | 路由矩阵：hybrid 双 Send / kb_first·kb_only 单 kb Send / direct·recall 直 synthesis 不动 / 非法·缺省 → hybrid / retriever 不可用 → 降级 hybrid Send |
| `test_kb_correction.py`（新）纠错轮 | ①信号 gate 三阈值触发判定（零命中/分数/来源数）；②回边恰一次（轮帽——第二次 gate 不足不再回边）；③反馈注入 rewrite（重写 prompt 含 KB 不足反馈）；④stage 序列=rewrite/search 重复发射合法形态断言；⑤correction_enabled=false → 不足直达 synthesis（现状） |
| `test_deep_reflect.py`（扩展）门控 | ①全绿短路 sufficient 零 LLM 调用；②任一红走 LLM 深评（现状）；③enabled=false 直通；④拓扑零改动（route_reflect 出边表逐字断言不变） |
| `test_deep_tools.py`（扩展）kb 工具 | ①返回含 external_content kind="kb" 包裹；②add_candidates 前过滤 drop 不占 ref_no；③零命中系统注；④retriever 不可用降级文案 |
| `test_api_kb.py`（扩展）溯源端点 | ①200 六必选逐字段；②飞书 fs:node: 两可选键/fs:blk: 省略/loc: 省略三态；③非 kb 来源 400001；④已删除 chunk/文档/越权/不存在 → 404001 单码；⑤user_id 过滤；⑥loc.page/paragraph 可空键恒出现；⑦content 零转义直出（转义归前端 DOMPurify——契约「仅进 UI 不进 LLM」） |
| `test_turn_service.py`（扩展）落库 | sources batch kb 卡 → Source 行 kb_doc_id/kb_chunk_ids 列断言；web 卡两列 None 回归 |
| 哨兵组（保持绿） | `test_requeue_status_set_matches_contract` / es_enabled=false 分支逐字节不变（B18 哨兵族零改动保持绿）/ 六态 IN 列表逐字 |

---

## 8. 验收门禁（效果口径与结构口径分列——ask 门禁级前置事实钉定纪律）

### 8.1 效果口径（黄金集混合复测——前提=回填完成 ∧ 双通道启用测试态）

- **探针**：`probe_es_bm25_golden.py` 扩展 `--mode hybrid`（复测口径冻结面=基线报告 §3.1 查询形态/§3.2 双口径判定/§4 指标——逐字复用，RRF 融合替代单通道排序；零题面特判零调参）；
- **判据（不低于纯 BM25 基线，ask 钉定）**：锚点口径 @1 ≥97 且 @5 ≥98 且 @10 ≥98；摘录口径 @1 ≥99 且 @5 =100 且 @10 =100；**floor 值沿旧 mapping 基线原值承继（mapping 变更不抬门槛、只兜回归——§11 增补）**；
- **英文 slice 门（§11 增补，2026-11-26 用户裁决「到时候看检索质量」的判定载体）**：金标英文/混排 **10 题**（≥3 高拉丁占比 chunk 题 + ≥3 中英混排题 + ≥2 跨语言题〔中文 query→英文术语 chunk，验向量通道前的词法基线〕+ ≥2 词干形态差题〔limits/limit 类〕）——**旧形态基线先跑留档**（现 mapping+现 fields）→ mapping 变更+重建 → **新形态复测不低于旧形态基线**；复测口径冻结面 §3.1 查询形态 fields 随 mapping 变更版本化 bump（+content.en），基线报告增补节留档新旧对照；
- **G059 清零 = 跟踪目标非硬门**（基线报告 §6.2 治理面——同标题兄弟块混淆为向量+rerank 对症面，如实报告不谎报达标）；
- **乐观偏差注记沿基线报告 §1**（题面同源词面重合偏高——混合增量对照措辞偏离场景与排序精细度解读）；
- **⛔ 禁令（ask 原文钉定）**：embedding_enabled=false / 存量回填未完成状态下，**禁止用退化向量腿产出「混合达标」结论**——该状态只判结构口径（8.2），效果口径判「暂不可判」如实留档。

### 8.2 结构口径（无论向量化状态均须过）

- 双通道并行发起（gather 形态实测）；RRF 融合序（跨 query×通道累计公式单测+集成断言）；降级链三级逐级演练（es off→单通道 / embedding off→腿短路 / 双 off→纯网络）；es_enabled=false 分支逐字节不变哨兵全绿；溯源端点契约面九组用例绿。

### 8.3 常规门禁（全批 DoD）

- `make test` 全量绿（unit+integration，ES/pg 不可达用例具名 skip 非绿；发版前起栈完整跑一轮 integration 全绿——B18 §8.4 先例）+ 覆盖率 ≥80%；`make typecheck` 0（pyright+ruff 含 scripts/）；
- kb_search 节点 DEV §5.4 五步闭环逐条留痕（写测试→实现→接入图→Studio/Langfuse 观察→验收）；溯源端点普通 TDD 面；
- 双评审（代码评审员+安全审计员——**KB 语料进上下文接线=外部内容面敏感**：顺序锁、drop 不占 ref_no、user_id 恒过滤、溯源不进 LLM 四点为审计焦点）；
- 文档巡检归收官主会话（ARCH/PROGRESS/README——本批零写）。

---

## 9. 触点清单（供派单与巡检）

| 触点 | 动作 |
|---|---|
| `apps/search-api/app/rag/embedding.py`（新） | build_embedding_input 纯函数 + embed_texts 网关客户端（3.2/3.3） |
| `apps/search-api/app/rag/chunker.py` | content_hash 方案 A 单点（:1041 + 结构收集器） |
| `apps/search-api/app/rag/pipeline.py` | chunking 段 embedding 分支接线 + embedding_enabled 读键 |
| `apps/search-api/app/rag/retrieval.py`（新） | KbRetriever 协议 + bm25/vector 双通道封装（ADR-017 决策⑨落点） |
| `apps/search-api/app/rag/indexers/es_indexer.py` | 必答项③分型 retryable + 删除腿 404 短路（P3#2）+ 新增 search 方法（B1 消费或 retrieval 直用 client） |
| `apps/search-api/app/rag/indexers/es_mapping.py` | **content `fields.en` multi-field（内置 english analyzer）+ mapping 断言单测（§11 增补 / A7）** |
| `apps/search-api/app/db/models/` 或 repo | pgvector 检索仓储方法（参数绑定，data-model §1.8 语句模板） |
| `apps/search-api/app/agent/nodes/kb_search.py`（新） | 五步闭环节点（4.1） |
| `apps/search-api/app/agent/nodes/merge_dedupe.py` | rrf_fuse_kb + 节点扩展（4.2） |
| `apps/search-api/app/agent/nodes/fuse_rerank.py` | 基数截取 + KB 来源卡 + 纠错信号 gate 调用（4.3/4.4） |
| `apps/search-api/app/agent/nodes/web_search.py` | search_fanout kb_route 判定序扩展 + 双 Send |
| `apps/search-api/app/agent/nodes/fast_rewrite.py` | RewriteOutput +kb_route 字段（缺省 hybrid）+ 两兜底路径 hybrid |
| `apps/search-api/app/agent/nodes/deep_reflect.py` | reflect gate 插入（4.5） |
| `apps/search-api/app/agent/nodes/deep_research.py` / `deep_tools.py` | kb_search 子代理工具（4.7） |
| `apps/search-api/app/agent/graphs/fast.py` | search_fanout_targets +kb_search |
| `apps/search-api/app/agent/state.py` | FastState/MainState +correction_round（+kb_route 投影；R14① 状态契约测试同步） |
| `apps/search-api/app/services/turn_service.py` | Source 落库 kb 两列接线（B5；_clamp_source_item 兼容核对） |
| `apps/search-api/app/rag/service.py` / `app/api/v1/kb.py` / `app/schemas/kb.py` | 溯源端点后端三件（4.6） |
| `apps/search-api/app/config_center/` 三源 | defaults.py kb 节新键 + validator.py KbSpec/deep 节扩 + nacos_init.py 同步（parity 先例） |
| `scripts/kb_es_backfill.py` | --mode hash-realign（必答项①）+ 连接异常隔离（P3#4） |
| `scripts/kb_embedding_backfill.py`（新） | 存量向量真实化回填（必答项②） |
| `scripts/probe_es_bm25_golden.py` | --mode hybrid 扩展（8.1）+ **查询形态 fields +content.en（§11 增补——冻结面版本化 bump）**+ `--xlsx` 承载英文 slice 文件（已有参数零扩展） |
| `apps/search-api/pyproject.toml` | dev 组 openpyxl==3.1.5（P3#3） |
| `apps/search-api/test/unit/test_config.py` | docstring 过期表述更新（P3#5） |
| `apps/search-api/test/unit/test_observability.py` 族 | elastic_transport TRACE 护栏断言（P3#6） |
| `.env.example` | :8 组划分行补 ES_/FEISHU_（P3#7；注释级零变量） |
| `docs/contract/data-model.md` / `nacos-config.md` / `api.md` | **本批已落**（v1.21 / v1.46 / v1.33 维持+判定行，见 §6）；**§11 增补**：data-model §1.19 增行随实施批落 **v1.22**；nacos/api/frontend 零变更判定（见 §6 追加判定） |
| `docs/ARCHITECTURE.md` / `docs/PROGRESS.md` / `README.md` | **本批零写**——归收官主会话巡检（ask 纪律） |
| `.env`（用户侧） | ai_search_app 最小权限角色两键 + 双通道翻键（必答项⑤）——用户自改，AI 零触碰 |

---

## 10. Risks（如实留档）

1. **效果口径依赖链长**：向量真实化（A1~A6）→ 回填 → 双通道翻键 → 黄金复测——任一环未完成则混合检索效果口径不可判（只判结构口径）；embedding 网关（text-embedding-v4）真实调用成本与限流未实测（R11 同族——百炼通用端点标准 Key，Token Plan 不抵扣 embedding/rerank，ARCH §7 :900-902 已钉），小批量估时先行是硬纪律；
2. **rerank 基数截取=行为增量**（R27 沿用）：现行全量无上界 → 30 上界改变 rerank 输入分布——契约已钉+TDD 显式测试，M4 评测观察 rerank 命中质量回归；
3. **纠错轮延迟代价**：kb_first/kb_only 不足触发回边=一轮内两趟检索（fast.turn_timeout_s=120 轮预算内双趟 rewrite+search 贴边风险）——`correction_enabled` 缺省 false 灰度 + TurnDeadline 包络既有护栏兜底，M4 评测后再放开；
4. **RRF 与 rerank 顺序耦合**：RRF 截 20 后 rerank ≤30 上界截取两级截取的复合行为需 e2e 形态断言（融合轮 stage 序列+sources 双向核对）——e2e 用例随 B10 补；
5. **零向量行窗口**：回填过程中 embedding_enabled=true 若提前翻键 → 向量腿命中未回填文档的零向量行（同质得分噪声）——时序纪律（回填完成→翻键）写入必答项②，回填脚本 audit 模式提供零向量计数对账出口；
6. **kb_route 误判面**：kb_only 误判（该搜网的题被判只查库）→ 库外事实缺失——三重压缩（rewrite 判据词表「显式内部知识信号」从严 + 缺省 hybrid + kb_only 零候选兜底转 web）；M4 误判探针候选（R17 同族，挂账不立项）；
7. **deep 子代理 kb 工具注入面**：KB 语料=外部内容——包裹+过滤+drop 不占 ref_no 三防线在位（§6.4 第 10 条）；子代理工具描述最小权限（批次 7 安全审计同族）——security-auditor 批内复审焦点；
8. **G059 清零不保证**：RRF+向量+rerank 对「同标题兄弟块」形态是对症但非必治——如实报告跟踪；
9. **契约零漂移前提**：本设计若实施中触 api.md 真实面（如评审认定纠错轮序列须 §7.4 形态行补记）→ 停止实施上报主会话契约批，不私改契约。
10. **content.en 中文单字噪声面（§11 增补）**：混排 query 经 english analyzer 产生中文字符单字 token，理论上可弱匹配 content.en 子字段中的中文单字——IDF 近零 + best_fields 取优压制，golden 100 题门槛守护；M4 评测观察项，不构成实施阻塞。

---

## 11. 增补节：英文词法加固（multi-fields 双查）——2026-11-26 用户裁决并入本批

> **裁决语境**：用户提出「中英混排文档分块与检索」议题 → 主会话 `_analyze` 只读探针实证 jieba 英文侧行为 → 用户裁决采用 **multi-fields 双查形态**（原话要点：「成本最低、复杂度最低、语言分类器为 0、两路自然并存……直接用这种，到时候看检索质量」）。

### 11.1 探针实证（2026-11-26，:19200 只读 `_analyze`，green）

| 面 | 实测 | 判定 |
|---|---|---|
| jieba 英文分词 | `gpt / - / 4o / api / rate / limit`——小写归一 ✅、词级切分 ✅ | 基本可用，缺口不在「分不出词」 |
| **词干化缺口（真伤）** | `limits` 原样进字典 ≠ `limit`——文档 `rate limits` vs 查询 `rate limit` BM25 词法**零命中**（复数/时态同族全丢） | english analyzer（porter）对症 |
| 噪声 token | 空白字符与独立 `.` 成独立 token 进字典（`endpoint / . / e / . / g / .`；`1.2 / . / 3`） | 字典膨胀面（mem1g 盒子/R22）——附属治理项非门禁 |
| 索引/检索对称 | `jieba_index` ≡ `jieba_search` 同形 tokens（探针 1/3 对照） | 同形查询必命中，无形态级额外损失 |
| 插件注册形态 | `/_nodes/plugins`：`getAnalyzers` + `getTokenizers` 双注册（legacy_methods 实测） | jieba 以 tokenizer 形态可用 → 自定义 analyzer 组合可行（噪声治理面预留） |

### 11.2 形态钉定（用户裁决「直接用这种」）

- **mapping**：`content` 增 multi-field `en`（`{"type":"text","analyzer":"english"}`——ES 内置=standard tokenizer+lowercase+porter stemmer，零新依赖）；`heading_path` 不加（轨迹中文形态为主，slice 观察项不扩）。
- **查询**：B1 multi_match `fields=[content, heading_path^2, content.en]`，`type=best_fields` 不变——ES multi_match **逐字段用各自 search_analyzer 分析查询**（content→jieba_search、content.en→english），同份 query 天然双路；混排 query 在 english 侧的中文字符单字 token IDF 近零、不构成排序面（Risk 10）。
- **语言路由自动涌现，零分类器**：纯中文 chunk 的 content.en 近空→自然不参与；纯英文 chunk 双路并存（best_fields 取优）；混排 chunk 两路并行。**被否**：逐文档语言路由（入库+查询两道分类器——项目分类器翻车先例三件：decision model 金标 78.6% / detect_query_lang CJK 24% 误判 / recall R17）；语言检测 ingest pipeline 分字段（混排 chunk 归属两难）。
- **融合形态**：BM25 通道内 best_fields 内部融合，**ADR-017 漏斗形状不变**（仍双通道 BM25‖向量 → RRF）——被否：三路 RRF（BM25-zh/BM25-en/向量，漏斗契约改写不值）。
- **形态差异红利**：`GPT-4o` 在 english 侧 standard tokenizer → `gpt / 4o`（无 jieba 侧 `-` 噪声 token）。

### 11.3 实施面（并入 A7 / B1 / §8.1）

- **序列**：mapping 变更 → 全量 ES 重建（`make kb-es-backfill`，25979 chunks，B18 工具现成）→ 新形态复测。**窗口安全性**：es_enabled=false 灰度期 BM25 零生产读，重建零生产影响；PG/chunk/embedding 面零触碰（ES 独立存储，content_hash 方案 A 与向量回填不受影响）。
- **金标英文/混排 slice 10 题**：新建 xlsx（建议 `resources/KB检索金标英文混排10条.xlsx`，列形态同 100 条现行表头——实施批可调）；语料侦察=ES/PG 拉丁字符占比 top chunks 选材（Spring 等英文术语密集文档为优先素材）；锚点/摘录双口径同 100 条现行 §3.1/§3.2。**data-engineer 小工单承载**。
- **复测对照链**：旧形态基线（现 mapping+现 fields，slice 先跑留档）→ mapping 变更+重建 → 新形态复测（100 题全量 §8.1 floor 门槛承继 + slice 不低于旧形态）→ 基线报告增补节留档新旧对照。
- **附属项（非门禁）**：jieba 噪声 token 治理——临时索引探针验证自定义 analyzer（jieba tokenizer + 空白/纯标点 token 过滤）；可行且 golden 无回归 → content 主字段换装随批；不可行 → 留档 M3+（噪声 token 对 BM25 打分影响≈0——IDF 近零，代价=字典膨胀）。
- **回退门**：生产 kill-switch 仍是 `kb.es_enabled=false`（通道级既有键）；mapping 级回退=revert+重建（灰度期零生产代价）。**无新增 Nacos 键**——mapping 形态属配置三分法第三行（实现结构），既有双开关键承载通道面。

### 11.4 契约与文档面（汇总）

- data-model §1.19 增行（要点级，mapping 事实源仍=`es_mapping.py`——§1.19 既有分层口径）→ **v1.22 随实施批落字**；
- nacos-config / api.md / frontend-api.md——零变更判定（§6 追加判定行）；
- ARCH §4.1 漏斗注 + ADR-017 增注（BM25 通道内双查形态）——归收官巡检同批（本批 ARCH 零写纪律不变）。
