# M3 批次 6（F6 前端 Library M3 管理面）设计钉版

> wf-level: T1 -- 纯前端单域批次（改动全部在 frontend/；后端零改动；契约五件已冻结先行）
> wf-gate: contract -- 契约已冻结：api.md v1.33（§4.1~4.9）/ frontend-api.md v1.34（§1/§4/§5/§7 v1.33 增量）/ types/api.d.ts KB 面已镜像（B19 备妥），均经 2026-11-22 确认点②用户批准

- 计划日期：2026-11-26；批次归属：M3 批次 6 = F6 前端 KB 增量（设计说明 v1.12 §9 挂账「实现归批次 6（F6）」）
- 设计基准：`docs/design/05-library-m3.html`（用户过审 2026-11-23「就这样可以了」，设计复核 15 条修订后）+ `docs/design/tokens.css` v1.1（**零新增 token**——稿内 feishu 双变量为内联提案，处置见 §4/D8）+ `docs/design/设计说明.md` §9
- 纪律：契约零新增（发现缺口停下记必答项，不得照稿实现缺口面）；后端零改动；ARCHITECTURE/PROGRESS/README/契约文档本批不动（收官主会话巡检）；AI 零 git 写操作
- 覆盖面 = 设计稿五面 + 答案页 M3 消费面（契约 §5 v1.33/v1.34 登记的实施面）：
  ① 导入入口（多文件批量上传 + URL 收藏）② 六态状态可视化（步进条 + fail_reason 阶段化）③ 删除级联确认流 ④ 溯源下钻 KbChunkDrilldown（答案页 KB 来源卡入口）⑤ 飞书连接器状态卡 + 导入任务卡（FeishuImportJobs）

---

## 0. 契约锚点速查（实现只认这里，设计稿 .note 为辅助）

| 面 | 契约锚点 | 钉定内容 |
|---|---|---|
| 列表轮询 | api.md §4.3（`docs/contract/api.md:560`） | 「前端对非终态条目每 5s 轮询本端点」——**轮询间隔 5s 为契约钉定值，不是自由裁量** |
| 六态状态机 | api.md §4.3（:560） | `pending → normalizing → chunking → indexing → ready`；任一阶段失败 `failed`；`fail_reason` 阶段化形如 `{stage}: {detail}`（stage ∈ normalizing\|chunking\|indexing；pending 期失败无前缀）；`parse_meta`（normalizing 完成前 null；failed 保留已达摘要）；`catalog_path`（本地上传/URL 恒 null） |
| 删除级联 | api.md §4.5（:570-572）+ frontend-api §1 v1.33 | 级联三存储 kb_chunks‖ES‖MinIO（原件+ir.json）+ 异步补偿校验；响应 `{"deleted":true}`；**前端删除动作二次确认并提示级联范围** |
| 溯源下钻 | api.md §4.6（:574-601） | GET `/api/v1/sources/{source_id}/kb-chunk`；六必选键（chunk_id/document_id/document_title/content/heading_path/page/paragraph——注意 page/paragraph 键恒出现值可空）+ 两可选键 `node_token`/`node_id`（**仅飞书来源携带、缺则省略不置 null**）；回跳 URL = `https://www.feishu.cn/wiki/{node_token}`；content 仅进 UI 过 DOMPurify；404001 单码（不存在/越权/已删除不泄漏存在性）→ 优雅降级；400001 非 KB 来源（前端仅对 KB 卡渲染入口）；heading_path 物化含目录链前缀（2026-11-24 钉注） |
| 单文档详情 | api.md §4.4（:566-568） | GET `/api/v1/kb/documents/{document_id}` 字段同列表项——**下钻面板文档面的 parse_meta/catalog_path 来源**（§4.6 chunk 响应不含这两个键） |
| 触发导入 | api.md §4.7（:603-621） | POST `{connector:"feishu"}`；409007（已有进行中任务）toast 服务端 message；400001 connector 非法；500001 兜底（含凭据未配置） |
| 任务列表 | api.md §4.8（:623-625） | 「**进行中任务每 5s 轮询沿列表节律**」；条目=详情基形**不含 failures** |
| 任务详情 | api.md §4.9（:627-664） | 进度五数 total/processed/imported/failed/skipped；`failure_summary`（category 非封闭枚举、**原样展示不解析**、与 failures 清单同源全量计数、与 skipped_docs 计数正交）；`failures`（title/category/message/document_id——**仅详情端点返回**） |
| 来源行键 | api.md §2.5（:268） | `sources[].kb_chunk_id`：KB 来源恒携带、web 来源 null；可选键缺省 null 消费（`types/api.d.ts` 镜像 `kb_chunk_id?: Uuid \| null`） |
| store 划分 | frontend-api §4 kbStore 行 | 状态=文档列表、轮询定时器、**importJobs（任务列表+进行中任务投影）**；动作=upload/collectUrl/remove/pollUntilSettled/**triggerImport/fetchImportJobs** 经 **`src/api/kb.ts` 唯一出口** |
| 下钻弹窗 | frontend-api §5 KbChunkDrilldown 行 | **数据面归 AnswerView 承载**；SourcePanel KB 卡溯源钮 emit `kbChunk(source_id, kb_chunk_id)` → `fetchKbChunk` GET §4.6（src/api/kb.ts 唯一出口；防重入）；content 纯文本 pre-wrap **不走 markdown**、先 DOMPurify 再文本插值；heading_path 面包屑；page/paragraph null 不渲染对应段；「打开飞书原文」**node_token 存在才渲染**（isHttpUrl 守卫 + target=_blank + rel="noopener noreferrer"）；404001 → 占位卡 `answer.kbChunkUnavailable` 不报错红不 toast；**壳复用 ThreadModal** |
| 导入状态面 | frontend-api §5 FeishuImportJobs 行 | Library 页承载：导入入口钮（409007 toast）+ 任务卡（进度五数 + failure_summary chips + failures 清单展开；skipped 不静默）；**免页面裁决** |
| i18n | frontend-api §7 v1.33（:150） | `library.importFeishu/importRunning/importFailedCount/importSkippedCount` + `answer.kbChunkTitle/kbChunkUnavailable`（zh/en）；六态词条 `library.statusNormalizing/Chunking/Indexing` **已在** `frontend/src/i18n/index.ts:186`（本批补 import*/kbChunk* 六词条） |

**Library 行内溯源钮不实现**：设计稿稿头注记明确「行内溯源钮为契约外演示位（灰置）——api.md §4.6 以 source_id 为解析键、Library 行仅有 document_id、无按文档取 chunk 端点；实现批不含此钮，实际入口 = 答案页 KB 来源卡溯源钮」。

---

## 1. 组件树与页面结构

### 1.1 组件树

```
LibraryView.vue（扩展，五面接入）
├── 页头（既有 page-head：收藏 URL / 上传文档双动作——上传钮触发多选文件选择器）
├── 检索融合说明卡（既有 fusion-card，开关静态留位不动）
├── FeishuImportJobs.vue（新，契约组件名；内含两分区）
│   ├── 连接器状态卡分区（设计稿 conn-card：图标/健康徽标/同步范围·最近同步·变更摘要只读行）
│   │   └── 触发钮两态：idle=「飞书导入」/ 有 pending|running 任务=disabled「导入进行中…」（409007 守卫前置）
│   └── 任务卡分区（设计稿 job-card：最新任务进度条 + 进度五数 + failure_summary chips
│       + <details> 失败清单（§4.9 详情承载）+ 上一次完成任务投影行）
├── upload-zone（既有，扩展 multiple 多选 + 逐文件校验循环）
├── url-row（既有不动）
├── 六态图例 legend（新：pending/normalizing/chunking/indexing/ready/failed 徽标示例——设计稿 .legend）
├── 文档表 doc-table（扩展列：+解析摘要 parse_meta 列）
│   ├── 名称格（扩展：+ catalog_path 目录链副行——仅 catalog_path 非空渲染〔飞书来源〕）
│   ├── 解析摘要格（新：`页 {pages}·表 {tables}·码 {code_blocks}`，null 段不渲染；parse_meta=null 显示 —）
│   ├── 状态格
│   │   ├── DocStatusBadge（既有六态徽标逻辑保留）
│   │   ├── DocStatusStepper.vue（新：normalizing→chunking→indexing→ready 四节点步进条；
│   │   │   done/cur/fail 三态；pending 态不渲染步进条〔由徽标承载〕）
│   │   └── failed 态：fail_reason 阶段化行（stage-tag + detail）+「重新上传」retry-link
│   └── 操作格（仅删除钮；溯源钮不实现——契约外演示位）
└── KbDeleteConfirmModal.vue（新：删除级联确认，ThreadModal 壳 role=alertdialog + 三存储清单）

AnswerView.vue（增量：下钻承载）
├── SourcePanel.vue（扩展：KB 来源卡溯源钮 → emit kbChunk(sourceId, kbChunkId)；web 卡无此钮）
└── KbChunkDrilldown.vue（新：ThreadModal 壳；§4.6 切片面 + §4.4 文档面两源合流）
```

新 icon symbol 4 枚入 `AppIconSprite.vue`（path 直取设计稿 defs）：`i-feishu`（连接器卡/目录链）、`i-scan`（OCR 通道注记）、`i-layers`（删除清单 ES 行）、`i-box`（删除清单 MinIO 行）；`i-db/i-ext/i-search/i-trash/i-upload/i-check/i-alert/i-clock/i-refresh` 既有复用。

### 1.2 页面结构（LibraryView 渲染分区序，对标设计稿 410~733 行）

页头 → 连接器状态卡 → 导入任务卡 → 检索融合卡 → 上传拖拽区 → URL 收藏行 → 六态图例 → 文档表 → 表底注（catalog_path/parse_meta/步进条/D4 口径注记）。

### 1.3 KbChunkDrilldown 面板内部结构（对标设计稿 ④ 824~893 行）

ThreadModal 壳（title=`answer.kbChunkTitle`）内两段：
- **文档面**（§4.4 合流源）：文档标题 + 【知识库】徽标 + catalog_path 目录链 path-line（catalog_path 非空才渲染）+ parse_meta chips（页/表格/代码块，null 段不渲染）
- **切片面**（§4.6）：heading_path 面包屑（空数组不渲染）→ page/paragraph 定位 pills（null 不渲染对应段）→ content 原文（pre-wrap、max-height 200px 内滚、`DOMPurify.sanitize` 后文本插值——沿 SnapshotModal.vue:40 同源卫生链）→ meta-mono 行（chunk_id/document_id/node_id/node_token 小字，node_id/node_token 可选键缺则不渲染）
- **动作行**：「关闭」ghost + 「打开飞书原文」accent 外链（`node_token` 存在 **且** `isHttpUrl('https://www.feishu.cn/wiki/' + node_token)` 守卫通过才渲染；target=_blank + rel="noopener noreferrer"）
- **404001 降级**：弹窗内容替换为占位卡（i-alert 图标 + `answer.kbChunkUnavailable` 文案）——不报错红、不 toast、不关闭弹窗（占位卡即弹窗体）
- **焦点管理**：沿 SnapshotModal 双相位先例（open 记录先前焦点落内容体 tabindex=-1、close/卸载归还）

---

## 2. 状态管理形态

### 2.1 新建 `src/api/kb.ts`（REST 唯一出口模块，frontend-api §4/§8.4）

现状 kb store 内联 URL 直接调 client（`frontend/src/stores/kb.ts:16-17`）——本批收敛为 api 模块（契约 §4 钉「经 `src/api/kb.ts` 唯一出口」；先例 `src/api/sources.ts`）：

```ts
fetchDocuments(): Promise<KbDocumentListData>            // GET /api/v1/kb/documents（§4.3）
uploadDocument(file: File, title: string): Promise<KbDocument>   // POST multipart（§4.1）
collectUrl(url: string): Promise<KbDocument & { duplicated?: boolean }>  // POST /kb/urls（§4.2）
fetchDocument(id: string): Promise<KbDocument>           // GET /kb/documents/{id}（§4.4——下钻文档面）
removeDocument(id: string): Promise<KbDeleteData>        // DELETE（§4.5）
fetchKbChunk(sourceId: string): Promise<KbChunkDetail>   // GET /sources/{id}/kb-chunk（§4.6——防重入归调用方）
triggerImport(): Promise<KbImportTriggerData>            // POST /kb/import/jobs {connector:"feishu"}（§4.7）
fetchImportJobs(): Promise<KbImportJobListData>          // GET /kb/import/jobs（§4.8）
fetchImportJobDetail(jobId: string): Promise<KbImportJobDetail>  // GET /kb/import/jobs/{id}（§4.9——failures 清单）
```

全部走 `apiGet/apiPost/apiDelete`（client.ts 唯一出口）；类型全部取 `types/api.d.ts` 镜像（§8.9：API 数据经镜像收窄，禁断言绕过）。

### 2.2 kbStore 扩展（setup store，§8.2）

**状态**（契约 §4 kbStore 行钉定集）：
- 既有：`documents` / `loading` / pollTimer
- 新增：`importJobs: KbImportJob[]`（§4.8 列表投影）；`importJobDetail: KbImportJobDetail | null`（最新任务的 failures 清单承载——仅详情端点）；`importTriggering: boolean`（防重入）

**动作**：
- 既有四件保留签名（upload/collectUrl/remove/pollUntilSettled/stopPolling），内部改走 api/kb.ts；`remove` 前置确认归视图层（store 只管 DELETE + 列表刷新）
- `triggerImport()`：POST §4.7 → 成功后立即 `fetchJobsIfAny()` 启动任务轮询；ApiError（409007 等）上抛由视图 toast（与 upload 拒绝同族口径）
- `fetchImportJobs()`：GET §4.8 → `importJobs` 替换式更新
- `fetchImportJobDetail(jobId)`：GET §4.9 → `importJobDetail` 更新（failures 清单展开时调用；展开态随轮询节律刷新）

**轮询重构——单 tick 双条件**（钉定形态）：

```
POLL_INTERVAL_MS = 5_000（契约 §4.3「每 5s」钉定值；既有常量保留 kb.ts:19）
schedulePollIfPending():
  tick 条件 = hasPendingDocs()（既有，kb.ts:29-31）|| hasActiveImportJobs()（新增：jobs 中存在 pending|running）
  tick 内按需发请求：文档有非终态 → fetchDocuments()；任务有进行中 → fetchImportJobs() + （详情展开中 → fetchImportJobDetail(展开的 job_id)）
  双条件皆空（全部终态且无进行中任务）→ 停表
  单 setTimeout 链式自排（沿既有 kb.ts:68-75 形态，幂等不重复排）
```

理由：契约 §4.8「进行中任务每 5s 轮询**沿列表节律**」——同一节拍承载两条件，一个定时器一个链，行为可测且无双定时器漂移。`stopPolling()` 视图卸载统一停（既有职责不变）。

### 2.3 溯源下钻状态（AnswerView 本地，不进 kbStore）

沿 SnapshotModal 先例（`AnswerView.vue:312-323,549`）：`kbChunkOpen` / `kbChunkData: KbChunkDetail | null` / `kbChunkDoc: KbDocument | null`（文档面）/ `kbChunkUnavailable: boolean`（404001 降级态）/ `kbChunkLoading`（防重入）。链路：SourcePanel `@kb-chunk` → `onKbChunk(sourceId, kbChunkId)` → 并发 `fetchKbChunk` + 成功后 `fetchDocument(chunk.document_id)`（文档面失败不遮蔽切片面——parse_meta/catalog_path 区降级隐藏）→ 弹窗。

### 2.4 下钻缓存（必答项 OQ-3 裁决落定后实现）

建议值：**成功态会话内存缓存**（AnswerView 级 `Map<source_id, {detail, doc}>`，同 source_id 重复点击零请求）；**404001 不缓存**（每次重查——文档删除后重开及时降级，避免陈旧成功态盖过删除事实）。弹窗关闭不弃缓存。

---

## 3. 五态推进的 UI 形态与轮询设计

### 3.1 轮询 vs SSE——按契约现行形态钉轮询

**钉定：轮询，不做 SSE。** api.md §4.3「前端对非终态条目每 5s 轮询本端点」+ §4.8「每 5s 轮询沿列表节律」均为契约明钉；KB 流水线（kb_documents/kb_import_jobs）无 SSE 事件面（api.md §7 事件枚举零 KB 条目）——造 SSE 即造新契约，违反纪律。间隔 5s 为契约值（非前端裁量；`POLL_INTERVAL_MS` 注释指明出处）。停止条件：文档全部终态（ready/failed）**且**导入任务无 pending|running。页面卸载停表；重进页面 `pollUntilSettled()` 重拉重启（既有语义）。

### 3.2 六态徽标 + 步进条投影（纯函数落 `src/utils/kbDisplay.ts`）

```ts
// 阶段化 fail_reason 解析（契约 §4.3：{stage}: {detail}；pending 期失败无前缀）
parseFailStage(failReason: string): { stage: 'normalizing'|'chunking'|'indexing'|null; detail: string }
// 匹配 `^(normalizing|chunking|indexing): (.+)$` 命中则拆分；不命中 = { stage: null, detail: 原文整体 }（防御回退）

// 步进条节点投影（设计稿 st-trail：done/cur/fail 三态 × 四节点）
stepperNodes(status: KbDocStatus, failStage): Array<{ key, state: 'done'|'cur'|'fail'|'todo' }>
// normalizing→cur；chunking→[done,cur]；indexing→[done,done,cur]；ready→全 done；
// failed→按 failStage 定位 fail 节点（failStage=null〔pending 期失败〕→ 全 todo）；pending→整条不渲染（视图层 v-if）
```

渲染形态照设计稿 214~233 行：徽标（既有 statusText/statusBadgeClass 保留）在上、步进条 `.st-trail` 在下、failed 加 `.status-reason`（stage-tag mono 小标签 + detail）与 retry-link。

### 3.3 failed 态 fail_reason 展示口径（D4）

**建议：常显**（非悬浮）——① PRD A37 钉「Library 页可见」，悬浮 title 读屏不可达、页面可见判定不成立；② 现状已常显（`LibraryView.vue:286` status-reason 直出）；③ 设计稿取常显即为此判定。stage 前缀解析为 stage-tag + detail 两段呈现（解析失败整体原样回退，不解析 category 语义）。**此为必答项 OQ-1，按建议值实施。**

「重新上传」retry-link（设计稿 696 行）：failed 行渲染，点击 → 打开文件选择器（预填不可行——浏览器安全限制不允许预选原文件），用户重选后走既有 §4.1 上传链（新文档新 ID，旧失败行保留可删）。**不做服务端 retry 端点**（契约无此面——同 D7 逻辑）。

### 3.4 parse_meta / catalog_path 列

- 解析摘要列：`parse_meta` 非空 → `页 {pages}·表 {tables}·码 {code_blocks}`，**pages=null（非页式）不渲染「页」段**、只渲染表/码（契约「null 段不渲染」）；`parse_meta=null`（normalizing 完成前）显示 `—`（设计稿 .pm-null）。纯展示格式化函数落 kbDisplay.ts（unit 可测）。
- catalog_path 副行：`catalog_path` 非空（飞书来源）→ 名称格副行渲染目录链（含 feishu 小图标）；本地上传/URL 恒 null 不渲染（契约 §4.3 钉）。

---

## 4. D1~D8 逐条处置表（设计稿稿头挂账 → 本批处置）

| # | 挂账语义 | 处置 | 依据 |
|---|---|---|---|
| D1 | A34 GWT#1 期望 parse_meta 记录「归一化通道」，api.md §4.3 v1.30 仅钉 {pages,tables,code_blocks} 三键 | **本批不实现**：按三键渲染；通道展示顺延留档为契约增量候选（需 api.md §4.3 增键——契约变更须单独批） | 设计稿 34-35 行注 + 契约优先纪律 |
| D2 | 批量拒绝队列卡为提案：frontend-api §1 钉「入口拒绝直接 toast 不入表」、§5 无队列组件行 | **本批不实现队列卡**：维持逐条 toast 契约现行形态；**多选批量上传本身照做**（input `multiple` + 循环逐文件 POST §4.1——端点本为单文件粒度，零契约增量），被拒文件逐条 toast（413001/415001 message）。队列卡留档为契约增量候选（需 §5 增组件行 + §1 措辞钉正，裁决前不得照稿实现） | 设计稿 37-39/571 行注；frontend-api §1 LibraryView 行 |
| D3 | A36 GWT#2 期望表格 chunk 回溯「页与行范围」，api.md §4.6 仅 page/paragraph 两键 | **本批不实现**：只渲染 page/paragraph 定位行（null 段不渲染）；行范围顺延留档 | 设计稿 40-41 行注 |
| D4 | fail_reason 呈现：契约钉「悬浮展示」vs PRD A37 钉「Library 页可见」——稿取常显二选一待裁决 | **建议常显并实施**（OQ-1）：A37 页面可见判定 + 现状已常显 + 读屏可达；契约「悬浮展示」措辞的钉正归收官契约巡检批（非本批私改） | 设计稿 42-43/734 行注；PRD A37 |
| D5 | PRD A53 GWT#2 措辞「node_token/block_id 构造 URL」，api.md §4.6 v1.31 仅钉 wiki/{node_token} | **本批从契约**：仅 `https://www.feishu.cn/wiki/{node_token}`；block_id 深链留档不实现 | 设计稿 44-45 行注；api.md §4.6 v1.31 |
| D6 | 下钻壳宽 440/620 提案 vs ThreadModal 钉 min(420px,100%) | **本批从契约钉定壳**：复用 ThreadModal（420px）；chunk content max-height 200px 内滚消化宽体内容；放宽留档 | 设计稿 46-47/884 行注；frontend-api §5「壳复用 ThreadModal」 |
| D7 | 逐条失败重试无契约端点（§4.7~4.9 无 retry）；连接器健康徽标契约无独立状态端点 | **本批不实现逐条重试**：恢复入口 = 连接器卡「飞书导入」触发钮重发 §4.7（409007 守卫）；健康徽标 = **最近任务成败投影**（latest job status ∈ {completed}→健康 / {failed}→异常 / {pending,running}→进行中；无任务→「未同步」中性态）。§4.9 补 retry 端点留档为契约增量候选 | 设计稿 48-49 行注；api.md §4.7 |
| D8 | tokens 归一化候选：--color-feishu/--color-feishu-bg 双 token + .f-doc/.f-txt 淡底硬编码 + btn-danger hover #ef5a5f | **本批不动 tokens.css**：feishu 双值以**组件级 scoped CSS 自定义属性**落（FeishuImportJobs.vue 作用域内 `--color-feishu: #3370ff; --color-feishu-bg: rgba(51,112,255,.14)` + 注释引用 D8），零全局污染；btn-danger hover 走 scoped 同法；.f-doc/.f-txt 淡底沿用 LibraryView 既有 scoped 实现（`LibraryView.vue:501-516` 同款先例）。tokens.css 合入留用户裁决（OQ-2——tokens.css 是唯一视觉基准，增量须用户批） | 设计稿 29-32/50-52 行注；设计说明 v1.12「tokens 零新增」 |

**稿内另两项非 D 编号注记的处置**：①「Library 行内溯源钮灰置演示位」→ 本批不含（见 §0 表下注）；②「补节·答案页 M3 增量」中 **SourcePanel KB 卡溯源钮**属本批（frontend-api §5 v1.33/34 登记、实施随批次 6）；**A34 GWT#3 占位符结构标记（ph-mark 呈现）不属本批**——其消费面依赖后端 `<document-ref>` 占位符协议在答案 md 的落位形态，落位归 02-answer 视觉基准增量批（设计稿 56 行注「最终落位 = 02-answer 增量批」），本批不动 AnswerRenderer。

---

## 5. 四层测试计划（TDD 红绿先行；验收门禁 = make test-frontend 全绿 + make typecheck 0 + 前三层覆盖率 ≥80%）

### 5.1 unit（纯逻辑，全 mock 零网络）

- **`test/unit/kb-display.test.ts`（新）**：`parseFailStage`（三 stage 前缀命中/pending 期无前缀/异形整体回退/空串）；`stepperNodes`（六态→节点态投影全矩阵：normalizing/chunking/indexing/ready/failed×3 stage/failed 无 stage/pending）；`formatParseMeta`（pages null 不渲染页段/全 null/正常三段）；`feishuJumpUrl`（node_token 拼接 + isHttpUrl 守卫）。
- **`test/unit/kb-store.test.ts`（扩展既有）**：importJobs 状态投影；`triggerImport` 成功后启动任务轮询 / ApiError 上抛不吞；双条件 tick（仅文档非终态→只拉文档；仅任务进行中→只拉任务；双条件→两请求同 tick；双条件皆空→停表）；`fetchImportJobDetail` 更新 detail；api 模块 mock 断言 URL 走 api/kb.ts。

### 5.2 integration（store+api 装配）

- **`test/integration/kb-api.test.ts`（新）**：api/kb.ts 九函数对 client 出口接线（URL/method/payload 断言，信封 roundtrip 形态沿 client-wiring.test.ts 先例）；multipart 形态（FormData 字段 file+title，勿手写 Content-Type）；§4.6 响应可选键缺省消费（node_token 缺 → 外链钮数据面 null）。
- **`test/integration/kb-polling.test.ts`（新）**：LibraryView 挂载→轮询生命周期（fake timers）：上传后 5s tick 刷新至终态停表；触发导入后任务卡随节律刷新；卸载停表（与轮询 docs 复用 sse-pipeline/scroll-follow 的装配形态）。

### 5.3 component（组件渲染 + a11y）

- **`test/component/library-view.test.ts`（扩展）**：parse_meta 列（null 显示 —/pages null 只渲染表码）；catalog_path 副行仅飞书渲染；DocStatusStepper 四态渲染（pending 不渲染步进条）；failed stage-tag + retry-link（点击触发文件选择器）；**删除级联确认流**（点删除→alertdialog 三存储清单可见→确认才发 DELETE→取消不发；a11y：role=alertdialog/aria-modal/焦点返还）；连接器卡两态触发钮（idle 可点 / 进行中 disabled）；任务卡（进度条 aria-valuenow/五数/failure chips 原样 category/failures details 展开→详情请求/skipped 不静默）；六态图例。
- **`test/component/kb-chunk-drilldown.test.ts`（新）**：heading_path 面包屑（空数组不渲染）；page/paragraph null 不渲染对应 pill；content DOMPurify 后文本插值（`<script>` 不执行、pre-wrap）；node_token 存在→外链钮（href/rel/target 断言）、缺省→整钮不渲染；404001→占位卡（kbChunkUnavailable 文案、无 danger 红、无 toast）；防重入（loading 中重复 emit 单请求）；焦点返还双相位（沿 snapshot-modal.test.ts 先例断言面）。
- **`test/component/source-panel.test.ts`（扩展）**：KB 来源卡（source_type=kb 且 kb_chunk_id 存在）渲染溯源钮、点击 emit `kbChunk(source_id, kb_chunk_id)` 载荷断言；web 卡无溯源钮；kb_chunk_id 缺省 null（存量轮形态）不渲染钮；快照胶囊位共存（KB 卡 snapshot_available=false 无胶囊、与溯源钮同位不重叠）。

### 5.4 e2e GUI 黑盒（playwright，复用现有基建：`frontend/playwright.config.ts` workers=1 / webServer pnpm dev / testDir ./test/e2e；不计覆盖率）

- **`test/e2e/library.spec.ts`（扩展）**：①页面骨架扩展断言（连接器卡/任务卡区/六态图例/解析摘要列表头可见）；②「URL 收藏入库」**摘 M3 skip 牌复跑**（后端 §4.2 已交付——`frontend/test/e2e/library.spec.ts:24` 现挂牌理由「apps/search-api/app/api/v1/ 无 kb.py」已过时）；③删除级联确认交互流（真服务：上传→确认弹层→确认→行消失）；④多选上传入口拒绝 toast（错类型备料）。⑤溯源下钻与飞书任务卡真服务用例：**环境性挂牌**沿批次纪律（依赖 KB 来源真轮 / 飞书凭据与租户树，非确定性——skip + 甄别注，活体验证归批次收官探针）。
- e2e 断言形态沿现有 spec（getByTestId + 真服务等待窗）；新 testid 约定：`kb-feishu-card` / `kb-import-trigger` / `kb-import-jobs` / `kb-doc-stepper` / `kb-delete-confirm` / `kb-chunk-drilldown` / `source-kb-drill`。

### 5.5 验收门禁

`make test-frontend`（三层 vitest 全绿）+ `make typecheck`（vue-tsc 0）+ 前三层覆盖率 ≥80%（门禁口径同 AGENTS.md「前端计前三层」）；e2e 真服务轮由实测员按现有流程执行、GUI 结果留档。

---

## 6. 必答项（实施按建议值，用户可改）

| # | 问题 | 建议值 | 理由 |
|---|---|---|---|
| OQ-1 | D4：fail_reason 常显还是悬浮展示？ | **常显** | PRD A37「Library 页可见」判定 + 现状已常显（LibraryView.vue:286）+ 读屏可达；悬浮仅为契约 §4.3 措辞，钉正归收官契约巡检 |
| OQ-2 | D8：--color-feishu 双 token 是否合入 tokens.css（唯一视觉基准的增量须用户批）？ | **本批不合入**——组件级 scoped CSS 变量承载（#3370ff / rgba(51,112,255,.14)），tokens.css 零改动 | 设计说明 v1.12 钉「tokens 零新增」；飞书蓝仅连接器卡一消费面，全局 token 化收益低；后续若 M3+ 扩消费面再裁 |
| OQ-3 | 溯源下钻缓存策略？ | **成功态会话内存缓存（Map<source_id>，AnswerView 级）；404001 不缓存** | 同源重复点击零请求；删除后重查及时降级（缓存成功态会掩盖 §4.5 级联删除事实） |
| OQ-4 | heading_path 契约物化已含目录链前缀（api.md §4.6 2026-11-24 钉注），与设计稿「文档面 catalog_path 行 + 切片面短链」分离呈现存在重复段——前端是否裁剪？ | **不裁剪，全链原样渲染** | 契约数据原样消费（前端解析物化规则=复刻后端逻辑、漂移风险）；文档面目录链与切片标题链语义不同，重复可接受 |
| OQ-5 | failed 态「重新上传」入口形态？ | **retry-link 打开文件选择器，重选后走 §4.1 新上传（新文档新 ID，旧行保留）** | 契约无 retry 端点；浏览器不允许预选原文件；旧失败行保留供溯源/删除 |
| OQ-6 | e2e 溯源下钻/飞书任务卡真服务数据依赖？ | **环境性挂牌（skip + 甄别注）**，活体验证归批次收官探针 | KB 来源真轮与飞书租户树非 e2e 确定性供给；沿批次 7 起的 skip 纪律 |

---

## 7. 触点清单与实施顺序

### 新建
| 文件 | 内容 |
|---|---|
| `frontend/src/api/kb.ts` | KB REST 唯一出口九函数（§2.1） |
| `frontend/src/components/library/FeishuImportJobs.vue` | 契约组件：连接器状态卡 + 触发钮两态 + 任务卡（§1.1⑤） |
| `frontend/src/components/library/KbDeleteConfirmModal.vue` | 删除级联确认（ThreadModal 壳 alertdialog + 三存储清单） |
| `frontend/src/components/library/DocStatusStepper.vue` | 四节点阶段步进条 |
| `frontend/src/components/answer/KbChunkDrilldown.vue` | 溯源下钻弹窗（§1.3） |
| `frontend/src/utils/kbDisplay.ts` | parseFailStage/stepperNodes/formatParseMeta 纯函数 |
| `frontend/test/unit/kb-display.test.ts` / `frontend/test/integration/kb-api.test.ts` / `frontend/test/integration/kb-polling.test.ts` / `frontend/test/component/kb-chunk-drilldown.test.ts` / `frontend/test/component/feishu-import-jobs.test.ts` | 对应层测试 |

### 改动
| 文件 | 内容 |
|---|---|
| `frontend/src/stores/kb.ts` | importJobs 状态 + triggerImport/fetchImportJobs/fetchImportJobDetail + 单 tick 双条件轮询 + 改走 api/kb.ts |
| `frontend/src/views/LibraryView.vue` | 五面接入（多选上传/图例/表格扩展/删除确认流/FeishuImportJobs 挂载） |
| `frontend/src/views/AnswerView.vue` | kbChunk 承载（onKbChunk + KbChunkDrilldown 挂载 + 缓存） |
| `frontend/src/components/answer/SourcePanel.vue` | KB 卡溯源钮 + kbChunk emit |
| `frontend/src/components/icons/AppIconSprite.vue` | +4 symbol（i-feishu/i-scan/i-layers/i-box，path 取设计稿 defs） |
| `frontend/src/i18n/index.ts` | v1.33 词条 zh/en：library.importFeishu/importRunning/importFailedCount/importSkippedCount + answer.kbChunkTitle/kbChunkUnavailable + 删除确认/重新上传/图例/连接器卡只读行等视图文案 |
| `frontend/test/unit/kb-store.test.ts` / `frontend/test/component/library-view.test.ts` / `frontend/test/component/source-panel.test.ts` / `frontend/test/e2e/library.spec.ts` | 扩展（§5） |

### 实施顺序（TDD 红绿，每件先测试后实现）

1. api/kb.ts + unit/integration 红→绿（出口层先行，store 无缝切换）
2. kbDisplay 纯函数 unit 红→绿（无 UI 依赖）
3. kbStore 扩展（unit 先红）→ integration 轮询装配
4. LibraryView 五面：DocStatusStepper → 删除确认流 → 表格扩展（parse_meta/catalog_path/failed 行）→ 多选上传 → FeishuImportJobs（component 红绿随件）
5. 溯源下钻：SourcePanel 钮 → KbChunkDrilldown → AnswerView 承载（component 红绿随件）
6. i18n + sprite 随件落
7. e2e 扩展 + 摘牌复跑 → 门禁三件（make test-frontend / make typecheck / 覆盖率）

### 不做清单（防实现越界）

Library 行内溯源钮；批量拒绝队列卡（D2）；占位符 ph-mark 渲染（02-answer 增量批）；逐条失败重试（D7）；tokens.css 改动（D8）；AnswerRenderer/渲染管线任何改动；后端任何改动；契约文档改动。
