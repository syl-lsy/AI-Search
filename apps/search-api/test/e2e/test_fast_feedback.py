"""e2e：轮反馈端点真链路（api.md §2.9 / §2.5 v1.16，REQ-057/A41）。

与 integration（test_turn_feedback.py）分工：integration 已用**管理面直插行**覆盖
upsert/改判/级联全语义，本层补的是「**真服务 + 真轮次**」的跨层链路——反馈写入的
turn 由真链路 POST /api/v1/threads 跑出（不是手工造行），压三件事：

1. 写通道：POST /turns/{id}/feedback → 200 信封 {"code":"0","data":{turn_id,helpful,
   updated_at},"message":"ok"}（§2.9 响应 data 三键，类型：str/bool/时间戳）；
2. 读通道（§2.5 feedback_helpful）：写入后 GET /turns/{id} 立刻回读现值 true；
   改判 false 后回读跟随——A41「刷新/回看还原」的两段式事实源；
3. 落库不变式：turn_feedback 表**恒一行**（UNIQUE(turn_id) 承载 upsert 改判）、
   created_at 不变而 updated_at 刷新（data-model §1.16）；
4. **清除往返（v1.17）**：再次点击已选中按钮发 null 即取消——DELETE 该轮行，响应
   {turn_id, helpful: null, updated_at: null}，§2.5 读回自然回 null（null=未反馈，
   A41 读通道兼容）；
5. 错误面：随机 uuid → 404/404004；helpful 非 boolean|null（strict=True，不做宽松
   强制转换，"yes"/1 仍拒）→ 400/400001。

断言纪律：断言结构与状态码/错误码，不比对 message 文案（integration 已锁死中文措辞，
本层只校验契约字段面）。轮次状态门槛协议面不设限（§2.9 原文「操作行渲染时机由前端
决定」）——故本文件不要求 completed 才可反馈，只要求轮次真实存在；收流仍走既有
_create_turn/_collect_stream/_get_detail 链路辅助（同 test_fast_round_contract.py），
避免在 e2e 层重造 SSE 解析。

题面选短请求（direct 判例原文同款）：反馈端点与被反馈轮的内容质量无关，选短轮只为把
本文件耗时压到秒级；若模型偶发改判 web，轮仍会 completed（仅耗时变长），不影响断言面。
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import psycopg2

from test.e2e.test_fast_round_contract import (
    _collect_stream,
    _create_turn,
    _get_detail,
)

#: 短请求题面（nacos-config.md §1 v1.20 direct 判例原文——零检索轮，秒级完成）
FEEDBACK_QUERY = "你好，能做什么？"


# ---- 链路辅助 ----


def _wait_terminal(base_url: str, turn_id: str) -> tuple[list[dict], dict]:
    """收 SSE 至 done（或流关闭）后取 §2.5 终态；返回 (frames, detail)。

    轮次状态门槛不在 §2.9 里（反馈对 running/queued 亦合法），故这里只做链路收束，
    不断言终态取值——终态断言归 test_fast_round_contract.py，本文件不重复其断言面。
    """
    frames, _heartbeats = _collect_stream(base_url, turn_id)
    detail = _get_detail(base_url, turn_id)
    return frames, detail


def _post_feedback(
    base_url: str, turn_id: str, helpful: object
) -> httpx.Response:
    """POST /api/v1/turns/{id}/feedback（§2.9）；helpful 故意放宽为 object 以测非法体。"""
    return httpx.post(
        f"{base_url}/api/v1/turns/{turn_id}/feedback",
        json={"helpful": helpful},
        timeout=30.0,
    )


def _parse_ts(value: object) -> datetime:
    """解析 API（ISO-8601，Z 或 +00:00 后缀）与 DB（datetime）两侧时间戳到 UTC。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _db_feedback_rows(turn_id: str) -> list[tuple[object, ...]]:
    """管理面读 turn_feedback（值全参数绑定——只读单表，不触碰业务写路径）。

    连接参数取 e2e conftest 已强注入的 DB_ 组（os.environ 与真服务/迁移同源）。
    """
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
            cursor.execute(
                "SELECT turn_id, helpful, created_at, updated_at FROM turn_feedback "
                "WHERE turn_id = %s",
                (turn_id,),
            )
            return cursor.fetchall()
    finally:
        conn.close()


# ---- 用例 ----


def test_e2e_feedback_write_read_and_flip(base_url):
    """真链路建轮 → 反馈 true（200 + 回读 true）→ 改判 false（200 + 回读 false）。

    压四件事：①写响应 data 三键类型；②§2.5 feedback_helpful 读回与写同源（A41 刷新
    还原通道）；③表恒一行、helpful 覆盖、created_at 不变、updated_at 刷新；④未反馈
    基线（首写前 2.5 恒 null——「null=未反馈」）。
    """
    turn_id = _create_turn(base_url, FEEDBACK_QUERY)
    frames, detail = _wait_terminal(base_url, turn_id)
    assert frames, "SSE 流为空——建轮/收流链路未跑通"
    # 未反馈基线（§2.5 v1.16：无行 = null）。放在真实轮上比对，避免只测手工造行。
    assert detail["feedback_helpful"] is None, "未反馈轮 2.5 应回 null"

    # ---- 首次：有帮助 ----
    first = _post_feedback(base_url, turn_id, True)
    assert first.status_code == 200, f"反馈写入失败：{first.status_code} {first.text}"
    first_body = first.json()
    assert first_body["code"] == "0"
    assert first_body["message"] == "ok"  # 成功信封统一 message（§0）
    data = first_body["data"]
    assert set(data) == {"turn_id", "helpful", "updated_at"}, (
        f"§2.9 响应 data 应恰三键，实测 {sorted(data)}"
    )
    assert data["turn_id"] == turn_id
    assert data["helpful"] is True
    assert isinstance(data["updated_at"], str) and data["updated_at"]

    assert _get_detail(base_url, turn_id)["feedback_helpful"] is True, (
        "§2.5 读回未跟随写入（2.9 写通道与 2.5 读通道应同源）"
    )

    rows_first = _db_feedback_rows(turn_id)
    assert len(rows_first) == 1, f"首写应恰一行，实测 {len(rows_first)}"
    row_turn, row_helpful, created_at, updated_at_first = rows_first[0]
    assert str(row_turn) == turn_id
    assert row_helpful is True
    # 写响应 updated_at 与库内行同一时刻（两侧 clocks 同源：func.now() + RETURNING）
    assert abs(_parse_ts(data["updated_at"]) - _parse_ts(updated_at_first)) < (
        timedelta(seconds=1)
    ), "响应 updated_at 与落库 updated_at 应同源（§2.9 写响应 = RETURNING 回读）"

    # ---- 改判：没有帮助（upsert 覆盖）----
    second = _post_feedback(base_url, turn_id, False)
    assert second.status_code == 200, f"改判失败：{second.status_code} {second.text}"
    assert second.json()["code"] == "0"
    assert second.json()["data"]["helpful"] is False
    assert second.json()["data"]["turn_id"] == turn_id

    assert _get_detail(base_url, turn_id)["feedback_helpful"] is False, (
        "§2.5 读回未跟随改判"
    )

    rows_second = _db_feedback_rows(turn_id)
    assert len(rows_second) == 1, "改判不得新增行（UNIQUE(turn_id) 一轮一行）"
    _, row_helpful_after, created_at_after, updated_at_after = rows_second[0]
    assert row_helpful_after is False
    assert created_at_after == created_at, "首次反馈时间不该被改判覆盖（data-model §1.16）"
    assert updated_at_after >= updated_at_first, "改判须刷新 updated_at"


def test_e2e_feedback_clear_roundtrip(base_url):
    """v1.17 清除往返：true → null（200 清除形态、行消失、§2.5 读回 null）→ true（再 upsert 正常）。

    §2.9 v1.17：null = 清除反馈（删除该轮 turn_feedback 行）——再次点击已选中按钮
    发 null 即取消（Perplexity 同钮再点即取消实机行为）。清除形态响应 =
    {turn_id, helpful: null, updated_at: null}（三键恒在、双 null）；§2.5
    feedback_helpful 读回自然回 null（null=未反馈，A41 读通道兼容）；
    清除后再 upsert 正常落行（改判/清除共用端点）。
    """
    turn_id = _create_turn(base_url, FEEDBACK_QUERY)
    frames, _detail = _wait_terminal(base_url, turn_id)
    assert frames, "SSE 流为空——建轮/收流链路未跑通"

    # ---- 前置：置 true（写通道语义已被 write_read_and_flip 锁死，这里只建前置态）----
    set_true = _post_feedback(base_url, turn_id, True)
    assert set_true.status_code == 200, f"反馈写入失败：{set_true.status_code} {set_true.text}"
    assert set_true.json()["data"]["helpful"] is True
    assert len(_db_feedback_rows(turn_id)) == 1, "清除前应恰一行"

    # ---- 清除：发 null（取消）----
    clear = _post_feedback(base_url, turn_id, None)
    assert clear.status_code == 200, f"清除失败：{clear.status_code} {clear.text}"
    clear_body = clear.json()
    assert clear_body["code"] == "0" and clear_body["message"] == "ok"
    clear_data = clear_body["data"]
    assert set(clear_data) == {"turn_id", "helpful", "updated_at"}, (
        f"§2.9 清除形态 data 应恰三键（值 null），实测 {sorted(clear_data)}"
    )
    assert clear_data["turn_id"] == turn_id
    assert clear_data["helpful"] is None
    assert clear_data["updated_at"] is None

    # 行消失（UNIQUE(turn_id) 一轮一行口径下「清除 = 删行」）
    assert _db_feedback_rows(turn_id) == [], "清除后 turn_feedback 行应已删除"

    # §2.5 读回自然回 null（null=未反馈，A41 读通道兼容）
    assert _get_detail(base_url, turn_id)["feedback_helpful"] is None, (
        "清除后 §2.5 读回应回 null"
    )

    # ---- 往返：清除后再 upsert 正常落行 ----
    reupsert = _post_feedback(base_url, turn_id, True)
    assert reupsert.status_code == 200
    assert reupsert.json()["data"]["helpful"] is True
    rows = _db_feedback_rows(turn_id)
    assert len(rows) == 1 and rows[0][1] is True, "清除后再 upsert 应正常落行"


def test_e2e_feedback_unknown_turn_404(base_url):
    """随机 uuid → 404 + code 404004（§0 错误码表：反馈端点轮次不存在）。

    与 404001 分码的理由（契约侧）：反馈面埋点需区分「轮不存在」与「轮域 404」。
    """
    missing = str(uuid.uuid4())
    response = _post_feedback(base_url, missing, True)
    assert response.status_code == 404, f"实为 {response.status_code} {response.text}"
    body = response.json()
    assert body["code"] == "404004"
    assert isinstance(body["message"], str) and body["message"]
    assert "data" not in body, "错误信封仅 {code,message}（§0）"


def test_e2e_feedback_invalid_body_400(base_url):
    """helpful 非 boolean|null → 400 + code 400001（strict=True：不做宽松强制转换）。

    真服务上断言（pydantic strict 边界 + 全局 RequestValidationError 处理器链路），
    与 unit 层的依赖覆盖路径不同——这是唯一验证「线上真栈也拒收」的位置。
    v1.17 起 null 合法（=清除反馈，test_e2e_feedback_clear_roundtrip 锁死），
    非法清单仅剩非 boolean|null 值。
    """
    # 路径参数合法（随机 uuid 亦可），失败必须发生在 body 校验层而非归属校验层
    turn_id = str(uuid.uuid4())
    for payload in ("yes", 1):
        response = _post_feedback(base_url, turn_id, payload)
        assert response.status_code == 400, (
            f"helpful={payload!r} 应 400，实为 {response.status_code} {response.text}"
        )
        body = response.json()
        assert body["code"] == "400001", (
            f"helpful={payload!r} 应落 400001，实为 {body['code']}"
        )
        assert "data" not in body

    # 缺键同样 400001（§2.9「helpful：boolean | null 必填」——null 合法=清除，缺失仍拒）
    response = httpx.post(
        f"{base_url}/api/v1/turns/{turn_id}/feedback", json={}, timeout=30.0
    )
    assert response.status_code == 400
    assert response.json()["code"] == "400001"
