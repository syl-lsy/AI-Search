"""FastAPI 依赖注入出口（api/ 层统一从这里拿依赖，不直捣 db/agent 层）。

已实装：
- get_db：请求级 AsyncSession——机制与事务边界见 app/db/session.py 的 get_db
  docstring（事务边界统一在 get_db：service 只 flush 不 commit；TurnRunner /
  KB worker 为短事务例外）。此处再导出 db/session.py 的同一实现，保证
  `Depends(get_db)` 全工程单一来源（单测守卫再导出一致性）。
- get_asset_center / get_asset_snapshot：Nacos 资产快照消费出口（config_center，
  nacos-config.md §6/§7）——api/ 层唯一入口，不直捣 config_center 内部；
  get_asset_center 零参可作 `Depends(get_asset_center)`；get_asset_snapshot(data_id)
  按键取当前不可变快照（未知 dataId 返回 None）。
- get_event_bus：轮次事件总线消费出口（services/event_bus.py，ADR-003）——SSE
  端点订阅（Last-Event-ID 重放源）唯一入口；持有者由 lifespan 调
  start_event_bus() 注册（B2 接线），未启动即取 fail-fast（同 get_asset_center）。

后续阶段锚点（占位注释，不提前实现——依赖注入含业务语义，随所属工单按
节点五步闭环纪律落地）：
- get_current_user：MVP 默认用户（启动幂等 seed，REQ 契约见 api.md）——B2。
"""
from app.config_center import AssetSnapshot, get_asset_center
from app.db.session import get_db
from app.services.event_bus import get_event_bus

__all__ = ["get_asset_center", "get_asset_snapshot", "get_db", "get_event_bus"]


def get_asset_snapshot(data_id: str) -> AssetSnapshot | None:
    """按 dataId 取当前资产快照（GET /api/v1/suggestions 等快照直读消费方的统一入口）。

    消费纪律：快照是不可变引用，读当下值即可、不缓存到请求外（热更对下次请求生效，
    nacos-config.md §6 home.suggest_pool 边界）；TurnRunner 的轮次捕获（OQ-11）由
    B2 在轮次启动时一次性取引用实现，同走 get_asset_center()。
    """
    return get_asset_center().snapshot(data_id)
