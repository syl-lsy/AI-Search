"""e2e：HITL 审批 REST 七形态真链路（api.md §2.13/§2.5/§2.7，REQ-069/A54）。

批次 2 交付档挂账原文（docs/b22-b2-delivery-20261127.md §4.2）「REST e2e 形态七
用例」当时落 integration（真服务假 spec 缝）——本文件为该挂账在 test/e2e/ 的落位
补齐：同一套七形态用例名，压「真 HTTP+真测试库+真调度器（零 monkeypatch 零假
spec 缝）」的端点装配链。与 integration 分工：integration 用假 spec 缝覆盖审批
语义全矩阵，本层只压跨层装配（真服务子进程 :18000、conftest 库引导沿用、真 Nacos
spec 快照）。

awaiting 轮测试内构造（gate_payload+审批行直插，ask 口径）：不依赖 hitl.enabled
运行态翻键——门 armed 驱动的真弹窗全链归真机 e2e 挂账族（批次 2/3/4 同族挂账，
翻键=用户动作）。七形态=挂账原文用例名逐一对应：

1. test_approval_endpoint_full_flow〔幂等 200+202〕
2. test_approval_endpoint_error_faces〔404001/400001/409008〕
3. test_approval_note_sanitized_r35
4. test_approval_timeout_hybrid_via_sweep（真扫描器 30s 墙钟轮询——唯一等扫描的用例）
5. test_cancel_endpoint_awaiting_branch
6. test_timeout_cancel_rest_canceled_key_omission〔canceled(timeout) 回看面
   approval 键整体省略——canceled 行直造承载投影断言；timeout-cancel 写入路径的
   on_timeout=cancel 依赖 spec 缝，integration 已锁，运行态翻键归挂账族〕
7. test_timeout_hybrid_2p5_expired_projected_approved〔expired 直造→2.5 投影
   approved 形态〕

断言纪律：REST 契约面（状态码/信封/data 键/投影形态/落库行），不比对 message
文案（integration 已锁中文措辞）。decide 成功触发的 BackgroundTask 阶段 2 真轮
不 await（真 LLM/外呼时窗波动——批次 4 余红甄别同族 207~288s 时窗先例；轮行随
测试自清理删除，后台轮缺失行自然失败退出零残留）。e2e 数据自清理：每用例
finally 删种子线程（FK 级联 turns→turn_approvals/turn_events）。

SQL 纪律：单行语句+全参数绑定（%s），零拼接零 f-string SQL。
"""

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg2

from test.e2e.test_fast_round_contract import _get_detail

_SEED_USER_ID = "00000000-0000-4000-8000-000000000001"

_GATE_PAYLOAD = {
    "schema_version": 1,
    "rewritten_queries": ["知识库 审批门 e2e"],
    "query_type": "factoid",
    "time_range": None,
    "query_lang": "zh",
    "kb_candidates": [
        {
            "chunk_id": "c-e2e-1",
            "kb_doc_id": "d-e2e-1",
            "title": "命中标题",
            "heading_path": ["文档x", "命中X"],
            "content": "命中内容（e2e 库内直造）",
            "score": 0.5,
            "query_index": 0,
        }
    ],
}


def _connect() -> Any:
    """真测试库连接（连接参数=e2e conftest 强注入的 DB_ 组同源）。"""
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
        connect_timeout=5,
    )


def _seed_approval_round(
    thread_id,
    turn_id,
    turn_status="awaiting_approval",
    approval_status="pending",
    expires_in=120.0,
    decided_by=None,
    choice=None,
    cancel_reason=None,
):
    """直造轮 + 审批行（真 API 不感知构造方式——黑盒面；SQL 全参数绑定）。"""
    now = datetime.now(UTC)
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, mode_summary, turn_count) VALUES (%s, %s, %s, %s, %s)",
                (str(thread_id), _SEED_USER_ID, "审批 e2e 线程", "fast", 1),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, rewritten_queries, chat_model_key, model_name, timing, usage, query_lang, cancel_reason, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(turn_id),
                    str(thread_id),
                    _SEED_USER_ID,
                    "fast",
                    turn_status,
                    "知识库里有审批门的资料吗？",
                    json.dumps(["知识库 审批门 e2e"]),
                    "qwen",
                    "chat-standard",
                    json.dumps({}),
                    json.dumps({}),
                    "zh",
                    cancel_reason,
                    now,
                    now if turn_status == "canceled" else None,
                ),
            )
            cursor.execute(
                "INSERT INTO turn_approvals (id, turn_id, status, verdict, hits_summary, previews, gate_payload, requested_at, expires_at, responded_at, choice, decided_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(turn_id),
                    approval_status,
                    "has_hit",
                    json.dumps([]),
                    json.dumps([]),
                    json.dumps(_GATE_PAYLOAD),
                    now,
                    now + timedelta(seconds=expires_in),
                    now if approval_status != "pending" else None,
                    choice,
                    decided_by,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _cleanup_thread(thread_id):
    """e2e 数据自清理（FK 安全序）：artifacts（turns FK 无 ondelete=CASCADE，后台
    真轮可能落行）→ turns → threads；其余子表（审批行/事件/反馈/快照/来源）随
    turns 的 DB 级 CASCADE 一并删除。"""
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM artifacts WHERE turn_id IN (SELECT id FROM turns WHERE thread_id = %s)",
                (str(thread_id),),
            )
            cursor.execute("DELETE FROM turns WHERE thread_id = %s", (str(thread_id),))
            cursor.execute("DELETE FROM threads WHERE id = %s", (str(thread_id),))
        conn.commit()
    finally:
        conn.close()


def _turn_row(turn_id):
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, cancel_reason, finished_at FROM turns WHERE id = %s",
                (str(turn_id),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip(("status", "cancel_reason", "finished_at"), row))
    finally:
        conn.close()


def _approval_row(turn_id):
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, verdict, choice, decided_by, note FROM turn_approvals WHERE turn_id = %s",
                (str(turn_id),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip(("status", "verdict", "choice", "decided_by", "note"), row))
    finally:
        conn.close()


def _event_payloads(turn_id, event_type):
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM turn_events WHERE turn_id = %s AND event_type = %s ORDER BY seq ASC",
                (str(turn_id), event_type),
            )
            return [r[0] for r in cursor.fetchall()]
    finally:
        conn.close()


def _poll(check, timeout_s, interval_s=2.0):
    """轮询至谓词成立或超时（末次必检）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval_s)
    return check()


def _post_approval(base_url, turn_id, body):
    return httpx.post(f"{base_url}/api/v1/turns/{turn_id}/approval", json=body, timeout=30.0)


def _post_cancel(base_url, turn_id):
    return httpx.post(f"{base_url}/api/v1/turns/{turn_id}/cancel", timeout=30.0)


def test_approval_endpoint_full_flow(base_url):
    """形态 1〔幂等 200+202〕：直造 awaiting 轮 → 首裁 202（status 恒 running+
    choice 回显）→ 同 choice 幂等 200；审批行 approved/decided_by=user。
    阶段 2 真轮 BackgroundTask 不 await（断言面=REST 契约，见模块 docstring）。"""
    thread_id, turn_id = uuid.uuid4(), uuid.uuid4()
    try:
        _seed_approval_round(thread_id, turn_id)
        first = _post_approval(base_url, turn_id, {"choice": "web_only"})
        assert first.status_code == 202, f"首裁应 202：{first.status_code} {first.text}"
        body = first.json()
        assert body["code"] == "0"
        data = body["data"]
        assert set(data) == {"turn_id", "status", "choice"}
        assert data["turn_id"] == str(turn_id)
        assert data["status"] == "running"  # §2.13：202 响应 status 恒 running
        assert data["choice"] == "web_only"
        second = _post_approval(base_url, turn_id, {"choice": "web_only"})
        assert second.status_code == 200, f"幂等应 200：{second.status_code} {second.text}"
        assert second.json()["data"]["choice"] == "web_only"
        row = _approval_row(turn_id)
        assert row is not None
        assert row["status"] == "approved" and row["decided_by"] == "user"
        assert _turn_row(turn_id)["status"] == "running"
    finally:
        _cleanup_thread(thread_id)


def test_approval_endpoint_error_faces(base_url):
    """形态 2〔404001/400001/409008〕：未知轮 404001；非法 choice 400001；
    非 awaiting 轮 409008；同轮异 choice 409008。"""
    thread_id, turn_id = uuid.uuid4(), uuid.uuid4()
    running_id = uuid.uuid4()
    try:
        _seed_approval_round(thread_id, turn_id)
        missing = _post_approval(base_url, uuid.uuid4(), {"choice": "both"})
        assert missing.status_code == 404
        assert missing.json()["code"] == "404001"
        illegal = _post_approval(base_url, turn_id, {"choice": "hybrid"})
        assert illegal.status_code == 400
        assert illegal.json()["code"] == "400001"
        _seed_approval_round(
            running_id, running_id, turn_status="running",
            approval_status="expired", decided_by="timeout", choice="both",
        )
        not_awaiting = _post_approval(base_url, running_id, {"choice": "both"})
        assert not_awaiting.status_code == 409
        assert not_awaiting.json()["code"] == "409008"
        first = _post_approval(base_url, turn_id, {"choice": "both"})
        assert first.status_code == 202
        conflict = _post_approval(base_url, turn_id, {"choice": "web_only"})
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "409008"
    finally:
        _cleanup_thread(thread_id)
        _cleanup_thread(running_id)


def test_approval_note_sanitized_r35(base_url):
    """形态 3：note 过 query 同款 sanitize 管道（R35/PII 占位词）——落库脱敏，
    原文不入审批行。"""
    thread_id, turn_id = uuid.uuid4(), uuid.uuid4()
    try:
        _seed_approval_round(thread_id, turn_id)
        response = _post_approval(
            base_url, turn_id,
            {"choice": "custom", "note": "补充：联系电话 13800138000 找我"},
        )
        assert response.status_code == 202
        stored = _approval_row(turn_id)
        assert stored is not None and stored["note"] is not None
        assert "13800138000" not in stored["note"]  # 原文不落库（占位词替换）
    finally:
        _cleanup_thread(thread_id)


def test_approval_timeout_hybrid_via_sweep(base_url):
    """形态 4：expires_at 已过 → 真 30s 扫描器收口（hybrid 缺省：行 expired/
    choice=both/decided_by=timeout + 轮回 running）→ 超时后人为裁决 409008。"""
    thread_id, turn_id = uuid.uuid4(), uuid.uuid4()
    try:
        _seed_approval_round(thread_id, turn_id, expires_in=-5.0)

        def _collected():
            row = _approval_row(turn_id)
            turn = _turn_row(turn_id)
            return (
                row is not None
                and row["status"] == "expired"
                and row["decided_by"] == "timeout"
                and row["choice"] == "both"
                and turn is not None
                and turn["status"] == "running"
            )

        assert _poll(_collected, 90.0), "扫描器 90s 窗口内未收口到期审批行"
        late = _post_approval(base_url, turn_id, {"choice": "both"})
        assert late.status_code == 409
        assert late.json()["code"] == "409008"  # 超时后再到的人为裁决
    finally:
        _cleanup_thread(thread_id)


def test_cancel_endpoint_awaiting_branch(base_url):
    """形态 5（§2.7 v1.37）：awaiting 取消=直接终态 canceled(user)+审批行
    canceled（decided_by=system）+ approval_resolved(resolved_by=cancel)+
    done(canceled)。"""
    thread_id, turn_id = uuid.uuid4(), uuid.uuid4()
    try:
        _seed_approval_round(thread_id, turn_id)
        response = _post_cancel(base_url, turn_id)
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "canceling"
        assert _poll(
            lambda: (_approval_row(turn_id) or {}).get("status") == "canceled", 15.0
        )
        row = _turn_row(turn_id)
        assert row is not None and row["status"] == "canceled"
        assert row["cancel_reason"] == "user"
        approval = _approval_row(turn_id)
        assert approval is not None and approval["decided_by"] == "system"
        resolved = _event_payloads(turn_id, "approval_resolved")
        assert resolved and resolved[0]["resolved_by"] == "cancel"
        done = _event_payloads(turn_id, "done")
        assert done and done[0]["finish"] == "canceled"
    finally:
        _cleanup_thread(thread_id)


def test_timeout_cancel_rest_canceled_key_omission(base_url):
    """形态 6（§2.5 v1.38 收口句）：canceled(timeout) 行回看面 approval 键整体
    省略（审计留痕在 turn_approvals 表）——canceled 行直造承载投影断言（写入
    路径依赖 spec 缝，integration 已锁；运行态翻键归真机 e2e 挂账族）。"""
    thread_id, turn_id = uuid.uuid4(), uuid.uuid4()
    try:
        _seed_approval_round(
            thread_id, turn_id, turn_status="canceled",
            approval_status="canceled", decided_by="timeout", cancel_reason="timeout",
        )
        detail = _get_detail(base_url, str(turn_id))
        assert "approval" not in detail, f"canceled 行 approval 键应省略，实测 {sorted(detail)}"
        assert detail["status"] == "canceled"
        assert detail["cancel_reason"] == "timeout"
    finally:
        _cleanup_thread(thread_id)


def test_timeout_hybrid_2p5_expired_projected_approved(base_url):
    """形态 7（§2.5 v1.37 投影统一）：expired 行 2.5 投影 approved+
    decided_by=timeout 区分（存储/投影命名分歧为契约自身定义）。"""
    thread_id, turn_id = uuid.uuid4(), uuid.uuid4()
    try:
        _seed_approval_round(
            thread_id, turn_id, approval_status="expired",
            decided_by="timeout", choice="both",
        )
        detail = _get_detail(base_url, str(turn_id))
        approval = detail.get("approval")
        assert approval is not None, "expired 行 2.5 应投影 approval 形态"
        assert set(approval) == {"status", "choice", "decided_by"}
        assert approval["status"] == "approved"
        assert approval["choice"] == "both"
        assert approval["decided_by"] == "timeout"
    finally:
        _cleanup_thread(thread_id)
