"""数据库层（SQLAlchemy 2.0 async）：session（引擎单例）/ base（声明基类）/ models（ORM）。

基座口径为 app/model/（engine.py），本项目按 AI Agent 模板口径改名为 app/db/
（engine.py → db/session.py）——schemas/（DTO）≠ db/models/（ORM），互不混用。
"""
