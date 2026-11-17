"""e2e：fast 轮 followups 事件真链路（api.md §7.2 / §2.5 v1.16，REQ-046/A29）。

**为什么单独起一个服务实例（L4 内置默认路径，端口 18001）**
共享 e2e 服务（:18000）的 Agent Spec 来自**运行态 Nacos L1**——该环境的
`ai_search.agent.spec.json` 仍是 v1.22 之前的存量内容（`followups.enabled=false`，
nacos-config.md v1.22 §8 已写明「存量已部署环境的 Nacos spec.enabled 不会自动翻转，
需一次性运维动作」），故在 :18000 上 followups 钩子按契约不触发（关断态零行为差异，
不是缺陷）。真正的「默认开启」形态 = **L4 代码内置默认**（nacos-config.md §3 v1.22 /
§7 降级阶梯；config_center/defaults.py 与 scripts/nacos_init.py 双侧 enabled=true，
由 test_config_center.TestNacosInitScriptParity + test_followups 双源锁测试兜住），
后端开发手工验证过的也正是这条服务器路径。

因此本文件以**显式环境覆盖**复现该路径并固化为用例（不伪造节点、不 mock 图外钩子）：
`NACOS_SERVER_ADDR=127.0.0.1:1`（不可达 → L1 拉取必然失败）+ `NACOS_STARTUP_TIMEOUT_S=1`
+ `NACOS_SNAPSHOT_DIR=<tmp>`（隔离磁盘快照 L3，避免误读仓库 `data/nacos_snapshot` 里
的存量 spec）+ `APP_ENV=dev`（L4 允许降级；production 会 503001 拒启动）。服务仍走
真 uvicorn + 真库（复用 session 级 conftest 已重建并 upgrade head 的测试库）+ 真 LLM
（L4 model.mapping.light=qwen3.5-4b-ollama → LiteLLM → 本地 Ollama）+ 真 Nacos 提示词
（L4 prompts，v1.20 正文）。

**压的契约面（结构断言，不比对文案）**
- 事件时序：`followups` 必然晚于 `done`（seq 更大）、至多一次、是本轮最后一个事件；
- 触发条件：`mode=fast` ∧ `status=completed` ∧ answer 非空 ∧ `enabled=true`（本环境恒
  满足），且出现 error 帧即违规；
- payload：`items` 为 1~5 条非空字符串、单条 ≤200 字符、无重复（§7.2 items 校验口径）；
- 语言跟随：items 整体中文（镜像 detect_query_lang 两条款——CJK 占比 ≥30% 或
  ≥2 连续 CJK，PRD v1.24 同源口径，OQ-2）；
- **三方一致**：SSE `followups.items` == §2.5 `followups`（REST 事实源）==
  `turns.followups` 列（先落库再发布不变式——事件到达时 2.5 已可对账）；
- **不入 turn_events**（ADR-003 第 4 条落库子集不变）：`turn_events` 无
  `event_type='followups'` 行。

**skip 守卫（同 test_fast_round_contract.py 纪律）**：只在「环境构造不出该路径」时挂牌——
第二服务起不来 / 轮未达 completed（LLM 或检索侧不可用，触发条件不成立）。**轮 completed
却无 followups 事件不挂牌**：本环境 enabled=true 是确定性前提，静默无事件即钩子回归
（静默放弃语义只对生成失败合法，不该由测试侧兜成 skip）。

题面取 v1.20 direct 判例原文（零检索轮）：followups 对 direct 轮同样适用（§7.2 v1.14
注——`{{sources_block}}` 空串合法），且免搜索源外部性，把「环境不满足」的概率压到最低。
"""

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg2
import pytest

from test.integration.test_sse import _assert_sse_envelope, _assert_stream_headers

ROOT = Path(__file__).resolve().parents[4]

#: 第二服务端口（:18000 为共享 e2e 服务；不冲突即可）
L4_PORT = 18001
L4_BASE_URL = f"http://127.0.0.1:{L4_PORT}"

#: 收流上限：单轮预算 120s（fast.turn_timeout_s，nacos-config §3 v1.25）+ 生成 8s
#: （generate_timeout_ms）+ 余量
STREAM_TIMEOUT_S = 150.0

#: items 契约上限（api.md §7.2：1~5 条、单条非空且 ≤200 字符）
ITEMS_MAX_COUNT = 5
ITEM_MAX_CHARS = 200

#: 语言判定 CJK 占比阈值（镜像 turn_service.CJK_RATIO_THRESHOLD = 0.30，OQ-2 同口径）
CJK_RATIO_THRESHOLD = 0.30
#: ≥2 连续 CJK 字符判 zh 的最短词组长度（镜像 turn_service.CJK_RUN_MIN = 2，PRD v1.24 方案A）
CJK_RUN_MIN = 2
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF))

#: 题面：nacos-config.md §1 v1.20 direct 判例原文（闲聊/能力询问 → 零检索直答）
FOLLOWUPS_QUERY = "你好，能做什么？"


# ---- 第二服务生命周期（L4 内置默认路径） ----


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop(process: subprocess.Popen) -> None:
    """整组收尾（同 e2e/conftest.py 口径：SIGTERM → 收尸 → 组内残留 SIGKILL）。"""
    pgid = process.pid
    _signal_group(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if _group_alive(pgid):
        _signal_group(pgid, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@pytest.fixture(scope="module")
def l4_base_url(tmp_path_factory) -> Iterator[str]:
    """起 L4 内置默认路径服务（:18001）：Nacos 不可达 + 快照隔离 → Agent Spec 落 L4。

    环境变量的优先级说明：pydantic-settings 里 OS 环境变量高于 `.env` 文件，故显式覆盖
    生效（真服务子进程继承本 env dict，不再走 conftest 的 .env.test Nacos 值）。
    """
    snapshot_dir = tmp_path_factory.mktemp("nacos-l4-snapshot")
    env = dict(os.environ)
    env["NACOS_SERVER_ADDR"] = "127.0.0.1:1"  # 必然连接失败 → L1 拉取失败
    env["NACOS_STARTUP_TIMEOUT_S"] = "1"  # 阶梯短预算（不拖慢测试）
    env["NACOS_SNAPSHOT_DIR"] = str(snapshot_dir)  # L3 隔离，防读入存量 spec
    env["APP_ENV"] = "dev"  # L4 仅 dev 允许（production 503001 拒启动）
    env["LOG_FILE_DIR"] = "tmp/logs"

    process = subprocess.Popen(
        [
            "uv", "run", "--package", "search-api",
            "fastapi", "dev", "apps/search-api/app/main.py",
            "--port", str(L4_PORT),
            "--no-reload",
        ],
        cwd=ROOT,
        env=env,
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{L4_BASE_URL}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if process.poll() is not None:  # 进程已退出：环境构造失败，直接挂牌
            _stop(process)
            pytest.skip(
                "L4 内置默认路径服务启动失败（退出码 "
                f"{process.returncode}）——环境构造不出该路径，followups 事件 e2e 挂牌"
            )
        time.sleep(0.5)
    else:
        _stop(process)
        pytest.skip("L4 内置默认路径服务 /health 未在 60s 内就绪——环境性，本文件挂牌")

    try:
        yield L4_BASE_URL
    finally:
        _stop(process)


# ---- 链路辅助 ----


def _create_turn(base_url: str, query: str) -> str:
    """POST /api/v1/threads 建线程并启动 fast 轮（§2.1）。"""
    resp = httpx.post(
        f"{base_url}/api/v1/threads",
        json={"query": query, "mode": "fast"},
        timeout=30.0,
    )
    assert resp.status_code == 200, f"建轮失败：{resp.status_code} {resp.text}"
    data = resp.json()["data"]
    assert data["turn_id"] and data["thread_id"]
    return data["turn_id"]


def _collect_until_close(base_url: str, turn_id: str) -> list[dict]:
    """收 SSE 流至**流关闭**（不只到 done——followups 在 done 之后、保活窗口内下发）。

    与 test_fast_round_contract._collect_stream 的差异：那里 done 即断（常规契约面），
    本文件必须继续读到服务端收口（followups 到达后关闭 / 窗口超时静默关闭），否则
    事件永远看不到。帧解析口径（三行式 + 心跳注释行）与 integration 一致。
    """
    frames: list[dict] = []
    current: dict = {}
    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(STREAM_TIMEOUT_S, connect=5.0)) as client:
        with client.stream("GET", f"{base_url}/api/v1/turns/{turn_id}/events") as resp:
            _assert_stream_headers(resp)
            for line in resp.iter_lines():
                assert time.monotonic() - started < STREAM_TIMEOUT_S, (
                    f"SSE 收流超时（>{STREAM_TIMEOUT_S:.0f}s）"
                )
                if line == "":
                    if current:
                        frames.append(current)
                        current = {}
                    continue
                if line.startswith(":"):
                    continue  # 心跳 ": ping" 与注释行不占 seq（§7.1）
                field, sep, value = line.partition(": ")
                assert sep, f"SSE 行形态违规（非 field: value）：{line!r}"
                if field == "id":
                    current["id"] = int(value)
                elif field == "event":
                    current["event"] = value
                elif field == "data":
                    current["data"] = json.loads(value)
    return frames


def _get_detail(base_url: str, turn_id: str) -> dict:
    resp = httpx.get(f"{base_url}/api/v1/turns/{turn_id}", timeout=30.0)
    assert resp.status_code == 200, f"终态对账失败：{resp.status_code} {resp.text}"
    body = resp.json()
    assert body["code"] == "0"
    return body["data"]


def _db_followups(turn_id: str) -> tuple[object, bool]:
    """管理面直读 turns.followups 与 turn_events 是否落 followups 行（ADR-003 不变式）。"""
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
        connect_timeout=5,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT followups FROM turns WHERE id = %s", (turn_id,))
            row = cursor.fetchone()
            assert row is not None, "轮行不存在——建轮链路未落库"
            cursor.execute(
                "SELECT COUNT(*) FROM turn_events WHERE turn_id = %s "
                "AND event_type = 'followups'",
                (turn_id,),
            )
            followups_event_rows = cursor.fetchone()[0]
            return row[0], followups_event_rows > 0
    finally:
        conn.close()


def _detect_answer_lang(text: str) -> str:
    """语言判定（镜像 detect_query_lang 两条款，PRD v1.24 口径同源——OQ-2）：
    zh ⇔ 非空白字符 CJK 占比 ≥30%（分母=非空白字符）**或**原始串存在 ≥2 连续 CJK
    字符（连续按原始串相邻计，空白/标点打断）；空串/零有效字符回落 en。
    独立实现、不 import 生产函数（e2e 侧自足性——与 test_fast_round_contract 同款镜像）。"""
    chars = [c for c in text if not c.isspace()]
    cjk = sum(1 for c in chars if any(lo <= ord(c) <= hi for lo, hi in _CJK_RANGES))
    if chars and cjk / len(chars) >= CJK_RATIO_THRESHOLD:
        return "zh"
    run = 0
    for ch in text:  # 原始串扫描：空白/标点打断连续（与生产口径一致）
        if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES):
            run += 1
            if run >= CJK_RUN_MIN:
                return "zh"
        else:
            run = 0
    return "en"


def _collect_replay(base_url: str, turn_id: str, last_seq: int) -> list[dict]:
    """带 Last-Event-ID 重连收帧（§7.1/§7.3：重放 + 终态收口；可能零帧）。"""
    frames: list[dict] = []
    current: dict = {}
    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(STREAM_TIMEOUT_S, connect=5.0)) as client:
        with client.stream(
            "GET",
            f"{base_url}/api/v1/turns/{turn_id}/events",
            headers={"Last-Event-ID": str(last_seq)},
        ) as resp:
            _assert_stream_headers(resp)
            for line in resp.iter_lines():
                assert time.monotonic() - started < STREAM_TIMEOUT_S, (
                    f"SSE 重放收流超时（>{STREAM_TIMEOUT_S:.0f}s）"
                )
                if line == "":
                    if current:
                        frames.append(current)
                        current = {}
                    continue
                if line.startswith(":"):
                    continue
                field, sep, value = line.partition(": ")
                assert sep, f"SSE 行形态违规（非 field: value）：{line!r}"
                if field == "id":
                    current["id"] = int(value)
                elif field == "event":
                    current["event"] = value
                elif field == "data":
                    current["data"] = json.loads(value)
    return frames


def _run_followups_round(base_url: str) -> tuple[str, list[dict], dict, dict, dict]:
    """跑一轮并返回 (turn_id, frames, detail, done 帧, followups 帧)；环境性失败挂牌。

    「环境构造不出该路径」的两种情形才 skip：无 done 帧（轮 failed，终态经 2.5 对账）/
    finish 非 completed（熔断降级，触发条件不成立）。轮 completed 但零 followups 事件
    **不挂牌**（本环境 enabled=true 为确定性前提，静默即钩子回归）。
    """
    turn_id = _create_turn(base_url, FOLLOWUPS_QUERY)
    frames = _collect_until_close(base_url, turn_id)
    detail = _get_detail(base_url, turn_id)

    assert frames, "事件流为空——建轮/收流链路未跑通"
    for frame in frames:
        _assert_sse_envelope(frame, turn_id)

    done_frames = [f for f in frames if f["event"] == "done"]
    if not done_frames:
        pytest.skip(
            "本轮无 done 帧（流提前关闭，failed 终态经 2.5 对账）——"
            f"环境性（LLM/检索不可用），detail.status={detail['status']}"
        )
    done = done_frames[0]
    if done["data"]["finish"] != "completed":
        pytest.skip(
            f"轮未 completed（finish={done['data']['finish']}）——"
            "followups 触发条件不成立，环境性挂牌"
        )

    followers = [f for f in frames if f["event"] == "followups"]
    assert len(followers) == 1, (
        "followups 事件应恰一次（§7.2「至多一次」）；实测 "
        f"{len(followers)} 次——零次 = 钩子回归（本环境 enabled=true 为确定性前提），"
        "多次 = 事件发布/重放面缺陷"
    )
    return turn_id, frames, detail, done, followers[0]


# ---- 用例 ----


def test_e2e_fast_followups_event_contract(l4_base_url):
    """fast 轮 followups 真链路：done 后至多一次事件 + items 结构/语言 + 三方一致。

    「三方一致」的三方 = SSE 事件 items / §2.5 `followups`（REST 事实源）/
    `turns.followups` 列；同时压「不入 turn_events」不变式（ADR-003 第 4 条）。
    """
    turn_id, frames, detail, done, followups_frame = _run_followups_round(l4_base_url)

    # 触发条件面：completed 轮不应出现 error 帧（§7.2 收束两句）
    assert not [f for f in frames if f["event"] == "error"], (
        "completed 轮出现 error 帧——收束语义违规（§7.2）"
    )

    # 时序：必然晚于 done，且是本轮最后一个事件（随后服务端收口关闭）
    assert followups_frame["id"] > done["id"], (
        f"followups.seq 应大于 done.seq：{followups_frame['id']} <= {done['id']}"
    )
    assert frames[-1]["event"] == "followups", (
        f"followups 后不应再有事件，末帧实为 {frames[-1]['event']}"
    )

    # payload 结构（§7.2：信封三键 + items）
    payload = followups_frame["data"]
    assert set(payload) == {"seq", "turn_id", "ts", "items"}, (
        f"followups payload 应为信封三键 + items，实测 {sorted(payload)}"
    )
    items = payload["items"]
    assert isinstance(items, list)
    assert 1 <= len(items) <= ITEMS_MAX_COUNT, (
        f"items 长度应 1~{ITEMS_MAX_COUNT}，实测 {len(items)}"
    )
    for item in items:
        assert isinstance(item, str), f"items 元素应为字符串：{item!r}"
        assert item.strip() == item and item, "items 元素应非空且无首尾空白（§7.2）"
        assert len(item) <= ITEM_MAX_CHARS, (
            f"单条应 ≤{ITEM_MAX_CHARS} 字符，实测 {len(item)}"
        )
    assert len(set(items)) == len(items), "items 不应有重复条目"

    # 语言跟随（答案/追问语言 = query_lang；§7.2 + nacos fast.followups ④；
    # 判定口径 = detect_query_lang 两条款镜像，PRD v1.24 同源）
    assert detail["query_lang"] == "zh"
    joined = "\n".join(items)
    assert _detect_answer_lang(joined) == "zh", (
        f"items 整体应为中文（占比 ≥{CJK_RATIO_THRESHOLD} 或 ≥{CJK_RUN_MIN} 连续 CJK），"
        f"实测判定 en：{items!r}"
    )

    # 三方一致：事件 == 2.5 == turns 列（先落库再发布不变式）
    assert detail["followups"] == items, (
        f"§2.5 followups 与事件 items 不一致：REST={detail['followups']} 事件={items}"
    )
    db_followups, has_followups_event_row = _db_followups(turn_id)
    assert db_followups == items, (
        f"turns.followups 与事件 items 不一致：DB={db_followups} 事件={items}"
    )
    assert not has_followups_event_row, (
        "followups 不应落 turn_events（ADR-003 第 4 条落库子集不变）"
    )

    # 实验要件「不计入 P50 / duration_ms」：duration_ms 在 done 已定稿，事件后不重算
    assert detail["duration_ms"] == done["data"]["duration_ms"], (
        "followups 生成晚于 done，不得改写 duration_ms（不计入 P50 语义）"
    )


def test_e2e_followups_replay_after_done(l4_base_url):
    """§7.2/§7.3 恢复边界②：followups 已发布后，持 Last-Event-ID=done.seq 重连 →
    只补下发 followups 后收口；游标已覆盖全部事件（≥ followups.seq）→ 零帧关闭。

    压的是**真 EventBus 内存重放缓冲 + 真端点泵**的组合面（unit 层的
    test_followups_sse.py 用假总线锁协议分支，本用例证明真服务上分支可达）。
    """
    turn_id, _frames, detail, done, followups_frame = _run_followups_round(l4_base_url)
    items = followups_frame["data"]["items"]

    replay = _collect_replay(l4_base_url, turn_id, last_seq=done["id"])
    assert [f["event"] for f in replay] == ["followups"], (
        "Last-Event-ID=done.seq 的重放应恰补下发 followups（§7.1「跟随」语义）；"
        f"实测 {[f['event'] for f in replay]}"
    )
    assert replay[0]["id"] == followups_frame["id"]
    assert replay[0]["data"]["items"] == items, "重放内容应与首发逐项一致"
    _assert_sse_envelope(replay[0], turn_id)
    assert detail["followups"] == items

    # 游标已覆盖 followups：无未取事件 → 零帧关闭（不空挂保活窗口）
    covered = _collect_replay(l4_base_url, turn_id, last_seq=followups_frame["id"])
    assert covered == [], (
        "Last-Event-ID ≥ followups.seq 时应零帧关闭（游标已覆盖全部事件），"
        f"实测 {[f['event'] for f in covered]}"
    )
