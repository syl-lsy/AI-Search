# M3 批次 3 · B20 飞书导入 SDK 通道 —— 契约增量提案

- 日期：2026-11-25　状态：**提案，待用户批准**（本文件不是契约；批准后另行落 `docs/contract/*`）
- 范围：B20 工单行（`docs/ARCHITECTURE.md:1098`）所需的契约增量；只列增量与缺口，已批契约不重写
- 纪律：草案文字按「可直接落文档」精度书写；现状引用给 `path:line`；未核实处标「待核」

## 0. 先看这一条：B20 工单行的块枚举号有三处错标

`docs/ARCHITECTURE.md:1098`（及本次派单背景第 1/6 点沿用的表述）写的是：

> 官方块枚举 code=14 / **table=30 / callout=18 / quote_container=33**

仓库内两份一手材料与它不一致：

| 块 | ARCH:1098 写法 | notebook 字面表 `notebooks/feishu_kb_tree_probe.ipynb:521-532` | 调研报告 `docs/research/feishu-kb-import-research-20261122.md:170` |
|---|---|---|---|
| 真表格 | table=**30** | 31=table（30=sheet）；:535 有锚点断言 `BLOCK_FIELD[31] == "table"` | 「30 电子表格、31/32 表格/单元格」 |
| 高亮块 | callout=**18** | 19=callout（18=bitable）；:536 锚点 `BLOCK_FIELD[19] == "callout"`；:493 实测分布 `19:callout` | 「19 高亮块」 |
| 引用容器 | quote_container=**33** | 34=quote_container（33=view）；**无锚点断言** | 「33 视图」 |

code=14 两边一致。按冲突优先级规则（ARCHITECTURE 低于契约与一手实证），本提案的映射表以 notebook 字面表为准，但**不自行改 ARCHITECTURE**，列为确认点 C1。工单 E 的三条验收断言如果照 :1098 的号写测试，会把电子表格断言成表格、把多维表格断言成高亮块。

另有一处命名漂移：`ARCHITECTURE.md:1290`（R25）写「failed_items reason_class=no_read_api」，而契约是 `failures[].category=unsupported_type`（`api.md:656`、`data-model.md:531`）。属 ARCH 侧陈旧表述，建议随 B20 文档巡检钉正，不涉契约变更。

---

## 1. 契约增量清单

### ① heading_path 顶层物化口径（data-model.md §1.18 ⑤、§1.7 catalog_path 行）

**现状**：`data-model.md:541` 写 `heading_path = catalog_path + [doc.title] + 文档内标题链`；`§1.7 :274` 的 catalog_path 释义为「目录链标题串（`/` 连接，如 `产品手册/安装指南`）」。代码侧 `tree_assembler.py:192-201` 对顶层 document 物化 `[]`，非顶层 = 祖先标题链（不含自身）；单测 `test/unit/test_kb_tree_assembler.py:136-140` 锁死了这个旧行为。

**增量草案（data-model.md §1.18，在「heading_path 物化细则」后追加一条）**：

> - **catalog_path 含自身标题（2026-11-25 B20 契约批钉定）**：飞书来源每个 document 节点（即 kb_documents 一行）必物化 `catalog_path = [祖先标题…, 自身标题]`；顶层文档（含孤儿兜底、环切后成根者）= `[自身标题]`，**不存在空链**。`catalog_ids` 口径不变（仍为祖先 ID 链，顶层 `[]`），故恒有 `len(catalog_path) = len(catalog_ids) + 1`。与构成公式的接缝：chunker 现行「末位同名则不追加」（`chunker.py:42`）自然吸收，物化结果 `heading_path` 不变——本条只改 kb_documents.catalog_path 列与 REST 投影的取值，**不改切片轨迹语义**。动因：漏物化则顶层文档在 Library 无路径可显，且「目录链非空」分支对顶层永不触发。上传/URL 来源 catalog_path 仍恒 null，零变化。

**增量草案（data-model.md §1.7 :274 catalog_path 行说明列末尾追加）**：

> **v1.19 钉注（2026-11-25）**：飞书来源恒非空且含自身标题（顶层 = 自身标题，§1.18）；非飞书来源恒 null。`/` 连接形态下末段即文档自身标题，消费方按路径渲染、不按「末段=父目录」解读。

**版本动作**：data-model.md v1.19 纯钉注不 bump（沿 2026-11-25 parse_meta 附加键登记判例）。

**受影响方同步**：`tree_assembler.py:192-201` 改物化逻辑；`test_kb_tree_assembler.py:136-140` 断言随改；`rag/service.py:136` 投影零改（直读列）；frontend-api.md §1 LibraryView 路径展示加一句钉注（catalog_path 末段=自身标题），`api.d.ts` 零变更。

**已知副作用**：文档改标题或节点移动会改变自身及整个子树的 catalog_path，进而改变已物化的 `kb_chunks.meta.heading_path` 与 ES heading_path——必须在同步 diff 里按「重导」处理（见 ④）。

### ② SDK blocks → IR 映射细则（data-model.md §1.18 新增一条）

**现状**：`ir_schema.py:30-41` 统一枚举九值（document/heading/paragraph/list/quote/code/table/image/file_attachment）；`:46-47` 未知类型兜底 paragraph + `meta.raw_type` 留痕；raw_type 命名惯例为 `<来源>.<类型>`（`markdown.code`、`docling.document`，见 `markdown_mapper.py:288`、`docling_adapter.py:877`）。契约里没有任何块级映射表。

**通用规则（草案，置于映射表之前）**：

> - **节点 ID**：块节点 `fs:blk:{block_id}`，文档节点 `fs:node:{node_token}`（§1.18④ 现行口径）。
> - **raw_type**：`feishu.<块名>`，块名取官方枚举名（下表「块名」列）；枚举外取 `feishu.block_<编号>`。文档节点 `meta.raw_type` = wiki 节点 obj_type 原值（如 `docx`），`meta.obj_edit_time` 沿 `ir_schema.py:100` 既有键。
> - **code(14) 与 table(31) 不降级规则**：无论嵌在哪个容器内，二者恒产出独立 `code` / `table` 节点（工单 E①② 的结构性保证）。
> - **容器透明展开**：标「透明容器」的块自身不产节点，子块按序映射后挂到容器的父级。
> - **枚举外块（16、>42、0）降级策略**：自身按 §13.9③ 降为 paragraph（可提取文本为空则不产节点），raw_type=`feishu.block_<编号>`；**其 children 不丢弃**，逐个递归适用本策略；每个此类块计入文档 `parse_meta.feishu.degraded_blocks["block_<编号>"]`（见 ③）。即「留痕不静默、正文不丢失」。
> - **行内元素**（沿探针 `ipynb:538-561` 形态）：text_run 取 content（带 link 时记 `[content](url)`）；mention_doc 的目标在本次导入树内 → 落 `<document-ref>` 占位符（§1.18 document-ref 契约，id=`fs:node:{目标 node_token}`），不在树内 → 仅保留标题文字；equation 取公式原值；mention_user / inline file / inline_block / link_preview / reminder 沿探针占位字面（`[mention_user]` 等），reminder 丢弃。

**映射表（草案）**：

| type | 块名 | IR type | 形态要点 |
|---|---|---|---|
| 1 | page | （不产节点） | 文档根；title 取 wiki 节点标题，children 即文档 children |
| 2 | text | paragraph | 行内元素按通用规则 |
| 3~11 | heading1~9 | heading | `level` = 编号 − 2（1~9） |
| 12 | bullet | list | `meta.list_kind="bullet"`；子块文本换行拼接为一个节点 |
| 13 | ordered | list | `meta.list_kind="ordered"`；同上 |
| 14 | code | **code** | text = elements 逐字拼接，**保留换行与缩进、不 strip**；`meta.lang` = `style.language` 原值，有 `language_alias` 另存 `meta.lang_alias` |
| 15 | quote | quote | 单块引用 |
| 16 | （未文档化） | paragraph | 枚举外降级策略 |
| 17 | todo | list | `meta.list_kind="todo"`、`meta.done` = done 原值；文本前缀 `[x] ` / `[ ] ` |
| 18 | bitable | paragraph 占位 | 文档内嵌多维表格，本期不读内容；占位文本 `[bitable]` |
| 19 | callout | **透明容器** | 自身不产节点；直接子节点各加 `meta.callout=true`；空 callout 不产节点（见确认点 C2） |
| 20 | chat_card | paragraph 占位 | `[chat_card]` |
| 21 | diagram | paragraph 占位 | `[diagram]` |
| 22 | divider | （不产节点） | — |
| 23 | file | file_attachment | `children=[]` + `meta.parse_status`（取值待核：沿 §13.9⑥ 既有字面）+ `meta.file_token` / `meta.file_name`；下载后走 REQ-019 归一化管线 |
| 24 / 25 | grid / grid_column | 透明容器 | 按 children 序展开 |
| 26 | iframe | paragraph 占位 | `[iframe]` |
| 27 | image | image | 先占位（`meta.placeholder=true`），素材下载入 MinIO 后回填 `meta.minio_key`；下载失败保留占位、不算文档失败 |
| 28 | isv | paragraph 占位 | `[isv]` |
| 29 | mindnote | paragraph 占位 | 作为 wiki 节点 obj_type 时走跳过留痕（见 ⑦） |
| 30 | sheet | paragraph 占位 | `[sheet]`；作为 wiki 节点 obj_type 的处理见确认点 C8 |
| 31 | table | **table** | 子块 32 被吸收；`meta.rows` / `meta.col_count`（沿 `markdown_mapper.py:266` 键名）。合并单元格：文本落合并区左上格、其余格空串（merge_info 实测形态待核） |
| 32 | table_cell | （不单独产节点） | 被 31 吸收 |
| 33 | view | 透明容器 | 子块形态待核；按「不丢正文」原则先展开 |
| 34 | quote_container | quote | 子块文本换行拼接为一个 quote 节点；内含 14/31 时按不降级规则提升为兄弟节点 |
| 35 | task | paragraph 占位 | `[task]` |
| 36 | okr | 透明容器 | — |
| 37~39 | okr_objective / okr_key_result / okr_progress | paragraph 占位 | `[okr_*]` |
| 40 | add_ons | paragraph 占位 | `[add_ons]` |
| 41 | jira_issue | paragraph 占位 | `[jira_issue]` |
| 42 | wiki_catalog | paragraph 占位 | `[wiki_catalog]` |

「paragraph 占位」= IR type paragraph、text 为方括号占位字面、raw_type 留痕、计入 `degraded_blocks`。

**版本动作**：data-model.md v1.19 钉注增条不 bump。**受影响方**：新增 feishu blocks 映射器（实现面）；chunker 零改（只消费 IR type 与 `meta.lang`/`meta.rows`）；ES mapping 零改；`test_kb_ir_schema.py` 增锚点断言（建议同时给 33/34 补锚点，现 notebook 只锚了 19/27/29/31）。

### ③ parse_meta 是否扩展键 —— 结论：REST 投影零新增；存储侧加一个 `feishu` 子对象

**评估**：
- `blocks 总数`、`伪表格纠正数`：**不加**。SDK 通道按构造不会产生伪表格，「纠正数」恒为 0 无信息量；工单 E④ 用探针脚本对入库结果核对即可（IR table 节点数 = SDK type 31 数）。blocks 总数归任务级 `stats` jsonb（不投影）。
- 但有两件事**必须**有持久化落点，否则功能不成立：
  1. 同步 diff 需要「这个飞书节点对应哪一行 kb_documents、上次导入时的 obj_edit_time 是多少」。kb_documents 现无任何列承载（§1.7 无 node_token 列；顶层文档 `catalog_ids=[]` 也无法反查）。不存它，obj_edit_time 判变更无从比起。
  2. 枚举外/占位块的留痕需要一个文档级落点。

**增量草案（data-model.md §1.7 :278 存储侧附加键登记句内追加）**：

> `feishu`〔B20 起，飞书来源专属、存储侧附加不进投影〕：`{space_id, node_token, obj_type, obj_edit_time}` 四键必带（同步 diff 的节点寻址与变更比对依据）；`degraded_blocks` 可选（`{块名或 block_<编号>: 计数}`，无降级块时省略键）。非飞书来源省略整个 `feishu` 键。`parser` 对 SDK 直读文档取 `feishu_blocks`（A34 GWT#1 的通道标识），附件取实际执行的归一化组件。

**版本动作**：v1.19 钉注不 bump；api.md §4.3 投影零变更。落点选 parse_meta 而非新列的理由：自用千级文档，jsonb 顺序扫描足够，零迁移；沿 2026-11-25 附加键登记先例。若将来要按 node_token 建表达式索引再加，不预留列。（见确认点 C7）

### ④ 同步 MVP 变更判定契约（api.md §4.7 追加一条；nacos 零键）

**现状**：`api.md:611` 写「变更判据 obj_edit_time 与 content_hash 兜底随批次实测钉定，OQ-34」——即尚无判定序。`data-model.md:272` 仍写 content_hash「现行零写入点」，已过时：`pipeline.py:486-491` 已写入。`ir_schema.py:72` 的字段名是 `edit_time`，而文档统一写 `obj_edit_time`（命名不一致，待核）。

**增量草案（api.md §4.7 语义段末尾追加）**：

> - **同步 diff 判定序（2026-11-25 钉定）**，对远端树上每个文档单元依次判定，命中即止：
>   ① 本地无该 node_token → 新增，重导；
>   ② 祖先链（本地记录的 catalog_ids）与远端不同 → 视为移动，该节点及其子树全部重导（路径变更会改写已物化的 heading_path）；
>   ③ 远端 obj_edit_time 大于本地记录值 → 变更，重导；
>   ④ 远端 obj_edit_time 缺失或为 0 → 启用 content_hash 兜底：重拉内容、与 `kb_documents.content_hash` 比对，不同才重导；
>   ⑤ 其余 → 未变更，跳过，不计入 total_docs。
>   「重导」= 整文档单元替换（§4.5 同款三存储级联后重建）。
> - 定时任务：进程内周期任务，间隔 24h，代码常量；触发时若已有 pending/running 任务则本轮跳过不建行；diff 结果零变更时不建行（仅日志）。

**content_hash 兜底的口径说明**：按 B20 行字面「不 bump 则 content_hash 全量比对兜底」若理解为「每次定时都对未变更文档全量重拉比对」，794 篇 × 5 QPS 每天多耗约 160s+ 且全量重归一化，成本与收益不成比例。本草案把它收窄为「仅 obj_edit_time 异常时启用」。前提是「内容编辑会 bump obj_edit_time」已被实测覆盖——ARCH:1098 称「V10 批次 0 实测钉定」，但我没找到 V10 三动作矩阵的留档（调研报告 `:337` 仍列为 P1 待验，`:461` 只说明 wiki 场景应使用 node.obj_edit_time）。**待核**：请指认 V10 留档位置；若「改标题/移动节点不 bump」未被排除，则判 定序 ② 已覆盖移动，改标题仍可能漏检，需要保留周期性全量 hash 比对。

**版本动作**：api.md 升版；`data-model.md:272` 的「零写入点」句钉正为已接线（纯钉注）；`§1.17` 同步条目加一句指向 api.md §4.7 判定序。**受影响方**：worker、crons 注册表、frontend-api.md（定时任务对前端不可见，零变更）。

### ⑤ §4.7~4.9 与 kb_import_jobs 的实现对齐缺口

| # | 缺口 | 证据 | 性质 |
|---|---|---|---|
| G1 | 三个端点零实现 | `app/` 下 `import/jobs` grep 零命中 | 接线 |
| G2 | 错误码 409007 未入错误码表代码 | 契约 `api.md:51` 已有；`errors.py:33-58` 无 | 接线 |
| G3 | ORM 与迁移已落，但契约承诺的完整 DDL 段未回填 | ORM `db/models/kb.py:133`；迁移 `0013_m3_kb.py:157`；`data-model.md:537` 仍写「随批次 5 工单落本节」 | 契约欠账：回填 DDL 并与 0013 逐列核对 |
| G4 | `failure_summary` 无存储列 | §1.17 只有 failures jsonb | 钉注：由 failures 派生、不落列 |
| G5 | 启动钩子只计数、不重入队 | `main.py:107-129` | 接线（§1.17:534 已有口径；B20 验收断言） |
| G6 | content_hash 已接线，契约注过时 | `pipeline.py:486-491` vs `data-model.md:272` | 钉正 |
| G7 | `trigger=scheduled` 无生产者；与 409007 的关系、零变更是否建行未定 | `api.md:611` 只说「同表」 | 契约空白（见 ④） |
| G8 | node_token ↔ kb_documents 行的寻址与上次 obj_edit_time 持久化 | §1.7 无承载 | 契约空白（见 ③） |
| G9 | 远端删除节点的处理 | 契约全文无；调研 `:299` 的口径是「差集删」 | 契约空白（确认点 C5） |
| G10 | `imported_docs` 的计数时点未定 | `api.md:655` 仅给公式 | 钉注：文档行建成并进入入库流水线即计；之后的归一化/索引失败算文档 failed，不回写任务 |
| G11 | SDK 直读文档的 doc_type / parser；附件类型与大小口径 | doc_type 五值（`api.md:267`）；上传白名单 pdf/md/txt/docx + 20MB（`api.md:46,507`） | 钉注：SDK 文档 doc_type=`docx`；附件同上传白名单，超限进失败清单 |
| G12 | sheet / bitable 类 wiki 节点本期处理 | B20 行只提 docx 与 file | 契约空白（确认点 C8） |
| G13 | shortcut 节点是否计文档单元 | `tree_assembler.py:139-148` 已切断子树；契约未说是否计入 total/skipped | 钉注候选：不计文档单元、不进五数 |
| G14 | B20 行块号错标；R25 命名漂移 | 见 §0 | ARCH 巡检项 |

**failure category 值域**：契约为非封闭枚举（`api.md:656`），不建议本批升为封闭。实现批建议取值：`unsupported_type`（mindnote/slides）、`block_fetch_failed`、`attachment_download_failed`、`attachment_unsupported`（类型或大小不在白名单）、`normalize_failed`、`index_failed`、`permission_denied`。

**版本动作汇总**：api.md 升版（G4/G7/G10/G11/G13 钉注 + ④）；data-model.md 钉注不 bump（G3/G6/G8 + ①②③）；frontend-api.md、`api.d.ts` 零变更（KbImport* 类型已登记，`frontend-api.md:158`；实施随批次 6）。

### ⑥ nacos kb 节是否增键 —— 结论：不增

- `import.qps`：`data-model.md:535` 已钉「令牌桶参数为代码常量/工单实装面，不入 Nacos——避免键位膨胀」。增键等于推翻已批契约，且 5 QPS 是飞书官方频控硬限，没有调参空间。
- 同步间隔：部署绑定、无需热更的静态值，落 crons 注册表代码常量（24h）。真要可配也应进 `.env` 而非 Nacos。
- 现行 kb 节 13 键（`config_center/defaults.py:304-336`）零变更；**nacos-config.md 零 bump、零 Nacos 操作**。

### ⑦ mindnote/slides 跳过留痕 —— 已落契约，零增量

载体已在：`api.md:655`（skipped 定义 + A53 第 3 GWT）、`api.md:656`（category=unsupported_type 计入 failure_summary）、`api.md:657`（failures 条目，document_id=null）、`data-model.md:531`。本批只需实现，并补一句钉注消除歧义：

> `api.md §4.9 failures 条目注（钉注）`：obj_type ∈ {mindnote, slides} 的 wiki 节点，每个文档单元 skipped_docs + 1，并写一条 `{title: 节点标题, category: "unsupported_type", message: "<obj_type> 无读取 API，已跳过", document_id: null}`；以其为根的子树内各文档单元逐一计入。slides 的 obj_type 字面待核。

---

## 2. 零契约面清单

| 面 | 理由 |
|---|---|
| feishu_client 模块（tenant token 获取与缓存、树遍历分页、blocks 分页、素材/附件下载） | 纯实现；对外的唯一契约是 IR 与 kb_* 表 |
| worker 内部（cursor jsonb 结构、令牌桶、退避、重试次数、断点推进粒度） | §1.17 只钉列存在与语义，未钉内部结构 |
| stats jsonb 内部结构 | 不投影 REST |
| 块映射器代码 | 契约只钉映射表（②），代码为实现事实源（§1.18:547 分层口径） |
| MinIO 键布局 | 附件沿用 `kb/{user_id}/{doc_id}/original.{ext}`；图片素材键为实现面 |
| lark-oapi 锁版 | ARCH S16（`:136`）已钉；落 pyproject + 护栏单测 |
| crons 注册、启动钩子接线 | ADR-001/010 既有机制 |
| 探针脚本、938 篇抽样核对脚本 | 评测/验收工具 |
| ES mapping、SSE 事件面、检索漏斗 | 与导入通道无关 |

---

## 3. 确认点清单（9 条）

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| C1 | ARCH:1098 块号三处错标（table=30/callout=18/quote_container=33），以哪份为准？ | (a) 以 notebook 字面表 + 调研 :170 为准，ARCH 巡检钉正；(b) 另跑探针复核后定 | (a)。两份一手材料互相印证，且 notebook 有锚点断言 |
| C2 | callout(19) 映射形态 | (a) 透明容器，子块各保其 type，加 meta.callout；(b) 整体映射为 quote（调研 :563 的合并写法）；(c) 新增 IR 枚举 callout | (a)。(b) 会把高亮块内的代码块压成引用文本，直接破坏 E①；(c) 要动 ES kind 维度与校验，收益不抵 |
| C3 | 枚举外块降级策略 | (a) 自身降 paragraph + children 递归不丢 + degraded_blocks 留痕；(b) 整支子树丢弃并留痕；(c) 文档进失败清单 | (a)。(b) 丢正文，(c) 对单个未知块过重 |
| C4 | 同步 diff 判定序；content_hash 兜底是「常态全量比对」还是「仅 obj_edit_time 异常时启用」 | (a) 仅异常时启用；(b) 每次定时全量重拉比对 | (a)，但前提是 V10 三动作矩阵有留档——请指认位置；没有则先补验再定 |
| C5 | 远端已删节点对应的本地文档如何处理 | (a) 走 §4.5 三存储级联删除，计数入 stats 不进五数；(b) 保留本地、仅留痕；(c) 本期不做删除 | (a)。与调研 :299 口径一致；但这是后台自动触发的销毁性操作，请明确授权。(b) 最保守 |
| C6 | nacos 是否增键；定时同步形态 | (a) 零新键，24h 代码常量，零变更/撞进行中任务不建行；(b) 增 `kb.import` 子节（qps/sync_interval） | (a)。与 §1.17:535 既有裁决一致 |
| C7 | node_token 寻址与 obj_edit_time 持久化落点 | (a) parse_meta.feishu 子对象；(b) source_url 存 `https://www.feishu.cn/wiki/{node_token}`；(c) 新列 + 迁移 | (a)。(b) 会与「source_url 仅 url 类型」释义冲突并可能被前端当外链渲染；(c) 为千级数据上迁移，过重 |
| C8 | sheet / bitable 类 wiki 节点本期是否读取 | (a) 跳过留痕，category=unsupported_type、message 注明「本期未实装读取通道」；(b) 本批实装 records/values API | (a)。B20 行范围未含；占比未统计（R25 V8）。若选 (a)，建议 message 用不同字面以便与「无 API」区分 |
| C9 | 顶层物化口径：catalog_path 含自身标题、顶层 = [自身标题] | (a) 按本提案 ① 落契约；(b) 维持顶层 []，另设他法让顶层文档有路径 | (a)。chunker 已兼容两种形态，改动面最小 |

---

## 4. 风险与未决问题

| # | 风险/未决 | 建议默认 |
|---|---|---|
| R-a | **V10 实测留档未找到**。「内容/改标题/移动均 bump obj_edit_time」是判定序③的前提；ARCH:1098 称已钉定但无出处可引 | 请指认留档；找不到则 B20 开工前补三动作×字段矩阵探针 |
| R-b | 33=view、34=quote_container 只有字面表、无锚点断言；合并单元格 merge_info 实测形态未核 | B20 护栏单测补 33/34 锚点；table 用例带真实合并单元格样本 |
| R-c | 容器型块（callout/grid/view/quote_container）嵌套 code/table 的真实分布未统计；E①② 的成立依赖「不降级规则」被测试覆盖 | 938 篇抽样时统计容器内 code/table 出现次数，作为用例样本 |
| R-d | 文档改标题/移动会触发子树整体重导（heading_path 物化的代价）；高频移动的知识库会让定时 diff 变重 | 接受；stats 记录重导数，异常偏高再议 |
| R-e | 附件 137 个的扩展名分布未统计；不在 pdf/md/txt/docx 白名单或超 20MB 的会整批进失败清单，A53 观感上像「导入失败率高」 | 先统计分布；占比高则裁决是否为飞书附件放宽白名单（契约变更，另批） |
| R-f | slides 的 obj_type 字面、file_attachment 的 parse_status 取值、shortcut 的 document-ref kind 字面均未核 | 实现批以 SDK 实测字面为准并回填本文档 |
| R-g | 版本号待核：AGENTS.md 文档地图写 api.md v1.31，PRD v1.35 版本行引 api.md v1.32 | 落契约前以 api.md 文件头为准 |
| R-h | 定时同步是后台自动触发的写操作（含 C5 的删除），无前端入口可关 | 24h 间隔 + 零变更不建行；如需开关进 `.env`，不进 Nacos |
| R-i | 本提案未读 PRD A53 三条 GWT 原文（行过长未取回），对 GWT 的引用均转引 `api.md:655-657` | 落契约前对照 PRD 原文核一遍 |
