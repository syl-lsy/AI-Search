"""FastAPI 依赖注入出口（占位锚点）。

规划依赖（ARCH §3.1，B1/B2 实装）：
- get_db：请求级 AsyncSession（事务边界所在——service 只 flush 不 commit；
  TurnRunner / KB worker 为短事务例外）；
- get_current_user：MVP 默认用户（启动幂等 seed，REQ 契约见 api.md）；
- get_event_bus：轮次事件缓冲与订阅（Last-Event-ID 重放源，services/event_bus.py）；
- get_asset_snapshot：Nacos 资产快照（config_center）。

占位阶段不写实现——依赖注入含业务语义，按节点五步闭环纪律随工单落地。
"""
