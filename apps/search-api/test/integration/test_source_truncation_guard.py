"""integration 层：D2-01 sources 限长列截断护栏（探针实证缺陷修复，2026-11-21）。

缺陷事实（docs/perf-sampling-m2.md D2-01）：英文长标题来源（>512 字符，探针轮
ref_no=34）直落 sources 表 → asyncpg StringDataRightTruncationError（title
varchar(512) 列宽超限）→ 组装段 INSERT 崩溃 → 轮兜底 degraded/failed(500001)
（该轮 33 来源已集、thinking/token 零产出）。

修复口径（用户裁决：当批即修·截断护栏形态，零迁移零契约升版）：TurnRunner 落库
前按 DDL 列宽截断（截断不丢行、ref_no/[n] 稳定、其余列值不变）——用例两件：
- **DB 拒收事实钉**（护栏必要性事实源，修复前后恒真）：裸 Source 行 >512 title
  直插 → StringDataRightTruncationError——钉 sources 列宽契约面（varchar(512)
  计字符），护栏必须在其上游；
- **TurnRunner 落库面护栏**（红→绿主断言面）：脚本假图发射超长标题 sources
  batch（真库真 INSERT）→ 轮 completed（修复前=failed(500001) 兜底）、行截断
  落库、ref_no/其余列值不变、turn_events payload 与落库行同源截断。
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.requires_db

_LONG_TITLE = "A" * 600  # 英文长标题（探针 D2-01 形态，>512 字符）

_LONG_SOURCE: dict[str, Any] = {
    "source_id": "11111111-1111-4111-8111-111111111111",  # 占位（TurnRunner 以行 id 回写）
    "ref_no": 1,
    "source_type": "web",
    "title": _LONG_TITLE,
    "url": "https://stub.example/long-title",
    "snippet": "D2-01 超长标题来源片段。",
    "score": 0.91,
    "snapshot_available": False,
    "kb_doc_id": None,
}

_NORMAL_SOURCE: dict[str, Any] = {
    "source_id": "11111111-1111-4111-8111-111111111112",
    "ref_no": 2,
    "source_type": "web",
    "title": "正常标题来源",
    "url": "https://stub.example/normal",
    "snippet": "对照卡片段。",
    "score": 0.72,
    "snapshot_available": False,
    "kb_doc_id": None,
}

_ANSWER = "答案[1][2]。"


class _LongTitleSourceGraph:
    """脚本假图：发射含 >512 字符英文长标题的 sources batch（D2-01 形态）+ write 段。

    形态对齐 stub_flow.FakeScriptedGraph（§7.4 快速主干）：sources 走 custom 流、
    token 走 messages 流（metadata.langgraph_node="synthesis"）。零 LLM/零真源。
    """

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        from langchain_core.messages import AIMessageChunk

        async def gen():
            yield (("fast:stub",), "custom",
                   {"type": "stage", "stage_key": "rewrite", "status": "done"})
            yield (("fast:stub",), "custom", {
                "type": "sources",
                "batch": [dict(_LONG_SOURCE), dict(_NORMAL_SOURCE)],
                "total": 2,
            })
            yield (("fast:stub",), "custom",
                   {"type": "stage", "stage_key": "write", "status": "running", "label": "综合中"})
            yield ((), "messages", (AIMessageChunk(content=_ANSWER),
                                    {"langgraph_node": "synthesis"}))
            yield (("fast:stub",), "custom",
                   {"type": "stage", "stage_key": "write", "status": "done"})
            yield ((), "updates", {
                "write": {
                    "answer_md": _ANSWER,
                    "rewritten_queries": ["D2-01 桩改写 query"],
                    "degraded": False,
                },
            })

        return gen()


def _spec_snapshot() -> Any:
    """Agent Spec 快照假体（followups 显式关闭——图外钩子零 LLM 依赖）。"""
    from app.config_center.cache import AssetSnapshot

    return AssetSnapshot(
        data_id="ai_search.agent.spec.json",
        version=1,
        md5="m",
        updated_at="t",
        payload={
            "turn": {"default_on_conflict": "reject"},
            "fast": {"turn_timeout_s": 30},
            "deep": {"turn_timeout_s": 600},
            "followups": {
                "enabled": False,
                "count": 5,
                "generate_timeout_ms": 8000,
                "sse_hold_window_ms": 10000,
            },
        },
    )


def _mapping_snapshot() -> Any:
    from app.config_center.cache import AssetSnapshot

    return AssetSnapshot(
        data_id="ai_search.model.mapping.json",
        version=1,
        md5="m",
        updated_at="t",
        payload={
            "chat": {
                "options": {"qwen": "qwen3.5-4b-ollama", "glm": "glm-4.7"},
                "default": "qwen",
            },
        },
    )


def _has_string_data_right_truncation(exc: BaseException) -> bool:
    """异常链上是否存在 asyncpg StringDataRightTruncationError（SQLAlchemy 可能包一层）。"""
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            return False
        if type(current).__name__ == "StringDataRightTruncationError":
            return True
        current = current.__cause__ or current.__context__


async def _seed_thread_turn(*, status: str = "running") -> tuple[str, str]:
    """播种 thread + turn 行（ORM 短事务——test_export_chain 同款行播种直驱口径）。"""
    from app.core.config import common_settings
    from app.db.models import Thread, Turn
    from app.db.session import get_session_factory

    user_uuid = uuid.UUID(common_settings.default_user_id)
    thread_uuid, turn_uuid = uuid.uuid4(), uuid.uuid4()
    factory = get_session_factory()
    async with factory() as session:
        session.add(Thread(id=thread_uuid, user_id=user_uuid, title="D2-01 截断护栏实测"))
        await session.flush()
        session.add(
            Turn(
                id=turn_uuid,
                thread_id=thread_uuid,
                user_id=user_uuid,
                mode="fast",
                status=status,
                query="D2-01 超长标题来源实测",
                chat_model_key="qwen",
                model_name="seed",
            )
        )
        await session.commit()
    return str(thread_uuid), str(turn_uuid)


async def _fetch_turn_row(turn_id: str) -> Any:
    from app.db.models import Turn
    from app.db.session import get_session_factory
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            select(Turn).where(Turn.id == uuid.UUID(turn_id))
        )).scalars().first()
        assert row is not None
        return row


async def _turn_row_by_id(turn_id: str) -> dict[str, Any] | None:
    from app.db.models import Turn
    from app.db.session import get_session_factory
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            select(Turn).where(Turn.id == uuid.UUID(turn_id))
        )).scalars().first()
        if row is None:
            return None
        return {"status": row.status, "answer_md": row.answer_md, "error": row.error}


# ---- DB 拒收事实钉（护栏必要性事实源；修复前后恒真）----


async def test_raw_overlong_title_insert_rejected_by_db(migrated_db) -> None:
    """裸 Source 行 >512 title 直插 → asyncpg StringDataRightTruncationError。

    钉 sources.title varchar(512) 列宽契约面（data-model.md §1.5，PG varchar(n)
    计字符）：任何不经截断护栏的落库路径在该形态下必崩——护栏必须在行构造上游。
    """
    from app.core.config import common_settings
    from app.db.models import Source
    from app.db.session import get_session_factory

    _, turn_id = await _seed_thread_turn()
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            Source(
                id=uuid.uuid4(),
                turn_id=uuid.UUID(turn_id),
                user_id=uuid.UUID(common_settings.default_user_id),
                ref_no=1,
                source_type="web",
                title=_LONG_TITLE,
                url="https://stub.example/long-title",
                snippet="片段",
            )
        )
        with pytest.raises(Exception) as exc_info:
            await session.commit()
    assert _has_string_data_right_truncation(exc_info.value), (
        f"期望 StringDataRightTruncationError（title varchar(512) 超限），"
        f"实际={type(exc_info.value).__name__}"
    )


# ---- TurnRunner 落库面护栏（红→绿主断言面）----


async def test_overlong_title_source_persist_truncated(
    migrated_db, b2_runtime, log_records
) -> None:
    """脚本假图发射超长标题 sources batch（真库真 INSERT）→ 截断落库轮正常收束。

    修复前该形态=组装段 INSERT 崩溃 → _fail_turn(500001) 兜底（探针 D2-01 实证，
    真库红）；修复后：轮 completed、两行落库（截断不丢行）、ref_no/[n] 不变、
    其余列值不变、turn_events payload 与落库行同源截断、AgentLog warning 留痕。
    """
    from sqlalchemy import select

    from app.agent.tools.registry import ToolRegistry
    from app.db.models import Source, TurnEvent
    from app.db.session import get_session_factory
    from app.services.turn_service import ThreadScheduler

    _, turn_id = await _seed_thread_turn()
    turn_row = await _fetch_turn_row(turn_id)
    scheduler = ThreadScheduler(
        bus=b2_runtime.bus,
        session_factory=get_session_factory(),
        graph=_LongTitleSourceGraph(),
        spec_snapshot=_spec_snapshot(),
        mapping_snapshot=_mapping_snapshot(),
        tools_registry=ToolRegistry(),  # 裸 registry：假图零检索零触网
    )

    await scheduler._execute_turn(turn_row)

    final = await _turn_row_by_id(turn_id)
    assert final is not None
    assert final["status"] == "completed", (
        f"截断护栏下轮应正常收束（修复前=failed(500001) 兜底），实际={final}"
    )
    assert final["answer_md"] == _ANSWER
    # 落库面：截断不丢行 + 编号稳定 + 其余列值零改动（真库读回）
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(
            select(Source)
            .where(Source.turn_id == uuid.UUID(turn_id))
            .order_by(Source.ref_no.asc())
        )).scalars().all()
        events = (await session.execute(
            select(TurnEvent)
            .where(TurnEvent.turn_id == uuid.UUID(turn_id))
            .order_by(TurnEvent.seq.asc())
        )).scalars().all()
    assert [r.ref_no for r in rows] == [1, 2]
    long_row, normal_row = rows
    assert long_row.title == "A" * 512  # 按列宽截断（varchar(512) 计字符）
    assert long_row.source_type == "web"
    assert long_row.url == _LONG_SOURCE["url"]
    assert long_row.snippet == _LONG_SOURCE["snippet"]
    assert str(long_row.score) == "0.9100"  # numeric(6,4)
    assert normal_row.title == _NORMAL_SOURCE["title"]  # 未超宽列零改动
    assert normal_row.url == _NORMAL_SOURCE["url"]
    # 事件面：turn_events sources payload 与落库行同源截断（§7.2 三面可对账）
    sources_payload = next(e.payload for e in events if e.event_type == "sources")
    assert sources_payload["batch"][0]["title"] == "A" * 512
    assert sources_payload["batch"][1]["title"] == _NORMAL_SOURCE["title"]
    done_payload = next(e.payload for e in events if e.event_type == "done")
    assert done_payload["source_count"] == 2
    assert done_payload["finish"] == "completed"
    # 截断留痕：AgentLog warning（列名+原长/截后长；零被截内容回显）
    clamp_warnings = [
        r for r in log_records
        if r["level"].name == "WARNING" and "sources 限长列截断" in str(r["message"])
    ]
    assert clamp_warnings, "截断发生须落 AgentLog WARNING 留痕"
    assert any("column=title" in str(r["message"]) for r in clamp_warnings)
    assert all("AAAAA" not in str(r["message"]) for r in clamp_warnings)


# ---- 日志捕获 sink（integration conftest 未内置，test_config_center 同款就地定义）----


@pytest.fixture
def log_records() -> Iterator[list[Any]]:
    from loguru import logger

    records: list[Any] = []
    sink_id = logger.add(
        lambda message: records.append(message.record), format="{message}", level=0
    )
    yield records
    try:
        logger.remove(sink_id)
    except ValueError:  # setup_logging 已 logger.remove() 全清（sink 不存在属预期）
        pass
