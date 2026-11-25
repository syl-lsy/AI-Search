"""定时飞书同步（M3/B20 B 线③——api.md §4.7 同步 MVP 定时形态；C4=(a)+G7）。

周期体（每 24h，代码常量——api.md §4.7 钉注「间隔 24h（代码常量，不入 Nacos）」）：
- 撞进行中任务（任意 pending/running）→ 本轮跳过不建行（G7）；
- diff 零变更且无删除 → 不建行（仅日志）；仅删除（C5 差集非空）也建行
  （trigger=scheduled、建行五数为 0）；
- 判定侧不重拉内容（content_hash 兜底判据在 worker 执行期精确判定）；
- 建行后经 worker 单例 enqueue 立即消费（与手动触发同缝——ADR-011 图外组件）。

失败口径：本轮任何异常（网络/凭据）沿注册表语义「本周期放弃、下周期重试」
（_cron_loop 统一捕获记 CronLog）；scheduled_sync_once 内部已收口凭据缺失面。
"""

from app.crons import CronTask
from app.rag.feishu_client import get_default_feishu_client
from app.rag.import_worker import (
    SYNC_INTERVAL_SECONDS,
    FeishuImportDocRepository,
    KbImportJobRepository,
    get_import_worker,
    scheduled_sync_once,
)

#: 24h 间隔（api.md §4.7 逐字——代码常量；crons 注册表消费）
FEISHU_SYNC_INTERVAL_SECONDS = SYNC_INTERVAL_SECONDS


async def run_feishu_sync() -> None:
    """定时全量 diff 单轮：判定 → 建行（仅当有变更/删除）→ worker 入队消费。"""
    created = await scheduled_sync_once(
        jobs=KbImportJobRepository(),
        docs=FeishuImportDocRepository(),
        client_provider=get_default_feishu_client,
        enqueue=lambda job_id: get_import_worker().enqueue(job_id),
    )
    if created == 0:
        return  # 零建行（零变更/撞进行中/无存量用户）——scheduled_sync_once 已留日志


# 注册表项（main.py lifespan 遍历注册为进程内 asyncio 周期任务）
FEISHU_SYNC_TASK = CronTask(
    name="feishu_sync",
    interval_seconds=FEISHU_SYNC_INTERVAL_SECONDS,
    coroutine_factory=run_feishu_sync,
)
