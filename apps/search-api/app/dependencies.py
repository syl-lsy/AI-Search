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
- get_model_mapping：model.mapping 快照 payload 消费出口（threads 建轮模型解析 +
  GET /models 切换器共用同源快照）。
- get_current_user：MVP 默认用户（REQ-011：user_id 隐式——服务端从请求上下文取，
  请求体不含 user_id；与启动幂等 seed 同源，api.md「user_id」行）。
"""
from collections.abc import Mapping
from typing import Any

from app.config_center import AssetSnapshot, get_asset_center
from app.core.config import common_settings
from app.db.session import get_db
from app.exception import ConfigCenterException
from app.services.event_bus import get_event_bus

__all__ = [
    "get_asset_center",
    "get_asset_snapshot",
    "get_current_user",
    "get_db",
    "get_event_bus",
    "get_model_mapping",
]

# model.mapping dataId（nacos-config.md §5：五 dataId 之一；与 turn_service 的
# 轮次捕获同源——此处的出口面向 API 请求路径，快照缺失转 503001）
_MAPPING_DATA_ID = "ai_search.model.mapping.json"


def get_asset_snapshot(data_id: str) -> AssetSnapshot | None:
    """按 dataId 取当前资产快照（GET /api/v1/suggestions 等快照直读消费方的统一入口）。

    消费纪律：快照是不可变引用，读当下值即可、不缓存到请求外（热更对下次请求生效，
    nacos-config.md §6 home.suggest_pool 边界）；TurnRunner 的轮次捕获（OQ-11）由
    B2 在轮次启动时一次性取引用实现，同走 get_asset_center()。
    """
    return get_asset_center().snapshot(data_id)


def get_model_mapping() -> Mapping[str, Any]:
    """model.mapping 当前快照 payload（threads 建轮模型解析 + GET /models 共用同源）。

    配置面不可用在 API 请求路径上一律按可预期运行态呈现：资产中心未启动
    （get_asset_center 的 RuntimeError fail-fast）与快照缺失（None——启动后 dataId
    未装载，正常不发生，start_asset_center 对五 dataId 全量装内置默认）均转
    ConfigCenterException（503001）自然冒泡，经全局业务异常处理器出 503，路由层
    零捕获（api.md §0；不落 500001——配置面不可用非未知异常，也不泄漏内部栈）。
    """
    try:
        snapshot = get_asset_snapshot(_MAPPING_DATA_ID)
    except RuntimeError as exc:
        raise ConfigCenterException(
            "配置中心不可达且无缓存", detail=f"model.mapping 快照不可用：{exc}"
        ) from exc
    if snapshot is None:
        raise ConfigCenterException(
            "配置中心不可达且无缓存", detail=f"{_MAPPING_DATA_ID} 快照缺失"
        )
    return snapshot.payload


def get_current_user() -> str:
    """MVP 默认用户：读 common_settings.default_user_id（.env DEFAULT_USER_ID）。

    api.md「user_id」行口径：隐式默认用户，请求体不含 user_id（REQ-011）；与启动
    幂等 seed（db/seed.py）同源。auth 二期换登录态（token→user 解析）时仅替换本
    函数实现——返回类型（user_id 字符串）与表结构不动。
    """
    return common_settings.default_user_id
