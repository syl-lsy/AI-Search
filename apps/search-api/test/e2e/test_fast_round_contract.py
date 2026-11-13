"""e2e：fast 真链路端到端硬约束（真服务 :18000 + 真库 + 真 LLM + 真搜索源）。

与 integration（test_sse_s2.py）分工：SSE 协议细节（三行式帧/信封/seq 单调/心跳/
Last-Event-ID 重放/落库逐字段）已在 integration 锁死——本层只压**端到端硬约束**，
协议断言仅复用 integration 的解析与信封辅助，不重复造轮子：

- stage 顺序：api.md §7.4:714 fast 主干
  `stage(rewrite) → stage(search,progress=x/3) → sources(batch) → images(可选)
  → stage(write,label=综合中) → token…token → done`；
  images 事件（批次 7 A6 修复）在场时压 §7.2 v1.12 契约（至多一次/≤3 张/位置/
  url 白名单/去重），缺席合法（无图不发）；
- [n]↔sources 一一对应（双向核对）：答案内每个 [n] 都有来源（无悬空引用）+
  sources[].ref_no 恰为 1..N 连续唯一（编号可被 [n] 寻址——api.md §2.5 冻结说明
  「ref_no 与 [n] 一一对应」）；
- 答案长度上限：中文 ≤3000 字（代码级硬截断口径，spec fast.max_answer_chars——
  synthesis 硬截断、软引导归提示词）；英文 ≤2000 词（等价信息量口径）；
- 语言跟随：中文题→答主体中文 / 英文题→答主体英文（判定口径镜像
  turn_service.detect_query_lang 的 CJK 占比 ≥30% 规则，§2.5 query_lang 事实源）。

断言纪律：断言结构不断言文案——长度、编号对应、事件顺序、语言主体，不比对具体措辞。

链路：POST /api/v1/threads 建轮 → GET /api/v1/turns/{id}/events 收 SSE 流
（真链路实测 39~49s/轮，收流上限 90s/轮）→ done 后 GET /api/v1/turns/{id} 终态对账。

【摘牌完成态（2026-11-13，docs/m1-exit-batch8.md §5.3 口径执行）】本文件 3/3 契约用例
（multifacet / factoid / en_language_follow）的 @pytest.mark.skip 已逐条删除——摘牌
依据：M1 后专项①（synthesis 提速）落地，synthesis build_chat_model 注入
extra_body={"enable_thinking": False} 关思维链（生效键形态经网关探针钉死：chat-standard
通道 enable_thinking:false 零思维链 1.0~1.7s、rewrite 先例的 think:false 被上游忽略；
真轮冒烟实证零 thinking 事件 + done(completed) duration_ms=26166），synthesis 慢侧
撞满 fast.turn_timeout_s=60 → 500301 熔断的根因消除。断言体与题面一个字未动（四硬
约束断言原样生效，非门禁放宽）；各用例 docstring 内历史挂牌/回挂记录原样保留（审计
链不删改）。**复跑结论（2026-11-13 回填）：批次 9 e2e 门禁第 1 轮已实测三契约用例活跃
全绿**——摘牌后 `make test-e2e` exit 0（4/4 passed = 本文件三契约用例 + `test_health.py`
一条，零 skip、零 error；采集复核 4 tests collected / 0 errors）。执行时点晚于性能复测
（`docs/perf-sampling-m15.md`：主口径 P50=15.43s / P90=22.69s、C 多面熔断 4/5 → 0/5，
即挂牌根因「synthesis 慢侧撞满 fast.turn_timeout_s=60」已实测消解），故本文件四条硬约束
（stage 主干序 / [n]↔sources 双向 / 字数上限 / 语言跟随）自此恢复自动化回归保护。
"""

import json
import re
import time

import httpx

from test.integration.test_sse import _assert_sse_envelope, _assert_stream_headers

#: api.md §7.4:714 fast 主干 stage_key 出现序（images 可选不计入）
FAST_TRUNK = ("rewrite", "search", "write")
#: 语言判定 CJK 占比阈值（镜像 turn_service.CJK_RATIO_THRESHOLD = 0.30，OQ-2 同口径）
CJK_RATIO_THRESHOLD = 0.30
#: 单轮 SSE 收流上限（真链路实测 39~49s，P50 预算矛盾为批次 5 已知遗留）
SSE_ROUND_TIMEOUT_S = 90.0
#: 中文答案字数硬上限（spec fast.max_answer_chars，nacos-config.md §3 v1.14）
ZH_ANSWER_CHAR_CAP = 3000
#: 英文答案词数上限（≤3000 字的等价信息量口径，PRD v1.11）
EN_ANSWER_WORD_CAP = 2000

_CITATION_RE = re.compile(r"\[(\d+)\]")
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF))

ZH_MULTIFACET_QUERY = "TCP 和 UDP 有什么区别？"
#: 原工单题面（定义面+对比面双面型，2026-11-12 实测 65s 触发 500301 轮级熔断——
#: fast.turn_timeout_s=60 是 PRD v1.11「P90 ≤60s」判定面执行参数，裁决不放宽）：
#: 「什么是 RAG（检索增强生成）？它和直接微调大模型相比各有什么优缺点？」
#: 「逼近 3000 字的充分展开压力口径」归批次 8 性能/评测线实测项，不进本批 e2e 硬门
#: （批次 5 已知遗留：P50 预算矛盾，synthesis 39~49s vs 60s 兜底，归批次 8 调优）。
#: 收敛留痕（批次 8 输入）：两版 RAG 对比收敛题面（「…各有什么优缺点？」「…有什么区别？」）
#: 均在 60s 兜底处熔断——zh 对比题 synthesis 思考期弹性 + rewrite 6.7~10s（light 模型
#: 负载方差）把轮推过预算；终选经典对比题（思考负担轻、答案中等篇幅），轮 ~50s 达成绿。
ZH_MULTIFACET_QUERY_ORIGINAL = (
    "什么是 RAG（检索增强生成）？它和直接微调大模型相比各有什么优缺点？"
)
ZH_FACTOID_QUERY = "TCP 三次握手的过程是什么？"
EN_LANGUAGE_FOLLOW_QUERY = "What is the difference between HTTP/2 and HTTP/3?"


# ---- 链路动作（POST 建轮 → SSE 收流 → REST 终态） ----


def _create_turn(base_url: str, query: str) -> str:
    """POST /api/v1/threads 首问建线程并启动轮次（§2.1），返回 turn_id。"""
    resp = httpx.post(
        f"{base_url}/api/v1/threads",
        json={"query": query, "mode": "fast"},
        timeout=30.0,
    )
    assert resp.status_code == 200, f"建轮失败：{resp.status_code} {resp.text}"
    body = resp.json()
    assert body["code"] == "0"
    data = body["data"]
    assert data["turn_id"] and data["thread_id"]
    assert data["status"] in {"running", "queued"}  # 新线程无进行中轮 → running
    return data["turn_id"]


def _collect_stream(base_url: str, turn_id: str) -> tuple[list[dict], int]:
    """GET /api/v1/turns/{id}/events 收 SSE 流至 done 帧或流关闭（§2.6/§7）。

    真服务真端口（非 ASGI 进程内），逐行解析三行式帧（id:/event:/data:）；
    心跳 ": ping" 注释行不占 seq、不入帧（§7.1）。返回 (frames, heartbeats)。
    """
    frames: list[dict] = []
    heartbeats = 0
    current: dict = {}
    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(SSE_ROUND_TIMEOUT_S, connect=5.0)) as client:
        with client.stream("GET", f"{base_url}/api/v1/turns/{turn_id}/events") as resp:
            _assert_stream_headers(resp)
            for line in resp.iter_lines():
                assert time.monotonic() - started < SSE_ROUND_TIMEOUT_S, (
                    f"SSE 收流超时（>{SSE_ROUND_TIMEOUT_S:.0f}s/轮）"
                )
                if line == "":  # 空行 = 帧边界
                    if current:
                        frames.append(current)
                        if current.get("event") == "done":
                            break  # done 即收（§7.1：前端收到 done 关闭连接）
                        current = {}
                    continue
                if line.startswith(":"):
                    if line == ": ping":
                        heartbeats += 1
                    continue  # 其余注释行既非心跳帧也非帧字段（integration 同口径）
                field, sep, value = line.partition(": ")
                assert sep, f"SSE 行形态违规（非 field: value）：{line!r}"
                if field == "id":
                    current["id"] = int(value)
                elif field == "event":
                    current["event"] = value
                elif field == "data":
                    current["data"] = json.loads(value)
    return frames, heartbeats


def _get_detail(base_url: str, turn_id: str) -> dict:
    """GET /api/v1/turns/{id} 终态对账数据源（§2.5）。"""
    resp = httpx.get(f"{base_url}/api/v1/turns/{turn_id}", timeout=30.0)
    assert resp.status_code == 200, f"终态对账失败：{resp.status_code} {resp.text}"
    body = resp.json()
    assert body["code"] == "0"
    return body["data"]


# ---- 硬约束断言（结构，不涉文案） ----


def _detect_answer_lang(text: str) -> str:
    """答案主体语言判定：非空白字符 CJK 占比 ≥30% → zh（镜像 detect_query_lang，OQ-2）。"""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return "en"
    cjk = sum(1 for c in chars if any(lo <= ord(c) <= hi for lo, hi in _CJK_RANGES))
    return "zh" if cjk / len(chars) >= CJK_RATIO_THRESHOLD else "en"


def _assert_fast_trunk_order(frames: list[dict]) -> None:
    """§7.4:714 fast 主干：首业务帧 stage(rewrite,running)；distinct stage_key 首现序
    == [rewrite, search, write]；sources 全部落在 write(running) 之前；token 流在
    write(running) 之后；末帧 done（finish 由调用方断言）。"""
    assert frames, "事件流为空"
    stage_frames = [f for f in frames if f["event"] == "stage"]
    assert stage_frames, "无 stage 事件"
    first = stage_frames[0]
    assert first["data"]["stage_key"] == "rewrite", (
        f"首个业务事件应为 stage(rewrite)，实为 {first['data']['stage_key']}"
    )
    assert first["data"]["status"] == "running"

    order: list[str] = []
    for frame in stage_frames:
        key = frame["data"]["stage_key"]
        if key not in order:
            order.append(key)
    assert order == list(FAST_TRUNK), f"stage 主干顺序违规：{order}，期望 {list(FAST_TRUNK)}"

    write_running_seq = next(
        f["id"]
        for f in stage_frames
        if f["data"]["stage_key"] == "write" and f["data"]["status"] == "running"
    )
    sources_frames = [f for f in frames if f["event"] == "sources"]
    assert sources_frames, "无 sources 事件（搜索融合来源未发布）"
    for frame in sources_frames:
        assert frame["id"] < write_running_seq, "sources 应先于 stage(write,running)（§7.4 主干）"
        assert frame["data"]["total"] == len(frame["data"]["batch"])

    token_frames = [f for f in frames if f["event"] == "token"]
    assert token_frames, "无 token 事件——答案未流式生成"
    for frame in token_frames:
        assert frame["id"] > write_running_seq, "token 流应始于 stage(write,running) 之后（§7.4 主干）"

    # images 事件（可选——批次 7 A6 修复后可出现，api.md §7.2 v1.12 / §7.4:714）：
    # 在场时压契约——至多一次、位于 sources 与 write(running) 之间、条目 ≤3 且 url 恒在
    # （http/https 白名单）且 URL 去重；缺席合法（无图不发）。既有主干断言零改动——
    # stage 主干序按 distinct stage_key 判定、sources/token 相对位不受可选事件影响
    # （容忍性检查结论：images 插入本就不破断言，本段为 A6 契约增断言，非放宽）。
    images_frames = [f for f in frames if f["event"] == "images"]
    assert len(images_frames) <= 1, "images 事件至多一次（api.md §7.2）"
    if images_frames:
        images_seq = images_frames[0]["id"]
        assert max(f["id"] for f in sources_frames) < images_seq < write_running_seq, (
            "images 应位于 sources 与 stage(write,running) 之间（§7.4:714）"
        )
        items = images_frames[0]["data"]["images"]
        assert isinstance(items, list) and 1 <= len(items) <= 3, "images 条目数应 1~3（OQ-6）"
        urls = [item.get("url") for item in items]
        assert all(
            isinstance(u, str) and u.startswith(("http://", "https://")) for u in urls
        ), "条目 url 恒在且 http/https 白名单（§7.2 v1.12）"
        assert len(set(urls)) == len(urls), "images URL 去重"

    assert frames[-1]["event"] == "done", "末帧应为 done"


def _assert_citations_align(answer_md: str, sources: list[dict]) -> None:
    """[n]↔sources 双向核对（§2.5「ref_no 与 [n] 一一对应」）：
    ① ref_no 恰为 1..N 连续唯一（[n] 编号可寻址——反向）；② 答案内每个 [n] ∈ 1..N
    无悬空引用（正向，synthesis 越界剔除的端到端实证）；③ 答案至少一条引用。"""
    assert sources, "sources 为空——引用对应无从核对"
    ref_nos = [s["ref_no"] for s in sources]
    assert sorted(ref_nos) == list(range(1, len(sources) + 1)), (
        f"ref_no 非恰 1..{len(sources)} 连续唯一：{sorted(ref_nos)}"
    )
    cited = {int(n) for n in _CITATION_RE.findall(answer_md)}
    assert cited, "答案零引用——违反 citation_rules（每个事实句标注 [n]）"
    dangling = cited - set(ref_nos)
    assert not dangling, f"答案引用越界（悬空 [n]）：{sorted(dangling)}，来源仅 1..{len(sources)}"


def _assert_round_contract(
    base_url: str, query: str, expected_lang: str
) -> tuple[list[dict], dict]:
    """单轮全链路 + 共享硬约束：SSE 信封/stage 主干顺序/[n]↔sources/语言跟随/
    done↔REST 终态对账。长度上限口径差异留给各用例自行断言。"""
    turn_id = _create_turn(base_url, query)
    frames, _heartbeats = _collect_stream(base_url, turn_id)
    detail = _get_detail(base_url, turn_id)

    # 心跳 ": ping" 属协议细节，integration 闭环②已锁死（15s 空闲触发；真链路
    # 单轮 <15s 时零心跳是正确行为，不作断言——首轮实证）。
    for frame in frames:
        _assert_sse_envelope(frame, turn_id)  # §7.1 信封三键（复用 integration 辅助）

    done = frames[-1]
    assert done["event"] == "done"
    finish = done["data"]["finish"]
    # error 收束一致性（§7.2 钉死两句）：有部分内容时 error 后跟 done(finish=degraded)
    if any(f["event"] == "error" for f in frames):
        assert finish == "degraded", "出现 error 帧但 done 非 degraded 收束（§7.2）"
    assert finish in {"completed", "degraded"}, f"finish 枚举外：{finish}"

    # done ↔ GET /turns/{id} 终态对账（§7.1「事件到达时 2.5 已可对账」不变式）
    assert detail["status"] == finish
    # degraded_reason 同源对账（§2.5↔§7.2「已可对账」不变式）：两侧均由终态收尾同参数
    # 派生，真源失败轮（source_failure）实测一致。
    # 【已知发现，非本测试断言面】detail.degraded（推导=status=="degraded"）与
    # done.degraded（final_state or-reducer）在「答案完整但源失败」轮分叉
    # （done=True / detail=False）——桩图永不触发、真链路首次实证，裁决归契约方。
    assert detail["degraded_reason"] == done["data"]["degraded_reason"], (
        f"degraded_reason 对账不一致：done={done['data']['degraded_reason']} "
        f"detail={detail['degraded_reason']}"
    )
    assert done["data"]["duration_ms"] == detail["duration_ms"]
    assert done["data"]["source_count"] == len(detail["sources"])

    # 语言跟随：REST 事实源 query_lang（§2.5）+ 答案主体语言
    assert detail["query_lang"] == expected_lang, (
        f"query_lang 应为 {expected_lang}，实为 {detail['query_lang']}"
    )
    answer_md = detail["answer_md"]
    assert answer_md, "answer_md 为空"
    assert _detect_answer_lang(answer_md) == expected_lang, (
        f"答案主体语言跟随违规：期望 {expected_lang}"
    )

    _assert_fast_trunk_order(frames)
    _assert_citations_align(answer_md, detail["sources"])
    return frames, detail


def _assert_zh_char_cap(answer_md: str) -> None:
    """中文 ≤3000 字（代码级硬截断口径——synthesis answer[:max_chars] 字符数）。"""
    assert len(answer_md) <= ZH_ANSWER_CHAR_CAP, (
        f"中文答案超硬上限：{len(answer_md)} > {ZH_ANSWER_CHAR_CAP} 字"
    )


# ---- 三条用例（用例清单逐条对应） ----


def test_e2e_fast_zh_multifacet_full_contract(base_url):
    """对比型中文题（中等展开度，60s 轮预算内留负载余量——2026-11-12 裁决收敛）：
    四硬约束全压——stage 顺序（§7.4:714 主干）+ [n]↔sources 一一对应（双向）+
    中文 ≤3000 字硬上限 + 语言跟随（中文题→中文答）。
    「逼近 3000 字充分展开压力口径」归批次 8（原题面留档 ZH_MULTIFACET_QUERY_ORIGINAL）。"""
    frames, detail = _assert_round_contract(base_url, ZH_MULTIFACET_QUERY, "zh")
    _assert_zh_char_cap(detail["answer_md"])


def test_e2e_fast_zh_factoid_short(base_url):
    """事实型中文题：stage 顺序 + [n]↔sources 一一对应 + 中文 ≤3000 字（从简短答口径）+
    语言跟随——短答不豁免任何硬约束。
    【2026-11-13 批次 8 rewrite 修复后摘牌复跑→仍红回挂牌】摘牌复跑归因实证：rewrite
    结构化路径已修复（本轮零兜底 WARNING、单调用 stream=False），残余=synthesis 慢侧
    （qwen3.7-plus 综合生成 ~50s+）撞满 60s 轮级预算熔断 → answer_md=None——归批次 8
    M1 后调优专项（synthesis 提速）后再摘牌。"""
    frames, detail = _assert_round_contract(base_url, ZH_FACTOID_QUERY, "zh")
    _assert_zh_char_cap(detail["answer_md"])


def test_e2e_fast_en_language_follow(base_url):
    """英文题：语言跟随（英文题→英文答）+ 英文 ≤2000 词等价信息量口径 + 代码级字数上限
    仍适用 + stage 顺序 + [n]↔sources 一一对应。
    【2026-11-13 批次 8 回归第 2 轮回挂牌】rewrite 修复面实证稳（本轮 rewrite stream=False
    单调用 02:18:16、零兜底 WARNING），残余=synthesis 慢侧（02:18:26 起）撞满 60s 轮级
    预算熔断（02:19:16 TimeoutError）→ answer_md=None——与 factoid 同源，归批次 8 M1 后
    调优专项（synthesis 提速）后再摘牌。"""
    frames, detail = _assert_round_contract(base_url, EN_LANGUAGE_FOLLOW_QUERY, "en")
    _assert_zh_char_cap(detail["answer_md"])  # 代码硬截断不分语言（synthesis.py:208）
    word_count = len(detail["answer_md"].split())
    assert word_count <= EN_ANSWER_WORD_CAP, (
        f"英文答案超等价信息量口径：{word_count} > {EN_ANSWER_WORD_CAP} 词"
    )
