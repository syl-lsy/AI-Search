"""integration 层：导出链（REQ-016 §2.8/§5.3）+ 快照查看（REQ-015 §5.1）+ sources
.snapshot_id 回填（批次 2 注⑦闭合）。

用例形态（真库 requires_db；快照 200 形态另需真 MinIO——缺凭据 skip，沿
test_save_snapshot_live 同款纪律）：
- **行播种直驱**（不经 POST /threads 全轮运行——端点读 turns 表状态面，播种等价且
  确定性；真实轮端到端导出一致实测归批次 7 A3 回看复验）：Thread/Turn/Source 行经
  ORM 播种（get_session_factory 短事务——test_save_snapshot_live 同款）；
- artifacts 目录隔离：autouse fixture 把 sandbox_settings.artifacts_dir 指向 tmp_path
  （零仓库污染）；文件名/rel_path 纯代码生成（安全面：零用户输入拼接）+ 路径锚定
  纵深防御（unit test_artifact_service.py 已锁 helper 层）；
- 下载端点 200 FileResponse（契约「302 预签名或 200 FileResponse 择一」落 200——
  产物为本机沙箱目录文件非 MinIO 对象，302 预签名面不适用）；路由须自设
  request.state.is_stream=True 跳过信封包装（core/middleware/response.py:8-10 非
  JSON 响应逃逸面——否则 json.loads 必炸 500001）；
- service 只 flush 不 commit（铁律 10）：导出在请求级事务内 flush，teardown 提交
  （test_artifact_service.py 源码级护栏已锁）。
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import common_settings, sandbox_settings

pytestmark = pytest.mark.requires_db

_ANSWER = "# 调研报告\n\n## 摘要\n\n核心发现。\n\n## 结论\n\n收束。\n"


@pytest.fixture(autouse=True)
def _isolate_artifacts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """产物目录隔离（零仓库污染）：SANDBOX_ARTIFACTS_DIR → tmp_path/sandbox。"""
    target = tmp_path / "sandbox"
    monkeypatch.setattr(sandbox_settings, "artifacts_dir", str(target))
    return target


def _user_uuid() -> uuid.UUID:
    return uuid.UUID(common_settings.default_user_id)


async def _seed_turn(
    *,
    mode: str = "deep",
    status: str = "completed",
    answer_md: str | None = _ANSWER,
    thread_id: str | None = None,
) -> tuple[str, str]:
    """播种 thread + turn 行（端点读表状态面——确定性构造）；传 thread_id 时把新轮
    挂进既有线程（同线程多轮形态＝同分钟跨轮碰撞构造面）。"""
    from app.db.models.thread import Thread
    from app.db.models.turn import Turn
    from app.db.session import get_session_factory

    user_uuid = _user_uuid()
    thread_uuid, turn_uuid = uuid.uuid4(), uuid.uuid4()
    factory = get_session_factory()
    async with factory() as session:
        if thread_id is None:
            session.add(Thread(id=thread_uuid, user_id=user_uuid, title="导出链实测"))
            await session.flush()
        else:
            thread_uuid = uuid.UUID(thread_id)
        session.add(
            Turn(
                id=turn_uuid,
                thread_id=thread_uuid,
                user_id=user_uuid,
                mode=mode,
                status=status,
                query="导出链实测",
                chat_model_key="qwen",
                model_name="seed",
                answer_md=answer_md,
                finished_at=datetime.now(UTC) if status in ("completed", "degraded", "canceled", "failed") else None,
            )
        )
        await session.commit()
    return str(thread_uuid), str(turn_uuid)


async def _seed_source(turn_id: str, *, url: str, ref_no: int = 1) -> str:
    """播种 source 行（snapshot_id 恒 NULL——回填/404002 构造面）。"""
    from app.db.models.source import Source
    from app.db.session import get_session_factory

    source_uuid = uuid.uuid4()
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            Source(
                id=source_uuid,
                turn_id=uuid.UUID(turn_id),
                user_id=_user_uuid(),
                ref_no=ref_no,
                source_type="web",
                title=f"来源{ref_no}",
                url=url,
                snippet="片段",
                snapshot_id=None,
            )
        )
        await session.commit()
    return str(source_uuid)


async def _count_artifacts(turn_id: str) -> int:
    from app.db.models.artifact import Artifact
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(select(Artifact).where(Artifact.turn_id == uuid.UUID(turn_id)))
        ).scalars().all()
        return len(rows)


# ==== POST /turns/{turn_id}/export（api.md §2.8）====


async def test_export_deep_completed_200_keys_and_file(
    async_client: Any,
    migrated_db: Any,
) -> None:
    """deep+completed 轮导出：200 信封 data 八键逐项（§2.8 示例形态）+ 产物文件落
    threads/{thread_id}/report_{YYYYMMDD_HHmm}.md（内容=answer_md）+ artifacts 表
    登记 report 类型；download_path 指向 §5.3 端点。"""
    thread_id, turn_id = await _seed_turn()
    resp = await async_client.post(f"/api/v1/turns/{turn_id}/export")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "0" and body["message"] == "ok"
    data = body["data"]
    assert set(data) == {
        "artifact_id",
        "thread_id",
        "artifact_type",
        "filename",
        "size_bytes",
        "content_type",
        "created_at",
        "download_path",
    }
    assert data["thread_id"] == thread_id
    assert data["artifact_type"] == "report"
    assert data["content_type"] == "text/markdown"
    assert data["filename"].startswith("report_") and data["filename"].endswith(".md")
    assert data["size_bytes"] == len(_ANSWER.encode("utf-8"))
    assert data["download_path"] == (
        f"/api/v1/threads/{thread_id}/artifacts/{data['artifact_id']}/download"
    )
    # 产物文件：threads/{thread_id}/report_*.md，内容=answer_md
    files = list((Path(sandbox_settings.artifacts_dir) / "threads" / thread_id).glob("report_*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == _ANSWER
    assert await _count_artifacts(turn_id) == 1
    # §2.3 has_artifacts 真实投影回归锁（批次 6 探针发现面：此前恒 false）：
    # 导出登记 artifacts 行后，线程详情的 EXISTS 投影必须为 true
    detail = (await async_client.get(f"/api/v1/threads/{thread_id}")).json()["data"]
    assert detail["has_artifacts"] is True


async def test_export_degraded_round_200(
    async_client: Any,
    migrated_db: Any,
) -> None:
    """completed|degraded 均可导出（api.md §0:43——degraded 轮有降级报告可导）。"""
    _, turn_id = await _seed_turn(status="degraded")
    resp = await async_client.post(f"/api/v1/turns/{turn_id}/export")
    assert resp.status_code == 200
    assert resp.json()["data"]["artifact_type"] == "report"


async def test_export_fast_round_409003(async_client: Any, migrated_db: Any) -> None:
    """fast 轮 → 409003（非深度轮——§0:43；前端本地导出口径 A40 不走本端点）。"""
    _, turn_id = await _seed_turn(mode="fast", answer_md="快速答案。")
    resp = await async_client.post(f"/api/v1/turns/{turn_id}/export")
    assert resp.status_code == 409
    assert resp.json()["code"] == "409003"


async def test_export_running_round_409003(async_client: Any, migrated_db: Any) -> None:
    """未完成（running）→ 409003（§0:43「轮次未到 completed/degraded」）。"""
    _, turn_id = await _seed_turn(status="running")
    resp = await async_client.post(f"/api/v1/turns/{turn_id}/export")
    assert resp.status_code == 409
    assert resp.json()["code"] == "409003"


async def test_export_canceled_round_409003(async_client: Any, migrated_db: Any) -> None:
    """canceled → 409003（终态但不在可导出集合——保留已产出≠可导出报告）。"""
    _, turn_id = await _seed_turn(status="canceled")
    resp = await async_client.post(f"/api/v1/turns/{turn_id}/export")
    assert resp.status_code == 409
    assert resp.json()["code"] == "409003"


async def test_export_unknown_turn_404001(async_client: Any, migrated_db: Any) -> None:
    """轮不存在（含越权同码——不泄漏存在性）。"""
    resp = await async_client.post(f"/api/v1/turns/{uuid.uuid4()}/export")
    assert resp.status_code == 404
    assert resp.json()["code"] == "404001"


async def test_export_repeat_generates_new_artifact_row(
    async_client: Any,
    migrated_db: Any,
) -> None:
    """重复导出生成新文件：两次导出 → 两个 artifact_id（两行登记）；同分钟内文件名
    相同（§2.8 文件名契约分钟精度——文件覆写内容等价、产物行按次登记）。"""
    _, turn_id = await _seed_turn()
    first = (await async_client.post(f"/api/v1/turns/{turn_id}/export")).json()["data"]
    second = (await async_client.post(f"/api/v1/turns/{turn_id}/export")).json()["data"]

    assert first["artifact_id"] != second["artifact_id"]
    assert first["filename"] == second["filename"]  # 分钟精度同名（契约格式）
    assert await _count_artifacts(turn_id) == 2


class _FrozenDatetime(datetime):
    """冻结时钟（P2-1 碰撞用例确定性）：datetime.now() 恒返回同一分钟。"""

    @classmethod
    def now(cls, tz: Any = None) -> "datetime":
        return datetime(2026, 11, 20, 8, 14, tzinfo=UTC)


async def test_export_same_minute_cross_round_409003(
    async_client: Any,
    migrated_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同线程双轮同分钟先后导出：第二轮 → 409003（rel_path 同不同 turn——会覆写
    物理文件使首产物行下载到第二轮内容）；首轮文件内容与行数零损；同轮重导出仍
    放行（同 turn_id 不属碰撞——覆写+按次登记）。"""
    from app.services import artifact_service

    monkeypatch.setattr(artifact_service, "datetime", _FrozenDatetime)
    thread_id, first_turn = await _seed_turn()
    _, second_turn = await _seed_turn(
        answer_md="# 第二轮报告\n\n不同内容。\n", thread_id=thread_id
    )

    first = (await async_client.post(f"/api/v1/turns/{first_turn}/export")).json()["data"]
    resp = await async_client.post(f"/api/v1/turns/{second_turn}/export")

    assert resp.status_code == 409
    assert resp.json()["code"] == "409003"
    # 首轮物理文件未被第二轮覆写（内容/行数零损）
    target = Path(sandbox_settings.artifacts_dir) / "threads" / thread_id / first["filename"]
    assert target.read_text(encoding="utf-8") == _ANSWER
    assert await _count_artifacts(first_turn) == 1
    assert await _count_artifacts(second_turn) == 0

    # 同轮重复导出仍放行：同文件名、新行登记（内容等价）
    again = (await async_client.post(f"/api/v1/turns/{first_turn}/export")).json()["data"]
    assert again["filename"] == first["filename"]
    assert again["artifact_id"] != first["artifact_id"]
    assert await _count_artifacts(first_turn) == 2


# ==== GET /threads/{thread_id}/artifacts/{artifact_id}/download（§5.3）====


async def test_download_artifact_200_content_matches(async_client: Any, migrated_db: Any) -> None:
    """下载 200 FileResponse（契约择一落 200——产物为本机沙箱目录文件）：正文=
    answer_md、media_type=text/markdown。"""
    thread_id, turn_id = await _seed_turn()
    data = (await async_client.post(f"/api/v1/turns/{turn_id}/export")).json()["data"]

    resp = await async_client.get(data["download_path"])
    assert resp.status_code == 200
    assert resp.text == _ANSWER
    assert resp.headers["content-type"].startswith("text/markdown")


async def test_download_unknown_artifact_404001(async_client: Any, migrated_db: Any) -> None:
    """artifact 不存在 → 404001。"""
    thread_id, _ = await _seed_turn()
    resp = await async_client.get(
        f"/api/v1/threads/{thread_id}/artifacts/{uuid.uuid4()}/download"
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "404001"


async def test_download_thread_artifact_mismatch_404001(async_client: Any, migrated_db: Any) -> None:
    """路径段 thread_id 与产物登记不符 → 404001（不泄漏存在性——越权面同码）。"""
    thread_id, turn_id = await _seed_turn()
    data = (await async_client.post(f"/api/v1/turns/{turn_id}/export")).json()["data"]
    other_thread_id, _ = await _seed_turn()

    resp = await async_client.get(
        f"/api/v1/threads/{other_thread_id}/artifacts/{data['artifact_id']}/download"
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "404001"


# ==== GET /sources/{source_id}/snapshot（api.md §5.1）====


async def test_snapshot_unknown_source_404001(async_client: Any, migrated_db: Any) -> None:
    """来源不存在（含越权同码）→ 404001（零 MinIO 依赖——前置校验先于对象面）。"""
    resp = await async_client.get(f"/api/v1/sources/{uuid.uuid4()}/snapshot")
    assert resp.status_code == 404
    assert resp.json()["code"] == "404001"


async def test_snapshot_source_without_snapshot_404002(async_client: Any, migrated_db: Any) -> None:
    """深度来源未抓原文（snapshot_id NULL）→ 404002（快速模式来源同码——§0:40）。"""
    _, turn_id = await _seed_turn()
    source_id = await _seed_source(turn_id, url="https://a.example/1")
    resp = await async_client.get(f"/api/v1/sources/{source_id}/snapshot")
    assert resp.status_code == 404
    assert resp.json()["code"] == "404002"


async def test_snapshot_200_seven_keys_live_minio(
    async_client: Any,
    migrated_db: Any,
) -> None:
    """快照查看 200（真 MinIO 活体——test_save_snapshot_live 同款纪律）：
    data 七键逐项（snapshot_id/url/content_type/size_bytes/fetched_at/download_url
    预签名/excerpt 前 2000 字符）；回填链路联动（source.snapshot_id 置位后可查）。
    cleanup：对象 remove + 行 delete（thread/turn 归 migrated_db 截断面）。"""
    import importlib

    from app.agent.tools.save_snapshot import save_snapshot
    from app.db.models.snapshot import Snapshot
    from app.db.models.source import Source
    from app.db.session import get_session_factory
    from app.core.config import minio_settings

    if not minio_settings.access_key.get_secret_value() or not minio_settings.secret_key.get_secret_value():
        pytest.skip("缺 MINIO_ 凭据（.env.test MINIO_ACCESS_KEY/MINIO_SECRET_KEY）——活体用例跳过")

    thread_id, turn_id = await _seed_turn()
    source_id = await _seed_source(turn_id, url="https://a.example/1")
    content = "# 快照正文\n\n" + "正文内容。" * 1000  # > 2000 字符——excerpt 截断可断言
    bucket = minio_settings.bucket_snapshot

    meta = await save_snapshot(
        url="https://a.example/1",
        content=content,
        user_id=str(_user_uuid()),
        thread_id=thread_id,
        turn_id=turn_id,
    )
    try:
        # 回填接线联动：source.snapshot_id 置位（生产路径=turn_service 回填面）
        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(Source).where(Source.id == uuid.UUID(source_id)))
            ).scalar_one()
            row.snapshot_id = uuid.UUID(str(meta["snapshot_id"]))
            await session.commit()

        resp = await async_client.get(f"/api/v1/sources/{source_id}/snapshot")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "0"
        data = body["data"]
        assert set(data) == {
            "snapshot_id",
            "url",
            "content_type",
            "size_bytes",
            "fetched_at",
            "download_url",
            "excerpt",
        }
        assert data["snapshot_id"] == str(meta["snapshot_id"])
        assert data["url"] == "https://a.example/1"
        assert data["content_type"] == "text/markdown"
        assert data["size_bytes"] == len(content.encode("utf-8"))
        assert data["fetched_at"]
        # 预签名：10 分钟有效期（§5.1——X-Amz-Expires=600 查询参数形态）
        assert data["download_url"].startswith("http")
        assert "X-Amz-Expires=600" in data["download_url"]
        # 摘录：前 2000 字符（读取期口径）
        assert data["excerpt"] == content[:2000]
        assert len(data["excerpt"]) == 2000
    finally:
        minio = importlib.import_module("minio")
        client = minio.Minio(
            minio_settings.endpoint,
            access_key=minio_settings.access_key.get_secret_value(),
            secret_key=minio_settings.secret_key.get_secret_value(),
            secure=minio_settings.secure,
        )
        try:
            client.remove_object(bucket, str(meta["object_key"]))
        except Exception:  # noqa: BLE001 —— cleanup 幂等（对象缺失不炸）
            pass
        async with get_session_factory()() as session:
            # 清理序：先解除 sources.snapshot_id 引用（FK），再删快照行
            src = (
                await session.execute(select(Source).where(Source.id == uuid.UUID(source_id)))
            ).scalar_one_or_none()
            if src is not None:
                src.snapshot_id = None
            snap = (
                await session.execute(
                    select(Snapshot).where(Snapshot.id == uuid.UUID(str(meta["snapshot_id"])))
                )
            ).scalar_one_or_none()
            if snap is not None:
                await session.delete(snap)
            await session.commit()


# ==== sources.snapshot_id 回填（批次 2 注⑦闭合——turn_service 短事务面）====


async def test_backfill_source_snapshot_ids(migrated_db: Any) -> None:
    """回填面：state.snapshots（url→snapshot_id）→ sources.snapshot_id（只补空）；
    幂等（二次调用零新增）；非本轮 URL 忽略；畸形 snapshot_id 跳过不炸。
    生产形态下 snapshot 行先于回填存在（save_snapshot 先 commit 行、record_snapshot
    后落 state）——测试同构先播种 snapshots 行（sources.snapshot_id FK 面）。"""
    from app.db.models.snapshot import Snapshot
    from app.db.models.source import Source
    from app.db.session import get_session_factory
    from app.services.turn_service import backfill_source_snapshot_ids

    thread_id, turn_id = await _seed_turn()
    source_a = await _seed_source(turn_id, url="https://a.example/1", ref_no=1)
    source_b = await _seed_source(turn_id, url="https://b.example/2", ref_no=2)
    snap_a, snap_b = uuid.uuid4(), uuid.uuid4()
    factory = get_session_factory()
    async with factory() as session:
        session.add_all(
            [
                Snapshot(
                    id=snap_a,
                    user_id=_user_uuid(),
                    thread_id=uuid.UUID(thread_id),
                    turn_id=uuid.UUID(turn_id),
                    url="https://a.example/1",
                    url_hash="a" * 64,
                    object_key=f"snapshots/x/{snap_a}.md",
                    content_type="text/markdown",
                    size_bytes=100,
                    fetched_at=datetime.now(UTC),
                ),
                Snapshot(
                    id=snap_b,
                    user_id=_user_uuid(),
                    thread_id=uuid.UUID(thread_id),
                    turn_id=uuid.UUID(turn_id),
                    url="https://b.example/2",
                    url_hash="b" * 64,
                    object_key=f"snapshots/x/{snap_b}.md",
                    content_type="text/markdown",
                    size_bytes=100,
                    fetched_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()

    filled = await backfill_source_snapshot_ids(
        get_session_factory(),
        turn_id,
        [
            {"url": "https://a.example/1", "snapshot_id": str(snap_a)},
            {"url": "https://b.example/2", "snapshot_id": str(snap_b)},
            {"url": "https://other.example/9", "snapshot_id": str(uuid.uuid4())},  # 非本轮
            {"url": "https://c.example/3", "snapshot_id": "not-a-uuid"},  # 畸形跳过
        ],
    )
    assert filled == 2

    async with factory() as session:
        row_a = (
            await session.execute(select(Source).where(Source.id == uuid.UUID(source_a)))
        ).scalar_one()
        row_b = (
            await session.execute(select(Source).where(Source.id == uuid.UUID(source_b)))
        ).scalar_one()
        assert row_a.snapshot_id == snap_a and row_b.snapshot_id == snap_b

    # 幂等：二次调用零新增（只补空——ref_no/其余列零触碰）
    filled_again = await backfill_source_snapshot_ids(
        get_session_factory(),
        turn_id,
        [
            {"url": "https://a.example/1", "snapshot_id": str(uuid.uuid4())},
        ],
    )
    assert filled_again == 0
    async with factory() as session:
        row_a = (
            await session.execute(select(Source).where(Source.id == uuid.UUID(source_a)))
        ).scalar_one()
        assert row_a.snapshot_id == snap_a  # 首填值不被覆写


async def test_backfill_empty_or_malformed_snapshots_noop(migrated_db: Any) -> None:
    """快照账本空/非 list → 零操作（零事务面）；record_snapshot 落 state 的四键形态
    之外的残缺条目防御跳过。"""
    from app.db.session import get_session_factory
    from app.services.turn_service import backfill_source_snapshot_ids

    _, turn_id = await _seed_turn()
    factory = get_session_factory()
    assert await backfill_source_snapshot_ids(factory, turn_id, []) == 0
    assert await backfill_source_snapshot_ids(factory, turn_id, None) == 0
    assert await backfill_source_snapshot_ids(factory, turn_id, "bad") == 0
    assert await backfill_source_snapshot_ids(factory, turn_id, [{"url": "https://x"}]) == 0  # 缺 snapshot_id
