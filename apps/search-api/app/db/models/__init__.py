"""ORM 表模型包（B1 按 data-model.md §1.1~§1.5 DDL 契约落地五表）。

统一 import 全部模型以确保进 Base.metadata（autogenerate 的 target_metadata，
见 migrations/env.py）；后续批次按 §1.7 起逐表追加（offload_chunk(M4)/
eval_*），并在此处同步 import。
（§1.16 turn_feedback 已随 0010 落地——M2 增量表；§1.6 snapshots、§1.9 artifacts
已随 0012/m2_deep 落地——M2 deep 底座；§1.7 kb_documents、§1.8 kb_chunks、
§1.17 kb_import_jobs 已随 0013/m3_kb 落地——M3 KB 底座。）
"""
from app.db.models.artifact import Artifact
from app.db.models.kb import KbChunk, KbDocument, KbImportJob
from app.db.models.source import Source
from app.db.models.snapshot import Snapshot
from app.db.models.thread import Thread
from app.db.models.turn import Turn
from app.db.models.turn_event import TurnEvent
from app.db.models.turn_feedback import TurnFeedback
from app.db.models.user import User

__all__ = [
    "Artifact",
    "KbChunk",
    "KbDocument",
    "KbImportJob",
    "Snapshot",
    "Source",
    "Thread",
    "Turn",
    "TurnEvent",
    "TurnFeedback",
    "User",
]
