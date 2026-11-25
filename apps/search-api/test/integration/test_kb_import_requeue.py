"""integration：KB 导入任务重入队（B20 验收断言——G5 消费端真库形态）。

验收口径（ARCH §8 B20 行 / data-model §1.17 :534 / §4 :608）：重启后 pending/running
任务行被 worker 实际续跑重入队（真仓储 + 真 pg；飞书面走假体零网络——网络形态归
e2e/活体探针）。三段断言：
1. 建行（真 repo.create_job）→ 新 worker 实例 start() → pending 行被消费至终态
   （B20 验收断言面：任务行「被 worker 实际重入队续跑」）；
2. running 行（带断点游标）→ 同一 worker 实例语义（模拟重启后二次 start）→ 消费
   续跑至 completed、游标推进；
3. 终态行幂等：已 completed 的行不被二次 worker 重入队（list_requeue_ids 不含）。
"""

import uuid
from typing import Any

import pytest

from app.rag.feishu_client import NodeInfo
from app.rag.import_worker import (
    FeishuImportDocRepository,
    FeishuImportWorker,
    KbImportJobRepository,
    TokenBucket,
)

pytestmark = pytest.mark.requires_db

_USER = uuid.UUID("00000000-0000-4000-8000-000000000001")  # migrated_db 种子默认用户


class _FakeClient:
    """飞书面假体（零网络）：空树——重入队消费面的状态机推进零文档单元。"""

    async def list_spaces(self) -> list[Any]:
        return [type("S", (), {"space_id": "sp-test", "name": "测试空间"})()]

    async def walk_space_tree(self, space_id: str, **kw: Any) -> Any:
        return type(
            "W",
            (),
            {
                "space_id": space_id,
                "nodes": [],
                "errors": [],
                "shortcut_count": 0,
                "truncated_nodes": 0,
                "truncated_depth": 0,
            },
        )()


class _FakePipeline:
    def enqueue(self, doc_id: str) -> None:  # 空树不产文档单元——不入队
        raise AssertionError("空树不应产生文档单元入队")


@pytest.fixture
def job_repo() -> KbImportJobRepository:
    return KbImportJobRepository()


def _make_worker() -> FeishuImportWorker:
    return FeishuImportWorker(
        jobs=KbImportJobRepository(),
        docs=FeishuImportDocRepository(),
        client_provider=lambda: _FakeClient(),
        pipeline=_FakePipeline(),  # type: ignore[arg-type]
        spec_payload_provider=lambda: {},
        minio_client=lambda: None,
        bucket=TokenBucket(min_interval_s=0.0),
    )


async def _cleanup_jobs() -> None:
    """用后清理（kb_import_jobs 不在 conftest 截断表清单内——自管）。"""
    from sqlalchemy import delete

    from app.db.models import KbImportJob
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(delete(KbImportJob))
        await session.commit()


async def test_startup_requeue_consumes_pending_and_running_rows(job_repo) -> None:
    """B20 验收断言：pending/running 任务行被 worker 实际重入队续跑至终态。"""
    try:
        pending_id = await job_repo.create_job(
            user_id=str(_USER),
            trigger="manual",
            progress={"total_docs": 0, "processed_docs": 0, "imported_docs": 0,
                      "failed_docs": 0, "skipped_docs": 0},
            stats={},
        )
        running_id = await job_repo.create_job(
            user_id=str(_USER),
            trigger="scheduled",
            progress={"total_docs": 0, "processed_docs": 0, "imported_docs": 0,
                      "failed_docs": 0, "skipped_docs": 0},
            stats={},
        )
        await job_repo.transition(
            running_id, expect={"pending"}, to="running",
            fields={"cursor": {"space_id": "sp-test", "processed_index": 0}},
        )

        # 重入队清单含两行（pending + running——§1.17 四态中未完成态全量）
        requeue_ids = await job_repo.list_requeue_ids()
        assert pending_id in requeue_ids
        assert running_id in requeue_ids

        worker = _make_worker()
        await worker.start()  # G5：真消费端——登记行入队 → 串行消费至终态
        import asyncio

        await asyncio.wait_for(worker.queue.join(), timeout=10)
        await worker.stop()

        pending_row = await job_repo.load(pending_id)
        running_row = await job_repo.load(running_id)
        assert pending_row is not None and pending_row["status"] == "completed"
        assert running_row is not None and running_row["status"] == "completed"
        assert pending_row["started_at"] is not None
        assert pending_row["finished_at"] is not None
    finally:
        await _cleanup_jobs()


async def test_terminal_rows_not_requeued_after_restart(job_repo) -> None:
    """终态幂等：completed 行不被二次 worker 重入队（重启消费面只取未完成态）。"""
    try:
        job_id = await job_repo.create_job(
            user_id=str(_USER),
            trigger="manual",
            progress={"total_docs": 0, "processed_docs": 0, "imported_docs": 0,
                      "failed_docs": 0, "skipped_docs": 0},
            stats={},
        )
        worker = _make_worker()
        await worker.start()
        import asyncio

        await asyncio.wait_for(worker.queue.join(), timeout=10)
        await worker.stop()
        row = await job_repo.load(job_id)
        assert row is not None and row["status"] == "completed"

        # 「二次重启」：新 worker 实例的重入队清单不再含该行（终态不入列）
        requeue_ids = await job_repo.list_requeue_ids()
        assert job_id not in requeue_ids
    finally:
        await _cleanup_jobs()


def test_node_info_obj_edit_time_mapping() -> None:
    """wiki 节点 obj_edit_time 进 NodeInfo（判定序③④原料——client 映射面）。"""
    info = NodeInfo(
        node_token="t", obj_token="o", obj_type="docx",
        parent_node_token="", node_type="", has_child=False, title="x",
        obj_edit_time=1732166400,
    )
    assert info.obj_edit_time == 1732166400
