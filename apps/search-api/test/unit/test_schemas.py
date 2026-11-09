"""unit 层：schemas DTO 契约边界（api.md §0/§2.1~§2.5）。

覆盖点：
- ThreadCreate/TurnCreate 校验边界：query 1~2000 字符、mode 枚举必填、images ≤3 张、
  on_conflict 四策略枚举、incognito 严格布尔（DTO 是 400001「请求参数错误」的第一道
  防线——校验失败经 RequestValidationError → 全局处理器映射 400001）；
- 分页口径（api.md §0）：limit 默认 50 上限 200、offset 默认 0、列表响应含 total；
- 分期口径恒值（用户裁决 2026-11-08）：TurnDetail.incognito 恒 false / followups 恒 null、
  ThreadDetail.has_artifacts 恒 false——真实值分别自 M5（0005）/ 0009-B7 / 0002-M2 起；
  memory_hits 不在 TurnDetail（api.md §2.5「M5 及之前无此字段」，评审 P2 勘误移除）；
- TurnDetail/SourceBrief/StageBrief 与 api.md §2.5 示例形状一致（sources/stages/usage/error）。
"""
import pytest
from pydantic import ValidationError

from app.schemas.common import PageRequest, PageResponse, OnConflict, SearchMode, TurnStatus
from app.schemas.thread import (
    ThreadBrief,
    ThreadCreate,
    ThreadCreateResult,
    ThreadDetail,
    ThreadListQuery,
    TurnSkeleton,
)
from app.schemas.turn import (
    SourceBrief,
    StageBrief,
    TurnCreate,
    TurnDetail,
    TurnError,
    UsageBrief,
)

USER_ID = "00000000-0000-4000-8000-000000000001"


# ---- ThreadCreate：§2.1 请求体校验边界 ----


def test_thread_create_defaults():
    payload = ThreadCreate(query="LangGraph 的 Send API 和 Router 的区别？", mode="fast")
    assert payload.model is None  # 缺省用 Nacos default（键集合热更，DTO 不做静态枚举）
    assert payload.images is None  # M4 预留
    assert payload.incognito is False
    assert payload.on_conflict is None  # 缺省取 Agent Spec turn.default_on_conflict


def test_thread_create_accepts_full_payload():
    payload = ThreadCreate(
        query="q",
        mode="deep",
        model="qwen",
        images=["data:image/png;base64,aaa"],
        incognito=True,
        on_conflict="enqueue",
    )
    assert payload.mode == "deep"
    assert payload.model == "qwen"
    assert payload.images == ["data:image/png;base64,aaa"]
    assert payload.incognito is True
    assert payload.on_conflict == "enqueue"


def test_thread_create_rejects_empty_query():
    with pytest.raises(ValidationError):
        ThreadCreate(query="", mode="fast")


def test_thread_create_query_boundary_2000():
    ThreadCreate(query="q" * 2000, mode="fast")  # 恰 2000 合法
    with pytest.raises(ValidationError):
        ThreadCreate(query="q" * 2001, mode="fast")  # 超 2000 → 400001


def test_thread_create_mode_required_and_enum():
    with pytest.raises(ValidationError):
        ThreadCreate(query="q")  # 必填
    with pytest.raises(ValidationError):
        ThreadCreate(query="q", mode="auto")  # 非法枚举
    assert ThreadCreate(query="q", mode="fast").mode == "fast"
    assert ThreadCreate(query="q", mode="deep").mode == "deep"


def test_thread_create_images_max_3():
    assert len(ThreadCreate(query="q", mode="fast", images=["u"] * 3).images) == 3
    with pytest.raises(ValidationError):
        ThreadCreate(query="q", mode="fast", images=["u"] * 4)  # ≤3 张（REQ-025，M4 预留）


def test_thread_create_incognito_strict_bool():
    with pytest.raises(ValidationError):
        ThreadCreate(query="q", mode="fast", incognito="yes")  # 非布尔 → 400001 口径
    assert ThreadCreate(query="q", mode="fast", incognito=True).incognito is True


def test_thread_create_on_conflict_enum_no_fallback():
    for value in ("reject", "enqueue", "interrupt", "rollback"):
        assert ThreadCreate(query="q", mode="fast", on_conflict=value).on_conflict == value
    with pytest.raises(ValidationError):
        # 官方枚举无 fallback（api.md §0：口语 fallback 即 rollback）
        ThreadCreate(query="q", mode="fast", on_conflict="fallback")


# ---- TurnCreate：§2.4 请求体同构（继承 ThreadCreate）----


def test_turn_create_same_shape_as_thread_create():
    assert issubclass(TurnCreate, ThreadCreate)
    payload = TurnCreate(query="那 CrewOS 呢？", mode="fast", on_conflict="reject")
    assert payload.query == "那 CrewOS 呢？"
    assert payload.on_conflict == "reject"
    with pytest.raises(ValidationError):
        TurnCreate(query="", mode="fast")  # 校验边界同构


# ---- 分页口径（api.md §0：limit 默认 50 上限 200 + offset 默认 0）----


def test_page_request_defaults_and_bounds():
    query = PageRequest()
    assert query.limit == 50
    assert query.offset == 0
    assert PageRequest(limit=200, offset=100).limit == 200  # 恰上限合法
    with pytest.raises(ValidationError):
        PageRequest(limit=201)
    with pytest.raises(ValidationError):
        PageRequest(limit=0)
    with pytest.raises(ValidationError):
        PageRequest(offset=-1)


def test_page_response_carries_total_and_items():
    brief = ThreadBrief(
        thread_id="7d2f0a1e-0000-4000-8000-000000000002",
        title="t",
        mode_summary="fast",
        turn_count=1,
        created_at="2026-11-01T08:12:00Z",
        updated_at="2026-11-01T09:40:11Z",
    )
    page = PageResponse[ThreadBrief](total=42, items=[brief])
    assert page.total == 42
    assert page.items[0].thread_id == brief.thread_id


def test_thread_list_query_mode_filter_enum():
    query = ThreadListQuery()
    assert query.mode is None  # 缺省=全部
    assert ThreadListQuery(mode="mixed").mode == "mixed"
    with pytest.raises(ValidationError):
        ThreadListQuery(mode="archived")  # 一期无归档概念（api.md §2.2）


# ---- 响应 DTO：§2.2/§2.3/§2.5 形状 ----


def test_thread_brief_and_skeleton_shapes():
    brief = ThreadBrief(
        thread_id="7d2f0a1e-0000-4000-8000-000000000002",
        title="LangGraph 的 Send API 和 Router 的区别？",
        mode_summary="mixed",
        turn_count=3,
        created_at="2026-11-01T08:12:00Z",
        updated_at="2026-11-01T09:40:11Z",
    )
    assert (brief.thread_id, brief.mode_summary, brief.turn_count) == (
        "7d2f0a1e-0000-4000-8000-000000000002",
        "mixed",
        3,
    )
    skeleton = TurnSkeleton(
        turn_id="c31b9d74-0000-4000-8000-000000000001",
        mode="deep",
        status="completed",
        query="对比 LangGraph 与 CrewOS",
        query_lang="zh",
        has_report=True,
        degraded=False,
        duration_ms=152300,
        created_at="2026-11-01T08:12:00Z",
    )
    assert skeleton.status == "completed"
    assert skeleton.has_report is True
    with pytest.raises(ValidationError):
        TurnSkeleton(
            turn_id="c31b9d74-0000-4000-8000-000000000001",
            mode="deep",
            status="CANCELLED",  # 非法状态枚举
            query="q",
            query_lang=None,
            has_report=False,
            degraded=False,
            duration_ms=None,
            created_at="2026-11-01T08:12:00Z",
        )


def test_thread_create_result_shape():
    result = ThreadCreateResult(
        thread_id="7d2f0a1e-0000-4000-8000-000000000002",
        turn_id="c31b9d74-0000-4000-8000-000000000001",
        status="pending",
    )
    assert result.status == "pending"  # 创建瞬时态；running/queued 由调度层推进（B2 下游）


def test_thread_detail_has_artifacts_constant_false():
    detail = ThreadDetail(
        thread_id="7d2f0a1e-0000-4000-8000-000000000002",
        title="t",
        mode_summary="fast",
        turn_count=0,
        created_at="2026-11-01T08:12:00Z",
        updated_at="2026-11-01T09:40:11Z",
        turns=[],
    )
    # 分期口径：恒 false，0002-M2 起真实值（用户裁决 2026-11-08）
    assert detail.has_artifacts is False


def test_turn_detail_defaults_phased_fields():
    detail = TurnDetail(
        turn_id="c31b9d74-0000-4000-8000-000000000001",
        thread_id="7d2f0a1e-0000-4000-8000-000000000002",
        mode="fast",
        status="completed",
        query="q",
        query_lang="zh",
        created_at="2026-11-01T08:12:00Z",
    )
    # 分期口径（用户裁决 2026-11-08）：恒值——真实值自 M5 / 0009-B7 起；
    # memory_hits 不在 TurnDetail（api.md §2.5「M5 及之前无此字段」，评审 P2 勘误）
    assert detail.incognito is False
    assert detail.followups is None
    assert "memory_hits" not in TurnDetail.model_fields
    assert detail.rewritten_queries == []
    assert detail.sources == []
    assert detail.parent_turn_id is None
    assert detail.cancel_reason is None
    assert detail.export_available is False


def test_turn_detail_parses_contract_example_shape():
    """api.md §2.5 示例形状：sources/stages/usage/error 载荷逐字段可解析。"""
    detail = TurnDetail(
        turn_id="c31b9d74-0000-4000-8000-000000000001",
        thread_id="7d2f0a1e-0000-4000-8000-000000000002",
        mode="deep",
        status="completed",
        query="对比 LangGraph 与 CrewOS 做调研 Agent 的取舍",
        query_lang="zh",
        rewritten_queries=["LangGraph 调研 agent 架构"],
        answer_md="# 调研报告",
        sources=[
            {
                "source_id": "f00d0000-0000-4000-8000-000000000003",
                "ref_no": 1,
                "source_type": "web",
                "title": "LangGraph docs — Send API",
                "url": "https://docs.langchain.com/",
                "snippet": "Send lets you fan out",
                "score": 0.9123,
                "snapshot_available": True,
                "kb_doc_id": None,
            }
        ],
        stages=[
            {
                "stage_key": "plan",
                "status": "done",
                "label": "规划 4 个子课题",
                "started_at": "2026-11-01T08:12:01Z",
                "ended_at": "2026-11-01T08:12:04Z",
                "duration_ms": 3200,
                "extra": {"subtopics": 4},
            }
        ],
        degraded=False,
        usage={
            "prompt_tokens": 84210,
            "completion_tokens": 5320,
            "total_tokens": 89530,
            "cost_usd": 0.0812,
        },
        duration_ms=152300,
        export_available=True,
        created_at="2026-11-01T08:12:00Z",
        finished_at="2026-11-01T08:14:32Z",
    )
    assert detail.sources[0].ref_no == 1
    assert detail.sources[0].source_type == "web"
    assert detail.stages[0].extra == {"subtopics": 4}
    assert detail.usage is not None and detail.usage.total_tokens == 89530
    assert detail.export_available is True


def test_turn_detail_error_and_kb_source_payloads():
    detail = TurnDetail(
        turn_id="c31b9d74-0000-4000-8000-000000000001",
        thread_id="7d2f0a1e-0000-4000-8000-000000000002",
        mode="fast",
        status="failed",
        query="q",
        query_lang=None,
        error=TurnError(code="500201", message="全部搜索源不可用", stage_key="search"),
        sources=[
            {
                "source_id": "aa310000-0000-4000-8000-000000000004",
                "ref_no": 2,
                "source_type": "kb",
                "title": "内部设计文档.md",
                "url": None,
                "snippet": "KB 候选",
                "score": 0.8871,
                "snapshot_available": False,
                "kb_doc_id": "9a770000-0000-4000-8000-000000000005",
            }
        ],
        degraded_reason="source_failure",
        cancel_reason=None,
        created_at="2026-11-01T08:12:00Z",
    )
    assert detail.error is not None and detail.error.code == "500201"
    assert detail.sources[0].source_type == "kb"
    assert detail.sources[0].kb_doc_id == "9a770000-0000-4000-8000-000000000005"
    assert detail.degraded_reason == "source_failure"


def test_shared_literal_enums():
    assert SearchMode.__args__ == ("fast", "deep")  # type: ignore[attr-defined]
    assert OnConflict.__args__ == ("reject", "enqueue", "interrupt", "rollback")  # type: ignore[attr-defined]
    assert TurnStatus.__args__ == (  # type: ignore[attr-defined]
        "pending",
        "running",
        "queued",
        "completed",
        "degraded",
        "failed",
        "canceled",
    )


def test_usage_and_stage_brief_optional_fields():
    usage = UsageBrief()
    assert usage.prompt_tokens is None and usage.cost_usd is None
    stage = StageBrief(stage_key="write", status="running", label="撰写报告")
    assert stage.started_at is None
    assert stage.duration_ms is None
    assert stage.extra is None
    with pytest.raises(ValidationError):
        StageBrief(stage_key="unknown", status="done", label="x")  # stage_key 枚举（§2.5）
