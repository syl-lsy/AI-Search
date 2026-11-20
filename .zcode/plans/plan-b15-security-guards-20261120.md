# B15 内容侧安全护栏工单：URL Guard + Injection Filter（2026-11-20 计划定稿，用户裁决 flag 观察先行 + 部署即启用）

wf-level: T1 -- 纯后端单域安全工单（内容侧安全护栏：URL 校验器 + 注入特征过滤），前端零改动，契约文档同批巡检

## Context

用户提出搜索 Agent 安全方案（URL 域名防护 + 提示注入拦截），对照勘查后收敛为两个真实缺口：
1. **SSRF/URL 校验缺失**：deep 模式读网页 URL 只查 scheme（`apps/search-api/app/agent/nodes/deep_research.py:358-368`），`http://169.254.169.254`、`http://litellm:4000`（Docker 网内容器）均可通过——备位 Crawl4AI 是 compose 网内容器，可被注入诱导的 URL 打到内网端点，正文回灌上下文造成信息渗出。
2. **网页内容注入特征检测缺失**：现有 safety 包只作用在用户输入侧（B13/B14）；搜索摘要、deep 读页正文、**子代理报告回传协调器**（deepagents task ToolMessage，无包裹的裸面）均无注入扫描。

已核实非缺口：`<external_content>` 包裹+转义硬边界完备（golden 锁面）；B14「history 未过 sanitize」已闭合（`_sanitize_history_rows`，turn_service.py:1927）；fast 模式模型零 tool_call=结构性免疫。

**用户裁决**：① injection_filter 上线姿态 = **flag 观察先行**（命中仅告警保留，跑出误判基线后 Nacos 单键热切 drop）；② 缺键生效语义 = **部署即启用**（运行面缺子键回落 defaults.py 内置种子）。

## 方案总览

两机制放现有 `ai_search.agent.spec.json` 的 `safety` 节内两个嵌套子节，**不新增 dataId**。原则：**代码硬底线 + 配置只能加强不能削弱**（scheme/私网判定是代码常量；Nacos 只扩黑名单与正则；关闸走 enabled 单键）。

```json
"url_guard": {"enabled": true, "blocked_hosts": [], "blocked_host_patterns": [], "max_url_length": 2048},
"injection_filter": {"enabled": true, "patterns": [种子12条左右], "on_hit": "flag", "max_scan_chars": 8000}
```

### 1. URL Guard（新模块 `apps/search-api/app/agent/safety/url_guard.py`）

`validate_url(url, config, *, resolver=...) -> Result(ok, reason)` 同步纯函数，DNS 经注入式 resolver（unit 零网络），调用点用 `asyncio.to_thread + wait_for` 包络（沿 safety_check.py:207-228 模式）。硬规则（代码常量）：
- scheme ∈ {http,https}（大小写不敏感）；含 userinfo 的 URL 拒绝；超长拒（max_url_length）
- host 归一化：percent-decode + NFKC（全角句点）+ 小写 + 去尾点；拒 `localhost`/`*.localhost`/`*.local`/`*.internal`/`*.home.arpa`/无点短主机名（Docker 容器名）
- 字面 IP：先归一化混淆形态（十进制 2130706433 / 十六进制 0x7f000001 / 八进制 0177.0.0.1 / 混合），再 `ipaddress` 判 is_loopback/is_private/is_link_local/is_reserved/is_multicast/is_unspecified + **显式元数据集** {169.254.169.254, 100.100.100.200, fd00:ec2::254} + CGNAT 100.64.0.0/10（ipaddress.is_private 不含）
- IPv6：[::1]、v4-mapped ::ffff:0:0/96 回查 v4 规则、fc00::/7 ULA、fe80::/10
- DNS：非字面 IP 主机做解析，任一解析结果命中上述类别 → 拒；NXDOMAIN 拒；resolver 异常 fail-open 放行+告警

### 2. Injection Filter（新模块 `apps/search-api/app/agent/safety/injection.py`）

`scan_injection(text, config) -> Scan(hits[rule_id], truncated)`；匹配集 = **内置种子 ∪ Nacos patterns**；归一化匹配（NFKC + 去 zero-width/format 字符 U+200B-200F/U+202A-202E/U+FEFF + casefold）；v1 无 redact、无 span 映射。种子 ~12 条高精度（defaults.py）：结构标记（`<|system|>`/`<|im_start|>`/`[INST]`/原始 `</external_content>`）+ EN 语义（ignore/disregard previous instructions、reveal system prompt、you are now）+ ZH（忽略以上指令/规则、输出系统提示词、你现在扮演）。

**接入点（内容进上下文的最早公共点）**：
| 面 | 位置 | 动作 |
|---|---|---|
| fast 搜索摘要 | `web_search.py:105-132` 写 candidates 处逐条扫 title+snippet+content | flag=保留+留痕；drop=丢弃（来源卡/[n] 自然一致） |
| deep 搜索 | `_build_contract_tools` web_search 闭包（deep_research.py:405-448）`add_candidates` 前 | 同上（drop 的不占 ref_no） |
| deep 读页 | `deep_research.firecrawl_read`（:451-463）`_window_block` 包裹前扫窗口文本 | drop=跳过 save_snapshot+系统注；flag=照常包裹+留痕 |
| 子代理报告→协调器 | `_SubagentGate.run`/`_extract_report`（:678/:738）扫 summary/hard_facts/quotes | drop=报告置 failed:true 占位；flag=保留+留痕 |
| deep URL 入口 | `_read_guidance_or_none`（:358-368）scheme-only→全量校验 + `firecrawl_read` 体二次校验 | 拒=guidance 系统注+record_error（零物理调用、零熔断计数） |
| 未接线工具面 | `url_reader.firecrawl_read`（url_reader.py:142-155）同判据同步（防未来接线漂移） | 同上 |
| 图片收口 | `fuse_rerank._image_item` source_url（现只查主 url） | url_guard 同判据 |
| 快照卫生 | `save_snapshot_tool`（deep_research.py:493-508）模型传参 url | url_guard 同判据 |

告警纪律：`AgentLog.warning` 只记 rule_id/host/字符数，**零 payload 原文、零完整 URL**；state `errors[]` reason ∈ {`url_rejected`, `injection_blocked`}（实现期核对 api.md reason 是否封闭枚举，封闭则当批补钉文字）；**不置 degraded=True**（枚举封闭 timeout|budget|source_failure）。

### 3. 配置面（与 safety.enabled 解耦）

- `apps/search-api/app/agent/safety/config.py` 新增独立 loader `load_content_guard_config(spec) -> ContentGuardConfig`：**safety.enabled=false 只关输入安全链，不打灭 SSRF/注入护栏**；缺子节 → 回落 defaults.py 种子（=部署即启用）；子节畸形 → None+告警（单机制 fail-open）；enabled 非 true（显式 false）→ 关该机制
- `apps/search-api/app/config_center/validator.py`：`UrlGuardSpec`/`InjectionFilterSpec` 嵌套模型挂 `SafetySpec`（缺失=None 合法）；正则抽公共校验函数（compile + `_has_catastrophic_form` ReDoS 硬拒收，复用 grey_rules 现成逻辑）；blocked_hosts ≤200 条/条 ≤253 字符；范围校验 max_url_length 64~8192 / max_scan_chars 1000~20000；on_hit Literal["flag","drop"]；enabled 严格 bool；顺手修 :804-805「三表」docstring 漂移
- `apps/search-api/app/config_center/defaults.py` 种子 = 运行时回落源（单一事实源）；`scripts/nacos_init.py` SPEC_CONFIG["safety"] 逐字同步（parity 测试强制）
- 命名纪律：新键名不得含 key/secret/token/password（validator 密钥嗅探递归扫）；patterns 条目不得以 sk-/tvly-/fc-/as_sk- 开头

## 文件清单

**新增**：`app/agent/safety/url_guard.py`、`app/agent/safety/injection.py`、`test/unit/test_url_guard.py`、`test/unit/test_injection_guard.py`
**修改**：`app/config_center/defaults.py`、`app/config_center/validator.py`、`scripts/nacos_init.py`、`app/agent/safety/config.py`+`__init__.py`、`app/agent/nodes/deep_research.py`、`app/agent/tools/url_reader.py`、`app/agent/nodes/web_search.py`、`app/agent/nodes/fuse_rerank.py`、既有测试 `test_url_reader.py`/`test_deep_research.py`/`test_safety.py`/`test_config_center_validator.py`/`test_prompts.py`/`test_external_content_escape.py`（golden 用例改非种子敌对文本保转义语义 + 新增顺序锁测试：过滤先于转义包裹）
**文档**：`docs/contract/nacos-config.md`（v1.35→v1.36：§0 不新增 dataId 注记/§3 safety 节示例+校验规则+【v1.36 增量】/§6 热更边界〔patterns 热更生效、on_hit flag→drop 单键切〕/§8 init 清单）、`docs/ARCHITECTURE.md`（→v1.65：§4.0/§4.2/§6.4/§6.6/ADR-015 增注〔第三层=内容侧护栏 flag 先行〕/§10 新 R21 误拦漏拦风险/§8 台账 B15 行）、`docs/PRD.md`（→v1.29：REQ-066 + A52 GWT + §5.3.3/§9.2）、`docs/PROGRESS.md`、`AGENTS.md` 版本行
**预期零变更**（巡检记「无需更新」+理由）：api.md / frontend-api.md / data-model.md（无新错误码/SSE 事件/持久化字段；errors[].reason 若封闭枚举例外，当批钉文字）

## 测试计划（TDD 红绿，全量门禁收口）

- **test_url_guard.py ~20 条**：scheme 表；回环形态（127.1/0.0.0.0/[::1]/[::ffff:127.0.0.1]）；私网边界精确断言（172.15 放行/172.16 拒）；元数据+混淆 IPv4 十进制/十六进制/八进制；IPv6 ULA；userinfo host 混淆双向（按解析后 host 判定）；控制字符/畸形/超长；短主机名与禁用后缀；resolver 注入（nip.io→127.0.0.1 拒、公网放行）；NXDOMAIN；reason 枚举稳定 + 零 payload 回显
- **test_injection_guard.py ~16 条**：EN/ZH/结构标记命中；**正当负例**（AI 安全文章、「如何写 system prompt」、否定句——防误拦是安全内核纪律）；大小写/空白/零宽/全角绕过；种子∪Nacos 并集语义（Nacos 削不掉种子）；空表/坏正则跳过+告警一次；异常 fail-open；max_scan_chars 截断标记
- **loader/契约**：load_content_guard_config（缺键回落种子=启用、畸形 None+告警、显式 false 关、与 safety.enabled 解耦锁死）；validator 新键合法/非法/ReDoS/范围；test_prompts.py 种子断言；test_config_center.py parity 自动锁 nacos_init
- **工具路径**：blocked URL 零物理调用+零熔断计数+系统注形态；注入 drop 跳过快照；deep 读工具 guidance/报告扫描 flag/drop；fast 候选过滤 ref_no/来源卡一致（`_wrap_generate` 捕获 ToolMessage）；AST 哨兵扩展（registry.read 直调须过 guard，仿 test_url_reader.py:487-554）
- **integration 1-2 条**：真 deep 图+假 registry 返回 `http://169.254.169.254/` 与含种子 payload 页 → read_calls==[]/visited_urls==[]/轮次正常收束
- **e2e 0 条**（工具面确定性行为，不产生新 SSE 契约）

## 验证与发布

1. 门禁：`make test`（后端 ≥80% 维持现 93%+ 余量）+ `make typecheck` exit 0（仓库根跑）
2. 发布次序**两序皆安全**（新键旧代码 extra=allow 不读；新代码缺键回落种子）：推荐先码后 Nacos——代码上线即启用种子防护
3. Nacos 发布**不用 NACOS_INIT_FORCE**（会整份回滚 spec 其他热更值，B12 教训），走单键热更脚本形态（备份→合并 safety 子键→发布→只读回读逐字节验证，仿 .zcode/tmp/hotswap_fast_synthesis.py 四步带差异护栏）；管理员凭据用户注入
4. 回滚门：`safety.url_guard.enabled=false` / `safety.injection_filter.enabled=false` 单键热更零重启；flag→drop 观察后单键切换
5. 活体冒烟（可选）：deep 轮投喂内网 URL/含 payload 页面，观察拦截日志与 errors[] 留痕

## 非目标与残余风险（如实）

**不做**（记录理由）：guard 模型二分类（P2，待 flag 期误判数据+M4 评测）；nonce 成对标记（转义硬边界已够）；redact 动作；HITL/Trusted 白名单放行制（杀搜索产品力）；deep_writer/reflect 第二道扫描（choke 点已覆盖）；followups/history/regenerate 扫描（上游已过滤；DB 存量旧数据=已接受残余）。
**残余风险**（记 R21）：应用侧校验不覆盖抓取方重定向至私网与 DNS rebinding/TOCTOU——第二道防线为 Crawl4AI 容器出网策略（运维面）；SaaS Firecrawl 侧信任其集群策略。

## 执行路线

动态工作流执行：实施（模块+配置面 → 接入点接线 → 文档+热更脚本三步）→ make test + make typecheck 门禁 → 代码评审 + 安全审计双评审 → P0-P2 修复复核 → 文档巡检独立复核（grep 实证）→ 交付热更脚本（用户执行）→ git 停工作区待用户提交
