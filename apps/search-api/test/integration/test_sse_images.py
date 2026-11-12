"""integration：A6 images 事件 SSE 全链路 + REST §2.5 重建（批次 7 前置小修——api.md §7.2/§2.5 v1.12 / §7.4）。

真库 + 桩主图（stub_flow.FAKE_IMAGES 旋钮）+ 真 EventBus + 进程内 ASGITransport 真流
（test_sse_s2 同口径）。TurnRunner 翻译通道（turn_service.py elif ptype == "images"）
零改动实证：图侧发射 → SSE 事件 + turn_events 落库逐字段对账。

两用例：
- ① FAKE_IMAGES 开启：images 帧恰一次、位置在 sources 之后 stage(write,running) 之前
  （§7.4 快速序列）、信封三键、payload 逐字段透传、turn_events 同 seq 落库、全流 seq 单调，
  且 REST §2.5 images[] 自 turn_events 重建逐字段对账（键可省形态透传——仅 url 条目
  不带 null 键，api.md §2.5 v1.12「自该轮 turn_events 的 images 事件 payload 重建」）；
- ② 默认（FAKE_IMAGES=None）：零 images 帧（api.md §7.2「无图不发本事件」——翻译面
  不发明事件）+ REST images[] 为空数组（零 images 行 → 空数组）。
"""

import pytest

from test.integration import stub_flow
from test.integration.test_sse import (
    _assert_sse_envelope,
    _assert_stream_headers,
    parse_sse_frames,
)
from test.integration.test_sse_s2 import _db_turn_events

_FAST_QUERY = "A6 联调：图片卡片事件与来源卡片的序列关系？"

#: 桩 images 条目（覆盖 v1.12 键可省两形态：全键条目 + 仅 url 条目）
_FAKE_TURN_IMAGES: list[dict] = [
    {
        "url": "https://stub.example/img/arch.png",
        "source_url": "https://stub.example/page",
        "title": "桩流架构图",
        "width": 1200,
        "height": 750,
    },
    {"url": "https://stub.example/img/bare.png"},
]


@pytest.mark.requires_db
async def test_images_event_position_envelope_and_turn_events_reconciliation(
    async_client, b2_rest, b2_runtime, monkeypatch
):
    """① images 事件：§7.4 位置（sources 后、write(running) 前）+ 恰一次 + 信封三键 +
    payload 逐字段透传 + turn_events 同 seq 落库对账 + 全流 seq 单调 +
    REST §2.5 images[] 自 turn_events 重建对账（api.md:249 v1.12）。"""
    monkeypatch.setattr(stub_flow, "FAKE_IMAGES", list(_FAKE_TURN_IMAGES))
    resp = await async_client.post("/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"})
    assert resp.status_code == 200
    turn_id = resp.json()["data"]["turn_id"]
    detail = await b2_rest.wait_turn_status(turn_id, {"completed"})

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    _assert_stream_headers(stream)
    frames, _ = parse_sse_frames(stream.text)

    images_frames = [f for f in frames if f["event"] == "images"]
    assert len(images_frames) == 1  # api.md §7.2：轮内至多一次
    images_frame = images_frames[0]
    _assert_sse_envelope(images_frame, turn_id)  # §7.1 信封三键（seq/turn_id/ts）
    assert images_frame["data"]["images"] == _FAKE_TURN_IMAGES  # 翻译一对一透传（键可省形态原样）

    # §7.4 快速序列位置：最后 sources 帧 < images 帧 < write(running) 帧
    sources_frames = [f for f in frames if f["event"] == "sources"]
    assert sources_frames, "无 sources 帧（前置阶段缺失）"
    last_sources_seq = max(f["id"] for f in sources_frames)
    write_running = next(
        f
        for f in frames
        if f["event"] == "stage"
        and f["data"]["stage_key"] == "write"
        and f["data"]["status"] == "running"
    )
    assert last_sources_seq < images_frame["id"] < write_running["id"]

    # turn_events 落库对账：恰一行 event_type=images、同 seq、payload 逐字段相等
    rows = await _db_turn_events(turn_id)
    image_rows = [r for r in rows if r.event_type == "images"]
    assert len(image_rows) == 1  # 落库白名单含 images（data-model.md §1.5）
    row = image_rows[0]
    assert row.seq == images_frame["id"]  # 发布盖章同一 seq 落库
    assert row.payload == {"images": _FAKE_TURN_IMAGES}

    # REST §2.5 重建对账（api.md §2.5 v1.12「images[] 自 turn_events 的 images 事件
    # payload 重建」）：逐字段等于落库行 payload["images"]；键可省形态原样透传——
    # 仅 url 条目 == {"url": ...}（缺值键不置 null，§7.2 v1.12 同口径）
    assert detail["images"] == row.payload["images"]
    assert detail["images"] == _FAKE_TURN_IMAGES
    assert detail["images"][1] == {"url": "https://stub.example/img/bare.png"}
    assert "source_url" not in detail["images"][1]

    # 全流 seq 单调连续（线程级计数器不因新事件类型破序）
    ids = [f["id"] for f in frames]
    assert ids == sorted(set(ids)) and ids == list(range(ids[0], ids[0] + len(ids)))


@pytest.mark.requires_db
async def test_no_images_by_default_emits_no_event(async_client, b2_rest, b2_runtime):
    """② 无图不发（api.md §7.2「无图不发本事件（不渲染空区块）」）：旋钮默认关闭 →
    全流零 images 帧；翻译面不发明事件。"""
    assert stub_flow.FAKE_IMAGES is None  # 默认基线（无图不发）
    resp = await async_client.post("/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"})
    assert resp.status_code == 200
    turn_id = resp.json()["data"]["turn_id"]
    await b2_rest.wait_turn_status(turn_id, {"completed"})

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _ = parse_sse_frames(stream.text)
    assert [f["event"] for f in frames if f["event"] == "images"] == []
    assert frames and frames[-1]["event"] == "done"  # 常规序列不受影响
    rows = await _db_turn_events(turn_id)
    assert all(r.event_type != "images" for r in rows)
    # REST §2.5：零 images 行 → 空数组（重建空集语义，api.md §2.5）
    detail = (await async_client.get(f"/api/v1/turns/{turn_id}")).json()["data"]
    assert detail["images"] == []
