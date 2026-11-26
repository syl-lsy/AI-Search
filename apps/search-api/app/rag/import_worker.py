"""飞书导入 worker（M3 批次 3 / B20 B 线——api.md §4.7~4.9；data-model §1.17）。

图外组件（ADR-011 v1.76 增注）：不进图拓扑、不产生 SSE 事件；进程内队列 + 串行
worker（自用单人——data-model §1.17 索引注），按文档单元逐个推进、可中断可恢复
（cursor 断点游标）。重启经启动钩子重入队 `status IN (pending,running)` 未完成
任务行（§1.17 :534 / §4 :608——消费端见 FeishuImportWorker.start，G5）。

**同步 diff 判定序**（api.md §4.7，2026-11-25 C4=(a)——对远端树上每个文档单元依次
判定，命中即止）：
① 本地无该 node_token → 新增，重导；
② 本地 catalog_path 全串（祖先+自身标题，data-model §1.18「含自身标题」条）与远端
   不同 → 移动/改名，该节点及其子树全部重导（子树全链物化随父变——比对基准=
   kb_documents.catalog_path 列，零新存储；轻审 P1-2 钉正：仅祖先链比对改标题不命中）；
③ 远端 obj_edit_time 大于本地记录值 → 变更，重导；
④ 远端 obj_edit_time 缺失或为 0 → 启用 content_hash 兜底：重拉内容、与
   kb_documents.content_hash 比对，不同才重导；
⑤ 其余 → 未变更，跳过，不计入 total_docs。
「重导」= 整文档单元替换（§4.5 同款三存储级联后重建）。远端删除（C5=(a) 用户授权）：
同步 diff 差集（远端已不存在的本地 node_token）→ 对应 kb_documents 走 §4.5 同款
三存储级联删除，删除计数入 stats.deleted_docs（不进五数）。

字面面（api.md §4.9 钉注，⑦/C8/G13）：mindnote/slides 每文档单元 skipped+1 且
failures 条目 message「<obj_type> 无读取 API，已跳过」；sheet/bitable message
「本期未实装读取通道」（指 wiki 文档单元级读取；docx 内嵌 sheet(30) 块经
**v1.20 契约批回填段**承接——装配后、切分前同步读 sheets API：placeholder 翻
false + meta.rows/col_count 物化，失败保留占位不算文档失败、超限截断计数留痕
parse_meta.feishu.sheet_backfill + degraded_blocks["sheet_backfill_truncated"]，
重入队幂等——placeholder=false 节点跳过；bitable(18) 走 Base API 域、本批零回填）；
shortcut 节点不计文档单元、不进五数；failure_summary 由
failures 数组按 category 派生（首见序）、不落存储列（G4）；附件类型与大小同上传
白名单（pdf/md/txt/docx + 20MB——读 kb_settings 与上传同源），超限/白名单外进失败
清单 category=attachment_unsupported；imported_docs 计数时点=文档行建成进流水线
即计（G10，其后归一化/索引失败算文档 failed 不回写任务）。

V10 三动作矩阵留档未找到——obj_edit_time 行为按判定序③④实现并在 judge_unit_action
注明「待真库实测复核」（M4 前补验）。

事务纪律：worker 为短事务例外口径（同 KbDocumentRepository）——每步独立 session
一次 commit；REST 建行侧（import_service）只 flush 不 commit（事务边界在 get_db）。
凭据纪律：凭据只经 feishu_client/settings 单例读取，零打印零回显。
"""

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update

from app.core.config import feishu_settings, kb_settings
from app.core.logger import BusinessLog
from app.db.models import KbDocument, KbImportJob, Source
from app.db.session import get_session_factory
from app.rag.feishu_client import (
    FeishuAttachmentTooLargeError,
    FeishuClient,
    FeishuClientError,
    SheetValuesData,
    get_default_feishu_client,
)
from app.rag.feishu_mapper import (
    PARSER_FEISHU_BLOCKS,
    FeishuMappingResult,
    build_document_node,
    build_feishu_parse_meta,
    map_blocks,
)
from app.rag.ir_schema import DOC_KINDS, IrNode, IrNodeType
from app.rag.pipeline import KbPipeline, _default_spec_payload, get_kb_pipeline
from app.rag.service import MinioPrefixPurger
from app.rag.storage import get_kb_minio_client, put_ir_json
from app.rag.tree_assembler import assemble_catalog_tree, attach_block_tree

# ---------------------------------------------------------------------------
# 常量（代码常量——不入 Nacos，data-model §1.17 注「令牌桶参数为代码常量」）
# ---------------------------------------------------------------------------

#: 5 QPS 令牌桶（api.md §4.7 钉注「5 QPS 令牌桶节流」）
IMPORT_QPS = 5
IMPORT_MIN_INTERVAL_S = 1.0 / IMPORT_QPS  # 0.2s

#: 失败分类（api.md §4.9 category 非封闭枚举——值域随实现批钉；state errors[] 先例）
CATEGORY_UNSUPPORTED_TYPE = "unsupported_type"
CATEGORY_BLOCK_FETCH_FAILED = "block_fetch_failed"
CATEGORY_ATTACHMENT_DOWNLOAD_FAILED = "attachment_download_failed"
CATEGORY_ATTACHMENT_UNSUPPORTED = "attachment_unsupported"
CATEGORY_NORMALIZE_FAILED = "normalize_failed"
CATEGORY_INDEX_FAILED = "index_failed"
CATEGORY_PERMISSION_DENIED = "permission_denied"

#: 瞬态失败分类（worker 内有界重试；其余分类确定性失败不重试——重试只救抖动不救裁决）
TRANSIENT_CATEGORIES: frozenset[str] = frozenset(
    {CATEGORY_BLOCK_FETCH_FAILED, CATEGORY_ATTACHMENT_DOWNLOAD_FAILED}
)
#: 单单元最大尝试次数（1 首试 + 2 重试）
UNIT_MAX_ATTEMPTS = 3

#: 可读取的文档单元类型（SDK 直读 docx + file 附件；wiki obj_type 其余值不可读）
READABLE_OBJ_TYPES: frozenset[str] = frozenset({"docx", "file"})
#: 本期未实装读取通道（C8=(a)——message 与「无读取 API」区分）
NOT_IMPLEMENTED_READ_TYPES: frozenset[str] = frozenset({"sheet", "bitable"})

#: kb_import_jobs 四态终态（重入队不入列）
_JOB_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed"})
#: 重入队状态集（§1.17 :534 / §4 :608——pending=已建未启动也须恢复）
JOB_REQUEUE_STATUSES: tuple[str, ...] = ("pending", "running")

#: 五数零值（建行初始形态——恒等式 processed=imported+failed+skipped）
FIVE_ZERO_PROGRESS: dict[str, int] = {
    "total_docs": 0,
    "processed_docs": 0,
    "imported_docs": 0,
    "failed_docs": 0,
    "skipped_docs": 0,
}


# ---------------------------------------------------------------------------
# 判定序数据形态（纯函数面——test_kb_import_worker 直接消费）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RemoteUnit:
    """远端文档单元（树物化目录链 + 变更判据；树 BFS 序）。"""

    node_token: str
    obj_token: str
    obj_type: str
    title: str
    obj_edit_time: int | None
    catalog_ids: list[str]
    catalog_path: list[str]  # 含自身标题（C9=(a)）
    is_shortcut: bool = False


@dataclass(slots=True)
class LocalDoc:
    """本地比对基准（kb_documents 行投影——判定序②③④的比对原料）。

    `space_id`：parse_meta.feishu.space_id（比对/差集的空间过滤键——P1：空间绑定
    错配不触发跨空间全删）；`title`：审计留痕面（级联删除逐文档日志）。"""

    document_id: str
    node_token: str
    obj_edit_time: int | None
    catalog_path: str | None  # 列原文（「/」连接串）
    content_hash: str | None
    space_id: str = ""
    title: str | None = None


@dataclass(slots=True)
class PlanUnit:
    """计划单元：远端单元 + 动作（import=重导；skip_trace=不可读类型跳过留痕）。"""

    remote: RemoteUnit
    action: str


@dataclass(slots=True)
class SyncPlan:
    """同步计划：units（待处理单元，五数 total_docs 基准）+ deletions（差集）。"""

    units: list[PlanUnit] = field(default_factory=list)
    deletions: list[LocalDoc] = field(default_factory=list)

    @property
    def meaningful(self) -> bool:
        """建行判据（G7）：有重导或差集非空（仅 skip_trace 不算变更）。"""
        return any(u.action == "import" for u in self.units) or bool(self.deletions)


def catalog_path_string(path: list[str]) -> str:
    """目录链标题串（data-model §1.7 catalog_path 列：「/」连接）。"""
    return "/".join(path)


def judge_unit_action(
    remote: RemoteUnit,
    local: LocalDoc | None,
    *,
    pull_content_hash: Callable[[RemoteUnit], str],
) -> str:
    """同步 diff 判定序（api.md §4.7 五步命中即止）——返回 "import" | "unchanged"。

    ①本地无 node_token → import；②catalog_path 全串不同 → import（移动/改名）；
    ③远端 obj_edit_time > 本地记录 → import（V10 三动作矩阵留档未找到——本行为按
    判定序实现，待真库实测复核，M4 前补验）；④远端 obj_edit_time 缺失/0 →
    content_hash 兜底（不同才 import）；⑤其余 → unchanged。④ 仅在 ③ 不可判
    （缺失/0）时可达；②③ 命中即止不触达重拉（B5 反例锚点）。
    """
    if local is None:  # ① 新增
        return "import"
    if (local.catalog_path or "") != catalog_path_string(remote.catalog_path):  # ② 移动/改名
        return "import"
    remote_edit = remote.obj_edit_time
    if remote_edit:  # ③ 正反例各一（V10 三动作矩阵留档未找到——行为按判定序实现，
        # 待真库实测复核，M4 前补验）；B5 反例锚点：edit_time 正常零重拉
        return "import" if remote_edit > (local.obj_edit_time or 0) else "unchanged"
    pulled = pull_content_hash(remote)  # ④ content_hash 兜底
    return "import" if pulled != (local.content_hash or "") else "unchanged"


def compute_sync_plan(
    units: list[RemoteUnit],
    local_by_token: dict[str, LocalDoc],
    *,
    content_hash_fn: Callable[[RemoteUnit], str] | None,
) -> SyncPlan:
    """同步计划：逐单元判定（shortcut 不计文档单元不进五数——G13）+ 差集。

    - 不可读类型（mindnote/slides/sheet/bitable 等）恒 skip_trace 留痕（每同步
      一条 failures——api.md §4.9 字面面），不进判定序；
    - content_hash_fn=None（定时判定侧）：④ 单元保守视为候选（不重拉内容，建行
      交执行期精确判定）——判定侧零内容拉取；
    - deletions = 本地有而远端无（差集，C5=(a)）。
    """
    plan_units: list[PlanUnit] = []
    remote_tokens: set[str] = set()
    for unit in units:
        if unit.is_shortcut:
            continue  # G13：引用边非文档单元
        remote_tokens.add(unit.node_token)
        if unit.obj_type not in READABLE_OBJ_TYPES:
            plan_units.append(PlanUnit(remote=unit, action="skip_trace"))
            continue
        local = local_by_token.get(unit.node_token)
        pull: Callable[[RemoteUnit], str] = (
            content_hash_fn or (lambda u: "<scheduled-conservative>")
        )
        if judge_unit_action(unit, local, pull_content_hash=pull) == "import":
            plan_units.append(PlanUnit(remote=unit, action="import"))
    deletions = [
        doc for token, doc in local_by_token.items() if token not in remote_tokens
    ]
    return SyncPlan(units=plan_units, deletions=deletions)


def derive_failure_summary(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """failure_summary 由 failures 按 category 计数派生（G4：不落存储列）。

    首见序排列；skip 类（unsupported_type）条目同样计入——与 skipped_docs 正交
    （前者=条目数按分类、后者=文档数）。
    """
    counts: dict[str, int] = {}
    for entry in failures:
        category = str(entry.get("category", ""))
        counts[category] = counts.get(category, 0) + 1
    return [{"category": category, "count": count} for category, count in counts.items()]


# ---------------------------------------------------------------------------
# sheet(30) 装配后回填段（data-model §1.18 v1.20 30 行——树装配后、切分前）
# ---------------------------------------------------------------------------

#: 回填超限降级在 parse_meta.feishu.degraded_blocks 的留痕键（升格本身非降级不计入
#: ——此处计的是回填护栏截断造成的内容缺失，契约 0 值省略键同款纪律）
SHEET_BACKFILL_TRUNCATED_KEY = "sheet_backfill_truncated"


@dataclass(slots=True)
class SheetBackfillReport:
    """sheet(30) 回填段产物（parse_meta.feishu 留痕原料——§1.18 v1.20 30 行）。"""

    backfilled: int = 0
    failed: int = 0
    failed_categories: dict[str, int] = field(default_factory=dict)
    truncated_nodes: int = 0
    truncated_rows: int = 0
    truncated_cols: int = 0
    truncated_cells: int = 0

    def parse_meta_block(self) -> dict[str, Any]:
        """parse_meta.feishu.sheet_backfill（0 值省略键；全零 → 空 dict → 调用方省略整键；
        存储侧附加不进 REST 投影——沿 parse_meta 附加键登记判例）。"""
        block: dict[str, Any] = {}
        if self.truncated_rows:
            block["truncated_rows"] = self.truncated_rows
        if self.truncated_cols:
            block["truncated_cols"] = self.truncated_cols
        if self.truncated_cells:
            block["truncated_cells"] = self.truncated_cells
        if self.failed:
            block["failed"] = self.failed
        if self.failed_categories:
            block["failed_categories"] = dict(self.failed_categories)
        return block

    def degraded_delta(self) -> dict[str, int]:
        """并入 parse_meta.feishu.degraded_blocks 的增量（超限降级留痕——空 = 零超限）。"""
        if self.truncated_nodes:
            return {SHEET_BACKFILL_TRUNCATED_KEY: self.truncated_nodes}
        return {}


def _iter_ir_nodes(node: IrNode) -> Any:
    """整树先序遍历（document 根 + children 递归；无兄弟指针——ir_schema 序）。"""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


async def backfill_sheet_placeholders(
    root: IrNode,
    fetch: Callable[[str], Awaitable[SheetValuesData]],
) -> SheetBackfillReport:
    """扫描占位升格表 → sheets 值矩阵回填（§1.18 v1.20 30 行：装配后、切分前同步读）。

    - 扫描面：type=table + meta.placeholder is True + meta.sheet_token 非空（bitable(18)
      占位走 Base API 域、非 sheets 通道——本批零回填，token 留痕即终点；image(27)
      素材占位归素材下载通道）；
    - 成功：meta.rows（物化矩阵，含表头首行）/ meta.col_count 物化 + placeholder 翻 false
      （`[sheet]` 占位字面恒保留——纯文本消费面兜底）；
    - 失败（FeishuClientError 族，含分类 FeishuSheetValuesError）：占位保留、不算文档
      失败（27 行 image 先例同款），failed/failed_categories 计数留痕；
    - 重入队幂等：placeholder 已非 true 的节点跳过——同一任务重跑不重复拉取、
      不重复写、不重复计数。
    """
    report = SheetBackfillReport()
    for node in _iter_ir_nodes(root):
        if node.type is not IrNodeType.TABLE:
            continue
        meta = node.meta
        if meta.get("placeholder") is not True:
            continue  # 非占位表（含已回填 placeholder=false）——幂等跳过
        token = str(meta.get("sheet_token") or "")
        if not token:
            continue  # bitable(18) 等：非 sheets 通道占位，本批零回填
        try:
            data = await fetch(token)
        except FeishuClientError as exc:
            category = str(getattr(exc, "category", "") or "connection")
            report.failed += 1
            report.failed_categories[category] = report.failed_categories.get(category, 0) + 1
            continue  # 保留占位、不算文档失败
        except Exception as exc:  # noqa: BLE001 —— 契约「回填失败保留占位、不算文档
            # 失败」无条件成立（评审 P3：意外异常若逃逸会经单元兜底整文档归
            # normalize_failed，且 loguru opt(exception) 全栈落 error 日志——栈含
            # 正文片段违反零 payload 纪律）。留痕只记类型名，零消息内容。
            category = f"unexpected:{type(exc).__name__}"
            report.failed += 1
            report.failed_categories[category] = report.failed_categories.get(category, 0) + 1
            continue  # 保留占位、不算文档失败
        meta["placeholder"] = False
        meta["rows"] = data.rows
        meta["col_count"] = max((len(row) for row in data.rows), default=0)
        report.backfilled += 1
        if data.truncated_rows or data.truncated_cols or data.truncated_cells:
            report.truncated_nodes += 1
        report.truncated_rows += int(data.truncated_rows)
        report.truncated_cols += int(data.truncated_cols)
        report.truncated_cells += int(data.truncated_cells)
    return report


# ---------------------------------------------------------------------------
# 令牌桶（5 QPS——代码常量；clock/sleep 注入缝供测试零等待）
# ---------------------------------------------------------------------------


class TokenBucket:
    """最小间隔节流（5 QPS → 200ms；api.md §4.7 钉注）——串行 worker 单消费者。"""

    def __init__(
        self,
        *,
        min_interval_s: float = IMPORT_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_ts: float | None = None

    async def acquire(self) -> None:
        now = self._clock()
        if self._last_ts is not None:
            wait = self._min_interval_s - (now - self._last_ts)
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
        self._last_ts = now


async def resolve_space_id(client: Any, *, preferred_space_id: str = "") -> str:
    """目标知识空间裁决：settings 绑定值优先（自用部署绑定，FEISHU_SPACE_ID）。

    评审 P2 收紧：未绑定时仅**单空间**直取；多空间显式拒绝（fail 任务要求绑定）——
    静默取首个会把全量 diff 对着错误空间跑（他空间内容导入 + 本地全量差集删除的
    诱导面）；零空间仍拒（无可用知识空间）。
    """
    if preferred_space_id:
        return preferred_space_id
    spaces = await client.list_spaces()
    if not spaces:
        raise FeishuClientError("无可用知识空间（应用未加为知识库成员或未开通只读权限）")
    if len(spaces) > 1:
        raise FeishuClientError(
            f"检测到 {len(spaces)} 个可用知识空间且 FEISHU_SPACE_ID 未绑定——"
            "须在 .env 显式绑定目标空间后触发导入"
        )
    return spaces[0].space_id


def _walk_nodes_to_units(walk_result: Any) -> list[RemoteUnit]:
    """walk 节点平铺 → 树装配物化目录链 → RemoteUnit 列表（树 BFS 序）。

    catalog_ids/catalog_path 由 assemble_catalog_tree 沿树物化（§1.18 C9=(a)：
    catalog_path 含自身标题、顶层=[自身标题]）；shortcut 经 meta.node_type 透传
    （装配器防御④同口径）；obj_edit_time 走 meta.obj_edit_time 契约键。
    """
    ir_nodes: list[IrNode] = []
    obj_token_by_token: dict[str, str] = {}
    for info in walk_result.nodes:
        meta: dict[str, Any] = {
            "raw_type": info.obj_type,  # 双层 type：obj_type 原值全量留痕（§1.8#4）
            "obj_edit_time": info.obj_edit_time,
        }
        if info.node_type:
            meta["node_type"] = info.node_type  # shortcut 透传（tree_assembler 防御④）
        ir_nodes.append(
            IrNode(
                id=f"fs:node:{info.node_token}",
                parent_id=f"fs:node:{info.parent_node_token}" if info.parent_node_token else None,
                type="document",  # type: ignore[arg-type]
                title=info.title or None,
                doc_kind=info.obj_type if info.obj_type in DOC_KINDS else None,
                meta=meta,
            )
        )
        obj_token_by_token[info.node_token] = info.obj_token

    root, _report = assemble_catalog_tree(ir_nodes)
    units: list[RemoteUnit] = []
    queue: deque[IrNode] = deque(root.children)
    while queue:
        node = queue.popleft()
        token = node.id.removeprefix("fs:node:")
        units.append(
            RemoteUnit(
                node_token=token,
                obj_token=obj_token_by_token.get(token, ""),
                obj_type=str(node.meta.get("raw_type", "")),
                title=node.title or "",
                obj_edit_time=edit_time_of(node),
                catalog_ids=list(node.catalog_ids),
                catalog_path=list(node.catalog_path),
                is_shortcut=node.meta.get("node_type") == "shortcut",
            )
        )
        queue.extend(node.children)
    return units


def edit_time_of(node: IrNode) -> int | None:
    """meta.obj_edit_time 读取（int | 数字串 | None 归一——SDK 字段形态防御）。"""
    value = node.meta.get("obj_edit_time")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _skip_trace_entry(unit: RemoteUnit) -> dict[str, Any]:
    """不可读类型 failures 条目（api.md §4.9 字面面——⑦/C8 两 message 区分）。"""
    if unit.obj_type in NOT_IMPLEMENTED_READ_TYPES:
        message = "本期未实装读取通道"
    else:
        message = f"{unit.obj_type} 无读取 API，已跳过"
    return {
        "title": unit.title or unit.node_token,
        "category": CATEGORY_UNSUPPORTED_TYPE,
        "message": message,
        "document_id": None,
    }


def _failure_entry(unit: RemoteUnit, category: str, message: str) -> dict[str, Any]:
    """失败清单条目（四键；document_id=null=文档未建成——api.md §4.9）。"""
    return {
        "title": unit.title or unit.node_token,
        "category": category,
        "message": message,
        "document_id": None,
    }


def _error_code_of(err: Any) -> str:
    """错误码提取（walk 错误串归一化面——`code=NNNNN` 形态；无码空串）。"""
    import re as _re

    match = _re.search(r"code=(\d+)", str(err))
    return f"code={match.group(1)}" if match else "code 未知"


# ---------------------------------------------------------------------------
# 仓储（短事务自管 session——图外 worker 例外口径，同 KbDocumentRepository）
# ---------------------------------------------------------------------------


class KbImportJobRepository:
    """kb_import_jobs 数据访问（worker/定时侧——乐观状态卫同六态流水线口径）。"""

    async def load(self, job_id: str) -> dict[str, Any] | None:
        """全列投影（worker 消费面：status/cursor/progress/failures/stats/user_id）。"""
        async with get_session_factory()() as session:
            row = await session.get(KbImportJob, _as_uuid(job_id))
            if row is None:
                return None
            return {
                "id": row.id,
                "user_id": row.user_id,
                "connector": row.connector,
                "trigger": row.trigger,
                "status": row.status,
                "cursor": row.cursor,
                "progress": row.progress,
                "failures": row.failures,
                "stats": row.stats,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
            }

    async def transition(
        self,
        job_id: str,
        *,
        expect: set[str],
        to: str,
        fields: dict[str, Any],
    ) -> bool:
        """乐观状态迁移（expect 状态集卫；rowcount 0=并发改写/行删 → abort）。"""
        stmt = (
            update(KbImportJob)
            .where(KbImportJob.id == _as_uuid(job_id), KbImportJob.status.in_(expect))
            .values(**fields, status=to)
        )
        async with get_session_factory()() as session:
            result = await session.execute(stmt)
            await session.commit()
            return (getattr(result, "rowcount", 0) or 0) > 0

    async def list_requeue_ids(self) -> list[str]:
        """启动钩子登记行 → 重入队 id 列表（status IN (pending,running)——§1.17）。"""
        async with get_session_factory()() as session:
            result = await session.scalars(
                select(KbImportJob.id).where(KbImportJob.status.in_(JOB_REQUEUE_STATUSES))
            )
            return [str(value) for value in result.all()]

    async def has_active(self) -> bool:
        """全局进行中任务存在（定时触发门——G7：撞进行中不建行）。"""
        async with get_session_factory()() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(KbImportJob)
                .where(KbImportJob.status.in_(JOB_REQUEUE_STATUSES))
            )
            return bool(int(count or 0))

    async def create_job(
        self,
        *,
        user_id: str,
        trigger: str,
        progress: dict[str, int],
        stats: dict[str, Any] | None = None,
    ) -> str:
        """建行（定时侧短事务；REST 建行走 import_service 请求级 session）。"""
        job_id = uuid.uuid4()
        async with get_session_factory()() as session:
            session.add(
                KbImportJob(
                    id=job_id,
                    user_id=uuid.UUID(user_id),
                    connector="feishu",
                    trigger=trigger,
                    status="pending",
                    cursor=None,
                    progress=dict(progress),
                    failures=[],
                    stats=stats,
                )
            )
            await session.commit()
        return str(job_id)


class FeishuImportDocRepository:
    """kb_documents worker 面数据访问（本地比对行 / 建行 / 级联删 pg 腿）。"""

    async def list_local_rows(self, user_id: str, *, space_id: str = "") -> list[LocalDoc]:
        """飞书来源本地行（parse_meta.feishu 四键承载 node_token/obj_edit_time）。

        `space_id` 过滤（评审 P1）：只取 parse_meta.feishu.space_id 与目标空间一致
        的行——他空间行不进判定序比对也不进差集（空间绑定错配不触发跨空间全删）；
        space_id 为空串时不过滤（防御形态，生产路径恒传解析后的目标空间）。

        附件行的 feishu 键由流水线 normalizing 段合并保留（见 pipeline._run_normalizing
        ——parse_meta 不得被归一化产物覆写丢失，否则同步 diff 恒判「新增」重复建行）。
        """
        uid = _as_uuid(user_id)
        feishu_col = KbDocument.parse_meta["feishu"]
        filters = [KbDocument.user_id == uid, feishu_col.is_not(None)]
        if space_id:
            filters.append(feishu_col["space_id"].astext == space_id)
        async with get_session_factory()() as session:
            rows = (await session.scalars(select(KbDocument).where(*filters))).all()
            local: list[LocalDoc] = []
            for row in rows:
                feishu = (row.parse_meta or {}).get("feishu") or {}
                token = str(feishu.get("node_token") or "")
                if not token:
                    continue
                edit = feishu.get("obj_edit_time")
                local.append(
                    LocalDoc(
                        document_id=str(row.id),
                        node_token=token,
                        obj_edit_time=int(edit) if isinstance(edit, (int, float)) else None,
                        catalog_path=row.catalog_path,
                        content_hash=row.content_hash,
                        space_id=str(feishu.get("space_id") or ""),
                        title=row.title,
                    )
                )
            return local

    async def list_feishu_user_ids(self) -> list[str]:
        """定时 diff 服务对象：已有飞书来源文档的用户（存量导入用户）。"""
        async with get_session_factory()() as session:
            result = await session.scalars(
                select(KbDocument.user_id)
                .where(KbDocument.parse_meta["feishu"].is_not(None))
                .distinct()
            )
            return [str(value) for value in result.all()]

    async def create(self, fields: dict[str, Any]) -> str:
        """建行（worker 短事务——commit 后行对流水线/启动钩子可见）。"""
        row = KbDocument(**fields)
        async with get_session_factory()() as session:
            session.add(row)
            await session.commit()
        return str(row.id)

    async def delete_cascade(self, document_id: str, *, user_id: str) -> None:
        """pg 腿删除：行删（kb_chunks 经 FK CASCADE）+ sources 反向引用清理
        （§4.5 同款——零 REFERENCES 手工维护）。

        评审 P3 纵深防御：DELETE/UPDATE 均带 user_id 卫（与 §4.5 手动删除
        _load_owned 双条件同口径——现路径 doc id 已源自 user 隔离查询，本卫为
        第二道腿）。"""
        doc_uuid = _as_uuid(document_id)
        uid = _as_uuid(user_id)
        async with get_session_factory()() as session:
            await session.execute(
                delete(KbDocument).where(KbDocument.id == doc_uuid, KbDocument.user_id == uid)
            )
            await session.execute(
                update(Source)
                .where(Source.kb_doc_id == doc_uuid, Source.user_id == uid)
                .values(kb_doc_id=None, kb_chunk_ids=None)
            )
            await session.commit()

    async def exists(self, document_id: str) -> bool:
        """行存在性（级联幂等判据——行已删（并发/上次续跑已级联）→ False）。"""
        async with get_session_factory()() as session:
            found = await session.scalar(
                select(KbDocument.id).where(KbDocument.id == _as_uuid(document_id))
            )
            return found is not None


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


class FeishuImportWorker:
    """飞书导入 worker：进程内队列 + 串行消费（图外组件，ADR-011）。

    依赖全注入（jobs/docs 仓储、client 工厂、入库流水线、spec 快照供给、MinIO
    工厂、级联清理协作者）——单测注入假体零网络零 DB；生产经 `get_import_worker`
    装配。`es_indexer` 为删除级联 ES 腿（B18 实装——生产注入 get_kb_es_indexer()，
    注入 None 时零操作=灰度安全兜底）。
    """

    def __init__(
        self,
        *,
        jobs: KbImportJobRepository,
        docs: FeishuImportDocRepository,
        client_provider: Callable[[], FeishuClient],
        pipeline: KbPipeline,
        spec_payload_provider: Callable[[], dict[str, Any]],
        minio_client: Callable[[], Any],
        purger: Any | None = None,
        es_indexer: Any | None = None,
        bucket: TokenBucket | None = None,
    ) -> None:
        self._jobs = jobs
        self._docs = docs
        self._client_provider = client_provider
        self._pipeline = pipeline
        self._spec_payload_provider = spec_payload_provider
        self._minio_factory = minio_client
        self._purger = purger if purger is not None else MinioPrefixPurger()
        self._es_indexer = es_indexer
        self._bucket = bucket or TokenBucket()
        self._deferred_step4: list[RemoteUnit] = []  # 判定序④收集器（计划期零拉取）
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[str] | None = None

    # ---- 队列面 ----

    @property
    def queue(self) -> asyncio.Queue[str]:
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    def enqueue(self, job_id: str) -> None:
        """登记任务（REST BackgroundTasks / 定时任务 / 启动钩子重入队同缝）。"""
        self.queue.put_nowait(str(job_id))

    async def start(self) -> None:
        """启动：消费启动钩子登记行（status IN (pending,running)）+ spawn worker。

        data-model §1.17 :534 / §4 :608：进程内队列无持久化、重启即失——启动时
        重入队未完成任务行续跑（断点游标续跑；pending=已建未启动也须恢复）。
        这是 B20 验收断言「重启后 pending/running 任务行被 worker 实际续跑重入队」
        的消费端（G5）。
        """
        for job_id in await self._jobs.list_requeue_ids():
            self.enqueue(job_id)
        if self._task is None:
            self._task = asyncio.create_task(self._worker_loop(), name="kb-import-worker")

    async def stop(self) -> None:
        """停机：取消 worker（队列内残留由下次启动重入队承接——队列无持久化）。"""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self.process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 单任务失败不拖垮消费循环
                BusinessLog(
                    message=f"kb 导入 worker 处理异常（{type(exc).__name__}，单任务失败已隔离）",
                    action="kb_import_worker",
                    entity="kb_import_job",
                    entity_id=job_id,
                ).error(exc)
            finally:
                self.queue.task_done()

    # ---- 任务推进 ----

    async def process_job(self, job_id: str) -> str:
        """推进单任务至终态；返回终态语义：completed|failed|gone|aborted。

        断点游标（cursor.processed_index）续跑：units 段按游标跳过已推进段；
        deletions 段幂等（行已删时级联为 no-op）。进度五数自游标续算。
        """
        row = await self._jobs.load(job_id)
        if row is None:
            return "gone"
        status: str = row["status"]
        if status in _JOB_TERMINAL_STATUSES:
            return status
        if status == "pending":
            if not await self._jobs.transition(
                job_id,
                expect={"pending"},
                to="running",
                fields={"started_at": datetime.now(UTC)},
            ):
                return "aborted"

        try:
            client = self._client_provider()
        except Exception as exc:  # noqa: BLE001 —— 凭据缺失/SDK 构建失败归任务失败
            await self._fail_job(job_id)
            BusinessLog(
                message=f"kb 导入任务客户端构建失败（{type(exc).__name__}）",
                action="kb_import_job",
                entity="kb_import_job",
                entity_id=job_id,
            ).warning(exc)
            return "failed"

        try:
            space_id = await resolve_space_id(
                client, preferred_space_id=feishu_settings.space_id
            )
        except FeishuClientError:
            await self._fail_job(job_id)
            raise

        cursor: dict[str, Any] = dict(row.get("cursor") or {})
        start_index = int(cursor.get("processed_index", 0))
        progress: dict[str, int] = {**FIVE_ZERO_PROGRESS, **(row.get("progress") or {})}
        failures: list[dict[str, Any]] = list(row.get("failures") or [])
        stats: dict[str, Any] = dict(row.get("stats") or {})
        user_id = str(row["user_id"])

        walk = await client.walk_space_tree(space_id)
        # 评审 P1 完成性门原料：walk.errors（子树失败即停）/截断（深度 6/节点 2000
        # 上限）→ units 缺席 ≠ 远端已删除——删除段见下方门
        walk_incomplete = bool(walk.errors) or walk.truncated_nodes > 0 or walk.truncated_depth > 0
        if start_index == 0:
            for err in walk.errors:
                # 子树遍历失败留痕（不中断整树——client 同语义）；非文档单元、不进
                # 五数；仅首趟登记（断点续跑不重复追加）。评审 P3：failures 只存
                # 归一短语+错误码——原始异常文本（含 parent token 等内部标识）仅落
                # 服务端日志，不经 §4.9 端点外泄
                BusinessLog(
                    message=f"kb walk 子树遍历失败留痕：{str(err)[:500]}",
                    action="kb_import_walk_error",
                    entity="kb_import_job",
                    entity_id=job_id,
                ).warning()
                failures.append(
                    {
                        "title": "知识空间子树",
                        "category": CATEGORY_PERMISSION_DENIED,
                        "message": f"子树遍历失败，已跳过（{_error_code_of(err)}）",
                        "document_id": None,
                    }
                )

        units = _walk_nodes_to_units(walk)
        local_rows = await self._docs.list_local_rows(user_id, space_id=space_id)
        local_by_token = {doc.node_token: doc for doc in local_rows}
        # tokens/obj_map 前移（评审 P1）：判定序④重拉侧 map_blocks 须与 ingest 同参
        # （known_node_tokens/obj_map 全量供给——mention_doc 解析同源），只依赖 units
        tokens = frozenset(u.node_token for u in units)
        obj_map = {u.node_token: u.obj_token for u in units}
        plan = compute_sync_plan(
            units, local_by_token, content_hash_fn=self._defer_step4_pull
        )
        plan = await self._resolve_deferred_step4(
            plan, local_by_token, tokens=tokens, obj_map=obj_map, space_id=space_id
        )
        progress["total_docs"] = len(plan.units)

        for index, unit in enumerate(plan.units):
            if index < start_index:
                continue  # 断点续跑：已推进段零重复拉取
            if unit.action == "skip_trace":
                progress["skipped_docs"] += 1
                failures.append(_skip_trace_entry(unit.remote))
            else:
                existing = local_by_token.get(unit.remote.node_token)
                if existing is not None:
                    # 重导=整文档单元替换（api.md §4.7）：§4.5 同款三存储级联先删后建
                    # ——替换删除不计入 stats.deleted_docs（净效果是替换非删除）
                    await self._cascade_delete(existing, user_id=user_id)
                imported, entry = await self._import_unit(
                    unit.remote,
                    space_id=space_id,
                    client=client,
                    user_id=user_id,
                    tokens=tokens,
                    obj_map=obj_map,
                )
                if imported:
                    progress["imported_docs"] += 1
                else:
                    progress["failed_docs"] += 1
                    if entry is not None:
                        failures.append(entry)
            progress["processed_docs"] += 1
            # 逐单元推进落列（断点游标 + 五数 + 失败清单；乐观卫 expect=running）
            await self._jobs.transition(
                job_id,
                expect={"running"},
                to="running",
                fields={
                    "cursor": {"space_id": space_id, "processed_index": index + 1},
                    "progress": progress,
                    "failures": failures,
                },
            )

        # 删除段（C5=(a)）：差集 → §4.5 同款三存储级联；幂等（行已删 no-op）。
        # 评审 P1 完成性门：删除只对「完整 walk 证明远端已不存在」的差集执行——
        # ① walk 不完整（子树失败/截断）：units 缺席 ≠ 已删除，本轮跳过删除留待
        #   下次完整 walk（导入段不受影响，照常推进）；
        # ② 远端零文档单元而本地非空：「空响应」不判「已清空」（API 静默空集防御）
        #   ——真清空的空间走手动 §4.5 删除，diff 永不承担全删动作。
        deletions = plan.deletions
        if walk_incomplete:
            deletions = []
            BusinessLog(
                message="kb 差集删除跳过：walk 不完整（子树失败/截断）——远端状态未知，"
                "留待下次完整遍历（P1 完成性门）",
                action="kb_import_deletion_gate",
                entity="kb_import_job",
                entity_id=job_id,
            ).warning()
        elif not units and local_by_token:
            deletions = []
            BusinessLog(
                message=f"kb 差集删除跳过：远端零文档单元而本地 {len(local_by_token)} 行"
                "——空响应不判已清空（P1 完成性门，真清空走手动删除）",
                action="kb_import_deletion_gate",
                entity="kb_import_job",
                entity_id=job_id,
            ).warning()
        deleted = 0
        for doc in deletions:
            if await self._cascade_delete(doc, user_id=user_id, job_id=job_id):
                deleted += 1
        if deleted:
            # 计数入 stats 不进五数（api.md §4.7 钉注）；跨断点续跑累加（重删幂等）
            stats["deleted_docs"] = int(stats.get("deleted_docs") or 0) + deleted

        await self._jobs.transition(
            job_id,
            expect={"running"},
            to="completed",
            fields={"finished_at": datetime.now(UTC), "progress": progress, "stats": stats},
        )
        BusinessLog(
            message=(
                f"kb 导入任务完成：total={progress['total_docs']}"
                f" imported={progress['imported_docs']} failed={progress['failed_docs']}"
                f" skipped={progress['skipped_docs']} deleted={deleted}"
            ),
            action="kb_import_job",
            entity="kb_import_job",
            entity_id=job_id,
        ).info()
        return "completed"

    async def _fail_job(self, job_id: str) -> None:
        await self._jobs.transition(
            job_id,
            expect=set(JOB_REQUEUE_STATUSES),
            to="failed",
            fields={"finished_at": datetime.now(UTC)},
        )

    def _defer_step4_pull(self, unit: RemoteUnit) -> str:
        """判定序④收集器（计划期零内容拉取）：④ 命中的单元登记待精判并返回哨兵
        （哨兵≠本地哈希 → 先按候选入计划）；随后 _resolve_deferred_step4 逐一
        重拉 content_hash 精确裁决（不同才重导）。"""
        self._deferred_step4.append(unit)
        return "<deferred-step4>"

    async def _resolve_deferred_step4(
        self,
        plan: SyncPlan,
        local_by_token: dict[str, LocalDoc],
        *,
        tokens: frozenset[str],
        obj_map: dict[str, str],
        space_id: str,
    ) -> SyncPlan:
        """④ 候选精判：重拉内容 → content_hash 同源比对——相同则移出计划（⑤ 出口）。

        tokens/obj_map/space_id 全量透传重拉侧（评审 P1）：与 ingest 同参是「同源
        比对」的结构前提——mention_doc 解析与 sheet 回填两侧不一致时 hash 恒异 →
        obj_edit_time 缺失文档每轮恒判「变更」级联重导死循环。"""
        deferred, self._deferred_step4 = self._deferred_step4, []
        if not deferred:
            return plan
        deferred_tokens = {u.node_token for u in deferred}
        kept: list[PlanUnit] = []
        for plan_unit in plan.units:
            remote = plan_unit.remote
            if (
                plan_unit.action == "import"
                and not remote.obj_edit_time
                and remote.node_token in deferred_tokens
                and remote.node_token in local_by_token
            ):
                local = local_by_token[remote.node_token]
                pulled = await self._pull_content_hash(
                    remote, tokens=tokens, obj_map=obj_map, space_id=space_id
                )
                if pulled == (local.content_hash or ""):
                    continue  # ④ 相同 → 未变更（不计 total_docs）
            kept.append(plan_unit)
        plan.units = kept
        return plan

    # ---- 单元导入 ----

    async def _import_unit(
        self,
        unit: RemoteUnit,
        *,
        space_id: str,
        client: FeishuClient,
        user_id: str,
        tokens: frozenset[str],
        obj_map: dict[str, str],
    ) -> tuple[bool, dict[str, Any] | None]:
        """重导单文档单元；返回 (imported, 失败条目|None)。

        失败分类清单重试（ARCH B20 行）：瞬态类（block/attachment 下载）有界重试
        UNIT_MAX_ATTEMPTS 次（令牌桶节流）；确定性失败（白名单/超限/装配）一次定裁决。
        """
        if unit.obj_type == "docx":
            return await self._import_docx_unit(
                unit, space_id=space_id, client=client, user_id=user_id,
                tokens=tokens, obj_map=obj_map,
            )
        return await self._import_file_unit(
            unit, space_id=space_id, client=client, user_id=user_id
        )

    async def _import_docx_unit(
        self,
        unit: RemoteUnit,
        *,
        space_id: str,
        client: FeishuClient,
        user_id: str,
        tokens: frozenset[str],
        obj_map: dict[str, str],
    ) -> tuple[bool, dict[str, Any] | None]:
        blocks, entry = await self._fetch_blocks(unit, client)
        if blocks is None:
            return False, entry  # 重试终败：失败条目随返回登记
        try:
            doc_id = await self._ingest_docx(
                unit, blocks=blocks, space_id=space_id, user_id=user_id,
                tokens=tokens, obj_map=obj_map, client=client,
            )
        except Exception as exc:  # noqa: BLE001 —— 装配/落桶/建行失败归类 normalize_failed
            BusinessLog(
                message=f"kb 导入装配失败（{type(exc).__name__}）",
                action="kb_import_unit",
                entity="kb_document",
                entity_id=unit.node_token,
            ).warning(exc)
            return False, _failure_entry(
                unit, CATEGORY_NORMALIZE_FAILED, f"文档装配失败：{type(exc).__name__}"
            )
        self._pipeline.enqueue(doc_id)  # G10：行建成进流水线即计（其后失败不回写）
        return True, None

    async def _import_file_unit(
        self, unit: RemoteUnit, *, space_id: str, client: FeishuClient, user_id: str
    ) -> tuple[bool, dict[str, Any] | None]:
        ext = unit.title.rsplit(".", 1)[-1].lower() if "." in unit.title else ""
        if ext not in kb_settings.allowed_types:
            # 白名单外（api.md §4.7 钉注：附件类型与大小同上传白名单）——先拒零下载
            return False, _failure_entry(
                unit, CATEGORY_ATTACHMENT_UNSUPPORTED, f"附件类型不支持（.{ext or '无扩展名'}）"
            )
        data, entry = await self._download_attachment(unit, client)
        if data is None:
            return False, entry  # 重试终败/超限：失败条目随返回登记
        if len(data) > kb_settings.max_file_mb * 1024 * 1024:
            # 纵深防御（封顶读已保证下游不越限——本卫防 max_bytes 缺省路径回归；
            # SDK 非流式，内存峰值面见 feishu_client._read_download_stream 注记）
            return False, _failure_entry(
                unit,
                CATEGORY_ATTACHMENT_UNSUPPORTED,
                f"附件超过大小上限（{kb_settings.max_file_mb}MB）",
            )
        try:
            doc_id = await self._ingest_file(
                unit, ext=ext, data=data, user_id=user_id, space_id=space_id
            )
        except Exception as exc:  # noqa: BLE001 —— 落桶/建行失败
            BusinessLog(
                message=f"kb 附件入库失败（{type(exc).__name__}）",
                action="kb_import_unit",
                entity="kb_document",
                entity_id=unit.node_token,
            ).warning(exc)
            return False, _failure_entry(
                unit, CATEGORY_NORMALIZE_FAILED, f"附件入库失败：{type(exc).__name__}"
            )
        self._pipeline.enqueue(doc_id)  # G10
        return True, None

    async def _fetch_blocks(
        self, unit: RemoteUnit, client: FeishuClient
    ) -> tuple[list[Any] | None, dict[str, Any] | None]:
        """blocks 拉取（瞬态有界重试）；终败 → (None, block_fetch_failed 条目)。"""
        last_error: Exception | None = None
        for attempt in range(UNIT_MAX_ATTEMPTS):
            if attempt:
                await self._bucket.acquire()
            try:
                result = await client.list_document_blocks(unit.obj_token)
                return list(result.items), None
            except FeishuClientError as exc:
                last_error = exc
        BusinessLog(
            message=f"kb blocks 拉取重试耗尽（{last_error}）",
            action="kb_import_unit",
            entity="kb_document",
            entity_id=unit.node_token,
        ).warning(last_error)
        return None, _failure_entry(
            unit, CATEGORY_BLOCK_FETCH_FAILED, "文档块拉取失败（重试耗尽）"
        )

    async def _download_attachment(
        self, unit: RemoteUnit, client: FeishuClient
    ) -> tuple[bytes | None, dict[str, Any] | None]:
        """附件下载（瞬态有界重试；评审 P2：max_bytes 封顶读——超限提前终止弃流、
        不向下传递超大产物；SDK 非流式的内存峰值面见 client `_read_download_stream`
        注记）。终败 → (None, 分类条目)：超限=attachment_unsupported（确定性不重试）；
        下载失败=attachment_download_failed（瞬态重试）。

        通道：`download_file`（云空间文件正解）——media.download 对 wiki file 节点
        恒 403 空体（2026-11-25 三通道对照探针实证）。"""
        max_bytes = kb_settings.max_file_mb * 1024 * 1024
        last_error: Exception | None = None
        for attempt in range(UNIT_MAX_ATTEMPTS):
            if attempt:
                await self._bucket.acquire()
            try:
                return (
                    await client.download_file(unit.obj_token, max_bytes=max_bytes),
                    None,
                )
            except FeishuAttachmentTooLargeError:
                # 确定性失败不重试——重试只救抖动不救裁决
                return None, _failure_entry(
                    unit,
                    CATEGORY_ATTACHMENT_UNSUPPORTED,
                    f"附件超过大小上限（{kb_settings.max_file_mb}MB）",
                )
            except FeishuClientError as exc:
                last_error = exc
        BusinessLog(
            message=f"kb 附件下载重试耗尽（{last_error}）",
            action="kb_import_unit",
            entity="kb_document",
            entity_id=unit.node_token,
        ).warning(last_error)
        return None, _failure_entry(
            unit, CATEGORY_ATTACHMENT_DOWNLOAD_FAILED, "附件下载失败（重试耗尽）"
        )

    async def _build_docx_tree(
        self,
        *,
        blocks: list[Any],
        space_id: str,
        node_token: str,
        title: str,
        obj_type: str,
        obj_edit_time: int | None,
        catalog_ids: Sequence[str],
        catalog_path: Sequence[str],
        tokens: frozenset[str],
        obj_map: dict[str, str],
        client: FeishuClient,
    ) -> tuple[IrNode, FeishuMappingResult, SheetBackfillReport]:
        """SDK 直读共享构建段（评审 P1：判定序④「同源比对」的结构前提）——ingest
        与 `_pull_content_hash` 共用同一组合：`map_blocks`（tokens/obj_map 全量供给
        ——mention_doc 解析同源）→ `build_document_node` → catalog 链（C9=(a)）→
        `attach_block_tree` → sheet(30) 装配后回填（§1.18 v1.20 30 行——失败保留
        占位不算文档失败，27 行 image 先例同款；超限截断计数留痕原料）。

        本地 content_hash 是回填后 ir.json 的切分产物；重拉侧缺回填段或 tokens/
        obj_map 缺供时，含 sheet 块或树内 mention_doc 引用的文档两侧 hash 恒异 →
        ④ 判定序（obj_edit_time 缺失/0）下每轮同步恒判「变更」级联重导死循环。
        """
        mapping = map_blocks(
            blocks,
            space_id=space_id,
            node_token=node_token,
            known_node_tokens=tokens,
            node_token_by_obj_token=obj_map,
        )
        doc_node = build_document_node(
            node_token=node_token,
            title=title,
            obj_type=obj_type,
            obj_edit_time=obj_edit_time,
            space_id=space_id,
        )
        doc_node.catalog_ids = list(catalog_ids)  # C9=(a)：含自身标题物化链
        doc_node.catalog_path = list(catalog_path)
        attach_block_tree(doc_node, mapping.refs, root_order=mapping.root_order)

        async def _fetch_sheet_values(sheet_token: str) -> SheetValuesData:
            return await client.fetch_sheet_values(sheet_token)

        backfill = await backfill_sheet_placeholders(doc_node, _fetch_sheet_values)
        return doc_node, mapping, backfill

    async def _ingest_docx(
        self,
        unit: RemoteUnit,
        *,
        blocks: list[Any],
        space_id: str,
        user_id: str,
        tokens: frozenset[str],
        obj_map: dict[str, str],
        client: FeishuClient,
    ) -> str:
        """SDK 直读入库：blocks → IR（含内容层）→ ir.json 落桶 → 建行 chunking。

        行以 status="chunking" 建成——IR 已就位，流水线自切分段续跑（chunking →
        indexing → ready）；宕机残留行经启动钩子文档重入队承接（中间态在
        _KB_REQUEUE_STATUSES 内）。content_hash 由流水线 chunking 段写入（OQ-34）。
        """
        doc_node, mapping, backfill = await self._build_docx_tree(
            blocks=blocks,
            space_id=space_id,
            node_token=unit.node_token,
            title=unit.title,
            obj_type=unit.obj_type,
            obj_edit_time=unit.obj_edit_time,
            catalog_ids=unit.catalog_ids,
            catalog_path=unit.catalog_path,
            tokens=tokens,
            obj_map=obj_map,
            client=client,
        )
        doc_id = uuid.uuid4()
        await put_ir_json(user_id, str(doc_id), doc_node, client=self._minio_factory())
        degraded = dict(mapping.degraded_blocks)
        for key, count in backfill.degraded_delta().items():
            degraded[key] = degraded.get(key, 0) + count
        parse_meta: dict[str, Any] = {
            "pages": None,
            "tables": sum(1 for ref in mapping.refs if ref.node.type.value == "table"),
            "code_blocks": sum(1 for ref in mapping.refs if ref.node.type.value == "code"),
            "parser": PARSER_FEISHU_BLOCKS,  # A34 GWT#1 通道标识（api.md §4.7 钉注）
            "feishu": build_feishu_parse_meta(
                space_id=space_id,
                node_token=unit.node_token,
                obj_type=unit.obj_type,
                obj_edit_time=unit.obj_edit_time,
                degraded_blocks=degraded,
            ),
        }
        sheet_backfill_block = backfill.parse_meta_block()
        if sheet_backfill_block:
            parse_meta["feishu"]["sheet_backfill"] = sheet_backfill_block
        return await self._docs.create(
            {
                "id": doc_id,
                "user_id": _as_uuid(user_id),
                "doc_type": "docx",
                "title": (unit.title or unit.node_token)[:512],
                "source_url": None,
                "file_object_key": None,  # SDK 直读无原件（正文即 blocks）
                "file_size_bytes": None,
                "status": "chunking",
                "fail_reason": None,
                "chunk_count": 0,
                "parse_meta": parse_meta,
                "catalog_ids": list(unit.catalog_ids),
                "catalog_path": catalog_path_string(unit.catalog_path),
            }
        )

    async def _ingest_file(
        self, unit: RemoteUnit, *, ext: str, data: bytes, user_id: str, space_id: str
    ) -> str:
        """附件入库：原件落 MinIO（对象先于记录）→ 建行 pending（走归一化管线）。

        parse_meta 预置 feishu 四键（同步 diff 寻址）——归一化产物在流水线
        normalizing 段合并保留该键（pipeline._run_normalizing）。
        """
        from app.rag.feishu_client import store_attachment

        doc_id = uuid.uuid4()
        key = await store_attachment(
            user_id, str(doc_id), unit.title, data, minio_client=self._minio_factory()
        )
        return await self._docs.create(
            {
                "id": doc_id,
                "user_id": _as_uuid(user_id),
                "doc_type": ext,
                "title": (unit.title or unit.node_token)[:512],
                "source_url": None,
                "file_object_key": key,
                "file_size_bytes": len(data),
                "status": "pending",
                "fail_reason": None,
                "chunk_count": 0,
                "parse_meta": {
                    "feishu": build_feishu_parse_meta(
                        space_id=space_id,
                        node_token=unit.node_token,
                        obj_type=unit.obj_type,
                        obj_edit_time=unit.obj_edit_time,
                        degraded_blocks={},
                    )
                },
                "catalog_ids": list(unit.catalog_ids),
                "catalog_path": catalog_path_string(unit.catalog_path),
            }
        )

    # ---- content_hash 兜底（判定序④）与级联删除（C5=(a)）----

    async def _pull_content_hash(
        self,
        unit: RemoteUnit,
        *,
        tokens: frozenset[str],
        obj_map: dict[str, str],
        space_id: str,
    ) -> str:
        """④ 兜底判据：重拉内容 → 与 kb_documents.content_hash 同源口径的 sha256。

        docx：blocks → **共享构建段**（`_build_docx_tree`——tokens/obj_map 全量供给
        + sheet(30) 回填，评审 P1：与 ingest 同组合是同源比对的结构前提；config 取
        `ChunkingConfig()` 缺省不影响 hash——B19 方案 A 起 document_content_hash 由
        叶子单元文本 + 标题结构清单构成，仍与切分窗配置无关）→ 全文扁平正文 +
        结构清单 hash；file：下载原件 → 归一化正文 → 同口径 hash。拉取失败 →
        哨兵值（与本地恒异 → 保守重导，不静默跳过）。
        """
        try:
            from app.rag.chunker import ChunkingConfig, chunk_forest

            if unit.obj_type == "docx":
                client = self._client_provider()
                result = await client.list_document_blocks(unit.obj_token)
                doc_node, _mapping, _backfill = await self._build_docx_tree(
                    blocks=list(result.items),
                    space_id=space_id,
                    node_token=unit.node_token,
                    title=unit.title,
                    obj_type=unit.obj_type,
                    obj_edit_time=unit.obj_edit_time,
                    catalog_ids=unit.catalog_ids,
                    catalog_path=unit.catalog_path,
                    tokens=tokens,
                    obj_map=obj_map,
                    client=client,
                )
                results = await asyncio.to_thread(
                    chunk_forest, doc_node, config=ChunkingConfig()
                )
            else:
                from app.rag.normalizers import normalize_document

                client = self._client_provider()
                ext = unit.title.rsplit(".", 1)[-1].lower() if "." in unit.title else "txt"
                # 有意不封顶（max_bytes=None）：封顶会把超限文件在 diff 阶段判为恒
                # 变更；未变文件靠 hash 相等跳过、真变了才在入库处按
                # attachment_unsupported 失败——评审确认此口径
                data = await client.download_file(unit.obj_token)  # 云空间文件通道（与 ingest 同源）
                normalized = await asyncio.to_thread(
                    normalize_document, data, ext, doc_kind="upload", file_object_key=None
                )
                results = await asyncio.to_thread(
                    chunk_forest, normalized.document, config=ChunkingConfig()
                )
            return results[0].document_content_hash if results else ""
        except Exception as exc:  # noqa: BLE001 —— 兜底判据失败=保守重导
            BusinessLog(
                message=f"kb content_hash 兜底拉取失败（{type(exc).__name__}）——保守重导",
                action="kb_import_diff",
                entity="kb_document",
                entity_id=unit.node_token,
            ).warning(exc)
            return "<pull-failed>"

    async def _cascade_delete(
        self, doc: LocalDoc, *, user_id: str, job_id: str = ""
    ) -> bool:
        """§4.5 同款三存储级联：pg 行删（chunks FK CASCADE + sources 反向清理）→
        ES 删除腿（B18 seam，None=零操作）→ MinIO 前缀清理 best-effort。

        评审 P2 审计留痕：删除前逐文档 BusinessLog（document_id/node_token/title/
        job_id，action=kb_import_cascade_delete）——后台销毁性动作可回查（§4.5
        手动删除尚有 kb_purge 日志，此处对齐审计粒度）。
        返回删除是否发生（行不存在=并发已删 → False，幂等）。
        """
        if not await self._docs.exists(doc.document_id):
            return False
        BusinessLog(
            message=(
                f"kb 差集级联删除：document_id={doc.document_id}"
                f" node_token={doc.node_token} title={doc.title or ''}"
                f" job_id={job_id}"
            ),
            action="kb_import_cascade_delete",
            entity="kb_document",
            entity_id=doc.document_id,
        ).info()
        await self._docs.delete_cascade(doc.document_id, user_id=user_id)
        if self._es_indexer is not None:
            try:
                await self._es_indexer.delete_document(
                    kb_doc_id=doc.document_id, user_id=user_id
                )
            except Exception as exc:  # noqa: BLE001 —— ES 腿失败不阻断级联主流程
                BusinessLog(
                    message=f"kb ES 删除腿失败（{type(exc).__name__}）——best-effort 继续",
                    action="kb_import_cascade",
                    entity="kb_document",
                    entity_id=doc.document_id,
                ).warning(exc)
        try:
            await self._purger.purge(user_id=user_id, doc_id=doc.document_id)
        except Exception as exc:  # noqa: BLE001 —— MinIO 抖动不阻断（best-effort）
            BusinessLog(
                message=f"kb MinIO 前缀清理失败（{type(exc).__name__}）——best-effort 继续",
                action="kb_import_cascade",
                entity="kb_document",
                entity_id=doc.document_id,
            ).warning(exc)
        return True


# ---------------------------------------------------------------------------
# 定时全量 diff（api.md §4.7——24h 代码常量；注册见 app/crons/feishu_sync.py）
# ---------------------------------------------------------------------------

#: 定时同步间隔（代码常量，不入 Nacos——api.md §4.7 钉注「间隔 24h（代码常量）」）
SYNC_INTERVAL_SECONDS = 24 * 3600


async def scheduled_sync_once(
    *,
    jobs: KbImportJobRepository,
    docs: FeishuImportDocRepository,
    client_provider: Callable[[], FeishuClient],
    enqueue: Callable[[str], None],
    preferred_space_id: str | None = None,
) -> int:
    """定时全量 diff 单轮（crons/feishu_sync.py 周期体）——返回建行数。

    - 撞进行中任务（任意 pending/running）→ 本轮跳过不建行（G7）；
    - 服务对象=已有飞书来源文档的用户（定时 diff 只服务存量导入用户——首导走手动）；
    - 判定侧不重拉内容（content_hash_fn=None——④ 单元保守视为候选，建行交 worker
      执行期精确判定）；
    - diff 零变更且无删除 → 不建行（仅日志）；仅删除也建行（轻审 P2 钉明——
      trigger=scheduled、建行五数为 0，删除计数由执行期入 stats.deleted_docs）。
    """
    try:
        client = client_provider()
    except Exception as exc:  # noqa: BLE001 —— 凭据缺失/SDK 失败：本轮放弃零建行
        BusinessLog(
            message=f"kb 定时同步客户端构建失败（{type(exc).__name__}）——本轮放弃",
            action="kb_import_sync",
        ).warning(exc)
        return 0
    if await jobs.has_active():
        BusinessLog(
            message="kb 定时同步跳过：已有进行中的导入任务（本轮不建行）",
            action="kb_import_sync",
        ).info()
        return 0
    users = await docs.list_feishu_user_ids()
    if not users:
        return 0
    space_id = await resolve_space_id(
        client,
        preferred_space_id=(
            feishu_settings.space_id if preferred_space_id is None else preferred_space_id
        ),
    )
    walk = await client.walk_space_tree(space_id)
    # 评审 P1 完成性门（定时判定侧同口径）：walk 不完整（子树失败/截断）或远端零
    # 文档单元而本地非空 → 远端状态未知 → 本轮零建行（plan.meaningful 因差集为真
    # 也不建——「未知即删」不被授权，api.md §4.7 C5=(a) 只授权删「远端已不存在」）
    if (
        walk.errors
        or walk.truncated_nodes > 0
        or walk.truncated_depth > 0
    ):
        BusinessLog(
            message="kb 定时同步跳过：walk 不完整（子树失败/截断）——远端状态未知，"
            "本轮零建行（P1 完成性门）",
            action="kb_import_sync",
        ).warning()
        return 0
    units = _walk_nodes_to_units(walk)
    if not units:
        BusinessLog(
            message="kb 定时同步跳过：远端零文档单元——空响应不判已清空"
            "（P1 完成性门；真清空走手动删除）",
            action="kb_import_sync",
        ).warning()
        return 0
    created = 0
    for user_id in users:
        local_rows = await docs.list_local_rows(user_id, space_id=space_id)
        plan = compute_sync_plan(
            units, {d.node_token: d for d in local_rows}, content_hash_fn=None
        )
        if not plan.meaningful:
            BusinessLog(
                message=f"kb 定时同步零变更（user={user_id[:8]}…）——不建行",
                action="kb_import_sync",
            ).info()
            continue
        job_id = await jobs.create_job(
            user_id=user_id, trigger="scheduled", progress=dict(FIVE_ZERO_PROGRESS), stats={}
        )
        enqueue(job_id)
        created += 1
        BusinessLog(
            message=(
                f"kb 定时同步建行：user={user_id[:8]}… job={job_id[:8]}…"
                f" imports={sum(1 for u in plan.units if u.action == 'import')}"
                f" deletions={len(plan.deletions)}"
            ),
            action="kb_import_sync",
        ).info()
    return created


# ---------------------------------------------------------------------------
# 生产装配（api 依赖缝——单测经 dependency_overrides 替换）
# ---------------------------------------------------------------------------

_import_worker_instance: FeishuImportWorker | None = None


def get_import_worker() -> FeishuImportWorker:
    """进程级单例（REST BackgroundTasks / 定时任务 / 启动钩子重入队同缝）。

    首次调用惰性装配：真仓储 + get_default_feishu_client（凭据只经 settings 单例读）
    + KB 入库流水线单例 + MinIO 惰性工厂 + ES 索引器（B18——级联删除 ES 腿接线，
    消费点与签名零改动）。start()/stop() 由宿主 lifespan 承担。
    """
    global _import_worker_instance
    if _import_worker_instance is None:
        from app.rag.indexers.es_indexer import get_kb_es_indexer

        _import_worker_instance = FeishuImportWorker(
            jobs=KbImportJobRepository(),
            docs=FeishuImportDocRepository(),
            client_provider=get_default_feishu_client,
            pipeline=get_kb_pipeline(),
            spec_payload_provider=_default_spec_payload,
            minio_client=get_kb_minio_client,
            es_indexer=get_kb_es_indexer(),
        )
    return _import_worker_instance
