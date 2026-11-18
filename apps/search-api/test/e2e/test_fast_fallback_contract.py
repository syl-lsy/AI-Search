"""e2e：fast 轮 synthesis 终局失败 → **兜底产出收束**真链路（A51 / api.md §7.4 v1.25）。

A51 第一条 GWT 的端到端面（真实失败诱导，非打桩）：检索完成（sources ≥1）后
synthesis 不可达 → 轮次必须以「零 error + digest 兜底 + done(degraded)」收束。
确定性实现面（digest 装配/句界清理/500101 收窄矩阵/事件序）已由 unit + integration
锁死；本文件只压**真服务 + 真库 + 真搜索源**下的收束形态与 SSE 序。

失败诱导方式（测试环境等价手段——不触 Nacos 运行值、不动生产代码）：
**起第二个真服务实例（:18001），仅 `LITELLM_BASE_URL` 指向本机不可达端口**
（`http://127.0.0.1:9`，ECONNREFUSED 即时失败）。语义等位于「chat 映射指不可达地址」：
- chat 强模型（synthesis）与 light（rewrite）同走该网关 → 全断；
- rewrite 走**机制兜底**（传输失败 → 原 query 单发，ARCH §6.2 非枚举机制兜底）；
- 搜索源（MCP）与 DB 保持真实 → fused 非空成立 → synthesis 终局失败走 digest 兜底；
- 环境变量注入经子进程 env（`BaseSettingsWithEnv` env > .env 优先级）——不影响首实例。

skip 挂牌纪律（沿批次 6/8 同款）：
- 端口 18001 被占用 / 第二实例 60s 未就绪 → skip（环境不具备，不硬红）；
- 本轮零 sources（外网搜索源全断 → 500201 形态）→ skip（兜底形态需 fused 非空）；
- 其余偏差（事件序/字段/收束语义）一律红——它们是契约回归，不是环境问题。
"""

import os
import re
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from test.e2e.test_fast_round_contract import (
    _collect_stream,
    _create_turn,
    _get_detail,
    _rewrite_strategy,
)
from test.integration.test_sse import _assert_sse_envelope

#: 兜底用例专用实例端口（首实例 18000 在 conftest）
FALLBACK_PORT = 18001
#: 不可达网关地址（本机 discard 端口，连接被即刻拒绝——确定性快速失败，不耗包络）
UNREACHABLE_GATEWAY = "http://127.0.0.1:9"
#: conftest 位于 apps/<svc>/test/e2e/ 下，parents[4] 即仓库根（服务子进程 cwd）
ROOT = Path(__file__).resolve().parents[4]

#: 中文事实型题（检索面稳定；不受 direct/recall 策略影响的常规 web 轮）
FALLBACK_QUERY = "TCP 三次握手的过程是什么？"


def _port_in_use(port: int) -> bool:
    """端口占用探测（connect_ex=0 即有人监听）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _group_alive(pgid: int) -> bool:
    """进程组仍有存活成员（信号 0 存在性检查；macOS 僵尸组 EPERM 视为已无存活）。"""
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop_service(process: subprocess.Popen) -> None:
    """整组终止第二个服务实例（SIGTERM→组内残留 SIGKILL；conftest 同款口径）。"""
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@pytest.fixture(scope="module")
def fallback_base_url() -> Iterator[str]:
    """第二个真服务实例（:18001，网关指向不可达端口）；环境不具备 → skip 挂牌。"""
    if _port_in_use(FALLBACK_PORT):
        pytest.skip(f"端口 {FALLBACK_PORT} 已被占用——兜底 e2e 挂牌（环境不具备）")

    env = {**os.environ, "LITELLM_BASE_URL": UNREACHABLE_GATEWAY}
    env.setdefault("LOG_FILE_DIR", "tmp/logs")  # conftest 口径：日志不落仓库根
    process = subprocess.Popen(
        [
            "uv", "run", "--package", "search-api",
            "fastapi", "dev", "apps/search-api/app/main.py",
            "--port", str(FALLBACK_PORT),
            "--no-reload",
        ],
        cwd=ROOT,
        env=env,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{FALLBACK_PORT}"
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.skip(
                "兜底用例专用实例 60s 未就绪（:18001，网关指向不可达端口）——挂牌"
            )
        yield base_url
    finally:
        _stop_service(process)


def test_e2e_fast_synthesis_digest_fallback(fallback_base_url: str) -> None:
    """A51 第一条 GWT：sources 已发布 + synthesis 终局失败 → 零 error；SSE 序
    `sources → stage(write,running) → token(digest 整块) → stage(write,done,extra.fallback)
    → done(degraded, degraded_reason=null)`；2.5 对账 answer_md=兜底全文、status=degraded。"""
    turn_id = _create_turn(fallback_base_url, FALLBACK_QUERY)
    frames, _heartbeats = _collect_stream(fallback_base_url, turn_id)
    detail = _get_detail(fallback_base_url, turn_id)

    for frame in frames:
        _assert_sse_envelope(frame, turn_id)

    # 环境性挂牌：外网搜索源全断 → 零 sources（500201 形态，不属兜底适用面）
    if not detail["sources"]:
        pytest.skip(
            "本轮零 sources（外网搜索源不可达）——兜底形态需 fused 非空，环境性挂牌"
        )

    # ---- 收束语义：零 error + done(degraded, reason=null) ----
    assert not [f for f in frames if f["event"] == "error"], (
        f"兜底轮不得产生 error 事件（api.md §7.4 v1.25）："
        f"{[f['data'] for f in frames if f['event'] == 'error']}"
    )
    done = frames[-1]
    assert done["event"] == "done"
    assert done["data"]["finish"] == "degraded"
    assert done["data"]["degraded"] is True
    assert done["data"]["degraded_reason"] is None  # 枚举零扩容（不误标 source_failure）
    assert done["data"]["source_count"] == len(detail["sources"])
    assert done["data"]["source_count"] > 0

    # ---- SSE 序（§7.4 兜底收束形态）：sources → write(running) → token(digest) → write(done) → done ----
    stage_frames = [f for f in frames if f["event"] == "stage"]
    write_running = [
        f for f in stage_frames
        if f["data"]["stage_key"] == "write" and f["data"]["status"] == "running"
    ]
    write_done = [
        f for f in stage_frames
        if f["data"]["stage_key"] == "write" and f["data"]["status"] == "done"
    ]
    assert write_running, "缺 stage(write,running)（synthesis 节点入口）"
    assert len(write_done) == 1, f"stage(write,done) 应恰一次：{len(write_done)}"
    # 兜底留痕键（M4 P50 采样排除判据）——digest 形态
    assert write_done[0]["data"]["extra"] == {"fallback": "synthesis_digest"}, (
        f"write(done).extra 应为 fallback=synthesis_digest：{write_done[0]['data']}"
    )

    sources_ids = [f["id"] for f in frames if f["event"] == "sources"]
    token_frames = [f for f in frames if f["event"] == "token"]
    assert len(token_frames) == 1, (
        f"digest 以**单事件整块 token** 发射（api.md §7.2 v1.25），实测 {len(token_frames)} 次"
    )
    token_frame = token_frames[0]
    assert token_frame["data"]["delta"], "digest token 内容为空"
    assert max(sources_ids) < write_running[0]["id"] < token_frame["id"] < write_done[0]["id"], (
        "事件序违规：期望 sources < write(running) < token(digest) < write(done)"
    )
    assert write_done[0]["id"] < done["id"]

    # ---- answer_md = 兜底全文（digest 结构）：[n] 与来源卡编号对齐（可寻址） ----
    answer_md = detail["answer_md"]
    assert answer_md, "兜底 answer_md 未落库（刷新即丢的缺陷不得复现）"
    assert answer_md == token_frame["data"]["delta"], (
        "digest token 与落库 answer_md 应同源（单块 token 即全文）"
    )
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer_md)}
    ref_nos = {s["ref_no"] for s in detail["sources"]}
    assert cited, "digest 应含至少一条 [n] 来源行"
    assert cited <= ref_nos, f"digest [n] 越界（悬空引用）：{sorted(cited - ref_nos)}"
    assert len(cited) <= 5, f"digest 来源行上限 5，实测 {len(cited)}"
    assert sum(1 for line in answer_md.splitlines() if line.startswith("- [")) == len(cited), (
        "digest 列表行数与 [n] 引用数不一致（结构漂移）"
    )

    # ---- 2.5 对账：status=degraded、无 error 卡、stages[] 透传 fallback ----
    assert detail["status"] == "degraded"
    assert detail["error"] is None, f"兜底轮不应有 error 记录：{detail['error']}"
    assert detail["degraded_reason"] is None
    write_stage_entries = [
        s for s in detail["stages"]
        if s["stage_key"] == "write" and s["status"] == "done"
    ]
    assert write_stage_entries and write_stage_entries[-1].get("extra") == {
        "fallback": "synthesis_digest"
    }, "§2.5 stages[] 应透传 extra.fallback（判兜底轮型与采样排除依据）"
    # 常规检索形态的 rewrite 留痕仍在（失败发生在 write 段，检索策略面未被覆盖）
    assert _rewrite_strategy(frames) in {"verify", "web"}
