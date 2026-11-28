"""unit 层：B22 HITL 审批服务纯逻辑面（批次 2 第一段）。

覆盖点（契约锚点见各用例 docstring）：
- turn_approvals 行形状常量与 gate_payload.schema_version（data-model.md §1.20，
  R30 跨进程契约——常量存在且为 int，读不懂的版本置 failed 不猜）；
- 幂等判读纯函数 `_classify_repeat_decision`（api.md §2.13 幂等语义：同轮同
  choice 幂等 200 / 异 choice 或非 awaiting 409008 / 超时收口后人为裁决 409008
  ——「超时后再到的人为裁决 → 409008」具体条覆盖一般幂等条）；
- 超时扫描 cron 注册（app/crons/approval_timeout_sweep.py → CRON_TASKS，低频兜底，
  data-model.md §4 v1.23 启动钩子注「惰性判定 + 低频扫描兜底〔R32〕」）。
"""

import asyncio

import pytest

from app.crons import CRON_TASKS, CronTask
from app.db.models.approval import (
    APPROVAL_CHOICES,
    APPROVAL_STATUSES,
    GATE_PAYLOAD_SCHEMA_VERSION,
    VERDICTS,
)
from app.services.turn_service import _classify_repeat_decision, _resolve_on_timeout


def _task_by_name(name: str) -> CronTask:
    return next(task for task in CRON_TASKS if task.name == name)


# ---- 行形状常量（data-model.md §1.20）----


def test_approval_status_domain_matches_contract():
    """§1.20 status 六态逐字：pending|approved|expired|canceled|shadow|no_hit。"""
    assert APPROVAL_STATUSES == (
        "pending",
        "approved",
        "expired",
        "canceled",
        "shadow",
        "no_hit",
    )


def test_approval_verdict_and_choice_domains():
    """§1.20 verdict 两值与 choice 四值逐字（误弹窗标注集分子=has_hit∧web_only）。"""
    assert VERDICTS == ("has_hit", "no_hit")
    assert APPROVAL_CHOICES == ("kb_only", "web_only", "both", "custom")


def test_gate_payload_schema_version_is_int_contract_anchor():
    """R30：schema_version 为跨进程契约锚点——int 且 ≥1（阶段 1 写/阶段 2 读，
    版本不符→轮 failed+明确 error，不猜）。"""
    assert isinstance(GATE_PAYLOAD_SCHEMA_VERSION, int)
    assert GATE_PAYLOAD_SCHEMA_VERSION >= 1


# ---- 幂等判读纯函数（api.md §2.13 幂等语义）----


@pytest.mark.parametrize(
    ("recorded_status", "recorded_choice", "recorded_decided_by", "submitted", "expected"),
    [
        # 同轮同 choice 重复提交（多标签页/网络重试）→ 幂等成功
        ("approved", "kb_only", "user", "kb_only", "idempotent"),
        # 同轮不同 choice → 409008
        ("approved", "kb_only", "user", "web_only", "conflict"),
        # 超时收口后人为裁决（含同 choice）→ 409008——具体条覆盖一般幂等条
        ("expired", "both", "timeout", "both", "conflict"),
        ("expired", "both", "timeout", "kb_only", "conflict"),
        # 用户取消收口后任何再提交 → 409008
        ("canceled", None, None, "kb_only", "conflict"),
        # shadow/no_hit 留痕态与 pending 均不可再裁决
        ("shadow", None, None, "kb_only", "conflict"),
        ("no_hit", None, None, "kb_only", "conflict"),
        ("pending", None, None, "kb_only", "conflict"),
        # 审批行缺失（非门轮直接调用）→ 409008
        (None, None, None, "kb_only", "conflict"),
    ],
)
def test_classify_repeat_decision_matrix(
    recorded_status: str | None,
    recorded_choice: str | None,
    recorded_decided_by: str | None,
    submitted: str,
    expected: "str",
) -> None:
    assert (
        _classify_repeat_decision(
            recorded_status=recorded_status,
            recorded_choice=recorded_choice,
            recorded_decided_by=recorded_decided_by,
            submitted_choice=submitted,
        )
        == expected
    )


# ---- 超时扫描 cron 注册（低频兜底，R32）----


def test_approval_sweep_registered_in_cron_tasks():
    """approval_timeout_sweep 已注册：name/低频间隔/协程工厂三元组齐备。"""
    from app.crons.approval_timeout_sweep import APPROVAL_SWEEP_INTERVAL_SECONDS

    task = _task_by_name("approval_timeout_sweep")
    assert task.interval_seconds == APPROVAL_SWEEP_INTERVAL_SECONDS
    assert asyncio.iscoroutinefunction(task.coroutine_factory)


def test_approval_sweep_interval_is_low_frequency_fallback():
    """低频兜底口径：间隔为正且远小于 hitl.timeout_s 缺省 120s（惰性判定为主，
    扫描只兜「无人触发惰性判定」的窗口；data-model §4 v1.23）。"""
    from app.crons.approval_timeout_sweep import APPROVAL_SWEEP_INTERVAL_SECONDS

    assert isinstance(APPROVAL_SWEEP_INTERVAL_SECONDS, int)
    assert 0 < APPROVAL_SWEEP_INTERVAL_SECONDS <= 60


async def test_sweep_spawns_resumes_without_awaiting_completion():
    """评审遗留③（批次 5 清偿）：扫描器并发恢复——resume_turn（阶段 2 完整
    包络，可达百秒级）串行 await 会令一条慢恢复阻塞批内其余轮的阶段 2 启动与
    下一扫描周期（可用性缺口）。本用例钉死：扫描在恢复任务**启动后即返回**
    （不 await 恢复完成），两条恢复都被 spawn。"""
    import app.crons.approval_timeout_sweep as sweep_module
    from app.crons.approval_timeout_sweep import sweep_expired_approvals

    release = asyncio.Event()
    started = asyncio.Event()
    resumed: list[str] = []

    class _SlowScheduler:
        async def collect_expired_approvals(self, *, now=None):
            return [
                {
                    "turn_id": "t1",
                    "thread_id": "th",
                    "choice": "both",
                    "decided_by": "timeout",
                    "on_timeout": "hybrid",
                },
                {
                    "turn_id": "t2",
                    "thread_id": "th",
                    "choice": "both",
                    "decided_by": "timeout",
                    "on_timeout": "hybrid",
                },
            ]

        async def resume_turn(self, turn_id):
            resumed.append(turn_id)
            started.set()
            await release.wait()
            return {"turn_id": turn_id, "status": "running"}

    result = await asyncio.wait_for(
        sweep_expired_approvals(scheduler=_SlowScheduler()), timeout=2.0
    )
    assert result.collected_count == 2
    await asyncio.sleep(0.01)  # 让步一个循环 tick——create_task 的任务才开跑
    assert started.is_set()  # 恢复任务已启动（扫描返回前 spawn）
    release.set()
    pending = [t for t in sweep_module._ACTIVE_RESUME_TASKS if not t.done()]
    if pending:
        await asyncio.gather(*pending)
    assert sorted(resumed) == ["t1", "t2"]


async def test_sweep_resume_task_failure_swallowed():
    """单条恢复失败不中断（任务内 WARN，幂等可重入下轮自然重试——原口径保留）。"""
    from app.crons.approval_timeout_sweep import _resume_one

    class _BoomScheduler:
        async def resume_turn(self, turn_id):
            raise RuntimeError("boom")

    await _resume_one(_BoomScheduler(), {"turn_id": "t1"})  # 不抛即过


# ---- on_timeout 解析（补裁决尾巴：collect 读 spec 不再硬钉 hybrid）----


@pytest.mark.parametrize(
    "hitl_section, expected",
    [
        # 缺省（节缺失/on_timeout 键缺失）→ hybrid（答比丢弃好——hitl-OQ-13）
        (None, "hybrid"),
        ({}, "hybrid"),
        ({"on_timeout": "hybrid"}, "hybrid"),
        # cancel：用户未回应 → 静默收尾 canceled（nacos §3 hitl.on_timeout 枚举）
        ({"on_timeout": "cancel"}, "cancel"),
        # 非法值回落 hybrid（validator 拒收之外的运行面第二道防线）
        ({"on_timeout": "bogus"}, "hybrid"),
        ({"on_timeout": None}, "hybrid"),
        ({"on_timeout": 1}, "hybrid"),
    ],
)
def test_resolve_on_timeout_three_forms(
    hitl_section: dict | None, expected: str
) -> None:
    """on_timeout 解析三形态（nacos-config §3 hitl.on_timeout：hybrid 缺省/cancel/
    非法回落 hybrid——validator 枚举外拒收之外的运行面兜底）。"""
    assert _resolve_on_timeout(hitl_section) == expected


@pytest.mark.parametrize(
    "section,expected",
    [
        (None, 500),
        ({}, 500),
        ({"note_max_chars": 500}, 500),
        ({"note_max_chars": 100}, 100),
        ({"note_max_chars": 1000}, 1000),
        ({"note_max_chars": 50}, 500),  # 越域下界（validator 值域 100~1000 之外回落）
        ({"note_max_chars": 1001}, 500),  # 越域上界
        ({"note_max_chars": True}, 500),  # bool 防御
        ({"note_max_chars": "300"}, 500),  # 非整型
    ],
)
def test_note_limit_reads_configured_key(
    section: dict | None, expected: int
) -> None:
    """评审遗留④（批次 5 清偿）：hitl.note_max_chars 运行面读取——服务层真实
    消费该键（原实现硬编码 500 键不消费）；畸形/越域回落缺省 500（validator
    值域 ge=100 le=1000，nacos-config §3 v1.50；_resolve_on_timeout 同款兜底）。"""
    from app.services.turn_service import _note_limit

    assert _note_limit(section) == expected


def test_usage_collector_restore_totals_cross_phase():
    """评审 medium#3：restore_totals 注入前段累计（B22 两阶段跨阶段合计——plan
    §3.3「usage_collector 必须跨阶段累计」）；仅已知三键正整数生效、畸形键忽略。"""
    from app.core.observability import UsageCollector

    collector = UsageCollector()
    collector.restore_totals(
        {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}
    )
    assert collector.total() == {
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "total_tokens": 130,
    }
    # 畸形键/负值/布尔形态忽略
    collector.restore_totals({"prompt_tokens": -5, "total_tokens": True, "bogus": 9})
    assert collector.total()["prompt_tokens"] == 100
    assert collector.total()["total_tokens"] == 130
    # 二段累加（阶段 2 新增 LLM usage 叠加在前段之上）
    collector.restore_totals({"total_tokens": 70})
    assert collector.total()["total_tokens"] == 200
