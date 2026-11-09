"""unit 层：app/dependencies.py 依赖出口。

get_db 是 api/ 层唯一拿会话的入口（api/ 不直捣 db 层）——用「同一对象」断言
守卫再导出契约：dependencies.get_db 必须就是 db/session.get_db 本体，
任何一方被替换/包壳都会在这里显式红掉。
get_event_bus 同口径：dependencies.get_event_bus 必须就是
services/event_bus.get_event_bus 本体（EventBus 消费的 api/ 层唯一出口）。
get_current_user：MVP 默认用户（REQ-011：user_id 隐式，请求体不含 user_id）——
读 common_settings.default_user_id（与启动幂等 seed 同源）；auth 二期换登录态
只改本函数实现，返回类型（user_id 字符串）与表结构不动。
（会话工厂绑定引擎单例、expire_on_commit 等机制断言见 test_engine.py。）
"""
from app.core.config import common_settings
from app.db.session import get_db as session_get_db
from app.dependencies import get_current_user, get_db, get_event_bus
from app.services.event_bus import get_event_bus as event_bus_get_event_bus


def test_get_db_reexport_is_session_get_db():
    """再导出一致性：dependencies.get_db is db/session.get_db（单一来源契约）。"""
    assert get_db is session_get_db


def test_get_event_bus_reexport_is_event_bus_get_event_bus():
    """再导出一致性：dependencies.get_event_bus is services/event_bus.get_event_bus。"""
    assert get_event_bus is event_bus_get_event_bus


def test_get_current_user_returns_settings_default_user_id():
    """MVP 默认用户：与 common_settings.default_user_id 同源（.env DEFAULT_USER_ID）。"""
    assert get_current_user() == common_settings.default_user_id
    assert isinstance(get_current_user(), str)  # 返回 user_id 字符串（api.md user_id 行）
