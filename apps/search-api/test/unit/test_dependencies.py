"""unit 层：app/dependencies.py 依赖出口。

get_db 是 api/ 层唯一拿会话的入口（api/ 不直捣 db 层）——用「同一对象」断言
守卫再导出契约：dependencies.get_db 必须就是 db/session.get_db 本体，
任何一方被替换/包壳都会在这里显式红掉。
get_event_bus 同口径：dependencies.get_event_bus 必须就是
services/event_bus.get_event_bus 本体（EventBus 消费的 api/ 层唯一出口）。
（会话工厂绑定引擎单例、expire_on_commit 等机制断言见 test_engine.py。）
"""
from app.db.session import get_db as session_get_db
from app.dependencies import get_db, get_event_bus
from app.services.event_bus import get_event_bus as event_bus_get_event_bus


def test_get_db_reexport_is_session_get_db():
    """再导出一致性：dependencies.get_db is db/session.get_db（单一来源契约）。"""
    assert get_db is session_get_db


def test_get_event_bus_reexport_is_event_bus_get_event_bus():
    """再导出一致性：dependencies.get_event_bus is services/event_bus.get_event_bus。"""
    assert get_event_bus is event_bus_get_event_bus
