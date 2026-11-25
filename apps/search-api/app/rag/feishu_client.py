"""飞书开放平台 SDK 客户端封装（M3 批次 3 / B20 A 线①——lark-oapi==1.8.0）。

工单面（ARCH B20 行 / plan-m3-b20-contract-proposal-20261125.md §2「零契约面清单」
首行）：tenant token 获取与缓存、树遍历分页、blocks 分页、素材/附件下载——纯实现；
对外的唯一契约是 IR（data-model §1.18）与 kb_* 表（§1.7/§1.17）。

调用形态沿 notebook `feishu_kb_tree_probe.ipynb` 一手先例（探针实证）：
- wiki v2 `space.list` / `space_node.list`（parent_node_token + page_token 翻页）；
- docx v1 `document_block.list`（page_size=500 分页翻全）；
- drive v1 `file.download`（**云空间文件**附件下载——wiki file 节点）；
  `media.download`（仅云文档内素材，B21 图片管线消费）；
- 节流 0.65s/次（wiki nodes 100 次/分钟频控的保守值）+ 99991400 指数退避。

凭据纪律：FEISHU_APP_ID/SECRET 只从 `feishu_settings` 单例（.env FEISHU_ 组）读取，
零打印零落盘零硬编码；一切错误文本经 `redact` 脱敏后归一 FeishuClientError（SDK
异常体可能携带含 token 的完整 URL）。同步 SDK 面（requests 体系）一律
asyncio.to_thread 下放（sleep 亦在 worker 线程内，不阻塞事件循环）。
"""

import asyncio
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.config import feishu_settings

# ── 频控/退避/防御常量（notebook 先例值；零契约面——工单实装面）──
#: wiki nodes 100 次/分钟 → 保守 0.65s/次（notebook feishu_kb_tree_probe.ipynb cell 6）
FEISHU_NODE_CALL_MIN_INTERVAL_S = 0.65
#: 飞书频控错误码（99991400 → 指数退避重试，notebook 先例）
FEISHU_RATE_LIMIT_CODE = 99991400
#: sheets token 不可解析错误码（1310251 = Path param :spreadsheet_token invalid）——
#: 单列 resolve_failed 分类（非 permission：2026-11-25 活体排查实证，v3 定位端点对
#: 不可解析/不可访问的内嵌 spreadsheet 真/畸变 token 同报此码，误归 permission 曾带偏归因）
FEISHU_SHEET_TOKEN_INVALID_CODE = 1310251
#: 单层/单文档翻页防御上限（远超正常规模；超限截断并留痕，不无限翻页）
FEISHU_MAX_PAGES = 100
#: 树遍历防御上限（notebook：深度 6 / 节点 2000——探针实测 931 节点量级的余量）
FEISHU_MAX_DEPTH = 6
FEISHU_MAX_NODES = 2000
#: docx blocks 分页 page_size（notebook 先例 500）
DOCX_BLOCK_PAGE_SIZE = 500
#: 下载分块读取块长（1MB——封顶读步长，评审 P2 内存面；media/file 两通道共用）
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024

# ── sheet(30) 回填护栏（data-model §1.18 v1.20 30 行钉定值——代码常量面，
#    纯逻辑防御值与环境无关不入 Nacos；契约定值实现批不得静默改，调整走增量 diff）──
#: 行 ≤500（超限截断保留前 N + 计数留痕）
SHEET_MAX_ROWS = 500
#: 列 ≤64
SHEET_MAX_COLS = 64
#: 单元格 ≤200 字符
SHEET_MAX_CELL_CHARS = 200
#: v2 values 分页行窗（实现面翻页驱动；500 行护栏 → cap 全取 + 1 页溢出证实 ≤6 页）
SHEET_VALUES_PAGE_ROWS = 100


class FeishuClientError(Exception):
    """飞书 SDK 面 IO/凭据失败归一（原异常信息脱敏后链入；密钥零回显）。

    `code`：API 错误码（构造器声明属性——失败分类面 `fetch_sheet_values`/
    `_classify_sheet_error` 的结构化原料；API 失败路径携带具体码，连接异常路径
    None。动态挂载形态被 pyright 静态检查禁止，第 2 轮门禁修复改构造器参数）。
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class FeishuAttachmentTooLargeError(FeishuClientError):
    """下载内容超过大小上限（download_file/download_media 的 max_bytes 封顶读提前
    终止——防下游误用超大产物的 worker 内存/存储面；worker 捕获后归
    attachment_unsupported 分类，不进瞬态重试）。"""


class FeishuSheetValuesError(FeishuClientError):
    """sheets 值通道失败归一（错误文本脱敏后链入；`category` 失败分类供 worker
    parse_meta.feishu.sheet_backfill.failed_categories 留痕——§1.18 30 行
    「回填失败保留占位 + parse_meta 留痕」的分类原料）。"""

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


def _classify_sheet_error(code: int | None) -> str:
    """sheets 值通道失败分类（官方排查文档码族；无码=连接异常）：
    - 99991672 应用 scope 缺失 / 13102* 文档权限未授权 → permission；
    - 1310251 Path param :spreadsheet_token invalid（token 不可解析——独立 drive 对象
      不可访问/ACL 未共享的同义面）→ resolve_failed（族内单列，避免误导归因）；
    - 99991400 频控（重试耗尽）→ rate_limited；其余码 → other。"""
    if code is None:
        return "connection"
    if code == FEISHU_RATE_LIMIT_CODE:
        return "rate_limited"
    if code == FEISHU_SHEET_TOKEN_INVALID_CODE:
        return "resolve_failed"
    if code == 99991672 or str(code).startswith("13102"):
        return "permission"
    return "other"


def _col_letter(num: int) -> str:
    """1 基列号 → 电子表格列字母（1→A … 26→Z、27→AA … 64→BL；range 上界段）。"""
    letters = ""
    n = num
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


@dataclass(slots=True)
class SheetValuesData:
    """sheets 值矩阵回填产物（护栏截断后）：rows 物化矩阵 + 三类截断计数
    （parse_meta.feishu.sheet_backfill.truncated_* 留痕原料；0 值由调用方省略键）。

    truncated_rows 为**证实窗内计数**：分页预算 = 行护栏全取 + 一页溢出证实页，
    超证实窗的行不再翻页（护栏预算面）——计数为已达成的下界（超限护栏的本意即
    遏制病态大表，不为精确计数无限烧配额）。"""

    rows: list[list[str]] = field(default_factory=list)
    truncated_rows: int = 0
    truncated_cols: int = 0
    truncated_cells: int = 0


def _apply_sheet_guardrails(fetched: list[list[Any]]) -> SheetValuesData:
    """护栏截断（§1.18 30 行：超限截断不丢弃——保留前 N + 计数留痕）：
    行取前 500 / 列取前 64 / 单元格截前 200 字符；None→空串、非字符串→str（ToString
    兜底面）；truncated_cells 只计物化区（护栏外行列不再计格数）。"""
    truncated_rows = max(0, len(fetched) - SHEET_MAX_ROWS)
    kept = fetched[:SHEET_MAX_ROWS]
    width = max((len(r) for r in kept), default=0)
    # 列截断计数（评审 P3）：仅当证实列（第 65 列起）存在非空格时计数——防 API
    # 按窗宽补齐空列的假阳性；窗内下界口径同行护栏（超窗列不再拉取）
    confirmation_non_empty = any(
        cell not in (None, "")
        for r in kept
        for cell in r[SHEET_MAX_COLS:]
    )
    truncated_cols = max(0, width - SHEET_MAX_COLS) if confirmation_non_empty else 0
    truncated_cells = 0
    rows: list[list[str]] = []
    for r in kept:
        out_row: list[str] = []
        for value in r[:SHEET_MAX_COLS]:
            if value is None:
                cell = ""
            elif isinstance(value, str):
                cell = value
            else:
                cell = str(value)
            if len(cell) > SHEET_MAX_CELL_CHARS:
                cell = cell[:SHEET_MAX_CELL_CHARS]
                truncated_cells += 1
            out_row.append(cell)
        rows.append(out_row)
    return SheetValuesData(
        rows=rows,
        truncated_rows=truncated_rows,
        truncated_cols=truncated_cols,
        truncated_cells=truncated_cells,
    )


def redact(text: str, *secrets: str) -> str:
    """异常/日志文本脱敏：已知密钥值**全掩码**（不保留任何前缀字符——前缀残留
    仍是密钥信息）+ 模式化 scrub（access_token=/token= 查询段/JSON 值——tenant
    token 若经 SDK 异常文本泄漏同样被掩；空密钥跳过）。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    # 模式化 scrub：token 类查询参数/字段值（大小写不敏感；值到 &/空白/引号/结尾）
    text = re.sub(
        r"(access_token=|token=|refresh_token=)([^\s&\"']+)",
        r"\1***",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _build_lark_client(app_id: str, app_secret: str, api_base: str, timeout_s: float) -> Any:
    """lark Client 构造（惰性 import——SDK 已入生产依赖，此处仅为 import 面延迟；
    Client 构建为离线操作，tenant_access_token 由 SDK 内部缓存、到期自动重取）。"""
    import lark_oapi as lark

    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .domain(api_base)
        .timeout(timeout_s)
        .log_level(lark.LogLevel.CRITICAL)  # SDK 内部 logger 压 CRITICAL（零 token 污染日志面）
        .build()
    )


def _coerce_edit_time(value: Any) -> int | None:
    """wiki 节点 obj_edit_time 归一（int | 数字串 → int；缺字段/非法 → None）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class NodeInfo:
    """wiki 节点条目（worker 侧映射 IrNode 目录层的输入形态；字段对齐 SDK 返回）。

    `obj_edit_time`：wiki 节点编辑时间（epoch 秒｜None）——同步 diff 判定序③④
    变更判据原料（meta.obj_edit_time 契约键，data-model §1.18 通用规则；SDK 形态
    防御：缺字段/非数字归一 None，worker 侧 edit_time_of 同口径）。"""

    node_token: str
    obj_token: str
    obj_type: str
    parent_node_token: str
    node_type: str
    has_child: bool
    title: str
    obj_edit_time: int | None = None


@dataclass(slots=True)
class SpaceInfo:
    space_id: str
    name: str


@dataclass(slots=True)
class TreeWalkResult:
    """全树遍历产物：节点平铺（BFS 发现序）+ 留痕。

    shortcut 本体保留（node_type=shortcut）、子树不展开（tree_assembler 防御④同口径
    消费）；子树失败即停该子树、errors 留痕不中断整树（notebook 同语义）。
    """

    space_id: str
    nodes: list[NodeInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    shortcut_count: int = 0
    truncated_nodes: int = 0
    truncated_depth: int = 0


@dataclass(slots=True)
class BlockPageResult:
    """docx blocks 全量翻页产物；truncated=翻页防御上限截断留痕。"""

    items: list[Any]
    truncated: bool = False


class FeishuClient:
    """lark-oapi 封装：凭据显式传入（生产走 `get_default_feishu_client`）。

    client 注入面供测试假体/复用；缺省按 (app_id, app_secret, api_base) 惰性构建
    lark Client（构建一次进程内复用——tenant_access_token 的获取与缓存由 SDK 内部
    负责，本层缓存 = Client 实例复用，避免每调用重建触发 token 重取；评审 P3 一手
    核实：SDK 缓存形态=进程内存 LocalCache（lark_oapi/core/cache/local_cache.py，
    纯 dict+过期戳），零磁盘持久化——升级/更换 SDK 时复核此项）。
    sleep_fn/clock 为注入缝（测试零等待）；throttle/backoff 状态按实例封闭
    （threading.Lock 守护——to_thread 多线程并发安全）。
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        api_base: str = "https://open.feishu.cn",
        client: Any | None = None,
        timeout_s: float = 20.0,
        min_interval_s: float = FEISHU_NODE_CALL_MIN_INTERVAL_S,
        backoff_base_s: float = 1.0,
        max_retries: int = 4,
        max_pages: int = FEISHU_MAX_PAGES,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app_id = app_id
        self._app_secret = app_secret
        self.api_base = api_base
        self.timeout_s = timeout_s
        self.min_interval_s = min_interval_s
        self.backoff_base_s = backoff_base_s
        self.max_retries = max_retries
        self.max_pages = max_pages
        self._sleep = sleep_fn
        self._clock = clock
        self._throttle_lock = threading.Lock()
        self._last_call_ts = 0.0
        if client is not None:
            self._sdk = client
        else:
            if not app_id or not app_secret:
                raise FeishuClientError(
                    "飞书凭据未配置（FEISHU_APP_ID / FEISHU_APP_SECRET 只进 .env；"
                    "见 api.md §4.7 500001 语义）"
                )
            self._sdk = _build_lark_client(app_id, app_secret, api_base, timeout_s)

    # ── 内部：脱敏 + 节流 + 同步调用下放 ──

    def _redact(self, text: str) -> str:
        return redact(text, self._app_secret, self.app_id)

    def _throttle(self) -> None:
        """0.65s 最小调用间隔（实例封闭 + 锁守护；sleep 在 to_thread worker 线程内）。"""
        with self._throttle_lock:
            gap = self._clock() - self._last_call_ts
            wait = self.min_interval_s - gap
            if wait > 0:
                self._sleep(wait)
            self._last_call_ts = self._clock()

    def _call_with_backoff(self, call: Callable[[], Any]) -> Any:
        """节流 + 99991400 指数退避重试（背板与 notebook 一致：base × 2**attempt）。

        重试耗尽仍限频 → FeishuClientError（调用方在树遍历层转 (items, err) 软失败）。
        连接异常归一 FeishuClientError（脱敏后链入）。
        """
        resp: Any = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = call()
            except Exception as exc:
                raise FeishuClientError(
                    f"飞书连接异常：{self._redact(f'{type(exc).__name__}: {exc}')}"
                ) from exc
            if getattr(resp, "code", None) == FEISHU_RATE_LIMIT_CODE:
                self._sleep(self.backoff_base_s * (2**attempt))
                continue
            break
        code = getattr(resp, "code", None)
        if resp is None or code != 0:
            raise FeishuClientError(
                f"飞书 API 失败：code={code} msg={self._redact(getattr(resp, 'msg', '') or '')}",
                code=code,  # 结构化错误码（分类原料；消息格式零变化）
            )
        return resp

    def _sync(self, fn: Callable[[], Any]) -> Any:
        return asyncio.to_thread(fn)

    # ── wiki：知识空间列表 / 单层子节点 / 全树遍历 ──

    async def list_spaces(self) -> list[SpaceInfo]:
        """wiki v2 space.list 分页全量（应用须为知识库成员）。"""

        def _once(page_token: str) -> Any:
            from lark_oapi.api.wiki.v2.model import ListSpaceRequest

            builder = ListSpaceRequest.builder().page_size(50)
            if page_token:
                builder = builder.page_token(page_token)
            return self._call_with_backoff(lambda: self._sdk.wiki.v2.space.list(builder.build()))

        def _all() -> list[SpaceInfo]:
            out: list[SpaceInfo] = []
            page_token = ""
            for _ in range(self.max_pages):
                resp = _once(page_token)
                body = resp.data
                if body is None:
                    break
                out.extend(
                    SpaceInfo(space_id=sp.space_id or "", name=sp.name or "")
                    for sp in (body.items or [])
                    if sp is not None
                )
                page_token = body.page_token or ""
                if not body.has_more or not page_token:
                    return out
            return out  # 防御上限截断（自用个位数空间，永不触达）

        return await self._sync(_all)

    async def list_space_nodes(
        self, space_id: str, parent_node_token: str = ""
    ) -> tuple[list[NodeInfo], str]:
        """wiki v2 space_node.list 单层子节点全量（parent_node_token + page_token 翻页）。

        返回 (items, err)：err 非空 = 该层失败（树遍历按「停该子树留痕、不中断整树」
        消费——notebook 同语义）；items 为已达成的部分集。
        """

        def _all() -> tuple[list[NodeInfo], str]:
            from lark_oapi.api.wiki.v2.model import ListSpaceNodeRequest

            collected: list[NodeInfo] = []
            page_token = ""
            try:
                for _ in range(self.max_pages):
                    builder = ListSpaceNodeRequest.builder().space_id(space_id).page_size(50)
                    if parent_node_token:
                        builder = builder.parent_node_token(parent_node_token)
                    if page_token:
                        builder = builder.page_token(page_token)
                    request = builder.build()
                    resp = self._call_with_backoff(lambda: self._sdk.wiki.v2.space_node.list(request))
                    body = resp.data
                    if body is None:
                        break
                    for nd in body.items or []:
                        if nd is None:
                            continue
                        collected.append(
                            NodeInfo(
                                node_token=nd.node_token or "",
                                obj_token=nd.obj_token or "",
                                obj_type=nd.obj_type or "",
                                parent_node_token=nd.parent_node_token or "",
                                node_type=nd.node_type or "",
                                has_child=bool(nd.has_child),
                                title=nd.title or "",
                                obj_edit_time=_coerce_edit_time(
                                    getattr(nd, "obj_edit_time", None)
                                ),
                            )
                        )
                    page_token = body.page_token or ""
                    if not body.has_more or not page_token:
                        return collected, ""
                return collected, f"分页超过 {self.max_pages} 页已截断（该层仅取前 {self.max_pages} 页）"
            except FeishuClientError as exc:
                return collected, str(exc)

        return await self._sync(_all)

    async def walk_space_tree(
        self,
        space_id: str,
        *,
        max_depth: int = FEISHU_MAX_DEPTH,
        max_nodes: int = FEISHU_MAX_NODES,
    ) -> TreeWalkResult:
        """BFS 全树遍历（notebook cell 6 生产化）：防御上限 + shortcut 不展开 + 环安全。

        - 环安全：node_token 首见为准（重复/环边不二次收录）；
        - shortcut：本体保留（node_type=shortcut），子树不展开（引用边非实体，
          tree_assembler 防御④同口径）；
        - 深度上限：超限节点的子层不再拉取（truncated_depth 留痕）；
        - 节点上限：超限不再收录（truncated_nodes 留痕）；
        - 子树失败：errors 留痕、其余分支继续（不中断整树）。
        """
        result = TreeWalkResult(space_id=space_id)
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque([("", 1)])
        while queue:
            parent_token, depth = queue.popleft()
            children, err = await self.list_space_nodes(space_id, parent_token)
            if err:
                result.errors.append(f"depth{depth} parent={parent_token or '<root>'} → {err}")
                continue
            for child in children:
                token = child.node_token
                if not token or token in seen:
                    continue  # 缺 token / 重复 / 环边：首见为准
                if len(result.nodes) >= max_nodes:
                    result.truncated_nodes += 1
                    continue
                seen.add(token)
                result.nodes.append(child)
                if child.node_type == "shortcut":
                    result.shortcut_count += 1
                    continue  # 引用边非实体，只计数不展开
                if not child.has_child:
                    continue
                if depth >= max_depth:
                    result.truncated_depth += 1
                    continue
                queue.append((token, depth + 1))
        return result

    # ── docx：blocks 直读（分页翻全）──

    async def list_document_blocks(self, document_id: str) -> BlockPageResult:
        """docx v1 document_block.list 分页翻全（page_size=500；防御上限截断留痕）。"""

        def _all() -> BlockPageResult:
            from lark_oapi.api.docx.v1.model import ListDocumentBlockRequest

            items: list[Any] = []
            page_token = ""
            for _ in range(self.max_pages):
                builder = ListDocumentBlockRequest.builder().document_id(document_id).page_size(DOCX_BLOCK_PAGE_SIZE)
                if page_token:
                    builder = builder.page_token(page_token)
                request = builder.build()
                resp = self._call_with_backoff(lambda: self._sdk.docx.v1.document_block.list(request))
                body = resp.data
                if body is None:
                    break
                items.extend(blk for blk in (body.items or []) if blk is not None)
                page_token = body.page_token or ""
                if not body.has_more or not page_token:
                    return BlockPageResult(items=items)
            return BlockPageResult(items=items, truncated=True)

        return await self._sync(_all)

    # ── sheets：v3 工作表定位 + v2 values 值矩阵（sheet(30) 回填通道）──

    def _sheet_values_error(self, exc: FeishuClientError) -> FeishuSheetValuesError:
        """FeishuClientError → 分类归一（错误码属性在 _call_with_backoff 失败分支附带）。"""
        return FeishuSheetValuesError(
            str(exc), category=_classify_sheet_error(getattr(exc, "code", None))
        )

    def _fetch_values_page(
        self, sheet_token: str, sheet_id: str, start_row: int, end_row: int
    ) -> list[list[Any]]:
        """v2 values 单页窗（GET /open-apis/sheets/v2/spreadsheets/:token/values/:range
        ——SDK 1.8.0 无该端点封装，走 Client.request 原始通道：BaseRequest + TENANT
        token；信封 JSON 的 data.values 为二维数组）。"""
        import json as _json

        from lark_oapi.core.enum import AccessTokenType, HttpMethod
        from lark_oapi.core.model import BaseRequest

        # 证实列（评审 P3 死计数修复）：窗拉到 SHEET_MAX_COLS+1（第 65 列=BM）——
        # 窗恒 ≤64 时 width−64 恒 0、truncated_cols 永不成立；护栏侧按证实列内容
        # 计数（防 API 按窗宽补齐空列的假阳性），窗内下界口径同行护栏证实页
        range_str = f"{sheet_id}!A{start_row}:{_col_letter(SHEET_MAX_COLS + 1)}{end_row}"
        request = (
            BaseRequest.builder()
            .http_method(HttpMethod.GET)
            .uri("/open-apis/sheets/v2/spreadsheets/:spreadsheet_token/values/:range")
            .token_types({AccessTokenType.TENANT})
            .paths({"spreadsheet_token": sheet_token, "range": range_str})
            .queries(
                [
                    ("value_render_option", "ToString"),
                    ("date_time_render_option", "FormattedString"),
                ]
            )
            .build()
        )
        resp = self._call_with_backoff(lambda: self._sdk.request(request))
        raw = getattr(resp, "raw", None)
        content = getattr(raw, "content", None) if raw is not None else None
        body: dict[str, Any] = {}
        if content:
            body = _json.loads(bytes(content).decode("utf-8"))
        values = (body.get("data") or {}).get("values")
        return values if isinstance(values, list) else []

    async def fetch_sheet_values(self, sheet_token: str) -> SheetValuesData:
        """sheets 值矩阵分页拉全 + 护栏截断（§1.18 30 行回填通道）。

        - 工作表定位：v3 `sheets.v3.spreadsheet_sheet.query`（SDK 封装在位）取首个
          非隐藏工作表（docx 内嵌 sheet 单工作表形态；全隐藏回退首个、零工作表 →
          not_found）；
        - 值矩阵：v2 values 按行窗分页（SHEET_VALUES_PAGE_ROWS=100）——短页自然耗尽；
          行护栏 500 全取后再取一页溢出证实页即止（超证实窗不翻页，计数为窗内下界）；
        - 失败归一 FeishuSheetValuesError（category 分类、文本脱敏）；调用方（worker
          回填段）捕获留痕后保留占位，不算文档失败。
        """

        def _locate() -> str:
            from lark_oapi.api.sheets.v3.model import QuerySpreadsheetSheetRequest

            request = QuerySpreadsheetSheetRequest.builder().spreadsheet_token(sheet_token).build()
            resp = self._call_with_backoff(
                lambda: self._sdk.sheets.v3.spreadsheet_sheet.query(request)
            )
            sheets = getattr(getattr(resp, "data", None), "sheets", None) or []
            for item in sheets:
                sheet_id = getattr(item, "sheet_id", None) or ""
                if sheet_id and not getattr(item, "hidden", None):
                    return sheet_id
            if sheets:  # 全隐藏防御回退：取首个有 id 的工作表
                return getattr(sheets[0], "sheet_id", None) or ""
            raise FeishuSheetValuesError("电子表格无可见工作表", category="not_found")

        def _all() -> SheetValuesData:
            try:
                sheet_id = _locate()
                fetched: list[list[Any]] = []
                start = 1
                for _ in range(self.max_pages):
                    if start > SHEET_MAX_ROWS + 1:
                        break  # 行护栏预算：cap 全取 + 溢出证实页已读，超窗不翻页
                    page = self._fetch_values_page(sheet_token, sheet_id, start, start + SHEET_VALUES_PAGE_ROWS - 1)
                    fetched.extend(page)
                    if len(page) < SHEET_VALUES_PAGE_ROWS:
                        break  # 短页 = 自然耗尽
                    start += len(page)
            except FeishuSheetValuesError:
                raise
            except FeishuClientError as exc:
                raise self._sheet_values_error(exc) from exc
            return _apply_sheet_guardrails(fetched)

        return await self._sync(_all)

    # ── drive：云空间文件附件 / 云文档图片素材下载 ──

    def _read_download_stream(
        self,
        stream: Any,
        *,
        max_bytes: int | None,
        empty_stream_msg: str,
        empty_bytes_msg: str,
    ) -> bytes:
        """下载响应流分块读 + 封顶（两通道共用；零内容/空流视为失败）。

        `max_bytes`：累计超过上限即 close 弃流并归一 FeishuAttachmentTooLargeError
        （提前终止，不继续读）。如实注记：lark_oapi 1.8.0 Transport.execute 非流式
        （响应体先整份落 resp.file 内存流），本封顶防的是下游误用超大产物，不是
        内存峰值。
        """
        if stream is None:
            raise FeishuClientError(empty_stream_msg)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(_DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()  # 提前终止：弃流（连接由 SDK/底层回收）
                raise FeishuAttachmentTooLargeError(
                    f"下载内容超过大小上限（{max_bytes} 字节）——流式读取提前终止"
                )
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise FeishuClientError(empty_bytes_msg)
        return data

    async def download_file(self, file_token: str, *, max_bytes: int | None = None) -> bytes:
        """drive v1 file.download → bytes（**云空间文件**专用通道——wiki file 节点附件）。

        通道分工（2026-11-25 三通道对照探针实证）：云空间资源文件（wiki file 节点
        附件，如 PDF/Word 原件）只能走本通道——`media.download` 对其恒 403 空体
        （官方文档：media 仅下载云文档中的素材）；docx 内 image 块等云文档素材走
        `download_media`（B21 图片管线消费）。

        `max_bytes`：分块封顶读（见 `_read_download_stream` 的非流式限制注记——
        封顶防下游误用超大产物，不是内存峰值；worker 传附件上限 kb_settings.
        max_file_mb）。失败归一 FeishuClientError（脱敏；403 空体 code=None 同走
        `_call_with_backoff` 失败分支）；零内容/空流视为失败（防半截产物入库）。
        """

        def _once() -> bytes:
            from lark_oapi.api.drive.v1.model import DownloadFileRequest

            resp = self._call_with_backoff(
                lambda: self._sdk.drive.v1.file.download(
                    DownloadFileRequest.builder().file_token(file_token).build()
                )
            )
            return self._read_download_stream(
                getattr(resp, "file", None),
                max_bytes=max_bytes,
                empty_stream_msg="文件下载返回空文件流",
                empty_bytes_msg="文件下载为空字节流",
            )

        return await self._sync(_once)

    async def download_media(self, file_token: str, *, max_bytes: int | None = None) -> bytes:
        """drive v1 media.download → bytes——**仅云文档中的素材**（docx 内 image 块等，
        B21 图片管线消费）。

        ★云空间文件（wiki file 节点附件）**不适用**本通道（恒 403 空体，2026-11-25
        探针实证）——附件下载走 `download_file`。

        `max_bytes`：分块封顶读——累计超过上限即提前终止弃流并归一
        FeishuAttachmentTooLargeError（封顶语义同 download_file：防下游误用超大
        产物，非内存峰值；图片素材通道不传保持原全量形态）。失败归一
        FeishuClientError（脱敏）；零内容/空流视为失败（防半截产物入库）。
        """

        def _once() -> bytes:
            from lark_oapi.api.drive.v1.model import DownloadMediaRequest

            resp = self._call_with_backoff(
                lambda: self._sdk.drive.v1.media.download(
                    DownloadMediaRequest.builder().file_token(file_token).build()
                )
            )
            return self._read_download_stream(
                getattr(resp, "file", None),
                max_bytes=max_bytes,
                empty_stream_msg="素材下载返回空文件流",
                empty_bytes_msg="素材下载为空字节流",
            )

        return await self._sync(_once)


# ── 默认客户端（进程内单例 = tenant token 缓存载体；凭据只从 settings 单例读）──

_default_client: FeishuClient | None = None


def get_default_feishu_client() -> FeishuClient:
    """生产形态入口：feishu_settings 单例凭据 → FeishuClient（进程内单例复用）。

    凭据缺失 → FeishuClientError（api.md §4.7 500001 语义面；消息零回显凭据值）。
    """
    global _default_client
    if _default_client is None:
        app_id = feishu_settings.app_id.get_secret_value()
        app_secret = feishu_settings.app_secret.get_secret_value()
        _default_client = FeishuClient(
            app_id=app_id,
            app_secret=app_secret,
            api_base=feishu_settings.api_base,
        )
    return _default_client


# ── file(23) 附件 / image(27) 素材 → MinIO（沿 storage 模块既有键面与通道）──

#: 图片素材 block_id 白名单（飞书 block_id 为字母数字串；防键尾段穿越——键卫生面）
_BLOCK_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
#: 图片素材扩展名白名单（评审 P3 加固——图片素材下载接线批〔B18 后〕的前置卫；
#: 现生产零调用，接线时 ext 白名单已就位不依赖临时补丁）
_IMAGE_EXT_WHITELIST: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
)


def image_material_object_key(user_id: str, doc_id: str, block_id: str, ext: str) -> str:
    """图片素材对象键（实现面——契约零面清单「图片素材键为实现面」）：
    `kb/{user_id}/{doc_id}/img/{block_id}.{ext}`。block_id 走字母数字白名单、ext 走
    图片扩展名白名单（先于任何 IO——"exe" 等形合法但非图片的扩展名不可过）。"""
    if not _BLOCK_ID_RE.match(block_id):
        raise ValueError(f"block_id 须为字母数字串：{block_id!r}")
    normalized = ext.lstrip(".").lower()
    if normalized not in _IMAGE_EXT_WHITELIST:
        raise ValueError(
            f"图片扩展名须 ∈ {sorted(_IMAGE_EXT_WHITELIST)}（接线批白名单）：{ext!r}"
        )
    return f"kb/{user_id}/{doc_id}/img/{block_id}.{normalized}"


async def store_attachment(
    user_id: str,
    doc_id: str,
    file_name: str,
    data: bytes,
    *,
    minio_client: Any | None = None,
) -> str:
    """file(23) 附件落 MinIO：`kb/{user_id}/{doc_id}/original.{ext}`（§1.7 :270 键约定，
    与上传原件同形——下载后走 REQ-019 归一化管线）。扩展名取自 file_name 并过
    storage 白名单（pdf/md/txt/docx——白名单外 ValueError，调用方转失败清单条目，
    api.md §4.9 failures 面）。"""
    from app.rag.storage import original_object_key, put_kb_object

    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else ""
    key = original_object_key(user_id, doc_id, ext)  # 白名单外 → ValueError（先于 IO）
    return await put_kb_object(key, data, client=minio_client)


async def store_image_material(
    user_id: str,
    doc_id: str,
    block_id: str,
    ext: str,
    data: bytes,
    *,
    minio_client: Any | None = None,
) -> str:
    """image(27) 素材落 MinIO（实现面键布局）：落成后 worker 回填节点 meta.minio_key；
    下载失败保留占位（meta.placeholder=true）、不算文档失败（§1.18 映射表 27 行）。

    ★接线挂账（评审 P3）：本通道生产零调用——图片素材下载回填未接线（worker 现形态
    保留 placeholder），接线批（B18 后）须补素材下载消费面；ext 白名单
    （_IMAGE_EXT_WHITELIST）已在本模块键卫生面就位，接线时不依赖临时补丁。"""
    from app.rag.storage import put_kb_object

    key = image_material_object_key(user_id, doc_id, block_id, ext)
    return await put_kb_object(key, data, client=minio_client)
