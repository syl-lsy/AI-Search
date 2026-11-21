"""unit 层：ThreadService（app/services/thread_service.py）。

覆盖点（api.md §2.1~§2.4 / data-model.md §1.2 聚合列维护口径）：
- create_thread_with_turn：建线程（title=首问前 50 字符 REQ-006）+ 首问轮（status=pending
  瞬时态，调度/运行态归 TurnRunner——B2 下游任务）+ 聚合列初始化（turn_count/mode_summary/
  updated_at）；只 flush 不 commit（BaseService 事务铁律，假体 commit 计数显式红掉）；
- create_turn（追问）：线程归属校验（不存在/不属当前用户 → NotFoundException 404001）、
  parent_turn_id 自动挂载（线程内最近一深度轮 status∈{completed,degraded} 时 fast 追问
  自动挂，api.md §2.4/REQ-017；deep 追问不挂）、聚合列推进；
- list_threads：分页（limit/offset）+ mode 过滤（fast|deep|mixed）+ updated_at DESC，
  SQL 全参数绑定（编译后 params 断言，无字符串拼接）；
- get_thread_detail：骨架映射（api.md v1.10 §2.3：has_report=mode=deep 且 answer_md 非空、
  degraded=status==degraded）+ has_artifacts 真实投影（artifacts 表 EXISTS，单查询）；
- 归属校验语句绑定 user_id 参数（越权即 404001，不泄漏存在性）。

DB 走假会话（同 test_main.py _FakeSession 形态：记录语句与写调用、按调用序回放结果）——
零网络零真实库；真实库路径由 integration 层覆盖。
"""
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.sql.selectable import Select

from app.db.models import Thread, Turn
from app.exception import NotFoundException, ThreadBusyException
from app.exception.handler.errors import ERROR_MAP
from app.schemas.thread import ThreadCreate, ThreadListQuery, ThreadPatchRequest, TurnSkeleton
from app.schemas.turn import TurnCreate
from app.services.thread_service import ThreadService

USER_ID = "00000000-0000-4000-8000-000000000001"
OTHER_USER_ID = "00000000-0000-4000-8000-000000000099"
THREAD_ID = "7d2f0a1e-0000-4000-8000-000000000002"


# ---- 假体：最小异步会话（零网络，记录调用面 + 按序回放查询结果）----


class _FakeScalars:
    def __init__(self, rows: Sequence[object]) -> None:
        self._rows = list(rows)

    def all(self) -> list[object]:
        return list(self._rows)

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """最小异步会话假体：scalars/scalar 按测试排队顺序出队；add/flush/commit 只记录。

    commit/close 显式计数——service「只 flush 不 commit（事务边界在 get_db）」铁律
    在每个持久化用例里断言 commits == 0。
    """

    def __init__(self) -> None:
        self.added: list[object] = []
        self.statements: list[Select] = []
        self.executed: list[object] = []  # Core DML（DELETE …）语句面
        self.flushes = 0
        self.commits = 0
        self.closed = False
        self.scalars_queue: list[Sequence[object]] = []
        self.scalar_queue: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def add_all(self, objs: Sequence[object]) -> None:
        self.added.extend(objs)

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        self.closed = True

    async def scalars(self, statement: Select) -> _FakeScalars:
        self.statements.append(statement)
        rows = self.scalars_queue.pop(0) if self.scalars_queue else []
        return _FakeScalars(rows)

    async def scalar(self, statement: Select) -> object | None:
        self.statements.append(statement)
        return self.scalar_queue.pop(0) if self.scalar_queue else None


_UNSET = object()  # 缺省哨兵：区分「未传」（视为已读）与显式 None（未读）


def _make_thread(
    user_id: str = USER_ID,
    *,
    pinned: bool = False,
    read_at: object = _UNSET,
    archived_at: datetime | None = None,
    title: str = "首问标题",
    updated_at: datetime | None = None,
) -> Thread:
    now = datetime.now(UTC)
    return Thread(
        id=uuid.UUID(THREAD_ID),
        user_id=uuid.UUID(user_id),
        title=title,
        mode_summary="deep",
        turn_count=1,
        pinned=pinned,
        read_at=now if read_at is _UNSET else read_at,
        archived_at=archived_at,
        created_at=now,
        updated_at=updated_at or now,
    )


def _make_turn(
    thread_id: uuid.UUID,
    *,
    mode: str = "deep",
    status: str = "completed",
    answer_md: str | None = "# 报告",
    created_at: datetime | None = None,
    parent_turn_id: uuid.UUID | None = None,
) -> Turn:
    return Turn(
        id=uuid.uuid4(),
        thread_id=thread_id,
        user_id=uuid.UUID(USER_ID),
        mode=mode,
        status=status,
        query="q",
        chat_model_key="qwen",
        model_name="qwen3.5-4b-ollama",
        answer_md=answer_md,
        parent_turn_id=parent_turn_id,
        created_at=created_at or datetime.now(UTC),
    )


def _uuid_params(statement: Select) -> set[object]:
    """编译语句取绑定参数值集合（断言外部输入全参数绑定，非字符串拼接）。"""
    return set(statement.compile().params.values())


# ---- create_thread_with_turn：建线程 + 首问轮 + 聚合初始化 ----


async def test_create_thread_with_turn_persists_thread_and_first_turn():
    fake = _FakeSession()
    service = ThreadService(fake)

    result = await service.create_thread_with_turn(
        ThreadCreate(query="这是一个首问", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="qwen3.5-4b-ollama",
    )

    assert uuid.UUID(result.thread_id)  # 合法 UUID v4 字符串（api.md §0 ID 口径）
    assert uuid.UUID(result.turn_id)
    assert result.status == "pending"  # 创建瞬时态；调度/运行态归 TurnRunner（B2 下游）

    thread, turn = fake.added[0], fake.added[1]
    assert isinstance(thread, Thread) and isinstance(turn, Turn)
    assert thread.title == "这是一个首问"
    assert thread.turn_count == 1  # 聚合写：首问轮即 1
    assert thread.mode_summary == "fast"  # 聚合写：首轮模式
    assert thread.updated_at is not None  # 聚合写：创建即写 updated_at
    assert thread.user_id == uuid.UUID(USER_ID)
    assert turn.thread_id == thread.id
    assert turn.query == "这是一个首问"
    assert turn.mode == "fast"
    assert turn.chat_model_key == "qwen"  # 入参传入（解析归下一个任务）
    assert turn.model_name == "qwen3.5-4b-ollama"
    assert fake.flushes == 1
    assert fake.commits == 0  # 只 flush 不 commit


async def test_create_thread_with_turn_truncates_title_to_50_chars():
    fake = _FakeSession()
    service = ThreadService(fake)
    long_query = "长" * 80  # REQ-006：title=首问前 50 字符

    await service.create_thread_with_turn(
        ThreadCreate(query=long_query, mode="deep"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )

    thread = fake.added[0]
    assert thread.title == "长" * 50
    assert thread.mode_summary == "deep"


async def test_create_thread_with_turn_turn_aggregates_untouched_by_turn_side():
    """轮侧聚合不重复维护：turn_count/mode_summary 只写在线程侧（data-model §1.2 口径）。"""
    fake = _FakeSession()
    service = ThreadService(fake)
    await service.create_thread_with_turn(
        ThreadCreate(query="q", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )
    turn = fake.added[1]
    assert turn.query == "q" and turn.status == "pending"


# ---- create_turn（追问）：归属校验 / parent 挂载 / 聚合推进 ----


async def test_create_turn_unknown_thread_raises_not_found():
    fake = _FakeSession()
    fake.scalars_queue = [[]]  # 归属查询无行
    service = ThreadService(fake)

    with pytest.raises(NotFoundException) as excinfo:
        await service.create_turn(
            THREAD_ID,
            TurnCreate(query="追问", mode="fast"),
            user_id=USER_ID,
            chat_model_key="qwen",
            model_name="m",
        )
    # 404001（ERROR_MAP 注册处）：不存在与越权同码，不泄漏资源存在性（api.md §0 404001 行）
    assert ERROR_MAP[NotFoundException][0] == "404001"
    assert "线程不存在" in excinfo.value.message


async def test_create_turn_foreign_user_raises_not_found():
    fake = _FakeSession()
    fake.scalars_queue = [[]]  # user_id 过滤后无行（越权即不存在）
    service = ThreadService(fake)

    with pytest.raises(NotFoundException):
        await service.create_turn(
            THREAD_ID,
            TurnCreate(query="追问", mode="fast"),
            user_id=OTHER_USER_ID,
            chat_model_key="qwen",
            model_name="m",
        )
    # 归属语句同时绑定 thread_id 与 user_id（全参数绑定）
    assert uuid.UUID(THREAD_ID) in _uuid_params(fake.statements[0])
    assert uuid.UUID(OTHER_USER_ID) in _uuid_params(fake.statements[0])


async def test_create_turn_fast_after_completed_deep_attaches_parent():
    fake = _FakeSession()
    thread = _make_thread()
    deep_turn = _make_turn(thread.id, mode="deep", status="completed")
    fake.scalars_queue = [[thread], [deep_turn], ["deep"]]  # 归属 / 最近深度轮 / 既有模式
    service = ThreadService(fake)

    result = await service.create_turn(
        THREAD_ID,
        TurnCreate(query="那 CrewOS 呢？", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )

    turn = fake.added[-1]
    assert isinstance(turn, Turn)
    assert turn.parent_turn_id == deep_turn.id  # REQ-017：fast 追问自动挂深度轮
    assert turn.status == "pending"
    assert result.turn_id == str(turn.id)


async def test_create_turn_fast_after_degraded_deep_attaches_parent():
    fake = _FakeSession()
    thread = _make_thread()
    deep_turn = _make_turn(thread.id, mode="deep", status="degraded")
    fake.scalars_queue = [[thread], [deep_turn], ["deep"]]
    service = ThreadService(fake)

    await service.create_turn(
        THREAD_ID,
        TurnCreate(query="q", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )
    turn = fake.added[-1]
    assert isinstance(turn, Turn)
    assert turn.parent_turn_id == deep_turn.id  # degraded 深度轮同样可挂（api.md 180 行）


async def test_create_turn_fast_after_running_deep_skips_parent():
    fake = _FakeSession()
    thread = _make_thread()
    deep_turn = _make_turn(thread.id, mode="deep", status="running")
    fake.scalars_queue = [[thread], [deep_turn], ["deep"]]
    service = ThreadService(fake)

    await service.create_turn(
        THREAD_ID,
        TurnCreate(query="q", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )
    turn = fake.added[-1]
    assert isinstance(turn, Turn)
    assert turn.parent_turn_id is None  # 报告未完成不挂


async def test_create_turn_deep_followup_never_attaches_parent():
    fake = _FakeSession()
    thread = _make_thread()
    deep_turn = _make_turn(thread.id, mode="deep", status="completed")
    fake.scalars_queue = [[thread], ["deep"]]  # 归属 / 既有模式（无 parent 查询）
    service = ThreadService(fake)

    await service.create_turn(
        THREAD_ID,
        TurnCreate(query="再深挖一层", mode="deep"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )
    turn = fake.added[-1]
    assert isinstance(turn, Turn)
    assert turn.parent_turn_id is None  # 仅 fast 追问挂 parent（REQ-017）
    # 深度追问不触发 parent 查询：只有归属 + 模式聚合两条语句
    assert len(fake.statements) == 2


async def test_create_turn_updates_thread_aggregates_to_mixed():
    fake = _FakeSession()
    thread = _make_thread()  # 既有 1 轮 deep（mode_summary="deep", turn_count=1）
    old_updated_at = thread.updated_at
    fake.scalars_queue = [[thread], [], ["deep"]]  # 归属 / 无深度轮(fast 追问) / 既有模式
    service = ThreadService(fake)

    await service.create_turn(
        THREAD_ID,
        TurnCreate(query="q", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )

    assert thread.turn_count == 2  # 聚合推进
    assert thread.mode_summary == "mixed"  # deep + fast → mixed
    assert thread.updated_at > old_updated_at  # 聚合写 updated_at
    assert fake.commits == 0


async def test_create_turn_all_fast_thread_stays_fast():
    fake = _FakeSession()
    thread = _make_thread()
    thread.mode_summary = "fast"
    fake.scalars_queue = [[thread], [], ["fast"]]
    service = ThreadService(fake)

    await service.create_turn(
        THREAD_ID,
        TurnCreate(query="q", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )
    assert thread.mode_summary == "fast"  # 全 fast → fast


async def test_create_turn_invalid_thread_id_is_not_found():
    fake = _FakeSession()
    service = ThreadService(fake)
    with pytest.raises(NotFoundException):
        await service.create_turn(
            "not-a-uuid",  # 非法 UUID 按「不存在」处理（不泄漏解析细节）
            TurnCreate(query="q", mode="fast"),
            user_id=USER_ID,
            chat_model_key="qwen",
            model_name="m",
        )


# ---- list_threads：分页 + mode 过滤 + updated_at DESC ----


async def test_list_threads_returns_page_response():
    fake = _FakeSession()
    t1, t2 = _make_thread(), _make_thread()
    fake.scalar_queue = [42]  # count 总数
    fake.scalars_queue = [[t1, t2]]  # 当前页行
    service = ThreadService(fake)

    page = await service.list_threads(ThreadListQuery(limit=2, offset=0), user_id=USER_ID)

    assert page.total == 42
    assert [item.thread_id for item in page.items] == [str(t1.id), str(t2.id)]
    assert page.items[0].title == "首问标题"
    assert page.items[0].turn_count == 1
    # 计数与取页两条语句均绑定 user_id（user 域过滤，参数化）
    assert uuid.UUID(USER_ID) in _uuid_params(fake.statements[0])
    assert uuid.UUID(USER_ID) in _uuid_params(fake.statements[1])
    assert "DESC" in str(fake.statements[1])  # 列表按 updated_at DESC（REQ-006）


async def test_list_threads_mode_filter_is_bound_param():
    fake = _FakeSession()
    fake.scalar_queue = [0]
    fake.scalars_queue = [[]]
    service = ThreadService(fake)

    page = await service.list_threads(
        ThreadListQuery(mode="fast"), user_id=USER_ID
    )

    assert page.total == 0 and page.items == []
    assert "mode_summary" in str(fake.statements[1])
    assert "fast" in _uuid_params(fake.statements[1])  # 过滤值走绑定参数，非内联


async def test_list_threads_applies_limit_offset():
    fake = _FakeSession()
    fake.scalar_queue = [0]
    fake.scalars_queue = [[]]
    service = ThreadService(fake)

    await service.list_threads(ThreadListQuery(limit=10, offset=20), user_id=USER_ID)

    page_stmt = str(fake.statements[1])
    assert "LIMIT" in page_stmt and "OFFSET" in page_stmt
    params = fake.statements[1].compile().params
    assert 10 in params.values() and 20 in params.values()


# ---- get_thread_detail：骨架映射 + 分期口径 ----


async def test_get_thread_detail_maps_turn_skeletons():
    fake = _FakeSession()
    thread = _make_thread()
    base = datetime.now(UTC)
    deep_done = _make_turn(
        thread.id, mode="deep", status="completed", answer_md="# 报告", created_at=base
    )
    fast_failed = _make_turn(
        thread.id,
        mode="fast",
        status="failed",
        answer_md=None,
        created_at=base + timedelta(seconds=1),
    )
    fast_degraded = _make_turn(
        thread.id,
        mode="fast",
        status="degraded",
        answer_md="部分内容",
        created_at=base + timedelta(seconds=2),
    )
    deep_empty = _make_turn(
        thread.id,
        mode="deep",
        status="completed",
        answer_md=None,
        created_at=base + timedelta(seconds=3),
    )
    fake.scalars_queue = [[thread], [deep_done, fast_failed, fast_degraded, deep_empty]]
    service = ThreadService(fake)

    detail = await service.get_thread_detail(THREAD_ID, user_id=USER_ID)

    assert detail.thread_id == THREAD_ID
    assert detail.has_artifacts is False  # 无产物登记行（EXISTS 假）→ false
    assert detail.turn_count == 1
    assert [t.turn_id for t in detail.turns] == [
        str(deep_done.id),
        str(fast_failed.id),
        str(fast_degraded.id),
        str(deep_empty.id),
    ]
    assert all(isinstance(t, TurnSkeleton) for t in detail.turns)
    # api.md v1.10 §2.3：has_report = mode=deep 且 answer_md 非空（两条件缺一即 false）
    assert detail.turns[0].has_report is True  # deep + 有报告
    assert detail.turns[0].degraded is False
    assert detail.turns[1].has_report is False  # fast + 无产出
    assert detail.turns[2].has_report is False  # fast 有综合回答但非报告
    assert detail.turns[2].degraded is True  # status==degraded 推导
    assert detail.turns[3].has_report is False  # deep 但无产出


async def test_get_thread_detail_has_artifacts_true_when_artifact_rows_exist():
    """has_artifacts 真实投影（api.md §2.3）：EXISTS(artifacts WHERE thread_id=…) 为真
    → true；语句走单次标量查询（无 N+1），thread_id 参数绑定。"""
    fake = _FakeSession()
    thread = _make_thread()
    fake.scalars_queue = [[thread], []]
    fake.scalar_queue = [True]
    service = ThreadService(fake)

    detail = await service.get_thread_detail(THREAD_ID, user_id=USER_ID)

    assert detail.has_artifacts is True
    # 三次查询：线程归属 → turns 骨架 → artifacts EXISTS（读侧单查询，无逐轮探测）
    assert len(fake.statements) == 3
    exists_stmt = fake.statements[-1]
    compiled = str(exists_stmt)
    assert "EXISTS" in compiled.upper()
    assert "artifacts" in compiled
    assert uuid.UUID(THREAD_ID) in _uuid_params(exists_stmt)
    assert fake.commits == 0  # 只 flush 不 commit（读侧零写：线程已读）


async def test_get_thread_detail_has_artifacts_false_without_artifact_rows():
    """无产物登记行（EXISTS 假 + 标量返回 None 的伪体极端形态）→ false。"""
    fake = _FakeSession()
    thread = _make_thread()
    fake.scalars_queue = [[thread], []]
    fake.scalar_queue = [False]
    service = ThreadService(fake)

    detail = await service.get_thread_detail(THREAD_ID, user_id=USER_ID)

    assert detail.has_artifacts is False


async def test_get_thread_detail_unknown_thread_raises_not_found():
    fake = _FakeSession()
    fake.scalars_queue = [[]]
    service = ThreadService(fake)
    with pytest.raises(NotFoundException):
        await service.get_thread_detail(THREAD_ID, user_id=USER_ID)


# ---- v1.18 线程管理（B10）：列表参数/排序/投影（api.md §2.2）----


class _FakePurger:
    """对象清理假体（MinIO best-effort 前缀删除的注入缝）：记录调用面/按需抛错。"""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._exc = exc

    async def purge_thread_objects(self, *, user_id: str, thread_id: str) -> None:
        self.calls.append((user_id, thread_id))
        if self._exc is not None:
            raise self._exc


async def test_list_threads_default_active_view_filters_and_order():
    """active 缺省：archived_at IS NULL + (pinned DESC, updated_at DESC)（§2.2 v1.18）。"""
    fake = _FakeSession()
    fake.scalar_queue = [0]
    fake.scalars_queue = [[]]
    service = ThreadService(fake)

    await service.list_threads(ThreadListQuery(), user_id=USER_ID)

    page_stmt = str(fake.statements[1])
    assert "archived_at IS NULL" in page_stmt  # 缺省=活跃集（D-05）
    assert "pinned DESC" in page_stmt  # 活跃视图置顶优先（D-07）
    assert "updated_at DESC" in page_stmt
    assert "read_at IS NULL" not in page_stmt  # unread 缺省 false → 无未读过滤


async def test_list_threads_archived_view_orders_updated_only():
    """archived=true：仅归档集 + updated_at DESC（pinned 不参与，D-07）。"""
    fake = _FakeSession()
    fake.scalar_queue = [0]
    fake.scalars_queue = [[]]
    service = ThreadService(fake)

    await service.list_threads(ThreadListQuery(archived=True), user_id=USER_ID)

    page_stmt = str(fake.statements[1])
    assert "archived_at IS NOT NULL" in page_stmt
    assert "pinned DESC" not in page_stmt  # 归档视图无置顶语义


async def test_list_threads_unread_filter_is_orthogonal_to_archived():
    """unread=true=read_at IS NULL；与 archived 正交（服务端无组合特判，D-05/B-4）。"""
    fake = _FakeSession()
    fake.scalar_queue = [0]
    fake.scalars_queue = [[]]
    service = ThreadService(fake)

    await service.list_threads(ThreadListQuery(unread=True), user_id=USER_ID)
    unread_stmt = str(fake.statements[1])
    assert "read_at IS NULL" in unread_stmt
    assert "archived_at IS NULL" in unread_stmt  # 未读视图=活跃集内过滤

    fake.scalar_queue = [0]
    fake.scalars_queue = [[]]
    await service.list_threads(
        ThreadListQuery(archived=True, unread=True), user_id=USER_ID
    )
    both_stmt = str(fake.statements[3])
    assert "archived_at IS NOT NULL" in both_stmt and "read_at IS NULL" in both_stmt


async def test_list_threads_q_is_escaped_ilike_bind_param():
    """q：标题 ILIKE 包含匹配；%/_/\\ 通配转义（用户输入不得放大匹配面）。"""
    fake = _FakeSession()
    fake.scalar_queue = [0]
    fake.scalars_queue = [[]]
    service = ThreadService(fake)

    await service.list_threads(ThreadListQuery(q="Lang%_Graph\\x"), user_id=USER_ID)

    stmt = fake.statements[1]
    compiled = stmt.compile()
    assert "%Lang\\%\\_Graph\\\\x%" in set(compiled.params.values())
    assert "ESCAPE" in str(stmt).upper()


async def test_list_threads_q_and_mode_and_view_are_and_combined():
    """q 与 mode/archived/unread AND 叠加（三者各自独立条件，D-05）。"""
    fake = _FakeSession()
    fake.scalar_queue = [0]
    fake.scalars_queue = [[]]
    service = ThreadService(fake)

    await service.list_threads(
        ThreadListQuery(q="LangGraph", mode="fast", archived=True, unread=True),
        user_id=USER_ID,
    )
    where_text = str(fake.statements[1])
    assert "mode_summary" in where_text
    assert "archived_at IS NOT NULL" in where_text  # archived=true 独立条件
    assert "read_at IS NULL" in where_text
    assert "lower(threads.title) LIKE lower" in where_text  # ilike 走 lower 比较（大小写不敏感）


async def test_list_threads_brief_projection_three_booleans_four_quadrants():
    """ThreadBrief 三布尔投影四象限（D-06/B-6）：read_at/archived_at NULL 与非 NULL。"""
    fake = _FakeSession()
    now = datetime.now(UTC)
    read_active = _make_thread()
    unread_active = _make_thread(read_at=None)
    pinned_read_archived = _make_thread(pinned=True, archived_at=now)
    unread_archived = _make_thread(read_at=None, archived_at=now)
    fake.scalar_queue = [4]
    fake.scalars_queue = [[read_active, unread_active, pinned_read_archived, unread_archived]]
    service = ThreadService(fake)

    page = await service.list_threads(ThreadListQuery(archived=True), user_id=USER_ID)

    assert [(i.pinned, i.is_unread, i.archived) for i in page.items] == [
        (False, False, False),
        (False, True, False),
        (True, False, True),
        (False, True, True),
    ]


# ---- v1.18 详情置已读写副作用（api.md §2.3 v1.18 注 / D-12）----


async def test_get_thread_detail_marks_unread_thread_read():
    fake = _FakeSession()
    thread = _make_thread(read_at=None)  # 未读
    fake.scalars_queue = [[thread], []]
    service = ThreadService(fake)

    before = datetime.now(UTC)
    detail = await service.get_thread_detail(THREAD_ID, user_id=USER_ID)

    assert detail.thread_id == THREAD_ID
    assert thread.read_at is not None and thread.read_at >= before  # 同事务置 now()
    assert fake.flushes == 1
    assert fake.commits == 0  # 写副作用仍在 get_db 事务边界内


async def test_get_thread_detail_already_read_is_idempotent_zero_write():
    fake = _FakeSession()
    thread = _make_thread()
    original = thread.read_at
    fake.scalars_queue = [[thread], []]
    service = ThreadService(fake)

    await service.get_thread_detail(THREAD_ID, user_id=USER_ID)

    assert thread.read_at == original  # 已读零写（幂等）
    assert fake.flushes == 0


# ---- v1.18 PATCH /threads/{id}（api.md §2.10 / D-08/D-09）----


async def test_patch_thread_updates_all_fields_without_touching_updated_at():
    fake = _FakeSession()
    thread = _make_thread()
    original_updated_at = thread.updated_at
    fake.scalars_queue = [[thread]]
    service = ThreadService(fake)

    brief = await service.patch_thread(
        THREAD_ID,
        ThreadPatchRequest(title="  新标题  ", pinned=True, is_unread=True, archived=True),
        user_id=USER_ID,
    )

    assert thread.title == "新标题"  # trim 后落库
    assert thread.pinned is True
    assert thread.read_at is None  # is_unread=true → read_at NULL
    assert thread.archived_at is not None
    assert thread.updated_at == original_updated_at  # D-09 冻结语义：管理动作不推 updated_at
    assert (brief.title, brief.pinned, brief.is_unread, brief.archived) == (
        "新标题",
        True,
        True,
        True,
    )
    assert fake.flushes == 1
    assert fake.commits == 0


async def test_patch_thread_mark_read_and_unarchive():
    fake = _FakeSession()
    thread = _make_thread(read_at=None, archived_at=datetime.now(UTC))
    fake.scalars_queue = [[thread]]
    service = ThreadService(fake)

    brief = await service.patch_thread(
        THREAD_ID, ThreadPatchRequest(is_unread=False, archived=False), user_id=USER_ID
    )

    assert thread.read_at is not None and thread.read_at <= datetime.now(UTC)
    assert thread.archived_at is None
    assert brief.is_unread is False and brief.archived is False


async def test_patch_thread_unknown_or_foreign_thread_raises_not_found():
    fake = _FakeSession()
    fake.scalars_queue = [[]]  # id+user 双条件无行（不存在/越权同码）
    service = ThreadService(fake)

    with pytest.raises(NotFoundException):
        await service.patch_thread(
            THREAD_ID, ThreadPatchRequest(pinned=True), user_id=OTHER_USER_ID
        )
    # 归属语句同时绑定 thread_id 与 user_id（全参数绑定）
    params = _uuid_params(fake.statements[0])
    assert uuid.UUID(THREAD_ID) in params and uuid.UUID(OTHER_USER_ID) in params


# ---- v1.18 DELETE /threads/{id}（api.md §2.11 / D-03/D-04/D-10/D-11）----


async def test_delete_thread_rejects_running_turn_with_409005():
    fake = _FakeSession()
    thread = _make_thread()
    fake.scalars_queue = [[thread]]
    fake.scalar_queue = [1]  # 存在 running/queued 轮
    service = ThreadService(fake)

    with pytest.raises(ThreadBusyException) as excinfo:
        await service.delete_thread(THREAD_ID, user_id=USER_ID)

    assert ERROR_MAP[ThreadBusyException][0] == "409005"  # MRO 精确命中（非 500001 兜底）
    assert "进行中" in excinfo.value.message
    assert fake.executed == []  # 拒绝时零删除
    # 判定状态集与 409001 同源（pending/running/queued）——走绑定参数
    flat_params: set[object] = set()
    for value in fake.statements[1].compile().params.values():
        if isinstance(value, (list, tuple, set)):
            flat_params.update(value)
        else:
            flat_params.add(value)
    assert {"pending", "running", "queued"} <= flat_params


async def test_delete_thread_cascades_turns_then_thread_and_purges_objects():
    fake = _FakeSession()
    thread = _make_thread(archived_at=datetime.now(UTC))  # 归档态可直接删除
    fake.scalars_queue = [[thread]]
    fake.scalar_queue = [0]
    purger = _FakePurger()
    service = ThreadService(fake, object_purger=purger)

    result = await service.delete_thread(THREAD_ID, user_id=USER_ID)

    assert result.deleted is True
    assert len(fake.executed) == 2
    assert "DELETE FROM turns" in str(fake.executed[0])  # 先删 turns（借 FK CASCADE 清从属）
    assert "DELETE FROM threads" in str(fake.executed[1])  # 再删 threads（借 artifacts 链）
    assert purger.calls == [(USER_ID, THREAD_ID)]  # 对象键含 user_id/thread_id 前缀
    assert fake.commits == 0


async def test_delete_thread_object_purge_failure_is_best_effort(log_records):
    """D-04：快照对象清理失败只 WARN 不回滚（DB 删除照常成功）。"""
    fake = _FakeSession()
    thread = _make_thread()
    fake.scalars_queue = [[thread]]
    fake.scalar_queue = [0]
    purger = _FakePurger(exc=RuntimeError("minio down"))
    service = ThreadService(fake, object_purger=purger)

    result = await service.delete_thread(THREAD_ID, user_id=USER_ID)

    assert result.deleted is True
    assert len(fake.executed) == 2
    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert warnings, "对象清理失败必须落 WARN 日志（可观测）"


async def test_delete_thread_unknown_or_foreign_thread_raises_not_found():
    fake = _FakeSession()
    fake.scalars_queue = [[]]
    purger = _FakePurger()
    service = ThreadService(fake, object_purger=purger)

    with pytest.raises(NotFoundException):
        await service.delete_thread(THREAD_ID, user_id=USER_ID)
    assert fake.executed == [] and purger.calls == []


# ---- v1.18 追问自动取消归档（api.md §2.4 v1.18 注 / D-13）----


async def test_create_turn_on_archived_thread_unarchives():
    fake = _FakeSession()
    thread = _make_thread(archived_at=datetime.now(UTC))
    fake.scalars_queue = [[thread], [], ["deep"]]
    service = ThreadService(fake)

    await service.create_turn(
        THREAD_ID,
        TurnCreate(query="追问", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )

    assert thread.archived_at is None  # 对话活动解除归档（回到活跃列表）
    assert thread.turn_count == 2


async def test_create_turn_on_active_thread_leaves_archived_at_none():
    fake = _FakeSession()
    thread = _make_thread()
    fake.scalars_queue = [[thread], [], ["deep"]]
    service = ThreadService(fake)

    await service.create_turn(
        THREAD_ID,
        TurnCreate(query="追问", mode="fast"),
        user_id=USER_ID,
        chat_model_key="qwen",
        model_name="m",
    )
    assert thread.archived_at is None

