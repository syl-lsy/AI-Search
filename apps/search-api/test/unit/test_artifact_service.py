"""unit 层：导出链/snapshot 域纯逻辑 helper（M2 批次 4——api.md §2.8/§5.1/§5.3
REQ-015/REQ-016；域服务 ORM 面由 test_export_chain.py integration 承载）。

钉住的语义（冻结契约 + 盘点钉点，逐条对应测试）：
- 导出门禁（api.md §0:43 409003 = 非深度轮、或轮次未到 completed/degraded）：
  **mode=deep ∧ answer_md 非空 ∧ status∈{completed,degraded}**——与 §2.5
  export_available 判据同源（turn_service.py:444-446 投影先例）；
- 文件名时间戳生成在**代码侧**（api.md §2.8 `report_{YYYYMMDD_HHmm}.md`——data-model
  §1.9 filename varchar(256) 契约格式；零用户输入拼接——安全面）；
- excerpt 读取期口径（save_snapshot.py:15-18 钉注：DDL 无正文列）＝对象正文前 2000
  字符（api.md §5.1）；
- 产物路径锚定（纵深防御）：rel_path 纯代码生成（threads/{thread_id}/report_*.md），
  resolve 后必须位于 SANDBOX_ARTIFACTS_DIR 之下——即便 DB rel_path 被污染也不逃逸
  （ Mimosa 安全面：目录穿越拒绝）；
- **同分钟跨轮碰撞 → 409003（P2-1，2026-11-20 终审收口）**：rel_path 分钟精度不含
  turn 维度——同线程不同轮同分钟导出会覆写物理文件、首产物行此后下载到第二轮内容；
  写文件/登记行**之前**查同 rel_path 且不同 turn_id 的既有 artifact 行，命中即
  `ExportNotAvailableException`（409003「不满足导出条件」，复用既有错误码）；同轮
  重复导出（同 turn_id）维持现状（物理覆写 + 按次登记，内容等价）。
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import sandbox_settings
from app.exception import ExportNotAvailableException
from app.exception.handler.errors import ERROR_MAP
from app.services import artifact_service
from app.services.artifact_service import (
    ArtifactService,
    _excerpt_of,
    _export_gate,
    _report_filename,
    _resolve_artifact_path,
)


def test_report_filename_matches_contract_format() -> None:
    """api.md §2.8 文件名契约 `report_{YYYYMMDD_HHmm}.md`（分钟精度、代码侧生成）。"""
    now = datetime(2026, 11, 20, 8, 14, 40, tzinfo=UTC)
    assert _report_filename(now) == "report_20261120_0814.md"


def test_report_filename_zero_padded() -> None:
    """月/日/时/分零填充（字典序＝时间序，产物列表稳定）。"""
    now = datetime(2026, 1, 2, 3, 4, 0, tzinfo=UTC)
    assert _report_filename(now) == "report_20260102_0304.md"


def test_export_gate_completed_deep_round_allowed() -> None:
    """deep + completed + answer 非空 → 可导出。"""
    assert _export_gate("deep", "completed", "# 报告") is True


def test_export_gate_degraded_round_allowed() -> None:
    """deep + degraded（completed|degraded 均可导出——api.md §0:43）。"""
    assert _export_gate("deep", "degraded", "# 降级报告") is True


def test_export_gate_fast_round_denied() -> None:
    """fast 轮 → 409003（非深度轮）。"""
    assert _export_gate("fast", "completed", "答案") is False


def test_export_gate_non_terminal_status_denied() -> None:
    """未完成（running/failed/canceled/queued/pending）→ 409003。"""
    for status in ("running", "failed", "canceled", "queued", "pending"):
        assert _export_gate("deep", status, "# 报告") is False, status


def test_export_gate_empty_answer_denied() -> None:
    """answer_md 空 → 409003（export_available 判据三条件同源）。"""
    assert _export_gate("deep", "completed", "") is False
    assert _export_gate("deep", "completed", None) is False


def test_excerpt_limits_to_2000_chars() -> None:
    """api.md §5.1 摘录＝开头 2000 字符（读取期口径）。"""
    content = "正" * 5000
    assert _excerpt_of(content) == "正" * 2000
    assert _excerpt_of("短正文") == "短正文"


def test_resolve_artifact_path_anchors_under_base(tmp_path: Path) -> None:
    """产物路径锚定（纵深防御）：合法 rel_path 解析到 base 下；穿越形态
    （../ 与绝对路径）拒绝（ValueError——零文件系统副作用）。"""
    base = tmp_path / "sandbox"
    base.mkdir()
    resolved = _resolve_artifact_path(base, "threads/t-1/report_20261120_0814.md")
    assert resolved == (base / "threads" / "t-1" / "report_20261120_0814.md").resolve()
    assert resolved.is_relative_to(base.resolve())

    with pytest.raises(ValueError):
        _resolve_artifact_path(base, "../escape.md")
    with pytest.raises(ValueError):
        _resolve_artifact_path(base, "threads/../../escape.md")  # .. 越出 base
    with pytest.raises(ValueError):
        _resolve_artifact_path(base, "/etc/passwd")


def test_artifact_service_has_no_commit() -> None:
    """service 只 flush 不 commit（铁律 10——事务边界在 get_db；源码级护栏：方法体
    零 commit 调用，回填面归 TurnRunner 短事务例外）。"""
    import inspect

    source = inspect.getsource(artifact_service)
    assert ".commit()" not in source


class _ColumnsResult:
    """scalars() 结果假体：first()/all() 两形态（Turn 归属取 first、碰撞查询取 all）。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _ScriptedSession:
    """按调用序回放结果批次的极简会话假体（零网络零真库）：export 路径按
    Turn 归属查询 → 同 rel_path 碰撞查询的顺序消费批次；add/flush 只记录。"""

    def __init__(self, *batches: list[Any]) -> None:
        self._batches = [list(batch) for batch in batches]
        self.added: list[Any] = []
        self.flushes = 0

    async def scalars(self, statement: Any) -> _ColumnsResult:
        assert self._batches, "scalars 调用次数超出预置批次（测试陈设错误）"
        return _ColumnsResult(self._batches.pop(0))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


def _deep_turn(*, answer_md: str = "# 报告\n\n正文。\n") -> SimpleNamespace:
    """最小 turns 行假体（导出路径触碰的字段集）。"""
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        mode="deep",
        status="completed",
        answer_md=answer_md,
    )


async def test_export_same_minute_cross_round_collision_409003(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同线程同分钟跨轮碰撞（同 rel_path 不同 turn_id）→ 409003「不满足导出条件」，
    且零物理覆写、零 artifact 行登记（判据在写文件/登记前生效）。"""
    base = tmp_path / "sandbox"
    monkeypatch.setattr(sandbox_settings, "artifacts_dir", str(base))
    turn = _deep_turn()
    other_turn_id = uuid.uuid4()
    session = _ScriptedSession([turn], [other_turn_id])
    service = ArtifactService(session)

    with pytest.raises(ExportNotAvailableException) as excinfo:
        await service.export_turn_report(str(turn.id), user_id=str(turn.user_id))

    assert excinfo.value.message == "不满足导出条件"
    assert ERROR_MAP[ExportNotAvailableException][0] == "409003"  # 复用既有错误码
    assert session.added == []  # 零行登记
    assert not any(base.rglob("report_*.md"))  # 零物理文件写入/覆写


async def test_export_same_turn_repeat_still_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同轮重复导出（同 turn_id）仍放行：物理文件覆写 + artifact 行按次登记
    （「生成新文件」语义由新行承载，内容等价）。"""
    base = tmp_path / "sandbox"
    monkeypatch.setattr(sandbox_settings, "artifacts_dir", str(base))
    turn = _deep_turn()
    session = _ScriptedSession([turn], [turn.id])
    service = ArtifactService(session)

    result = await service.export_turn_report(str(turn.id), user_id=str(turn.user_id))

    assert result.thread_id == str(turn.thread_id)
    assert result.artifact_type == "report"
    assert result.content_type == "text/markdown"
    assert len(session.added) == 1  # 本次登记一行（既有同轮行不算碰撞）
    assert session.added[0].turn_id == turn.id
    files = list((base / "threads" / str(turn.thread_id)).glob("report_*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == turn.answer_md
    assert session.flushes == 1
