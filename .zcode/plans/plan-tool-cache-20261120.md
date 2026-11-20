# 工具结果缓存（Tool Result Cache）立项提案

| 项 | 内容 |
|---|---|
| 版本 | v1.0（**待用户批准**——T2 小工单立项；批准后随排期 slot 实施） |
| 日期 | 2026-11-20 |
| 提案路径 | 用户 2026-11-20 会话提议（工具节点同工具+同参数直接返回缓存结果）→ AI 设计评审后成文 |
| 基线 | ARCHITECTURE v1.67 / nacos-config v1.37 / api.md v1.27 / PROGRESS v1.58 |
| 级别 | **T2 小工单**（纯后端工具层 + Nacos 键；SSE/REST/DB 契约预期零变更） |

---

## 0. 一句话

同工具+同参数的**只读工具**调用复用缓存结果（省搜索 API 配额与 1~6s 延迟）；**轮内（Scope A）零风险先做**，跨轮只读+短 TTL+时敏绕行（Scope B）条件做，写工具与全局缓存**否决**。

## 1. 背景与动机

用户观察：短期记忆按节点持久化（checkpointer），工具节点输出已随状态留存——相同工具+相同参数的重复调用可以直接返回结果。评审确认这是真实的优化点，但边际价值要摆正：

- **已有覆盖**：merge_dedupe 已做同轮 URL 级结果去重；deep `visited_urls`（批次 3/4 钉语义）已做页面级去重；save_snapshot 已幂等；regenerate 只重跑 synthesis 不重搜。
- **边际价值 = 省掉工具调用本身**（Tavily/BoCha 配额 + 1~6s/源延迟），不是结果去重（已做）。
- **诚实预期**：跨轮命中率受 rewrite 非确定性压制（同一问题两次改写出的 queries 大概率不同，精确参数匹配天花板不高）——轮内场景（reflect 补搜重复、子代理重叠）才是主要收益面。

## 2. 概念定界（重要）

**工具结果缓存（memoization）≠ 记忆模块**。记忆=用户事实跨会话沉淀（memory_service）；缓存=工具输出的复用（工具层包装器）。落点 = 工具 registry 包装层（独立 cache 模块），**不进 memory_service、不占 memory-* 契约、不进 PRD**（内部性能优化，非用户可见需求项）；失效策略/生命周期与记忆完全正交。

## 3. 范围三分

| Scope | 内容 | 判定 |
|---|---|---|
| **A. 轮内缓存**（同 run；key=工具名+参数哈希） | reflect 补搜重复 query、子代理重叠调用直接复用——时间上下文相同零风险，数据源即 checkpointer 已有节点输出 | ✅ **必做（TC1）** |
| **B. 跨轮缓存**（线程级 + TTL） | **只读工具限定**；firecrawl_read 先行（visited_urls 的自然延伸，TC2）；web_search 跨轮**挂 M4 信号**（重复查询率）再议（TC3 挂观察） | ⚠️ 条件做 |
| C. 全局缓存（跨线程/用户） | — | ❌ 否决（个人量级无收益） |

## 4. 两条硬禁令

1. **写工具永不缓存**：save_snapshot 等有副作用的工具，缓存 = 跳过副作用 = 直接错误。
2. **缓存写入发生在护栏通过之后 + 重放时护栏重跑**：url_guard/injection_filter（B15）是纯函数、微秒级——缓存存原始结果、重放时重跑过滤，**防 Nacos patterns 热更后旧缓存绕过新规则**（否则缓存 = 绕过安全护栏的后门）。

## 5. 设计要点

- **key 规范化**：tool 名 + params canonical JSON（键排序序列化）→ sha256 截断；canonical 化单测锁死（同参数不同键序必同 key）。
- **存储**：进程内 LRU（`max_entries` 上限）；重启即清空——可接受（纯优化，无持久化必要）。
- **temporal 绕行**：`query_type=temporal` 或 `time_range≠null` → 强制 bypass（B12 时敏硬排除镜像）。
- **可观测**：命中在 stage detail 标注 `cache_hit` + Langfuse 留痕（沿检索策略观测三通道纪律）；**命中来源照常进 sources、照常标 [n]**——它仍是证据来源，SSE 协议零变更。
- **失败语义**：缓存查找失败 = 视为 miss 正常调用（fail-open 静默）；缓存写失败 = 忽略（只损失优化）；缓存永不服务未过护栏的结果（写入时机在护栏后）。
- **P50 口径**：缓存命中属正常性能优化，正常计入 P50（只会更好），不适用「实验功能不计入」豁免。
- **与 regenerate 无交互**：regenerate 只重跑 synthesis 不重搜。

## 6. Nacos 增量（spec 新增顶层 `tool_cache` 节）

```json
"tool_cache": { "enabled": false, "scope": "run", "ttl_seconds": 1800, "bypass_temporal": true, "max_entries": 256 }
```

- 校验：`enabled` 布尔；`scope ∈ {run, thread}`；`ttl_seconds ≥ 60`；`bypass_temporal` 布尔；`max_entries ≥ 16`；**节缺失回落代码内置默认（enabled=false）并告警**——灰度安全、无发布次序约束（沿 safety fail-open 先例）。
- 回退门：`enabled=false` 单键热更即回现状，零行为差异。
- 归属：AI 行为参数 → Nacos（配置三分法）；缓存数据本身在进程内存，不入 DB/Nacos。

## 7. 工单拆分与闭环条件

| 工单 | 内容 | 判定 |
|---|---|---|
| TC1（必做） | 轮内缓存 + `tool_cache` Nacos 节 + 观测标注 + 单测面（key canonical 化稳定性 / temporal bypass / 写工具禁缓存 / **护栏重放〔patterns 热更场景〕** / fail-open / 默认关闭零行为差异回归） | 本批 |
| TC2（建议同批） | firecrawl_read 线程级复用（visited_urls 延伸：同 URL 同线程内直接复用快照/读结果，TTL 内） | 建议并入 |
| TC3（挂 M4 信号） | web_search 跨轮缓存——重复查询率数据说话后再议 | 挂观察 |

- 门禁：全量 `make test` + `make typecheck`；默认关闭零行为差异回归对比；护栏重放测试含「patterns 热更后旧缓存重过滤」场景。
- 闭环条件：默认关上线 → 开 enabled 观测命中率/误命中（stage detail + Langfuse）→ 报告 → 定 TTL/范围微调 → TC3 决策。

## 8. 契约/文档触点

| 文档 | 增量 | 时点 |
|---|---|---|
| ARCHITECTURE | §4 工具层缓存注记 + §8 工单行 + 风险条目（缓存陈旧/TTL 策略） | 随实施批 |
| nacos-config | `tool_cache` 节 + 校验规则 + §8 init 条 | 随实施批 |
| api.md / data-model.md / frontend-api.md / PRD / types | **零变更**（stage detail extra 非封闭——B15 errors[] 同款判定留档；内部性能优化不进 PRD） | 判定留档实施批巡检 |

## 9. 风险与预期管理

| 风险 | 处置 |
|---|---|
| 跨轮命中率天花板（rewrite 非确定性） | M4 重复查询率数据定 TC3 去留 |
| 护栏 patterns 热更 × 旧缓存交互 | 重放必重跑护栏（§4 禁令 2） |
| 进程重启缓存清空 | 可接受（纯优化；无持久化必要） |
| 缓存陈旧误导回答 | TTL + temporal bypass + 来源照常带检索时间 |

## 10. 待用户拍板（批准本提案时一并定）

| # | 决策点 | 建议 |
|---|---|---|
| ① | TC 范围：A only / **A+B（firecrawl_read 并入）** | **A+B**（TC2 边际成本低，visited_urls 基础现成） |
| ② | 排期 slot：**M2 批次间隙插队独立小批** / M5 前置批搭车 / 其他 | M2 批次间隙（当前批次 5 运行中，批次 6 后的间隙是自然窗口；缓存与 deep 工具链同域，趁上下文新鲜） |

批准后流程：ARCH/nacos 增量随实施批落契约 → TDD 实施（TC1 优先，TC2 视裁决）→ 默认关闭上线 → 观测后定开关与 TC3。
