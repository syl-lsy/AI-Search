# M3 批次 4 工单 B18 实施设计：ES BM25 写入通道（plan-m3-b18-es-write-20261125.md）

> wf-level: T1 -- 纯后端工单（compose + settings + pipeline indexing 段 + 删除腿 + 回填脚本），计划模式免声明
> 产出日期：2026-11-25。状态：设计钉版待实施（本计划不写产品代码、不改 docs/contract 与 docs/ARCHITECTURE——契约触点仅列清单，归主会话巡检/契约批）。

---

## 0. 输入与现状核实（设计证据基线）

### 0.1 ask 指定输入（全部已读）

| 输入 | 关键事实（本设计依赖） |
|---|---|
| `docs/research/m3-probe-es-jieba-20261122.md` | ES 8.4.1 + sing1ee jieba 插件（8.4.1 分支，子模块 `4c16d01`，MIT+Apache-2.0 双宽松许可）在 `127.0.0.1:19200` 全链路可用；插件构建配方（§3，gradle 7.6 + ES 镜像内置 JDK18 + maven.aliyun）；手动解压安装形态（顶层 `jieba/` 目录，非 `elasticsearch-plugin install` 标准格式，§4）；analyzer 名 = `jieba_index`（索引时）/ `jieba_search`（检索时）（§5.1 实测）；512m 堆容器实占 873MiB（§4）；Docker Hub registry 本机不可达、docker.elastic.co 可达（§3/附）；首选 infinilabs 插件已官方下架（§2 三重证据）；**探针容器已 `--rm` 清理、`/tmp/probe_es` 已删（§7）——构建产物 zip 不在盘上，实施批须按配方重建** |
| `docs/ARCHITECTURE.md`（v1.77 现行） | §4.1 KB 混合检索管道（ADR-017）：每 query ES BM25 top30 ‖ pgvector top30 → RRF(k=60) → kb_fuse_top_k=20 → rerank ≤30（B19 消费，本批只管写入侧）；§5 elasticsearch 行（:581）：镜像锁 tag、jieba「自建薄镜像叠加或挂载目录安装」、`19200`（9200+10000）、单节点 `ES_JAVA_OPTS=-Xms512m/-Xmx512m` 起步 + `mem_limit: 1g` + 独立卷 `es_data`、**pg 为事实源、ES 可整体重建（OQ-28，重建脚本随 B18 交付）**、⚠️ VM 配额用户拍板项（R22）；§8 B18 行（:1096 附近）：ES 索引管理四要求 + 双写 + 删除三存储级联 + **embedding 输入标题轨迹前置 + 向量陈旧裁决随本单契约批必答**；R22（:1288）/R23（:1289）：供应链纪律沿 ADR-014 四件套、IK 回退门触发须用户回报 |
| `apps/search-api/app/rag/pipeline.py` | 六态流水线；`resolve_indexing_confirmation`（:77-83）现形态 `es_pending_seam=True` 留痕放行（:397-405 消费点）；`REQUEUE_STATUSES`（:49-54）含 indexing；`replace_chunks`（:223-252）chunking 段全量替换先删后插；`embedding_placeholder`（:144-146）零向量占位「B18 向量通道落地前写入缝」；依赖全注入构造形态（:280-292） |
| `apps/search-api/app/core/config.py` | `BaseSettingsWithEnv` 分组模式；compose 消费型变量**不声明**（模块 docstring :7-17 例外清单先例：SEARXNG_SECRET 等）；`Port` 注解（:70）；单例逐组并列（:271-282）；现无 ES_ 组 |
| `apps/search-api/app/db/models/kb.py` | `KbDocument`（含 catalog_path :94 / content_hash :71 / chunk_count :76）；`KbChunk`（meta 七键 :126-127、`uq_kb_chunks_doc_idx` (kb_doc_id, chunk_index) 唯一 :101-104、embedding vector(1024) :125、kb_doc_id FK CASCADE :117）；`KbDocStatus` 六态 :44-46 |
| `deploy/docker-compose.yml` | 服务形态先例：bind 127.0.0.1、healthcheck CMD-SHELL、named volumes、`${VAR:?}` fail-fast / `${VAR:-}` 可空注入；searxng 行（:388-412）为「备源不阻塞主栈、无 depends_on」先例；inode 坑注（:309-316 目录级挂载根治） |
| `Makefile` | `COMPOSE := docker compose --project-directory . -f deploy/docker-compose.yml`（:14）；`preflight`（:18）/`smoke-infra` 七组件循环（:50-58）/`typecheck-backend`（:131-133，范围含 scripts/）；无 ES 相关目标 |
| `.env.example` | **ES_ 组六键已预置（:267-274）**：ES_HOST/ES_PORT/ES_USERNAME/ES_PASSWORD/ES_MEM_LIMIT/ES_INDEX_PREFIX，注记「core/config.py ES_ 分组随批次 3 B18 实装新增，本模板先行同步 ARCH §7 清单」——本批兑现该注记 |

### 0.2 本会话实测（检查已跑，非转述）

| 检查 | 命令 | 结果 |
|---|---|---|
| Docker VM 内存（R22 前置） | `docker info \| grep -iE "total memory\|cpus:\|server version"` | **Total Memory: 10.44GiB**、10 CPUs、29.6.1——探针时代 7.75GiB 配额已上调（与 ask「实测 10.4GB」一致），R22「用户拍板项」在环境侧已解除 |
| ES 镜像在位 | `docker images \| grep elasticsearch` | `docker.elastic.co/elasticsearch/elasticsearch:8.4.1`（1.13GB）本地已有——探针保留项兑现，零拉取（Docker Hub 不可达不影响） |
| 端口 19200 | `lsof -nP -iTCP:19200 -sTCP:LISTEN` | 无监听（exit 1）——空闲可用 |
| ES 镜像内 curl/uid | `docker run --rm --entrypoint /bin/bash docker.elastic.co/elasticsearch/elasticsearch:8.4.1 -c "command -v curl; id -u"` | `/usr/bin/curl` 存在、默认 uid=1000（elasticsearch）——healthcheck 用 curl 成立、插件属主钉 1000:0 |
| 存量数据 | `docker exec ai-search-postgres-1 psql -U ai_search -d ai_search -t -A -c "select status, count(*) from kb_documents group by status; select count(*) from kb_chunks"` | **ready=915**（与 ask「存量 915 行已 ready」一致）、kb_chunks 共 **24802** 行——回填规模基线 |
| 依赖现状 | `grep elasticsearch apps/search-api/pyproject.toml uv.lock` | 零命中——elasticsearch 客户端为**新增运行依赖**；`aiohttp==3.14.5` 已在 uv.lock（:90） |
| 删除缝 | `grep -n es_indexer app/rag/import_worker.py` | :713 构造参 `es_indexer: Any \| None = None`（B18 seam）、:1413-1422 级联删除 ES 腿消费点（签名 `delete_document(kb_doc_id=…, user_id=…)` 已被缝合钉死）、:1547 生产装配现注 `None` |
| 删除主路 | `app/rag/service.py:420-440` | `delete_document`：行删（chunks FK CASCADE）→ sources 反引用清理 → MinIO 前缀清理 best-effort（失败 WARN 不阻断）——**无 ES 腿** |
| 重入队钩子 | `app/main.py:99/:108/:233` | `_KB_REQUEUE_STATUSES` 与 `REQUEUE_STATUSES` 同源、启动钩子重入队已含 indexing——本批零改动 |
| preflight 端口清单 | `scripts/infra_smoke/preflight.py:18 PORTS` | 现无 19200（本批触点，§10） |
| KB 切片字段 | `app/rag/chunker.py:183-203` + `app/rag/ir_schema.py:60-61` | `Chunk` 含 `kind`（统一枚举值）/`raw_type`/`node_id`/`heading_path`/`loc`/`references`/`char_start`/`char_end`/`content_hash`（chunk 级不入库）；`DOC_KINDS` 六值 = (docx, file, sheet, bitable, upload, url)，`doc_kind` 是 **document 节点专属**（IR root 上，非 kb_documents 列） |

### 0.3 契约锚点（冻结方向，本设计不得冲突）

- **data-model.md §1.19**（:593-604）：索引名 `{prefix}_kb_chunks`；字段要点 `chunk_id/kb_doc_id/user_id(keyword, term 过滤恒携带)/content(text, jieba)/heading_path(text 独立字段, multi_match 权重高于正文, keyword 副本)/kind·raw_type(keyword)/chunk_index·char_start·char_end·created_at`；检索口径 BM25 top30（B19）；一致性=**pg 事实源、ES 可整体重建（重建脚本随工单交付，OQ-28）、双写顺序 pgvector 先 ES 后、失败重试耗尽→failed（fail_reason=索引同步失败）不静默 ready、ready=启用通道均确认、删除三存储级联**；**mapping 细节随实装落 `app/rag/indexers/`（:604 事实源分层条款——本设计即该落点的先行钉版，零契约升版候选）**。
- **pipeline.py 模块 docstring**（:14-15）：es_enabled=false 灰度态 pgvector 单通道确认即 ready；true 双通道——B19 seam 现留痕放行，本批替换为真写入。
- **api.md §4.3**（fail_reason 阶段化 `{stage}: {detail}`）+ **§4.5**（删除 200 `{"deleted": true}`，三存储级联）——本批不动 REST 契约面。

---

## 1. 范围边界（先钉边界再钉决策）

**本计划 = B18 的 ES BM25 写入通道**（ask 明确收敛；AGENTS.md 批次 4 行同口径「compose elasticsearch :19200 + jieba 分析器 + BM25 写入通道」）。含：compose 服务、ES_ settings、索引设计与创建、indexing 段真写入、删除级联 ES 腿、存量回填/重建/审计脚本、测试面。

**不含（如实列出，防静默缩窄）**：B18 冻结工单行内另有「embedding 输入标题轨迹前置 + 向量陈旧裁决必答项」（调研 §9：用户已裁决「按照建议点来落地」）。其中：
- **向量陈旧裁决（必答项）**——本计划 §9 给出 architect 方案（待用户确认），兑现「architect 出方案经用户确认」的契约批义务；
- **embedding 向量通道接线**（调 embedding 模型写真实向量、替换 `embedding_placeholder` 零向量占位）——不在本计划实现范围（ask 未列、批次 4 工单行口径为 ES 通道），排期归主会话/用户裁决（可作 B18 内第二小批或独立小工单）。不接线时 pgvector 通道维持零向量占位现状，B19 检索行为不受本批影响。

---

## 2. 决策一：compose ES 服务规格（钉死）

```yaml
# deploy/docker-compose.yml 追加（形态对齐 searxng 行先例——备源不阻塞主栈）
  elasticsearch:
    build:
      context: ./deploy/es          # 自建薄镜像叠加（决策理由见下）
    image: ai-search/elasticsearch-jieba:8.4.1   # 本地构建 tag（防 compose 误拉远端同名）
    restart: always
    ports:
      - "127.0.0.1:${ES_PORT:-19200}:9200"       # 9200+10000 惯例（ARCH §5）；绑 127.0.0.1
    environment:
      discovery.type: single-node                # 探针 §4 实测配方
      xpack.security.enabled: "false"            # 探针 §4；安全性由 127.0.0.1 绑定承担（ARCH §5 同前提）
      ES_JAVA_OPTS: "-Xms512m -Xmx512m"          # 探针实测配方；不变量：堆 ≤ mem_limit/2（注释钉入）
    mem_limit: "${ES_MEM_LIMIT:-1g}"             # ask 钉定：走 ES_MEM_LIMIT，缺省 1g
    volumes:
      - es_data:/usr/share/elasticsearch/data    # 独立命名卷（ARCH §5 ES 行）
    healthcheck:
      # 镜像内 /usr/bin/curl 已实测在位（本会话 docker run 验证）；9200 为容器内端口
      test: ["CMD-SHELL", "curl -sf 'http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=5s' || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s          # jieba adapter 多 search 线程初始化 + 冷启动慢（探针 §4 启动零错误但 JVM 起动重）
# volumes: 段追加  es_data:
```

**逐项取舍**：

| 项 | 钉定 | 理由（含被否备选） |
|---|---|---|
| 镜像 tag | `docker.elastic.co/elasticsearch/elasticsearch:8.4.1` | 探针全链路实测配方；**jieba 插件 descriptor 硬校验 `elasticsearch.version=8.4.1`**（探针 §3——插件与 ES 版本互锁，升版须重构建，R23 沿用）；镜像本地已在位（0.2 实测），Docker Hub 不可达无影响（docker.elastic.co 可达，探针附）。锁 tag 不落 latest（ADR-014 纪律） |
| 插件交付 | **自建薄镜像叠加**：`deploy/es/Dockerfile` = `FROM docker.elastic.co/elasticsearch/elasticsearch:8.4.1` + `COPY plugins/jieba/ /usr/share/elasticsearch/plugins/jieba/` + `chown -R 1000:0`（uid 实测默认 1000）；构建产物（约 7.3MB：插件 jar + jieba-analysis jar + descriptor + dic 字典）**入库 `deploy/es/plugins/jieba/`**，配套 `scripts/build_es_jieba_plugin.sh` 固化探针 §3 配方 + `deploy/es/PROVENANCE.md` 记录供应链（sing1ee 8.4.1 分支 commit、子模块 4c16d01、gradle 7.6 sha256、双许可证） | ①ARCH §5 明示「自建薄镜像叠加或挂载目录安装」两条都合规，选镜像叠加因 **macOS Docker bind 目录属主/权限不可控**（VirtioFS uid 映射不确定，ES 插件装载要求 elasticsearch 用户可读——探针 §4 用容器内 `docker cp + chown` 正说明 bind 不可靠）；②`elasticsearch-plugin install` 标准形态被插件 zip 非标准结构排除（探针 §3 波折 3）；③产物入库后 compose `up` 零构建外网依赖、幂等可复验；④R22 候选「全源 vendor + CI 构建」为更重的供应链形态，归 M3+ 供应链工单（本批以 PROVENANCE + 配方脚本作过渡，风险 §11 如实留档） |
| 端口 | `127.0.0.1:19200→9200` | ask/ARCH §5/探针三方一致；9200+10000 惯例（postgres 15432 / redis 16379 / searxng 18080 同族） |
| mem_limit | `${ES_MEM_LIMIT:-1g}` | ask 钉定。缺省 1g = ARCH §5 ES 行字面（mem_limit 1g）；环境侧 R22 已解除（VM 10.44GiB 实测）。堆维持探针实测 512m 不随 mem_limit 联动——单旋钮原则（用户若调低 ES_MEM_LIMIT 须同步改堆行，注释钉明「堆 ≤ mem_limit/2」不变量）；联动推导属过度工程不采。ARCH §5 行内「mem1g 复验」关切实为堆容量，本配方 = 探针已验证的 heap512m + 仅外增容器上限（严格更安全），无复验阻塞项 |
| healthcheck | curl `_cluster/health?wait_for_status=yellow&timeout=5s` | 镜像内 curl 在位（0.2 实测）；yellow 判据：单节点 `number_of_replicas: 0`（§4 决策三）下健康即 green，yellow 为索引尚未创建时的中间态——宽进不误杀；start_period 30s 防 jieba 初始化期误杀（interval 10s × retries 12 与 nacos/langfuse 行同参族） |
| depends_on | 无 | searxng/crawl4ai 先例：与主链并行起，ES 不可达由应用层失败语义兜（§5 决策四），不阻塞 postgres/minio 主栈 |
| 卷 | `es_data` 命名卷 | ARCH §5 ES 行「独立卷 es_data」字面；重建=删索引非删卷（OQ-28 语义），卷保留 segment 缓存与重启加速 |

---

## 3. 决策二：settings 六键形状（钉死）

`app/core/config.py` 新增（组头注释对齐 ARCH §7 ES_ 组）：

```python
# Elasticsearch（ES_，M3 批次 4 B18）：应用读五键（host/port/username/password/index_prefix）；
# ES_MEM_LIMIT 为 compose 消费型（注入 elasticsearch 容器 mem_limit，应用不读取）——
# 沿 SEARXNG_SECRET/MINIO_ROOT_* 例外清单先例，在此注记、不声明字段。
class ESSettings(BaseSettingsWithEnv):
    host: str = "127.0.0.1"
    port: Port = 19200
    username: str = ""                    # 空=不带 basic auth（compose 侧 security 关闭）
    password: SecretStr = SecretStr("")   # 与 username 成对；SecretStr 全链路打码
    index_prefix: str = "ai_search"       # 索引名前缀（data-model §1.19 {prefix}_kb_chunks）
    model_config = {"env_prefix": "ES_"}

    @field_validator("index_prefix")
    @classmethod
    def _validate_index_prefix(cls, value: str) -> str:
        # ES 索引名合法字符子集收紧：小写字母/数字/下划线（防 \/*?"<>| 空格,# 等非法字符注入索引名）
        if not re.fullmatch(r"[a-z0-9_]{1,32}", value):
            raise ValueError(f"ES_INDEX_PREFIX 必须为小写字母/数字/下划线（1~32 位），得到：{value!r}")
        return value
```

模块单例区追加 `es_settings = ESSettings()`（:271-282 同列）。

**六键职责划分（钉死）**：
- **应用侧消费五键**：host/port/username/password/index_prefix——`rag/indexers/es_client.py` 工厂经 `es_settings` 单例读；
- **MEM_LIMIT 仅 compose 消费**（`${ES_MEM_LIMIT:-1g}`）——ESSettings **不声明** `mem_limit` 字段（compose 消费型不声明纪律，config.py docstring 例外清单补一行注记）；
- **零 .env 配置可跑**：六键全有缺省（host 127.0.0.1 / port 19200 / 空 auth / 前缀 ai_search / mem 1g 在 compose 侧）——新机器 `cp .env.example .env` 不填 ES_ 段即可起 ES 并跑通（探针配方即零鉴权形态）；
- **开关不在 settings**：`kb.es_enabled` 是 Nacos Agent Spec 键（`read_es_enabled`，pipeline.py:86——非严格布尔缺省 false 灰度安全），本批零新增 Nacos 键（nacos-config.md 零变更候选）；
- username/password 语义：compose 侧 security=false 时应用发 auth 头也无害，但钉定为「username 非空才携带 basic_auth」——未来开 security 单侧改 compose 即可，应用零改码。

---

## 4. 决策三：ES 索引设计（钉死）

落点 `app/rag/indexers/`（data-model §1.19 :604 事实源分层条款指定的实装落点）：

```
apps/search-api/app/rag/indexers/
├── __init__.py
├── es_client.py      # AsyncElasticsearch 惰性单例工厂（连接参数全自 es_settings）
├── es_mapping.py     # 索引名/索引 settings/mapping/bulk action 构造——纯函数（unit 直驱）
└── es_indexer.py     # KbEsIndexer：ensure_index / index_chunks / delete_document / count_by_doc
```

### 4.1 索引命名

`{es_settings.index_prefix}_kb_chunks`——缺省 `ai_search_kb_chunks`（契约 §1.19 字面）。单索引全用户共享（user_id keyword 过滤恒携带承担隔离）：915 文档/2.5 万 chunks 自用单租户量级，按用户拆索引为过度设计。

### 4.2 索引 settings

```python
{
  "number_of_shards": 1,          # 单节点——多分片只增开销
  "number_of_replicas": 0,        # 单节点副本不可分配；health 才能到 green（healthcheck yellow 兜中间态）
  "refresh_interval": "1s",       # 缺省显式化（B19 检索容忍亚秒可见性滞后；回填脚本尾部显式 _refresh）
}
```

analysis 段**不自定义**：`jieba_index`/`jieba_search` 两 analyzer 由插件注册（探针 §5.1 实测名），mapping 直接引用——零自定义 analysis 即零 analyzer 配置漂移面。

### 4.3 mapping（与 kb_chunks 行 + meta 七键 + 文档级两键对齐）

```python
{
  "settings": {...同上...},
  "mappings": {
    "_source": {"excludes": []},
    "properties": {
      "chunk_id":    {"type": "keyword"},                    # = kb_chunks.id（str(UUID)）
      "kb_doc_id":   {"type": "keyword"},                    # 删除/重建按文档寻址键
      "user_id":     {"type": "keyword"},                    # term 过滤恒携带（契约 §1.19；REQ-011/050）
      "content":     {"type": "text", "analyzer": "jieba_index", "search_analyzer": "jieba_search"},
      "heading_path": {                                      # 标题链（root→leaf join，分隔符 " > "）
        "type": "text", "analyzer": "jieba_index", "search_analyzer": "jieba_search",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},   # 精确过滤副本（契约钉注）
      },
      "catalog_path": {                                      # 文档级目录链（kb_documents.catalog_path 原值）
        "type": "keyword",
        "fields": {"txt": {"type": "text", "analyzer": "jieba_index", "search_analyzer": "jieba_search"}},
      },   # txt 子字段是否进 B19 默认 multi_match 由 B19 定，本批只保证可检索
      "doc_kind":    {"type": "keyword"},                    # 文档级（IR root.doc_kind，DOC_KINDS 六值；可空）
      "kind":        {"type": "keyword"},                    # chunk 首来源单元统一类型（Chunk.kind）
      "raw_type":    {"type": "keyword"},                    # meta 七键（str() 归一后写入）
      "chunk_index": {"type": "integer"},
      "char_start":  {"type": "integer"},                    # meta 七键
      "char_end":    {"type": "integer"},                    # meta 七键
      "token_count": {"type": "integer"},                    # kb_chunks 行列（B19 截断预判可用）
      "created_at":  {"type": "date"},                       # kb_chunks.created_at（契约 §1.19）
      "node_id":     {"type": "keyword"},                    # meta 七键（溯源寻址）
      "loc":         {"type": "object", "enabled": False},   # meta 七键——只入 _source 不索引（溯源权威在 pg）
      "references":  {"type": "object", "enabled": False},   # meta 七键——同上（list[dict] 序列化存储）
    }
  },
}
```

**取舍理由**：
- `loc/references` 用 `enabled: false`（存 `_source` 不建索引）：溯源 O(1) 契约走 kb_chunks.meta 物化字段（data-model §1.18/§1.8），ES 副本仅为 `_source` 可读性（调试/导出）；建索引零收益还拉长倒排。
- `doc_kind` 来源 = **IR root 节点**（ir_schema.py:60 六值，document 节点专属），indexing 段 `get_ir_json` 一次 MinIO GET 取 root.doc_kind；IR 不可达（异常态）→ WARN 留痕 + 存 null 继续——doc_kind 是辅助过滤字段，不阻断写入（kind/raw_type 才是 chunk 级过滤字段、恒可得）。不采「doc_type 映射 doc_kind」：两枚举语义不同（doc_type 五值 vs DOC_KINDS 六值），映射表是第二事实源。
- `chunk_id` 进字段（而不只当 `_id`）：`_id` 用确定性复合键 `{kb_doc_id}:{chunk_index}`（见下），chunk_id 作为 pg 行主键的显式字段供 B19 回投与一致性核对直查。
- `_id = {kb_doc_id}:{chunk_index}`：确定性 → **重写幂等**（同 id 覆盖不重复）；pg 侧 `uq_kb_chunks_doc_idx`（kb.py:101-104）保证 (kb_doc_id, chunk_index) 唯一 → 1:1 映射零碰撞。随机 _id 会让重索引必须先删后插且无法对账——被否。

### 4.4 写入/删除操作面（KbEsIndexer 协议）

| 方法 | 语义 |
|---|---|
| `ensure_index()` | index exists? 否则 create（幂等；mapping 变更不自动迁移——索引可整体重建，OQ-28 语义） |
| `index_chunks(kb_doc_id, user_id, chunks, doc_meta)` | 先 `delete_by_query {term:{kb_doc_id}}`（清旧版残留——重切分 chunk 数变少时不留高序号残影）→ bulk `{"index": {"_index", "_id"}}` NDJSON → 判定：response `errors==False` 且逐 item status ∈ {201 created, 200 updated} 之外为零 → 成功；任一 item 失败 → 抛带失败计数的异常（pipeline 转 failed） |
| `delete_document(kb_doc_id, user_id)` | `delete_by_query` 按 kb_doc_id（**签名已被 import_worker.py:1415 缝钉死，B18 实装必须原样**）；`delete_by_query` 返回 failures 非空 → 异常 |
| `count_by_doc(kb_doc_id)` | `_count` term 查询（回填 skip 判定 + audit） |

### 4.5 客户端与依赖

- **官方 `elasticsearch[async]==8.4.1`**（AsyncElasticsearch）== 锁定：与 server 8.4.1 **精确同版**——插件 descriptor 已把 server 锁死在 8.4.1（R23），client 同版使兼容矩阵退化为单点，零版本警告噪声；`helpers.async_bulk` 的逐 item 错误聚合是失败判定（§5）的依赖面，自研易错。新增传递依赖 elastic-transport + aiohttp（aiohttp==3.14.5 已在 uv.lock，实现批 `uv lock` 后核对解析版本是否被抬——抬版须跑既有回归，风险 §11）。
- 被否备选：自研 httpx 薄客户端（项目有 searxng wrapper 先例）——bulk NDJSON + 逐 item 错误解析 + delete_by_query 校验约 200+ 行且重试语义自担，收益（省 2 传递依赖）不抵风险；httpx 路线留作「官方客户端与 aiohttp 版本互锁爆雷时」的降级备选。
- 工厂形态：`get_es_client()` 惰性单例（`get_engine`/`get_kb_minio_client` 先例）；`hosts=[{host, port, scheme:"http"}]`，`basic_auth` 仅 username 非空时携带；超时/重试参数集中工厂（transport 层 max_retries=1 兜连接抖动，业务级重试归 pipeline——两层不混淆）。

---

## 5. 决策四：indexing 段语义（钉死）

### 5.1 缝替换：`resolve_indexing_confirmation` → 真写入

`IndexingConfirmation` 形态升级（pipeline.py:68-83）：

```python
@dataclass(frozen=True)
class IndexingConfirmation:
    es_enabled: bool
    channel: str            # "pgvector"（灰度单通道）| "dual"（双通道）
    es_pending_seam: bool   # 保留字段、语义反转注记：仅 es_indexer 未接线时为 true（留痕放行兜底）
```

- `es_enabled=false`：`channel="pgvector"`，**不触碰 ES**——indexing→ready 直迁（现状字节级不变，915 存量行零扰动；分支先于 es_indexer 判定，接线缺失不影响灰度态）；
- `es_enabled=true` 且 `es_indexer` 已注入：indexing 段执行真写入（5.2），确认成功才 `transition(indexing → ready)`——契约 §1.19「ready=启用通道均确认」兑现；
- `es_enabled=true` 且 `es_indexer=None`（接线缺失/单测未注入）：维持现留痕放行行为（BusinessLog WARN「B18 缝未接线」）——**灰度安全兜底**，不因装配遗漏而全量文档 failed； FeishuImportWorker es_indexer seam（import_worker.py:713）同款先例。

### 5.2 写入流程（es_enabled=true、缝已接线）

```
indexing 段（process_document :392-411 替换）：
  1. chunks = repo.list_chunks_by_doc(kb_doc_id)        # pg 事实源读回（新增仓储方法，ORDER BY chunk_index）
     ——不重切：chunking 段已 replace_chunks+transition，resume-from-indexing（重启钩子）也走本读回路径，
        幂等且与 OQ-34 content_hash 落列口径自洽
  2. doc_meta = {catalog_path: row 取，doc_kind: get_ir_json(root).doc_kind（失败 WARN → None）}
  3. es_indexer.ensure_index() → index_chunks(...)      # 决策四 4.4（先删后 bulk，确定性 _id）
  4. 确认 = bulk response errors==False 且零失败 item     # 「ES 确认点」判据（refresh=false 不等可见，
                                                          #  ack 即确认——B19 读路径容忍 ≤1s refresh 滞后）
  5. 确认成功 → transition({"indexing"} → ready, ready_at)   # 与现状同一乐观卫，零新状态值
```

**双写顺序对齐**：契约 §1.19「pgvector 先 ES 后」——pg chunks 写入（replace_chunks）在 chunking 段已 commit，indexing 段只做 ES，顺序天然成立。

### 5.3 失败语义（与六态状态机、启动钩子自洽——「不永卡 indexing」三条保证）

| 场景 | 语义 | 状态机落点 |
|---|---|---|
| ES 瞬态失败（连接拒/超时/503/429） | pipeline 层重试 **2 次**（退避 1s/3s，asyncio.sleep；retryable 集合=ConnectionError + TransportError(status∈{429,5xx})）——与 litellm 网关重试去重同量级 | 重试中状态仍 indexing（乐观卫期望集不变） |
| 重试耗尽 | `mark_failed(fail_reason="indexing: ES 写入失败: {detail[:…]}")`——阶段化前缀对齐 api.md §4.3；detail 零 chunk 内容回显（仅类型名/状态码计数） | failed（**不静默 ready**，契约 §1.19 钉字）；文档可见 failed+原因，原件零 purge 可重跑 |
| ES 永久不可达 + es_enabled=true | 同上耗尽 → failed；重启钩子不捞 failed（REQUEUE_STATUSES 只含四个中间态，pipeline.py:49-54） | 不永卡：每路径终态 ∈ {ready, failed, gone, aborted}，indexing 是过渡态 |
| 重启 resume-from-indexing | 启动钩子重入队（main.py:233）→ process_document 直入 indexing 段 → 步骤 1 读回 pg chunks → 步骤 3 先删后 bulk——**重复执行零重复数据**（确定性 _id 覆盖） | 状态机零新分支（pipeline.py:392 注「indexing 直入确认段」既有语义） |
| 处理中 spec 热更 es_enabled=true→false | 读 es_enabled 在 indexing 段入口（现 :394 位置不变）——false 分支即灰度单通道确认 ready | 灰度语义（§1.19「启用通道均确认」——启用面动态） |
| aborted（并发改写/行删） | 现状口径不变：transition rowcount 0 → return "aborted"，绝不覆盖他人状态 | pipeline.py:359-363 铁律原样 |

**es_enabled=false 分支字节级不变的自证要求**：unit 用例断言「false 时 es_indexer 任何方法零调用 + transition 参数逐字段与现状相同」——变异测试式锁死（§8）。

---

## 6. 决策五：删除级联（钉死）

### 6.1 两条删除路径接线

| 路径 | 现状 | B18 接线 |
|---|---|---|
| REST 删除（`app/rag/service.py:420 delete_document`） | 行删（chunks FK CASCADE）→ sources 反引用清理 → MinIO purge best-effort；无 ES 腿 | MinIO purge **之前**插 ES 腿：`es_indexer.delete_document(kb_doc_id, user_id)`，best-effort——异常 WARN（action=`kb_purge_es`）继续，**不阻断删除主流程**；`KbService` 构造注入可选 `es_indexer=None`（缺省零操作=现状），生产装配在 `get_kb_service` 缝补注入 |
| 飞书级联删除（`app/rag/import_worker.py:1413-1422`） | seam 已在：`self._es_indexer` 注入 None 即零操作 | **仅改装配**：`get_import_worker`（:1547）由 `es_indexer=None` 改为 `get_kb_es_indexer()`——消费点与签名零改动 |

### 6.2 ES 不可达表现语义（钉死）

- **best-effort WARN，删除不失败**：对齐同函数内 MinIO purge 先例（service.py:431-439）与 import_worker 级联注（:1420「ES 腿失败不阻断级联主流程」）；REST 响应恒 `200 {"deleted": true}`（api.md §4.5 契约零变更、零新增错误码）。
- **残余孤儿治理**：ES 删除腿失败 → ES 残留「pg 已删」的孤儿 chunk（B19 检索可能召回已删文档的 chunk——溯源端点有「已删除降级」兜，api.md §4.6）。治理归重建脚本 `audit` 模式（§7）：核对「ES _id 前缀 kb_doc_id ∉ pg kb_documents」清单计数 + `--mode audit --purge-orphans` 一键清孤儿。自用单机 + pg 事实源（ES 可整体重建）语义下接受窗口期，不做删除重试队列（复杂度不抵收益）。
- 删除顺序：pg 行删 → ES 腿 → MinIO purge——pg 先行保持「事实源先删」（ES/MinIO 均为可重建副本侧），与契约 §1.19 级联口径同构。

---

## 7. 决策六：存量回填（钉死）

**落点 `scripts/kb_es_backfill.py`**（运维脚本进 `scripts/`、入 typecheck-backend 门禁范围——Makefile :131 范围含 scripts/；uv run 从仓库根跑，env_file 相对 cwd 解析）。

### 7.1 三模式

| 模式 | 行为 | 幂等语义 |
|---|---|---|
| `--mode backfill`（默认） | 遍历 `kb_documents WHERE status='ready'` 逐文档：`count_by_doc == pg chunk_count` → **skip**；否则读回 kb_chunks + doc_meta → ensure_index → 先删后 bulk | 二次执行近零成本（skip 判定）；重跑任一文档零重复（确定性 _id + 先删后 bulk） |
| `--mode rebuild` | 删除 `{prefix}_kb_chunks` 整索引 → ensure_index（重建 mapping）→ 全量 backfill → 显式 `POST /_refresh` | **OQ-28「ES 可整体重建、重建脚本随 B18 交付」的兑现**；pg 为事实源零风险（契约钉字） |
| `--mode audit` | 只读核对：ES 总数 vs pg 总数、逐文档 count diff 清单、孤儿清单（ES kb_doc_id ∉ pg）计数；`--purge-orphans` 可选清孤儿 | 只读缺省，写操作显式 flag |

### 7.2 bulk 尺寸与节律（钉死）

- **逐文档一个 bulk**：存量 24802 chunks / 915 docs ≈ 平均 27 chunks/doc；单 chunk ≤800 字符 + 重叠 → 单 bulk 数十 KB，远低于 ES 官方安全线（5–15MB）——零分批复杂度；
- 护栏仍钉死（防御异常大文档）：单 bulk **≤500 actions 或 ≤8MB** 先到者切分（纯函数窗切器，unit 可测）；
- 文档间**串行**、零 sleep：单节点本地、自用零并发需求；915 文档全量耗时预估分钟级（毫秒级/文档 × ES ack）——不做并发（正确性 > 速度，失败单文档可重跑）；
- 全量完成后显式 `_refresh`（backfill/rebuild 模式尾部）。

### 7.3 装配与门禁

- DB 走 `get_session_factory`（asyncio 驱动，沿 `scripts/probe_kb_pipeline.py` 形态）；ES 走 `get_es_client` 同一工厂——生产路径与脚本零分叉；
- 日志 BusinessLog/计数打印，**零 chunk 内容回显**（安全纪律）；
- Makefile 新 target：`kb-es-backfill:`（`uv run --package search-api python scripts/kb_es_backfill.py $(ARGS)`）——`make kb-es-backfill ARGS="--mode rebuild"` 形态（触点清单 §10，设计只列）。

---

## 8. 决策七：测试方案（钉死）

### 8.1 unit（全 mock 零网络——门禁硬面）

| 文件 | 断言面 |
|---|---|
| `test/unit/test_kb_pipeline.py`（扩展） | ①es_enabled=true + 假 es_indexer 成功 → ready 前 index_chunks 恰调一次、先 ensure_index 后 bulk；②假体抛 retryable → 2 次重试后 failed、fail_reason 前缀 `indexing:`；③es_enabled=false → es_indexer **零调用** + transition 字段与现状逐字段相同（字节级不变自证）；④es_indexer=None + true → 留痕放行 ready（缝兜底）；⑤resume：行 status=indexing 直入写入段、不重切（chunk_forest 零调用）；⑥bulk response errors 非空 item → failed |
| `test/unit/test_kb_es_indexer.py`（新） | 纯函数面：索引名拼接、mapping/settings 快照断言（analyzer 名 `jieba_index/jieba_search`、`_id` 形态 `{kb_doc_id}:{chunk_index}`、heading_path keyword 副本、loc/references enabled:false）、bulk action NDJSON 构造（meta 七键 → 字段逐键映射、doc_kind/catalog_path 注入）、delete_by_query body、**auth 仅 username 非空**、重试集合判定（429/5xx retryable、400 不可 retryable）、bulk 尺寸窗切器 |
| `test/unit/test_config.py`（扩展） | ESSettings 六键缺省值；ES_MEM_LIMIT **不声明**（extra=ignore 兜住、声明反例断言其不入 ESSettings——SEARXNG_SECRET 反例先例同款）；index_prefix 非法字符拒收；password SecretStr repr 打码 |
| `test/unit/test_kb_service.py`（扩展，若已有则并入） | 删除 ES 腿 best-effort：假体抛异常 → 返回值仍 `deleted=True`、WARN 留痕；注入 None → 零调用 |
| `test/unit/test_requeue_status_set_matches_contract`（既有） | REQUEUE_STATUSES 含 indexing 逐字断言保持绿（零改动自证） |

### 8.2 integration（真 ES :19200——skip 门控，非绿不可的是 unit 面）

- **门控形态**（对齐 integration/conftest.py:186-219 `requires_db` 先例）：新 `requires_es` fixture——session 级先 `GET /`（或 ping），不可达 → `pytest.skip("ES 不可达（127.0.0.1:19200；make infra-up 起 elasticsearch）——requires_es 用例跳过")`；ES 连接参数读 settings 缺省（零 .env.test 必填——.env.test.example 补一行注释键即可，非硬要求）；
- 用例（`test/integration/test_kb_es_channel.py` 新）：
  1. ensure_index 幂等（二次 create 不报错、mapping 不漂）；
  2. 真库建 1 文档 3 chunks（requires_db + requires_es 双门控）→ index_chunks → `_count` ==3 → 重跑 index_chunks（模拟 resume）仍 ==3（**确定性 _id 幂等实测**）；
  3. jieba analyzer `_analyze` 黄金断言：`中华人民共和国国歌` → `['中华人民共和国', '国歌']` 词级 token（探针 §5.1 输出逐字锁定——分词器回归哨兵）；
  4. user_id term 隔离：他 user 查询零命中；
  5. delete_document 后 `_count` ==0；ES 停机形态（client 指错误端口注入）→ delete_document 抛异常（供 best-effort 上层捕获断言）。

### 8.3 黄金测试集探针规格（ask 钉定项）

**落点 `scripts/probe_kb_es_golden.py`**（沿 `scripts/probe_kb_pipeline.py` 脚本形态；部署验收工具，不进 make test 门禁——同断言面已被 integration 覆盖，脚本供起栈后一键复验）：

- **语料**：固定 20 条中文 chunk 样例（内置脚本常量，零外部依赖）——三类覆盖：词级命中型（探针 §5.2 十条样例扩展）、专名/型号型（BM25 词汇轴核心价值，ADR-017 背景）、标题链型（heading_path 加权面）；
- **查询**：固定 8 条——①专名精确词 ②标题链词 ③正文词 ④混合词 ⑤他 user 同词（隔离负例）⑥已删文档词（删除负例）⑦standard 单字噪声对照（「学习」类高频单字——jieba 词级得分应显著高于单字撒网，探针 §5.3 8.8 vs 2.8 形态）⑧空结果边界；
- **断言面**：全 8 条 top1 命中预期或负例零命中；⑦号对照组得分差 >阈值；user_id 隔离；重跑（先删后 bulk）count 与得分稳定——**确定性探针，全断言可机器判定**；输出 PASS/FAIL 汇总 + 得分表（零正文回显）。

### 8.4 门禁口径

- `make test`（unit + integration + 双端覆盖率）：ES integration 用例在 :19200 不可达时 skip（skip 是明示降级不是绿）——**门禁通过的硬面 = unit 全绿 + 既有 requires_db 面 + ES 用例绿或具名 skip**；发版前在 ES 起栈状态下完整跑一轮 integration 全绿（实施批 DoD 写明）；
- `make typecheck` 0（pyright + ruff，范围自动覆盖 rag/indexers 与 scripts/）；
- e2e 不新增（KB 检索 e2e 面 = B19 消费侧，本批写入侧以 integration+golden 覆盖）。

---

## 9. B18 行内必答项：embedding 轨迹前置与向量陈旧裁决（architect 方案——**待用户确认**）

> 依 ARCHITECTURE §8 B18 行「向量陈旧裁决随本单契约批必答——architect 出方案（hash 纳入 heading_path 与否）经用户确认」+ 调研 `chunking-semantic-integrity-research-20261124.md` §9 附随裁决②。以下为方案，**未经用户确认前实现批不得改 content_hash 行为**。

**① heading_path 前置进 embedding 输入**（用户已裁决「按照建议点来落地」，本节落护栏）：
- embedding 输入文本 = `标题轨迹：{" > ".join(heading_path)}\n{content}`（轨迹根→叶）；
- 护栏一（轨迹长度占比上限）：轨迹串截断 ≤ **200 字符**（代码常量，落 embedding 接线模块；超限保**叶端**——leaf 侧语义最具体，root 侧「文档题名」已由 doc 级标题承担）；
- 护栏二（重嵌入判据）：文档 `content_hash` 变更 = 唯一重嵌入触发器（见②）。

**② 向量陈旧裁决——方案 A（建议采纳）：content_hash 输入纳入标题结构**：
- `document_content_hash`（chunker.py:213 既有产出）计算输入由「document_text」扩为「document_text + '\n' + 标题结构清单」（标题结构清单 = IR 全部 heading 节点的 `heading_path` join 串按序拼接）；
- 效果：标题改名/增删/移动 → hash 变 → 飞书 diff 判「变更」→ 整文档三存储级联重导 → 重切 + **重嵌**（真实向量时代）+ ES 重写（本批已幂等）——向量与轨迹永不陈旧；
- 取舍理由：915 文档量级下「标题改名整文档重嵌」成本可接受（embedding 按量计费、单文档 chunks ≤数十）；**反对方案**（独立结构 hash 列）需 DDL + 契约升版，违背最小方案；**反对方案**（hash 不动）= 轨迹陈旧向量静默服役，与调研 §9 采纳「重嵌入判据」护栏相悖。
- 契约触点（实现批随批，主会话契约批执行）：data-model §1.7 content_hash 语义注 + §1.8 检索注；nacos-config 零新键。

**③ 实施边界重申**：本节①②的代码落点在 embedding 向量通道接线批（§1 范围边界）；②若用户确认，可在本 B18 实现批顺带落地（chunker 单点改动 + fixture 锁定），也可挂到向量通道批——**由用户在契约批一并裁决**。

---

## 10. 决策八：文档触点清单（巡检归主会话——只列清单不代写）

| 触点 | 动作 |
|---|---|
| `deploy/docker-compose.yml` | +elasticsearch 服务（§2 全文）+ es_data 卷 |
| `deploy/es/Dockerfile`、`deploy/es/plugins/jieba/`（构建产物入库）、`deploy/es/PROVENANCE.md`、`scripts/build_es_jieba_plugin.sh` | 插件交付四件（§2） |
| `apps/search-api/app/core/config.py` | ESSettings 组 + 单例 + docstring 例外清单补 ES_MEM_LIMIT 行 |
| `apps/search-api/app/rag/indexers/`（新包三文件）+ `pipeline.py` + `rag/service.py` + `rag/import_worker.py:1547 装配` + `KbDocumentRepository.list_chunks_by_doc` | 应用侧接线面（§3~§6） |
| `scripts/kb_es_backfill.py`、`scripts/probe_kb_es_golden.py` | 回填/重建/审计 + 黄金探针（§7/§8.3） |
| `Makefile` | +`kb-es-backfill` target（$(ARGS) 透传三模式） |
| `scripts/infra_smoke/preflight.py` | PORTS 清单 +19200（起栈前置端口空闲预检——0.2 实测现缺） |
| `scripts/infra_smoke/es_smoke.py` + `Makefile smoke-infra` 循环 | 七组件→八组件：GET / + `_cat/plugins` 断言 jieba 1.0.0 在位（实现批判定，建议加） |
| `.env.example` | ES_ 组六键**已存在零新增**；仅注记更新（compose 服务名/索引名示例/MEM_LIMIT compose 消费型归并口径） |
| `apps/search-api/pyproject.toml` + `uv.lock` | +`elasticsearch[async]==8.4.1` == 锁定 + 注释（与 server 同版理由、R23 联动升版注） |
| `docs/contract/*`（主会话契约批） | data-model §1.19：mapping 落字细节指路 `app/rag/indexers/`（:604 事实源分层条款——**零升版候选判定**归主会话）；api.md §4.5 删除语义注（ES 腿 best-effort、契约面零变更判定）；nacos-config.md **零变更**（kb.es_enabled 已在 v1.43 kb 节 13 键内，零新键）；§9 ② 若用户确认 → data-model §1.7 content_hash 语义注升版 |
| `docs/ARCHITECTURE.md` / `docs/DEVELOPMENT-PROCESS.md` / `docs/PROGRESS.md` / README | §5 ES 行补 compose 实装注、§8 B18 行状态、R22/R23 处置状态写回（VM 配额已上调 10.44GiB 实测）、巡检记录、批次 4 行——全部归主会话巡检 |

---

## 11. 实施序（供派单，TDD 纪律贯穿）

1. **基建先就绪**（B18 行硬前置）：插件构建脚本跑通 → `deploy/es/` 四件入库 → compose 起 ES → `_cat/plugins` 断言 jieba → es_smoke/preflight 增量；
2. config + es_client + es_mapping 纯函数（红→绿）；
3. es_indexer 操作面 + integration `requires_es` 门控与 5 用例（红→绿，需 ES 活体）；
4. pipeline indexing 段替换（含失败/重试/resume/灰度分支，红→绿）+ 删除两路接线；
5. backfill/rebuild/audit 脚本 + Makefile target；
6. golden 探针 → **存量回填 915 docs**（backfill → audit 核对 ES==24802 → golden 探针全绿）；
7. 全量门禁（make test / typecheck / ES 起栈态 integration 全绿）+ 双评审 + 巡检归主会话。

---

## 12. Risks（如实留档）

1. **插件构建产物入库（~7.3MB 二进制 jar）**：供应链折衷——探针首选 infinilabs 已下架（三重证据），sing1ee 为单人维护项目（bus factor=1）；本批以 PROVENANCE + 配方脚本过渡，**全源 vendor + CI 构建归 M3+ 供应链工单**（R22/R23 候选线），须用户知情；
2. **ES 版本锁死 8.4.1**：插件 descriptor 硬校验 + master 分支因子模块指针丢失不可构建（探针 §3）——server 升版 = client+插件+重构建三联动（R23 沿用），非本批可解；
3. **探针构建产物不在盘上**：探针 §7 已清理 /tmp/probe_es 与探针容器——实施批须先跑 `scripts/build_es_jieba_plugin.sh` 重建（配方探针 §3 固化，gradle 下载走腾讯云镜像 sha256 校验；本会话未预跑构建，属实施批第 1 步）；
4. **aiohttp/elastic-transport 传递依赖**：aiohttp==3.14.5 已锁（uv.lock:90），elasticsearch[async] 解析后若抬版须跑 langfuse/litellm 关联回归（实现批 uv lock 后核对）；
5. **无双 phase commit**：ES 写在 pg chunks commit 之后，indexing 失败 → failed 但 pg 已有 chunks——重跑幂等（先删后 bulk）恢复；「ES 有 pg 无」孤儿由 audit --purge-orphans 兜底。接受理由：pg 事实源 + ES 可整体重建（OQ-28）；
6. **删除腿 best-effort 的孤儿窗口**：ES 不可达时删除留孤儿、B19 可能短暂召回已删文档 chunk（溯源端点「已删除降级」兜）——audit 模式治理，不做删除重试队列；
7. **golden 语料为构造样例**：20 条为脚本内置非真实 KB 语料——分词质量正式结论归 M4 评测线（探针 §6⑤ 同口径），本探针只锁「词级分词 + 隔离 + 幂等」确定性面；
8. **VM 配额复核**：10.44GiB 为本会话 `docker info` 实测，但**现役栈实占未复测**（探针时代 ~6.1GiB）——起栈后 `docker stats` 复核 ES 实占 ~0.9-1GiB 塞得下（heap 512m + mem 1g 上限），归实施批第 1 步留档；
9. **B18 冻结工单行内 embedding 腿不在本批**：范围边界（§1）与 ask 口径收敛所致——若主会话认为 embedding 向量通道须并入批次 4 同批交付，需重新扩界（契约必答项方案已在 §9 待确认，不阻塞 ES 通道先行）。

---

## 13. 实施偏离（2026-11-25 应用侧实施批追加——TDD 全程留痕，门禁面见交付说明）

> 实施批 = B18 ES BM25 写入通道**应用侧**（基建侧 deploy/es/ 四件 + compose + preflight 已由前置批落地）；本节按 ask 纪律「偏离须在设计文件追加实施偏离节」逐条留档。全部偏离均经实测核验（elasticsearch==8.4.1 + elastic-transport==8.19.1 实装环境），判据语义与设计钉版逐字保持。

| # | 设计原文 | 实施落地 | 理由（实测证据） |
|---|---|---|---|
| 1 | §5.3 retryable 集合「=ConnectionError + TransportError(status∈{429,5xx})」 | `es_indexer.is_retryable_es_error`：连接族（`elastic_transport.ConnectionError` + `ConnectionTimeout`）+ **ApiError 族**（status 取 `meta.status`/兼容属性 `status_code`，429/5xx retryable；4xx 不重试） | 实装探针证实 elastic-transport 8.19 的 `TransportError.__init__(message, errors)` 无 status（`elasticsearch.TransportError` 纯 re-export）；HTTP 错误实际形态 = `elasticsearch.ApiError` 族（elasticsearch/exceptions.py:24-39 有具名 4xx 子类，status_code property 读 meta.status）。判据语义（连接类/超时 + 429/5xx 重试、400 等 4xx 直接 failed）与设计逐字一致，仅异常类名随 8.x 客户端实际形态落定 |
| 2 | §4.3 mapping 含 `kind`（「chunk 首来源单元统一类型 Chunk.kind」）× §5.2「pg 事实源读回不重切」 | kind 走 **IR 树派生**：pipeline 新增模块级 `collect_node_kinds(root)`（node.id → node.type.value 全量映射），随 doc_kind 同一次 `get_ir_json` 取得，doc_meta 第三键 `node_kinds` 注入 `es_mapping.build_chunk_doc`；IR 不可达 → kind=None（doc_kind 同语义不阻断） | 设计内部张力：`Chunk.kind` 不在 meta 七键（data-model §1.8 契约钉死）、无列——§5.2 强制 pg 读回路径下 kind 不可直接得（重切被 §5.2 否、meta 加键属契约变更被禁）。派生与 chunker 同一事实源（chunker.py:900/958 `kind=first.unit.kind`，unit.kind=`node.type.value` chunker.py:765）——非第二事实源、零 DDL、零契约变更；ES 端 keyword 可空 |
| 3 | §4.4 bulk 判定「response errors==False 且逐 item status∈{201,200} 之外为零」 | `helpers.async_bulk(raise_on_error=False)` → `(successes, errors[])` 聚合形态：errors 空=成功；非空 → EsIndexError 带「失败 N/total item」计数 | §4.5 依赖面钉定本就是 helpers.async_bulk（「逐 item 错误聚合是失败判定的依赖面」）——聚合形态与逐 item status 扫描语义同构，实现采 helpers 原生返回形态 |
| 4 | §5.2 步骤 2 doc_meta 两键（catalog_path + doc_kind） | 实现三键（+`node_kinds`） | 服务偏离 2 的 kind 派生；`build_es_doc_meta` 提升为 pipeline 模块级函数供回填脚本同源复用（§7.3「生产路径与脚本零分叉」） |
| 5 | 回填脚本落点：ask 文本 `scripts/backfill_es.py` | `scripts/kb_es_backfill.py`（Makefile target `kb-es-backfill` 同名族） | 按 ask 纪律「按设计落码」采 §7.1 钉定名；三模式（backfill/rebuild/audit + --purge-orphans）全交付，`--help`/audit 只读模式实测通过（pg 25979 chunks / ES 索引未建态如实呈现，exit 0） |
| 6 | （设计未指定） | pipeline 退避 sleep 走模块级缝 `_es_retry_sleep = asyncio.sleep`（单测 monkeypatch 免真等 1s+3s） | 项目测试纪律：模块级名 monkeypatch 替换（stub_pipeline_seams 同款先例） |
| 7 | （设计未点名触点） | pyproject `[tool.pytest.ini_options]` 注册 `requires_es` marker | §8.2 门控形态（marker + fixture skip）需注册否则 pytest 告警；与 requires_db 五 marker 同列 |
| 8 | §11 步骤 6「存量回填 915 docs → audit → golden 探针」 | 本应用侧实施批**未执行**全量生产回填与 golden 探针（ask 交付清单不含 `scripts/probe_kb_es_golden.py` 与回填执行） | ask 口径=应用侧代码+测试+脚本交付；幂等由 integration rerun 用例（真 ES 确定性 _id 重跑 count==3 不变）+ backfill skip 判定承担，全量回填归批次收尾（主会话/后续派单执行 `make kb-es-backfill`） |
| 9 | （依赖核验，风险 4 兑现） | `elasticsearch[async]==8.4.1` 解析新增 elastic-transport 8.19.1；**aiohttp 保持 3.14.5 未被抬**（uv lock 后核对）；uv.lock 已同步 | `uv lock` 输出仅 +elastic-transport +elasticsearch 两包，风险 4 预警解除 |
| 10 | §8.3 钉定 `scripts/probe_kb_es_golden.py`：内置 20 chunk 语料 + 8 固定查询（含⑦standard 单字噪声对照）+ 全断言机器判定、PASS/FAIL 汇总的**确定性部署验收快检探针**（2026-11-26 主会话收口批补登记——评审 P3#9 指出的线头；§14 修复批后已知偏离此前未入表） | **判定：未交付，降级为可选后续项**（不再单独交付）。树内实际并存的是另一个定位不同的工具：`scripts/probe_es_bm25_golden.py` = **B19 混合检索对照基线测量工具**——读黄金测试集 xlsx 100 题、以契约钉版固定查询形态（multi_match content+heading_path^2 / term user_id / top30）测真实 KB 语料上的 BM25 命中率（锚点@10=98、摘录@10=100，基线报告 `docs/research/m3-b18-golden-bm25-baseline-20261125.md` 已落），退出码 0=完成、不作合格判定；两者不可互相替代（一个测「写入/分词/隔离/幂等」的确定性面，一个测「真实语料排序质量」的基线面）。降级理由三条：①§8.3 的「部署验收」目的已被两处既有资产覆盖——`integration/test_kb_es_channel.py` 5 用例承担确定性面（ensure 幂等 / 重跑 count 不变 / jieba `_analyze` 词级黄金 token / user_id 隔离 / 删除清零+停机异常），§14 修复批的 `scripts/infra_smoke/elasticsearch_smoke.py` 承担起栈后一键快检（版本 + jieba 插件在位 + `jieba_search` 黄金分词）；②§8.3 设计自述「不进 make test 门禁——同断言面已被 integration 覆盖，脚本供起栈后一键复验」，该复验位现由 smoke 填补；③**如实注明残余未覆盖面**：⑦standard 对 jieba 的单字噪声得分差对照、⑧空结果边界、标题链加权 top1 断言三项目前无任何自动化断言承载（标题链加权仅由 xlsx 基线在真实语料上间接观测）——属排序质量面、由 B19 混合检索评测线与 xlsx 基线回归承接更合适，故留作可选后续项而非宣称「已被覆盖」；若 B19 需要分词器回归哨兵再立项。同时订正 #8：存量回填已于批次收尾执行（25979 chunks 入 ES），#8「全量回填归批次收尾」一句已兑现 |

**门禁实跑记录（本节为设计文件内留痕，非 docs/ 巡检替代）**：
- unit 定向（test_kb_pipeline/test_kb_service/test_kb_es_indexer/test_config/test_kb_import_worker/test_api_kb/test_main/test_dependencies/test_db_models）：**295 passed**（红期记录：test_kb_pipeline 25 failed + indexers/config 收集错误 → 实装后全绿）；
- integration 定向（test_kb_es_channel 5 用例真 ES:19200 + test_kb_import_cascade + test_health）：**9 passed**；jieba `_analyze` 黄金断言逐字过（`中华人民共和国国歌` → `['中华人民共和国','国歌']`）；
- `make typecheck-backend`：pyright 0 errors + ruff All checks passed（初跑曾 1 pyright 报错 + 2 ruff F401，已修复复跑绿）。

## 14. 修复批（2026-11-26 双评审建议修复遗留三项——主会话派单）

> 本批只清偿评审建议修复项，批次主体（应用侧代码/脚本/契约面）零行为扩界。三项逐条留痕：

| # | 评审项 | 修复落地 | 验证 |
|---|---|---|---|
| 1 | **P2（#2+#12 同根）** `pipeline.py::_write_es_with_retry` 的 `ensure_index()` 曾在重试 try 面之外 → ES 瞬态不可达命中 ensure 即零重试终态 failed，且原始异常串（含内部 host:port）经 `process_document` 的 `f"{stage}: {exc}"` 兜底落入用户可见 fail_reason | `ensure_index` 移入重试 try 面内（`index_ready` 标志：就绪后不重复调用，仅省一次往返）；ensure 阶段的连接拒/超时与写入共用同一 `is_retryable_es_error` 判定 + `_es_retry_sleep` 退避节奏；耗尽/不可重试一律经既有 `_es_failure` 包装脱敏（`indexing: ES 写入失败: {类型名}(attempts=N)`，零原始 message） | TDD 红→绿：新增 `test_es_ensure_index_retryable_failure_recovers`（ensure 首抛 retryable、二次成功 → ready + ensure 恰 2 次 + index_chunks 1 次 + 退避 [1.0]）、`test_es_ensure_index_retryable_exhausts_and_fails_desensitized`（恒 retryable → failed + 前缀 `indexing: ES 写入失败:` + 断言零 `127.0.0.1`/`19200`/`refused` 回显 + attempts=3）；红期实测 `assert 1 == 3`（旧形态零重试）；哨兵用例（es_enabled=false 零 ES 调用、transition 逐字段不变）零改动保持绿 |
| 2 | **P2（#10）** `scripts/build_es_jieba_plugin.sh` 对源码 pin 零校验（分支 HEAD 跟随 + submodule PIN 仅 echo） | 新增 `PLUGIN_COMMIT=cbca4e7…`/`SUBMODULE_PIN=4c16d01…`（取值=PROVENANCE 钉版）→ clone/submodule update 后逐项比对，任一不符 `FATAL ... 实际 vs 钉版` + exit 1（对齐 gradle zip sha256 硬校验风格）；步骤 4 追加产物 sha256 留痕（非位级可复现，仅人工核对） | `bash -n` 过；活体上游核验：`git clone --depth 1 --branch 8.4.1` HEAD == `cbca4e7ebc5fe6f6b1e5e1804a6daf0da7039c7e`、`git ls-tree HEAD jieba-analysis` gitlink == `4c16d01ea549c8e11315f49c0922ac4f8a07accc`（钉值非猜测）；校验块三态实跑：子模块 pin 错/源 commit 错 → FATAL exit 1，双对 → exit 0。**未跑全脚本**（会覆盖现有 jar——按 ask 纪律禁止） |
| 3 | **设计 §10 收尾缺口** smoke-infra 七→八组件未落地（基建/实现两阶段互相推诿） | 新建 `scripts/infra_smoke/elasticsearch_smoke.py`（形态对齐既有 smoke：`_load_env/_clean/_is_placeholder/_NoRedirect/_OPENER/_validate_local_url/_http/_emit`，仅标准库、URL 仅 127.0.0.1、ES_USERNAME/ES_PASSWORD 非占位时 basic auth 且零打印）；四 checks=`port_19200_open` + `root_version`（version.number==8.4.1）+ `plugin_jieba_present`（_cat/plugins jieba 1.0.0）+ `analyze_jieba_search_golden`（jieba_search「中华人民共和国国歌」→ 同时含『中华人民共和国』『国歌』）；`Makefile smoke-infra` 循环追加 `elasticsearch`、注释七→八 | 活体全绿（4/4 checks，exit 0）；失败路径实测（同代码指 19201 空端口 → 0/4 checks、exit 1）；`make -n smoke-infra` 循环含 elasticsearch 且单迭代实跑 PASS；`docker compose config --quiet` 过 |

**偏离（如实标注）**：ask 文本给的文件名 `scripts/infra_smoke/es_smoke.py` 与同批要求的「Makefile 循环追加 `elasticsearch`」互斥——既有循环按 `scripts/infra_smoke/{name}_smoke.py` 推导脚本名，`elasticsearch` 条目要求 `elasticsearch_smoke.py`。取**约定一致性优先**（文件名=compose 服务名，循环纯追加零特例），交付 `elasticsearch_smoke.py`；内容与原 ask 描述逐项一致。

**门禁实跑记录（修复批）**：
- 定向 pytest `test_kb_pipeline` + `test_kb_es_indexer`：**72 passed**（新增 2 用例红→绿，哨兵零改动）；
- `make typecheck-backend`：pyright **0 errors** + ruff **All checks passed**（范围含 scripts/，新 smoke 脚本过门禁）；
- 未跑全量 `make test`（按 ask 口径归主会话统一执行）。

## 15. 修复批 2（2026-11-26 轻审 3×P2 + 2×P3 清偿——主会话派单）

| # | 评审项 | 修复落地 | 验证 |
|---|---|---|---|
| 1 | **P2-1** 失败出口传 `attempts_total` → 不可重试异常（400 类）首试即 failed 却谎报 `attempts=3` | `_write_es_with_retry` 改传 `attempt + 1`（真实已尝试次数）；`_es_failure`/`_write_es_with_retry` docstring 同步钉语义（不可重试首试 → attempts=1） | TDD 红→绿：`test_es_ensure_index_non_retryable_fails_immediately_with_real_attempts`（400 ApiError → failed、ensure 恰 1 次、`fail_reason == "indexing: ES 写入失败: ApiError(400 attempts=1)"`）；红期实测 `ApiError(400 attempts=3)` |
| 2 | **P2-2** 脱敏断言仅 3 条子串负向 → 可被信息泄漏绕过 | 两处改正向全等：ensure 耗尽 → `"indexing: ES 写入失败: ConnectionError(attempts=3)"`；503 写入耗尽 → `"indexing: ES 写入失败: ApiError(503 attempts=3)"`（原 `startswith` + 子串断言删除） | 两用例绿；上游失败出口任何多余串（message/host/port/正文）都会破坏全等 |
| 3 | **P2-3** 构建脚本 ①sha256 只回显不校验 ②`cp -R` 前不清空 ③PROVENANCE「9 files」与 7 件入库不一致 ④compose 段硬编码 `.` | ①四件逐字稳定产物（`plugin-descriptor.properties` + 三 dic）sha256 钉入脚本常量，解包后 `verify_pinned` 逐件比对，不符/缺件 → FATAL exit 1（在清空 OUT_DIR **之前**校验——坏构建不污染仓库），两 jar 保持仅回显 + 注记；②`rm -rf OUT_DIR && mkdir -p` 后再 `cp -R`（防上游删件残留）；③PROVENANCE 产物行改「入库 7 件」+ 注 zip/临时目录已清、以产物清单为准，「重建方法」段同步两级校验口径；④compose 段改 `--project-directory "${REPO_ROOT}"` 且 `-f` 亦取 REPO_ROOT 绝对路径 | `bash -n` 过；盘上入库件对锁正跑 4/4 OK；篡改 `dic/user.dict` → FATAL exit 1（输出实际 vs 钉版）；删 `dic/stopwords.txt` → `FATAL: 构建产物缺件` exit 1。**未跑全脚本**（会覆盖现有 jar，纪律禁止） |
| 4 | **P3-4** `_es_failure` 重复实现状态码提取 | 改复用 `es_indexer._es_error_status`（`status_code` → `meta.status` 兜底，与 retryable 判定同源）；`EsIndexError`/`_es_error_status`/`is_retryable_es_error` 统一提到模块顶部 import（删 `_es_failure` 内的延迟 import；`get_kb_pipeline` 内的惰性装配 import 是装配懒加载形态、保留） | pyright 0 / ruff passed；全量定向 pytest 绿 |
| 5 | **P3-5** `.env.example` 两处注记仍写「待 ESSettings 落地后接线」 | 两处改已落地事实：应用读取键经 `core/config.py` ESSettings（:282 类 / :316 `es_settings` 单例）+ `es_client` 工厂消费；B18 段头改「已交付」。**本轮按派单允许编辑 `.env.example`（注释行，零变量改动）** | 纯注释变更，行为零影响；`.env` 未动 |

**未落/边界如实标注**：`.env.example` 顶部「组划分」行（:8）仍只列到 `LOG_`，未含 `ES_`/`FEISHU_` 等新组（同一陈旧类的邻近行，但不在本次两条派单范围内，且 ARCH §7 巡检归主会话）——留待主会话巡检一并判定。

**门禁实跑记录（修复批 2）**：定向 pytest `test_kb_pipeline` + `test_kb_es_indexer` = **73 passed**（新增 1 用例）；`make typecheck-backend` = pyright 0 errors + ruff All checks passed；`bash -n` 过。全量 `make test` 归主会话。
