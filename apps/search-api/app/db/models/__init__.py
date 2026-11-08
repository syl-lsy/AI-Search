"""ORM 表模型包（B1 按 data-model.md §1.1~§1.5 DDL 契约落地五表）。

统一 import 全部模型以确保进 Base.metadata（autogenerate 的 target_metadata，
见 migrations/env.py）；后续批次按 §1.6 起逐表追加（snapshot/kb_*/artifact/
offload_chunk(M4)/eval_*），并在此处同步 import。
"""
from app.db.models.source import Source
from app.db.models.thread import Thread
from app.db.models.turn import Turn
from app.db.models.turn_event import TurnEvent
from app.db.models.user import User

__all__ = ["Source", "Thread", "Turn", "TurnEvent", "User"]
