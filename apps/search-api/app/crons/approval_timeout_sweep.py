"""HITL 审批超时扫描（低频兜底周期任务；B22/REQ-069，R32 审批死轮缓解）。

背景（data-model.md §4 v1.23 启动钩子注 / api.md §2.5 awaiting 语义注④）：审批
超时的主判定路径是**惰性判定**（approval 端点与 2.5 读取时点按 expires_at 判读），
本任务是**低频扫描兜底**——「用户永不回来/重启后无人触发惰性判定」的窗口里，必须
有人把轮从 awaiting_approval 推走，否则线程被永久占线（同线程单 in-flight 不变式）。

调度形态沿 crons 注册表先例（trace_cleanup.py / feishu_sync.py）：注册进 CRON_TASKS，
main.py lifespan 建常驻周期循环（立即首轮 → sleep(interval) 循环；异常记 CronLog.error
后进下个周期重试，不中断循环）。

频率口径（代码常量，配置三分法——环境无关纯逻辑值）：30s。上限约束 = 远小于
`hitl.timeout_s` 缺省 120s（nacos-config.md §3 hitl.timeout_s 值域 30~600）——
扫描只兜「无人触发惰性判定」的窗口，staleness 上界一个扫描间隔，等待体验面由
前端弹层倒计时（消费 expires_at）与服务端收口先后无感衔接（approval_resolved
事件 + 2.5 对账）。

收口语义（ThreadScheduler.collect_expired_approvals）：on_timeout=hybrid 缺省 →
choice=both（缺省策略值「两者都要」）+ decided_by=timeout + 审批行 status='expired'
+ 轮置 running（阶段 2 恢复调度归批次 2 后续段接线——恢复面就绪前本任务收口的轮
由恢复段按清单续跑，收口本身不丢状态）。失败语义：单行条件 UPDATE 落败（并发裁决
先行）跳过不中断批内其余；DB 故障由 lifespan 循环捕获记日志、下周期重试（幂等
可重入——重复扫描只对剩余 pending 到期行重复收口）。

导入边界：本模块 → turn_service 单向（turn_service 零依赖 crons，无环）——与
trace_cleanup 只依赖 core 同型的单向依赖面。
"""

from dataclasses import dataclass
from datetime import datetime

from app.core.logger import CronLog
from app.services.turn_service import ThreadScheduler, get_thread_scheduler

#: 扫描间隔（秒）——低频兜底（惰性判定为主，R32；见模块 docstring 频率口径）
APPROVAL_SWEEP_INTERVAL_SECONDS = 30

_TASK_NAME = "approval_timeout_sweep"  # 注册表 name（CronLog task 字段同源）


@dataclass(frozen=True)
class ApprovalSweepResult:
    """单轮扫描汇总：实际收口明细（turn_id/choice/decided_by 字典序列）。"""

    collected: tuple[dict[str, str], ...]

    @property
    def collected_count(self) -> int:
        return len(self.collected)


async def sweep_expired_approvals(
    scheduler: ThreadScheduler | None = None, *, now: datetime | None = None
) -> ApprovalSweepResult:
    """扫描收口全部到期审批行（幂等可重入；结构化结果落 CronLog）。

    Args:
        scheduler: ThreadScheduler（单测注入假体）；缺省取进程内单例
            get_thread_scheduler()（未启动 fail-fast——本任务只在 lifespan 内运行）。
        now: 当前时间（单测注入固定时钟）；透传 collect_expired_approvals。
    """
    if scheduler is None:
        scheduler = get_thread_scheduler()
    collected = await scheduler.collect_expired_approvals(now=now)
    # 阶段 2 恢复调度（plan §3.4——超时收口后按缺省策略续跑；单条失败不中断批内
    # 其余——恢复是幂等收敛型操作，残余下轮自然重试）
    for item in collected:
        try:
            await scheduler.resume_turn(item["turn_id"])
        except Exception as exc:  # noqa: BLE001 —— 单条恢复失败下轮重试
            CronLog(
                message=(
                    f"approval_timeout_sweep 阶段 2 恢复失败（下轮重试）："
                    f"turn={item['turn_id']} {type(exc).__name__}"
                ),
                task=_TASK_NAME,
            ).warning()
    result = ApprovalSweepResult(
        collected=tuple(dict(item) for item in collected)
    )
    log = CronLog(
        message=(
            f"approval_timeout_sweep 完成：收口 {result.collected_count} 条到期审批"
        ),
        task=_TASK_NAME,
        total=result.collected_count,
    )
    if result.collected_count:
        log.info()
    else:
        log.debug()
    return result
