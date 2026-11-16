"""轮反馈服务（REQ-057 / api.md §2.9 v1.17 / data-model.md §1.16）。

语义：**每轮一条记录**——`UNIQUE(turn_id)` 是并发兜底（两次提交竞态下由 PG 唯一索引
串行化，不靠应用层判重）：

- helpful=True/False → **upsert 改判**：INSERT … ON CONFLICT (turn_id) DO UPDATE SET
  helpful=…, updated_at=now() …… RETURNING 一次往返完成「写 + 回读现值」（两按钮互斥、
  单击即改判）；
- helpful=None → **清除反馈**（v1.17）：DELETE 该轮行（参数化语句、幂等——无行 no-op
  仍 200），返回清除形态 `{turn_id, helpful: None, updated_at: None}`（行已删除，
  §2.9 v1.17 清除形态示例；§2.5 feedback_helpful 读回自然回 null，A41 兼容）。改判/
  清除共用本端点，无独立「清除反馈」端点。

分层：DTO 进 DTO 出（ORM 不出本层）、只 execute 不 commit（事务边界在请求级 get_db，
BaseService 铁律；本端点无 BackgroundTask，无需 scope="function" 特化）。
归属校验：id + user_id 双条件（与 get_turn_detail/cancel_turn 同口径，越权与不存在同码
404004——不泄漏存在性），清除路径同样先过归属校验。
"""
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Turn, TurnFeedback
from app.exception import FeedbackTurnNotFoundException
from app.schemas.feedback import FeedbackResponse
from app.services.base import BaseService

__all__ = ["TurnFeedbackService"]


class TurnFeedbackService(BaseService):
    """轮反馈写入（upsert 改判）与清除（null 删行）；会话由 api 层依赖注入（get_db 请求级事务）。"""

    async def upsert_feedback(
        self, turn_id: str, *, helpful: bool | None, user_id: str
    ) -> FeedbackResponse:
        """写入/改判/清除该轮反馈（404004 = 轮不存在或不属于当前用户）。

        helpful 为 True/False → upsert 改判并回读现值；helpful 为 None → 清除
        （DELETE 该轮行，幂等）。参数全部由边界层校验（helpful 为 bool | None、
        路径参数已过 pydantic 路由校验）；内部只做归属存在性判定，不重复校验类型。
        """
        try:
            turn_uuid = uuid.UUID(turn_id)
            user_uuid = uuid.UUID(user_id)
        except ValueError as exc:
            raise FeedbackTurnNotFoundException(
                "轮次不存在", detail=f"非法 turn/user id：{turn_id!r}"
            ) from exc
        exists_stmt = select(Turn.id).where(
            Turn.id == turn_uuid, Turn.user_id == user_uuid
        )
        if (await self.db.scalars(exists_stmt)).first() is None:
            raise FeedbackTurnNotFoundException(
                "轮次不存在",
                detail=f"turn_id={turn_id} 不存在或不属于当前用户",
            )
        if helpful is None:
            # 清除反馈（§2.9 v1.17）：参数化 DELETE，无行时 no-op（幂等清除）。
            # 行已删除 → 无 RETURNING 现值可回读，响应直接取清除形态（三键恒在、
            # helpful/updated_at 双 null）。
            clear_stmt = delete(TurnFeedback).where(
                TurnFeedback.turn_id == turn_uuid
            )
            await self.db.execute(clear_stmt)
            return FeedbackResponse(
                turn_id=str(turn_uuid), helpful=None, updated_at=None
            )
        stmt = (
            insert(TurnFeedback)
            .values(
                id=uuid.uuid4(),
                turn_id=turn_uuid,
                helpful=helpful,
                # 插入与改判同口径取库时 now()：created_at 走 server_default，
                # updated_at 显式赋值（改判路径覆盖它——data-model §1.16）
                updated_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=[TurnFeedback.turn_id],
                set_={"helpful": helpful, "updated_at": func.now()},
            )
            .returning(
                TurnFeedback.turn_id, TurnFeedback.helpful, TurnFeedback.updated_at
            )
        )
        row = (await self.db.execute(stmt)).first()
        if row is None:  # 防御：RETURNING 恒有行（竞态下行被级联删除的极端面）
            raise FeedbackTurnNotFoundException(
                "轮次不存在", detail=f"turn_id={turn_id} upsert 未返回行"
            )
        return FeedbackResponse(
            turn_id=str(row.turn_id),
            helpful=bool(row.helpful),
            updated_at=row.updated_at,
        )
