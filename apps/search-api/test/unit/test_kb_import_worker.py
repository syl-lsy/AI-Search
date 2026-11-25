"""unit 层：飞书导入 worker（B 线②③⑤——api.md §4.7 同步 diff 判定序 + §4.9 五数/字面面）。

契约锚点：
- api.md §4.7（五步判定序命中即止：新增→移动〔catalog_path 全串比对〕→obj_edit_time
  变大→缺失/0 才启用 content_hash 兜底→跳过不计 total_docs；重导=整文档单元三存储
  级联替换；附件白名单 pdf/md/txt/docx+20MB 超限 attachment_unsupported；shortcut
  不计文档单元不进五数〔G13〕；imported_docs 计数时点=文档行建成进流水线即计〔G10〕；
  5 QPS 令牌桶代码常量）；
- api.md §4.9（mindnote/slides skipped+1+failures 条目 message「无读取 API，已跳过」、
  sheet/bitable message「本期未实装读取通道」〔C8〕；failure_summary 由 failures 派生
  不落列〔G4〕、skip 类也计入且与 skipped_docs 正交；stats.deleted_docs 不进五数
  〔C5=(a)〕；进度恒等式 processed=imported+failed+skipped）；
- data-model.md §1.17（cursor 断点游标续跑；worker 图外组件 ADR-011；catalog_path
  列=「/」连接串；§1.18 catalog_path 含自身标题——判定序②比对基准）。

全假体零网络零 DB（DB 形态归 integration）；令牌桶/重试节奏经注入 clock/sleep 零等待。
"""

import asyncio
import uuid
from typing import Any

import pytest

from app.core.config import feishu_settings
from app.rag.feishu_client import (
    FeishuAttachmentTooLargeError,
    FeishuClientError,
    NodeInfo,
)
from app.rag.import_worker import (
    CATEGORY_ATTACHMENT_DOWNLOAD_FAILED,
    CATEGORY_ATTACHMENT_UNSUPPORTED,
    CATEGORY_BLOCK_FETCH_FAILED,
    CATEGORY_NORMALIZE_FAILED,
    CATEGORY_PERMISSION_DENIED,
    CATEGORY_UNSUPPORTED_TYPE,
    IMPORT_MIN_INTERVAL_S,
    IMPORT_QPS,
    NOT_IMPLEMENTED_READ_TYPES,
    READABLE_OBJ_TYPES,
    UNIT_MAX_ATTEMPTS,
    TRANSIENT_CATEGORIES,
    FeishuImportDocRepository,
    FeishuImportWorker,
    KbImportJobRepository,
    LocalDoc,
    RemoteUnit,
    SyncPlan,
    TokenBucket,
    catalog_path_string,
    compute_sync_plan,
    derive_failure_summary,
    judge_unit_action,
    resolve_space_id,
    scheduled_sync_once,
)

_USER = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _pin_feishu_space_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定 FEISHU_SPACE_ID=未绑定：单元面不依赖 .env 绑定态，space 裁决走假体
    spaces[0]（本机 .env 可能已配置真实绑定值——环境依赖即 flaky）。"""
    monkeypatch.setattr(feishu_settings, "space_id", "", raising=False)

# ---------------------------------------------------------------------------
# 常量组（api.md §4.7 钉注 / data-model §1.17——令牌桶参数为代码常量不入 Nacos）
# ---------------------------------------------------------------------------


def test_rate_constants_pin_5qps() -> None:
    assert IMPORT_QPS == 5
    assert IMPORT_MIN_INTERVAL_S == 0.2  # 5 QPS → 200ms 间隔


def test_category_constants_pin_contract_values() -> None:
    """category 非封闭枚举——实现批钉值域（api.md §4.9；提案建议值）。"""
    assert CATEGORY_UNSUPPORTED_TYPE == "unsupported_type"
    assert CATEGORY_BLOCK_FETCH_FAILED == "block_fetch_failed"
    assert CATEGORY_ATTACHMENT_DOWNLOAD_FAILED == "attachment_download_failed"
    assert CATEGORY_ATTACHMENT_UNSUPPORTED == "attachment_unsupported"
    assert CATEGORY_NORMALIZE_FAILED == "normalize_failed"
    assert CATEGORY_PERMISSION_DENIED == "permission_denied"
    assert READABLE_OBJ_TYPES == frozenset({"docx", "file"})
    assert NOT_IMPLEMENTED_READ_TYPES == frozenset({"sheet", "bitable"})
    assert TRANSIENT_CATEGORIES == frozenset(
        {CATEGORY_BLOCK_FETCH_FAILED, CATEGORY_ATTACHMENT_DOWNLOAD_FAILED}
    )
    assert UNIT_MAX_ATTEMPTS == 3  # 1 首试 + 2 重试


# ---------------------------------------------------------------------------
# 同步 diff 判定序（api.md §4.7 五步命中即止——每步一正一反 + 顺序性）
# ---------------------------------------------------------------------------


def _remote(
    *,
    token: str = "tok1",
    obj_type: str = "docx",
    edit: int | None = 100,
    path: list[str] | None = None,
    title: str = "文档一",
) -> RemoteUnit:
    return RemoteUnit(
        node_token=token,
        obj_token=f"obj-{token}",
        obj_type=obj_type,
        title=title,
        obj_edit_time=edit,
        catalog_ids=[f"fs:node:{t}" for t in (path or [])[:-1]] if path else [],
        catalog_path=path if path is not None else ["文档一"],
    )


def _local(
    *,
    token: str = "tok1",
    edit: int | None = 100,
    path: str = "文档一",
    content_hash: str | None = "a" * 64,
    document_id: str = "11111111-1111-4111-8111-111111111111",
    space_id: str = "sp1",
    title: str | None = None,
) -> LocalDoc:
    return LocalDoc(
        document_id=document_id,
        node_token=token,
        obj_edit_time=edit,
        catalog_path=path,
        content_hash=content_hash,
        space_id=space_id,
        title=title,
    )


def test_step1_local_missing_imports_without_pull() -> None:
    """① 本地无该 node_token → 新增重导；命中即止——content_hash 兜底不被触达。"""
    pulls: list[RemoteUnit] = []
    action = judge_unit_action(
        _remote(edit=999), None, pull_content_hash=pulls.append
    )
    assert action == "import"
    assert pulls == []  # ① 命中即止：④ 重拉未触达（同满足①③归①——①先命中）


def test_step2_catalog_path_change_reimports() -> None:
    """② 本地 catalog_path 全串与远端不同 → 移动/改名重导（比对基准=列全串，
    含自身标题——改标题也命中；轻审 P1-2 钉正）。"""
    pulls: list[RemoteUnit] = []
    action = judge_unit_action(
        _remote(edit=100, path=["根", "新标题"]),
        _local(edit=100, path="根/旧标题"),
        pull_content_hash=pulls.append,
    )
    assert action == "import"
    assert pulls == []  # ② 命中即止（②优先于③④）


def test_step2_move_with_ancestor_change_hits_before_step3() -> None:
    """②优先于③：祖先链变更且 obj_edit_time 同时变大 → 判定序归②（import，
    重拉不触达——顺序性观测点）。"""
    pulls: list[RemoteUnit] = []
    action = judge_unit_action(
        _remote(edit=200, path=["新父", "文档一"]),
        _local(edit=100, path="旧父/文档一"),
        pull_content_hash=pulls.append,
    )
    assert action == "import"
    assert pulls == []


def test_step3_remote_edit_time_bigger_reimports() -> None:
    """③ 远端 obj_edit_time 大于本地记录值 → 变更重导；不触达 content_hash 兜底。"""
    pulls: list[RemoteUnit] = []
    action = judge_unit_action(
        _remote(edit=201), _local(edit=100), pull_content_hash=pulls.append
    )
    assert action == "import"
    assert pulls == []


def test_step3_equal_edit_time_is_unchanged_no_pull() -> None:
    """③ 反例：obj_edit_time 相等 → ⑤ 未变更跳过（不计 total_docs 由 plan 层承担）；
    B5 反例：obj_edit_time 正常时不得触发 content_hash 重拉——拉取调用 0 次。"""
    pulls: list[RemoteUnit] = []
    action = judge_unit_action(
        _remote(edit=100), _local(edit=100), pull_content_hash=pulls.append
    )
    assert action == "unchanged"
    assert pulls == []


def test_step4_only_reachable_when_edit_time_missing_or_zero() -> None:
    """④ 仅 obj_edit_time 缺失/0 可达：重拉内容比对 content_hash——不同才重导。"""
    pulls: list[RemoteUnit] = []

    def _pull(unit: RemoteUnit) -> str:
        pulls.append(unit)
        return "b" * 64

    changed = judge_unit_action(
        _remote(edit=None), _local(edit=100, content_hash="a" * 64), pull_content_hash=_pull
    )
    same = judge_unit_action(
        _remote(edit=0), _local(edit=100, content_hash="b" * 64), pull_content_hash=_pull
    )
    assert changed == "import"
    assert same == "unchanged"
    assert len(pulls) == 2  # 仅④两例各拉一次；①②③路径均未拉取


def test_step4_local_hash_missing_counts_as_changed() -> None:
    """④ 本地 content_hash 缺失（None）→ 视为不同 → 重导（兜底判据失效即保守重导，
    不静默跳过——漏判=内容漂移不入库）。"""
    action = judge_unit_action(
        _remote(edit=0), _local(edit=100, content_hash=None), pull_content_hash=lambda u: "b" * 64
    )
    assert action == "import"


def test_step5_unchanged_not_counted_in_plan_units() -> None:
    """⑤ 未变更 → 跳过，不计入 total_docs（plan.units 不含该单元）。"""
    plan = compute_sync_plan(
        [_remote(edit=100, token="a"), _remote(edit=100, token="b", path=["文档二"])],
        {"a": _local(token="a", edit=100), "b": _local(token="b", edit=100, path="文档二")},
        content_hash_fn=lambda u: "x",
    )
    assert plan.units == []  # 五数 total_docs=0（恒等式 processed=0=0+0+0）


def test_catalog_path_string_joins_with_slash() -> None:
    """catalog_path 列=「/」连接串（data-model §1.7：目录链标题串）。"""
    assert catalog_path_string(["产品手册", "安装指南"]) == "产品手册/安装指南"
    assert catalog_path_string(["顶层文档"]) == "顶层文档"  # C9=(a) 顶层=自身标题


# ---------------------------------------------------------------------------
# compute_sync_plan：shortcut 不计单元（G13）/ unsupported 恒 skip 留痕 / 差集
# ---------------------------------------------------------------------------


def test_plan_excludes_shortcut_and_counts_unsupported_as_skip_trace() -> None:
    units = [
        RemoteUnit(
            node_token="s", obj_token="o", obj_type="docx", title="快捷方式",
            obj_edit_time=1, catalog_ids=[], catalog_path=["快捷方式"], is_shortcut=True,
        ),
        _remote(token="d1", obj_type="docx"),
        _remote(token="mn", obj_type="mindnote", title="调研 mindnote"),
        _remote(token="sh", obj_type="sheet", title="数据表"),
    ]
    plan: SyncPlan = compute_sync_plan(units, {}, content_hash_fn=lambda u: "x")
    # shortcut 不计文档单元不进五数；mindnote/sheet 恒 skip_trace（不进判定序）
    assert [u.remote.node_token for u in plan.units] == ["d1", "mn", "sh"]
    assert plan.units[0].action == "import"
    assert plan.units[1].action == "skip_trace"
    assert plan.units[2].action == "skip_trace"


def test_plan_deletions_is_remote_missing_set() -> None:
    """差集（C5=(a)）：本地有而远端无的 node_token → 级联删除清单。"""
    units = [_remote(token="keep")]
    local_by_token = {
        "keep": _local(token="keep"),
        "gone1": _local(token="gone1", document_id="id-1"),
        "gone2": _local(token="gone2", document_id="id-2"),
    }
    plan = compute_sync_plan(units, local_by_token, content_hash_fn=lambda u: "x")
    assert sorted(d.node_token for d in plan.deletions) == ["gone1", "gone2"]


# ---------------------------------------------------------------------------
# failure_summary 派生（G4：由 failures 按 category 计数，不落列）
# ---------------------------------------------------------------------------


def test_failure_summary_derived_in_first_seen_order_and_counts_skips() -> None:
    failures = [
        {"title": "a", "category": CATEGORY_BLOCK_FETCH_FAILED, "message": "m", "document_id": None},
        {"title": "b", "category": CATEGORY_UNSUPPORTED_TYPE, "message": "m", "document_id": None},
        {"title": "c", "category": CATEGORY_BLOCK_FETCH_FAILED, "message": "m", "document_id": None},
        {"title": "d", "category": CATEGORY_ATTACHMENT_UNSUPPORTED, "message": "m", "document_id": None},
    ]
    summary = derive_failure_summary(failures)
    assert summary == [
        {"category": CATEGORY_BLOCK_FETCH_FAILED, "count": 2},
        {"category": CATEGORY_UNSUPPORTED_TYPE, "count": 1},  # skip 类条目同样计入
        {"category": CATEGORY_ATTACHMENT_UNSUPPORTED, "count": 1},
    ]


def test_failure_summary_empty_for_no_failures() -> None:
    assert derive_failure_summary([]) == []


# ---------------------------------------------------------------------------
# TokenBucket（5 QPS——200ms 注入 clock 零等待断言）
# ---------------------------------------------------------------------------


def test_token_bucket_enforces_200ms_interval_with_injected_clock() -> None:
    now = {"t": 0.0}
    sleeps: list[float] = []

    def _clock() -> float:
        return now["t"]

    async def _sleep(d: float) -> None:
        sleeps.append(d)
        now["t"] += d

    bucket = TokenBucket(clock=_clock, sleep=_sleep)

    async def _drive() -> list[float]:
        await bucket.acquire()  # 首次零等待（不产生 sleep 记录）
        await bucket.acquire()  # 相邻间隔恒 ≥200ms
        await bucket.acquire()
        return sleeps

    sleeps = asyncio.run(_drive())
    assert len(sleeps) == 2  # 首次零等待——只后两次各补足一次
    assert sleeps == [pytest.approx(0.2), pytest.approx(0.2)]  # 5 QPS 令牌桶节奏


# ---------------------------------------------------------------------------
# worker 执行面假体
# ---------------------------------------------------------------------------


class _FakeJobsRepo:
    """kb_import_jobs 假体（内存行 dict——worker 全部消费面）。"""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = rows or {}
        self.created: list[dict[str, Any]] = []
        self.transitions: list[tuple[str, str, dict[str, Any]]] = []

    async def load(self, job_id: str) -> dict[str, Any] | None:
        return self.rows.get(job_id)

    async def transition(
        self, job_id: str, *, expect: set[str], to: str, fields: dict[str, Any]
    ) -> bool:
        row = self.rows.get(job_id)
        if row is None or row["status"] not in expect:
            return False
        row.update(fields)
        row["status"] = to
        self.transitions.append((job_id, to, dict(fields)))
        return True

    async def list_requeue_ids(self) -> list[str]:
        return [
            jid
            for jid, row in self.rows.items()
            if row["status"] in ("pending", "running")
        ]

    async def has_active(self) -> bool:
        return any(row["status"] in ("pending", "running") for row in self.rows.values())

    async def create_job(
        self, *, user_id: str, trigger: str, progress: dict[str, int], stats: dict[str, Any]
    ) -> str:
        jid = str(uuid.uuid4())
        self.rows[jid] = {
            "id": jid,
            "user_id": user_id,
            "connector": "feishu",
            "trigger": trigger,
            "status": "pending",
            "cursor": None,
            "progress": dict(progress),
            "failures": [],
            "stats": dict(stats),
        }
        self.created.append({"job_id": jid, "user_id": user_id, "trigger": trigger})
        return jid


class _FakeDocsRepo:
    """kb_documents worker 面假体（本地比对行 + 建行 + 级联删）。"""

    def __init__(
        self,
        local_rows: list[LocalDoc] | None = None,
        *,
        users: list[str] | None = None,
    ) -> None:
        self.local_rows = local_rows or []
        self.created_rows: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.feishu_users = users or ([_USER] if local_rows else [])
        self.existing_ids: set[str] = {d.document_id for d in self.local_rows}

    async def list_local_rows(self, user_id: str, *, space_id: str = "") -> list[LocalDoc]:
        # 与真仓储同语义：按 parse_meta.feishu.space_id 过滤（跨空间行不进比对/差集）
        return [d for d in self.local_rows if not space_id or d.space_id == space_id]

    async def list_feishu_user_ids(self) -> list[str]:
        return list(self.feishu_users)

    async def create(self, fields: dict[str, Any]) -> str:
        doc_id = str(fields.get("id") or uuid.uuid4())
        self.created_rows.append(dict(fields, id=doc_id))
        self.existing_ids.add(doc_id)
        return doc_id

    async def delete_cascade(self, document_id: str, *, user_id: str = "") -> None:
        self.deleted.append(document_id)
        self.existing_ids.discard(document_id)

    async def exists(self, document_id: str) -> bool:
        return document_id in self.existing_ids


class _FakePipeline:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, doc_id: str) -> None:
        self.enqueued.append(doc_id)


class _FakeEsIndexer:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    async def delete_document(self, *, kb_doc_id: str, user_id: str) -> None:
        self.deleted.append((kb_doc_id, user_id))


class _FakePurger:
    def __init__(self) -> None:
        self.purged: list[tuple[str, str]] = []

    async def purge(self, *, user_id: str, doc_id: str) -> None:
        self.purged.append((user_id, doc_id))


class _FakeClient:
    """FeishuClient 假体：树遍历 / blocks / 附件下载（调用计数供顺序性断言）。"""

    def __init__(
        self,
        nodes: list[NodeInfo],
        *,
        spaces: list[tuple[str, str]] | None = None,
        blocks_by_obj: dict[str, list[Any]] | None = None,
        block_fail_times: int = 0,
        download: bytes | None = b"pdf-bytes",
        download_error: bool = False,
        walk_errors: list[str] | None = None,
        truncated_nodes: int = 0,
        truncated_depth: int = 0,
        sheet_values: dict[str, Any] | None = None,
        sheet_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.nodes = nodes
        self.spaces = spaces or [("sp1", "空间")]
        self.blocks_by_obj = blocks_by_obj or {}
        self.block_fail_times = block_fail_times  # 前 N 次 block 调用失败（瞬态假体）
        self.block_calls: list[str] = []
        self.download_calls: list[str] = []
        self.file_calls: list[str] = []  # download_file 通道（附件正解）
        self.media_calls: list[str] = []  # media 误接哨兵（应为空）
        self.sheet_values = sheet_values or {}  # sheet(30) 回填通道：token → SheetValuesData
        self.sheet_errors = sheet_errors or {}  # token → 分类异常（失败分支）
        self.sheet_calls: list[str] = []
        self._download = download
        self._download_error = download_error
        self._walk_errors = walk_errors or []
        self._truncated_nodes = truncated_nodes
        self._truncated_depth = truncated_depth

    async def list_spaces(self) -> list[Any]:
        return [type("S", (), {"space_id": s, "name": n})() for s, n in self.spaces]

    async def walk_space_tree(self, space_id: str, **kw: Any) -> Any:
        return type(
            "W",
            (),
            {
                "space_id": space_id,
                "nodes": self.nodes,
                "errors": list(self._walk_errors),
                "shortcut_count": 0,
                "truncated_nodes": self._truncated_nodes,
                "truncated_depth": self._truncated_depth,
            },
        )()

    async def list_document_blocks(self, document_id: str) -> Any:
        self.block_calls.append(document_id)
        if len(self.block_calls) <= self.block_fail_times:
            raise FeishuClientError("飞书 API 失败：code=23001 msg=blocked")
        return type("BP", (), {"items": self.blocks_by_obj.get(document_id, []), "truncated": False})()

    async def download_file(self, file_token: str, *, max_bytes: int | None = None) -> bytes:
        """云空间文件通道（download_file）——附件下载唯一正解；调用计数供接线断言。

        与真 download_file 同形态：上限封顶读（超限 → TooLarge，不全量入内存）。
        """
        self.file_calls.append(file_token)
        self.download_calls.append(file_token)
        if self._download_error:
            raise FeishuClientError("飞书 API 失败：code=23001 msg=download")
        assert self._download is not None
        if max_bytes is not None and len(self._download) > max_bytes:
            raise FeishuAttachmentTooLargeError(
                f"下载内容超过大小上限（{max_bytes} 字节）——流式读取提前终止"
            )
        return self._download

    async def download_media(self, file_token: str, *, max_bytes: int | None = None) -> bytes:
        """误接通道哨兵：media.download 对云空间文件恒 403（2026-11-25 探针实证）——
        worker 附件下载 / 兜底 hash 必须走 download_file，走错即红。"""
        self.media_calls.append(file_token)
        raise AssertionError(
            "附件下载须走 download_file（云空间文件通道）；media.download 已证伪 403"
        )

    async def fetch_sheet_values(self, sheet_token: str) -> Any:
        """sheet(30) 回填通道假体（§1.18 v1.20——真形态 SheetValuesData / 分类异常）。"""
        self.sheet_calls.append(sheet_token)
        err = self.sheet_errors.get(sheet_token)
        if err is not None:
            raise err
        return self.sheet_values[sheet_token]


def _node(
    token: str,
    *,
    obj_type: str = "docx",
    parent: str = "",
    title: str | None = None,
    node_type: str = "",
    obj_token: str | None = None,
    has_child: bool = False,
    edit: int | None = 100,
) -> NodeInfo:
    return NodeInfo(
        node_token=token,
        obj_token=obj_token or f"obj-{token}",
        obj_type=obj_type,
        parent_node_token=parent,
        node_type=node_type,
        has_child=has_child,
        title=title or f"文档 {token}",
        obj_edit_time=edit,
    )


# --- SDK blocks 假体（对齐 test_kb_feishu_mapper 形态——page 根 + 文本块） ---


def _sdk_page(children: list[str]) -> Any:
    return type(
        "B",
        (),
        {"block_id": "page1", "parent_id": "", "children": children, "block_type": 1},
    )()


def _sdk_text(bid: str, content: str, parent: str) -> Any:
    run = type("R", (), {"content": content, "text_element_style": None})()
    elem = type("El", (), {"text_run": run, "mention_doc": None})()
    payload = type("T", (), {"elements": [elem], "style": None})()
    return type(
        "B",
        (),
        {
            "block_id": bid,
            "parent_id": parent,
            "children": [],
            "block_type": 2,
            "text": payload,
        },
    )()


def _docx_blocks(obj_token: str, count: int = 2) -> list[Any]:
    root_children = [f"b{i}" for i in range(count)]
    blocks = [_sdk_page(root_children)]
    blocks.extend(_sdk_text(f"b{i}", f"段落{i}", "page1") for i in range(count))
    return blocks


def _pending_job(job_id: str = "job-1") -> dict[str, Any]:
    return {
        "id": job_id,
        "user_id": _USER,
        "connector": "feishu",
        "trigger": "manual",
        "status": "pending",
        "cursor": None,
        "progress": {},
        "failures": [],
        "stats": None,
        "started_at": None,
        "finished_at": None,
    }


def _make_worker(
    jobs: _FakeJobsRepo,
    docs: _FakeDocsRepo,
    client: _FakeClient,
    *,
    pipeline: _FakePipeline | None = None,
    es_indexer: _FakeEsIndexer | None = None,
    purger: _FakePurger | None = None,
    bucket: TokenBucket | None = None,
    spec: dict[str, Any] | None = None,
) -> FeishuImportWorker:
    return FeishuImportWorker(
        jobs=jobs,  # type: ignore[arg-type]
        docs=docs,  # type: ignore[arg-type]
        client_provider=lambda: client,
        pipeline=pipeline or _FakePipeline(),  # type: ignore[arg-type]
        spec_payload_provider=lambda: spec if spec is not None else {},
        minio_client=lambda: None,
        purger=purger or _FakePurger(),
        es_indexer=es_indexer,
        bucket=bucket or TokenBucket(min_interval_s=0.0),
    )


# ---------------------------------------------------------------------------
# worker 执行面：docx 直读 / 附件 / skip 字面 / 恒等式 / 断点 / 重试 / 级联
# ---------------------------------------------------------------------------


async def test_process_job_docx_happy_path_counts_and_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docx 直读主线：建行（doc_type=docx/status=chunking/parser=feishu_blocks/
    catalog 列物化）→ 流水线入队 → imported+1（G10 计数时点=行建成进流水线即计）。"""
    client = _FakeClient(
        [_node("t1", title="导入文档")],
        blocks_by_obj={"obj-t1": _docx_blocks("obj-t1")},
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    outcome = await worker.process_job("job-1")

    assert outcome == "completed"
    row = jobs.rows["job-1"]
    assert row["status"] == "completed"
    progress = row["progress"]
    assert progress == {
        "total_docs": 1,
        "processed_docs": 1,
        "imported_docs": 1,
        "failed_docs": 0,
        "skipped_docs": 0,
    }  # 恒等式 processed=imported+failed+skipped
    assert pipeline.enqueued == [docs.created_rows[0]["id"]]
    created = docs.created_rows[0]
    assert created["doc_type"] == "docx"
    assert created["status"] == "chunking"  # SDK 直读：IR 已落桶，直接进切分段
    assert created["parse_meta"]["parser"] == "feishu_blocks"
    assert created["parse_meta"]["feishu"]["node_token"] == "t1"
    assert created["catalog_path"] == "导入文档"  # C9=(a)：顶层=自身标题（「/」串）
    assert created["catalog_ids"] == []


async def test_process_job_mindnote_slides_skip_literals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mindnote/slides：每单元 skipped+1 + failures 条目（category=unsupported_type，
    message「<obj_type> 无读取 API，已跳过」、document_id=null——api.md §4.9 钉注）。"""
    client = _FakeClient(
        [
            _node("mn", obj_type="mindnote", title="调研 mindnote"),
            _node("sl", obj_type="slides", title="汇报 slides"),
        ]
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    row = jobs.rows["job-1"]
    assert row["progress"] == {
        "total_docs": 2,
        "processed_docs": 2,
        "imported_docs": 0,
        "failed_docs": 0,
        "skipped_docs": 2,
    }
    assert row["failures"] == [
        {
            "title": "调研 mindnote",
            "category": "unsupported_type",
            "message": "mindnote 无读取 API，已跳过",
            "document_id": None,
        },
        {
            "title": "汇报 slides",
            "category": "unsupported_type",
            "message": "slides 无读取 API，已跳过",
            "document_id": None,
        },
    ]
    assert docs.created_rows == [] and pipeline.enqueued == []


async def test_process_job_sheet_bitable_literal_differs_from_no_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sheet/bitable：category 同 unsupported_type 但 message=「本期未实装读取通道」
    字面（与「无读取 API」区分——C8=(a)）。"""
    client = _FakeClient(
        [
            _node("sh", obj_type="sheet", title="数据表"),
            _node("bt", obj_type="bitable", title="多维表"),
        ]
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    messages = [f["message"] for f in jobs.rows["job-1"]["failures"]]
    assert messages == ["本期未实装读取通道", "本期未实装读取通道"]
    assert all(f["category"] == "unsupported_type" for f in jobs.rows["job-1"]["failures"])


async def test_process_job_mindnote_subtree_units_counted_individually(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子树逐一计入：mindnote 根 + 其子树内各文档单元逐一 skipped+1（api.md §4.9）。"""
    client = _FakeClient(
        [
            _node("mn", obj_type="mindnote", title="画板根", has_child=True),
            _node("child", obj_type="mindnote", parent="mn", title="画板子"),
        ]
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    progress = jobs.rows["job-1"]["progress"]
    assert progress["total_docs"] == 2 and progress["skipped_docs"] == 2
    assert len(jobs.rows["job-1"]["failures"]) == 2


async def test_process_job_shortcut_not_counted_not_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shortcut 不计文档单元不进五数（G13）——零 skipped 零 failures。"""
    client = _FakeClient([_node("sc", node_type="shortcut", title="快捷方式")])
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    progress = jobs.rows["job-1"]["progress"]
    assert progress["total_docs"] == 0 and progress["processed_docs"] == 0
    assert jobs.rows["job-1"]["failures"] == []
    assert jobs.rows["job-1"]["status"] == "completed"


async def test_process_job_unchanged_unit_skipped_out_of_five_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑤ 未变更 → 不计 total_docs：本地行一致（catalog_path/obj_edit_time 均同）→
    五数全 0、零 failures、零建行——判定序⑤出口。"""
    client = _FakeClient([_node("t1", title="导入文档", edit=100)])
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    async def _pull(unit: RemoteUnit, **_: Any) -> str:  # pragma: no cover —— B5 反例：不应触达
        raise AssertionError("content_hash 兜底不应被触达（obj_edit_time 正常）")

    monkeypatch.setattr(worker, "_pull_content_hash", _pull)
    docs.local_rows = [
        LocalDoc(
            document_id="11111111-1111-4111-8111-111111111111",
            node_token="t1",
            obj_edit_time=100,
            catalog_path="导入文档",
            content_hash="a" * 64,
            space_id="sp1",  # P1 space 过滤：与目标空间一致才进比对
        )
    ]

    await worker.process_job("job-1")

    progress = jobs.rows["job-1"]["progress"]
    assert progress == {
        "total_docs": 0,
        "processed_docs": 0,
        "imported_docs": 0,
        "failed_docs": 0,
        "skipped_docs": 0,
    }
    assert client.block_calls == []  # 未变更零物理拉取
    assert docs.created_rows == []


async def test_process_job_reimport_cascades_then_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重导=整文档单元替换：obj_edit_time 变大 → §4.5 同款三存储级联先删（pg 行删/
    ES seam 删除/MinIO 前缀清理）后重建（新行 + 流水线入队）——真 ES 腿挂 B18。"""
    client = _FakeClient(
        [_node("t1", title="导入文档", edit=200)],
        blocks_by_obj={"obj-t1": _docx_blocks("obj-t1")},
    )
    old_row = LocalDoc(
        document_id="22222222-2222-4222-8222-222222222222",
        node_token="t1",
        obj_edit_time=100,
        catalog_path="导入文档",
        content_hash="a" * 64,
        space_id="sp1",
        title="导入文档",
    )
    docs = _FakeDocsRepo([old_row])
    jobs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakePipeline()
    es_indexer, purger = _FakeEsIndexer(), _FakePurger()
    worker = _make_worker(
        jobs, docs, client, pipeline=pipeline, es_indexer=es_indexer, purger=purger
    )

    await worker.process_job("job-1")

    assert es_indexer.deleted == [("22222222-2222-4222-8222-222222222222", _USER)]
    assert docs.deleted == ["22222222-2222-4222-8222-222222222222"]
    assert purger.purged == [(_USER, "22222222-2222-4222-8222-222222222222")]
    assert len(docs.created_rows) == 1  # 重建新行
    assert pipeline.enqueued == [docs.created_rows[0]["id"]]
    assert jobs.rows["job-1"]["progress"]["imported_docs"] == 1


async def test_process_job_block_fetch_retries_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败分类清单重试：block_fetch_failed 为瞬态类——首试+2 重试全败 → failed+1 +
    failures 条目（四键，document_id=null）。"""
    client = _FakeClient([_node("t1", title="导入文档")], block_fail_times=999)  # 恒失败
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    assert client.block_calls == ["obj-t1"] * UNIT_MAX_ATTEMPTS  # 1 首试 + 2 重试
    progress = jobs.rows["job-1"]["progress"]
    assert progress["failed_docs"] == 1 and progress["processed_docs"] == 1
    (entry,) = jobs.rows["job-1"]["failures"]
    assert set(entry) == {"title", "category", "message", "document_id"}
    assert entry["category"] == "block_fetch_failed"
    assert entry["document_id"] is None


async def test_process_job_block_fetch_recovers_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """瞬态失败重试成功路径：首试失败 → 重试拉到 blocks → imported+1（零 failures）。"""
    client = _FakeClient(
        [_node("t1", title="导入文档")],
        blocks_by_obj={"obj-t1": _docx_blocks("obj-t1")},
        block_fail_times=1,  # 第 1 次失败、第 2 次起恢复
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    assert len(client.block_calls) == 2
    assert jobs.rows["job-1"]["progress"]["imported_docs"] == 1
    assert jobs.rows["job-1"]["failures"] == []


async def test_process_job_attachment_whitelist_and_size_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """附件白名单（pdf/md/txt/docx）+ 20MB：白名单外先拒（零下载）；超限进失败清单
    category=attachment_unsupported（api.md §4.7 钉注）。"""
    client = _FakeClient(
        [_node("f1", obj_type="file", title="归档.zip"), _node("f2", obj_type="file", title="大文件.pdf")],
        download=b"\0" * (20 * 1024 * 1024 + 1),
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    assert client.download_calls == ["obj-f2"]  # zip 白名单外：先拒零下载
    progress = jobs.rows["job-1"]["progress"]
    assert progress["failed_docs"] == 2
    assert [f["category"] for f in jobs.rows["job-1"]["failures"]] == [
        "attachment_unsupported",
        "attachment_unsupported",
    ]
    assert all(f["document_id"] is None for f in jobs.rows["job-1"]["failures"])


async def test_process_job_attachment_success_imports_via_pending_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """附件成功：下载 → 建行 pending（doc_type=ext、走归一化管线）→ imported+1。"""
    client = _FakeClient([_node("f1", obj_type="file", title="手册.pdf")], download=b"%PDF-1.4")
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    created = docs.created_rows[0]
    assert created["doc_type"] == "pdf"
    assert created["status"] == "pending"  # 附件走原件归一化管线（normalizing 起步）
    assert created["parse_meta"]["feishu"]["node_token"] == "f1"
    assert pipeline.enqueued == [created["id"]]
    assert jobs.rows["job-1"]["progress"]["imported_docs"] == 1


async def test_process_job_attachment_download_failure_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        [_node("f1", obj_type="file", title="手册.pdf")], download_error=True
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    assert client.download_calls == ["obj-f1"] * UNIT_MAX_ATTEMPTS
    assert jobs.rows["job-1"]["failures"][0]["category"] == "attachment_download_failed"


async def test_attachment_download_uses_file_channel_not_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通道接线断言：附件下载走 download_file（云空间文件通道）——media.download 对
    wiki file 节点恒 403（2026-11-25 探针三通道对照实证，145 份 PDF 全败的根因）。"""
    client = _FakeClient([_node("f1", obj_type="file", title="手册.pdf")], download=b"%PDF-1.7")
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    assert client.file_calls == ["obj-f1"]
    assert client.media_calls == []  # 误接通道零调用
    assert jobs.rows["job-1"]["progress"]["imported_docs"] == 1


async def test_process_job_resume_from_cursor_skips_processed_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """断点游标续跑（data-model §1.17）：cursor.processed_index=1 → 首单元零重复
    拉取，进度自游标续算（total_docs 重算、已推进数保留）。"""
    client = _FakeClient(
        [_node("t1", title="文档一"), _node("t2", title="文档二")],
        blocks_by_obj={"obj-t2": _docx_blocks("obj-t2")},
    )
    job = _pending_job()
    job.update(
        status="running",
        cursor={"space_id": "sp1", "processed_index": 1},
        progress={
            "total_docs": 2,
            "processed_docs": 1,
            "imported_docs": 1,
            "failed_docs": 0,
            "skipped_docs": 0,
        },
        failures=[],
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": job}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    assert client.block_calls == ["obj-t2"]  # t1 已推进段零重复拉取
    progress = jobs.rows["job-1"]["progress"]
    assert progress["imported_docs"] == 2 and progress["processed_docs"] == 2
    assert jobs.rows["job-1"]["cursor"]["processed_index"] == 2


async def test_process_job_delete_skipped_when_remote_empty_but_local_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 完成性门（保守臂）：远端零文档单元而本地非空 → 「远端状态未知」不判
    「远端已删除」——删除段整段跳过（零级联、零 deleted_docs、任务仍 completed）。
    真正清空的空间走手动 §4.5 删除，diff 永不承担全删动作。"""
    client = _FakeClient([])
    gone_row = LocalDoc(
        document_id="33333333-3333-4333-8333-333333333333",
        node_token="gone",
        obj_edit_time=1,
        catalog_path="旧文档",
        content_hash=None,
        space_id="sp1",
        title="旧文档",
    )
    docs = _FakeDocsRepo([gone_row])
    jobs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakePipeline()
    es_indexer, purger = _FakeEsIndexer(), _FakePurger()
    worker = _make_worker(
        jobs, docs, client, pipeline=pipeline, es_indexer=es_indexer, purger=purger
    )

    await worker.process_job("job-1")

    row = jobs.rows["job-1"]
    assert row["progress"] == {
        "total_docs": 0,
        "processed_docs": 0,
        "imported_docs": 0,
        "failed_docs": 0,
        "skipped_docs": 0,
    }
    assert row["status"] == "completed"
    assert "deleted_docs" not in (row["stats"] or {})  # 零删除计数
    assert docs.deleted == []  # 完成性门：零级联
    assert es_indexer.deleted == [] and purger.purged == []


async def test_process_job_deletion_runs_for_partial_remote_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 完成性门（正臂）：远端 walk 返回非空文档单元集且遍历完整 → 差集真实
    可信——仅删除场景照常建删（五数 0、stats.deleted_docs 承载、级联三面触达）。"""
    keep_row = LocalDoc(
        document_id="44444444-4444-4444-8444-444444444444",
        node_token="keep",
        obj_edit_time=100,
        catalog_path="保留文档",
        content_hash="a" * 64,
        space_id="sp1",
        title="保留文档",
    )
    gone_row = LocalDoc(
        document_id="33333333-3333-4333-8333-333333333333",
        node_token="gone",
        obj_edit_time=1,
        catalog_path="旧文档",
        content_hash=None,
        space_id="sp1",
        title="旧文档",
    )
    docs = _FakeDocsRepo([keep_row, gone_row])
    # 远端仅剩 keep（与本地全同 → 判定序⑤ unchanged 不进计划）；gone 差集可信
    client = _FakeClient([_node("keep", title="保留文档", edit=100)])
    jobs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakePipeline()
    es_indexer, purger = _FakeEsIndexer(), _FakePurger()
    worker = _make_worker(
        jobs, docs, client, pipeline=pipeline, es_indexer=es_indexer, purger=purger
    )

    await worker.process_job("job-1")

    row = jobs.rows["job-1"]
    assert row["progress"]["total_docs"] == 0  # keep unchanged 不计 total_docs
    assert row["stats"]["deleted_docs"] == 1  # 仅删除照常执行（完成性门正臂）
    assert docs.deleted == ["33333333-3333-4333-8333-333333333333"]
    assert es_indexer.deleted == [("33333333-3333-4333-8333-333333333333", _USER)]
    assert purger.purged == [(_USER, "33333333-3333-4333-8333-333333333333")]


async def test_process_job_deletion_skipped_when_walk_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 完成性门（walk.errors）：子树遍历失败 → 差集不可信（缺席≠已删除）——
    删除段跳过留待下次完整 walk（api.md §4.7 C5=(a) 只授权删「远端已不存在」）。"""
    keep_row = LocalDoc(
        document_id="44444444-4444-4444-8444-444444444444",
        node_token="keep",
        obj_edit_time=100,
        catalog_path="保留文档",
        content_hash="a" * 64,
        space_id="sp1",
        title="保留文档",
    )
    gone_row = LocalDoc(
        document_id="33333333-3333-4333-8333-333333333333",
        node_token="gone",
        obj_edit_time=1,
        catalog_path="旧文档",
        content_hash=None,
        space_id="sp1",
        title="旧文档",
    )
    docs = _FakeDocsRepo([keep_row, gone_row])
    # keep 在远端、gone 不在（差集名义非空）——但 walk 带子树失败 → 状态未知
    client = _FakeClient(
        [_node("keep", title="保留文档", edit=100)],
        walk_errors=["depth2 parent=tokX → 飞书 API 失败：code=99991679 msg=forbidden"],
    )
    jobs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakePipeline()
    es_indexer, purger = _FakeEsIndexer(), _FakePurger()
    worker = _make_worker(
        jobs, docs, client, pipeline=pipeline, es_indexer=es_indexer, purger=purger
    )

    await worker.process_job("job-1")

    row = jobs.rows["job-1"]
    assert row["status"] == "completed"
    assert "deleted_docs" not in (row["stats"] or {})
    assert docs.deleted == [] and es_indexer.deleted == [] and purger.purged == []


async def test_process_job_deletion_skipped_when_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 完成性门（截断）：max_depth/max_nodes 截断 → units 缺席不等于已删除。"""
    keep_row = LocalDoc(
        document_id="44444444-4444-4444-8444-444444444444",
        node_token="keep",
        obj_edit_time=100,
        catalog_path="保留文档",
        content_hash="a" * 64,
        space_id="sp1",
        title="保留文档",
    )
    gone_row = LocalDoc(
        document_id="33333333-3333-4333-8333-333333333333",
        node_token="gone",
        obj_edit_time=1,
        catalog_path="旧文档",
        content_hash=None,
        space_id="sp1",
        title="旧文档",
    )
    docs = _FakeDocsRepo([keep_row, gone_row])
    client = _FakeClient(
        [_node("keep", title="保留文档", edit=100)], truncated_nodes=3
    )
    jobs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakePipeline()
    es_indexer, purger = _FakeEsIndexer(), _FakePurger()
    worker = _make_worker(
        jobs, docs, client, pipeline=pipeline, es_indexer=es_indexer, purger=purger
    )

    await worker.process_job("job-1")

    row = jobs.rows["job-1"]
    assert row["status"] == "completed"
    assert "deleted_docs" not in (row["stats"] or {})
    assert docs.deleted == []


async def test_process_job_local_rows_filtered_by_space_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 space 过滤：parse_meta.feishu.space_id 不匹配目标空间的本地行既不进
    判定序比对也不进差集——空间绑定错配不触发跨空间全删。"""
    same_space = LocalDoc(
        document_id="44444444-4444-4444-8444-444444444444",
        node_token="keep",
        obj_edit_time=100,
        catalog_path="保留文档",
        content_hash="a" * 64,
        space_id="sp1",
        title="保留文档",
    )
    other_space = LocalDoc(
        document_id="55555555-5555-4555-8555-555555555555",
        node_token="alien",
        obj_edit_time=1,
        catalog_path="他空间文档",
        content_hash=None,
        space_id="sp-other",
        title="他空间文档",
    )
    docs = _FakeDocsRepo([same_space, other_space])
    client = _FakeClient([_node("keep", title="保留文档", edit=100)])
    jobs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakePipeline()
    es_indexer, purger = _FakeEsIndexer(), _FakePurger()
    worker = _make_worker(
        jobs, docs, client, pipeline=pipeline, es_indexer=es_indexer, purger=purger
    )

    await worker.process_job("job-1")

    assert docs.deleted == []  # 他空间行不进差集（未被误删）
    assert jobs.rows["job-1"]["stats"] == {} or "deleted_docs" not in (
        jobs.rows["job-1"]["stats"] or {}
    )


async def test_process_job_credentials_missing_fails_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据缺失 → 任务行 failed（500001 语义面在端点前置；运行期兜底同样收口）。"""
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = FeishuImportWorker(
        jobs=jobs,  # type: ignore[arg-type]
        docs=docs,  # type: ignore[arg-type]
        client_provider=lambda: (_ for _ in ()).throw(FeishuClientError("凭据未配置")),
        pipeline=pipeline,  # type: ignore[arg-type]
        spec_payload_provider=lambda: {},
        minio_client=lambda: None,
        purger=_FakePurger(),
        bucket=TokenBucket(min_interval_s=0.0),
    )

    outcome = await worker.process_job("job-1")

    assert outcome == "failed"
    assert jobs.rows["job-1"]["status"] == "failed"
    assert jobs.rows["job-1"]["finished_at"] is not None


async def test_process_job_walk_errors_traced_as_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子树遍历失败留痕（不中断整树）→ failures 条目 category=permission_denied；
    P3 归一化面：failures 只存固定短语+错误码——原始异常文本（含 parent token 等内部
    标识）仅落服务端日志，不经 §4.9 端点外泄。"""
    client = _FakeClient([_node("t1")])

    async def _walk(space_id: str, **kw: Any) -> Any:
        return type(
            "W",
            (),
            {
                "space_id": space_id,
                "nodes": client.nodes,
                "errors": ["depth2 parent=tokX → 飞书 API 失败：code=99991679 msg=forbidden"],
                "shortcut_count": 0,
                "truncated_nodes": 0,
                "truncated_depth": 0,
            },
        )()

    monkeypatch.setattr(client, "walk_space_tree", _walk)
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    (entry,) = jobs.rows["job-1"]["failures"]
    assert entry["category"] == CATEGORY_PERMISSION_DENIED
    assert "tokX" not in str(entry)  # 内部标识不外泄（P3）
    assert "forbidden" not in str(entry)  # 原始异常文本不外泄（P3）
    assert "99991679" in entry["message"]  # 归一短语+错误码可诊断


async def test_process_job_terminal_row_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = _FakeJobsRepo({"job-1": dict(_pending_job(), status="completed")})
    docs, pipeline = _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, _FakeClient([]), pipeline=pipeline)

    assert await worker.process_job("job-1") == "completed"
    assert jobs.transitions == []  # 终态幂等直返
    assert await worker.process_job("missing") == "gone"


# ---------------------------------------------------------------------------
# 定时全量 diff（scheduled_sync_once——C4=(a)+G7：进行中不建行/零变更不建行/仅删除建行）
# ---------------------------------------------------------------------------


def _run_sync(
    jobs: _FakeJobsRepo,
    docs: _FakeDocsRepo,
    client: _FakeClient | Exception,
    *,
    enqueue: list[str] | None = None,
) -> int:
    seen = enqueue if enqueue is not None else []

    def _provider() -> Any:
        if isinstance(client, Exception):
            raise client
        return client

    def _enqueue(job_id: str) -> None:  # 与 worker.enqueue 同形态（同步入队）
        seen.append(job_id)

    return asyncio.run(
        scheduled_sync_once(
            jobs=jobs,  # type: ignore[arg-type]
            docs=docs,  # type: ignore[arg-type]
            client_provider=_provider,
            enqueue=_enqueue,
        )
    )


def test_scheduled_sync_skips_when_active_job_exists() -> None:
    """触发时已有 pending/running 任务 → 本轮跳过不建行（G7）。"""
    jobs = _FakeJobsRepo({"busy": dict(_pending_job(), status="running")})
    docs = _FakeDocsRepo(users=[_USER])
    created = _run_sync(jobs, docs, _FakeClient([_node("t1")]))
    assert created == 0 and jobs.created == []


def test_scheduled_sync_no_row_when_zero_change_and_no_deletion() -> None:
    """diff 零变更且无删除 → 不建行（仅日志）。"""
    docs = _FakeDocsRepo(
        [LocalDoc(document_id=str(uuid.uuid4()), node_token="t1", obj_edit_time=100,
                  catalog_path="文档 t1", content_hash=None, space_id="sp1")],
        users=[_USER],
    )
    jobs = _FakeJobsRepo()
    created = _run_sync(jobs, docs, _FakeClient([_node("t1", title="文档 t1", edit=100)]))
    assert created == 0 and jobs.created == []


def test_scheduled_sync_creates_row_for_import_changes() -> None:
    docs = _FakeDocsRepo(
        [LocalDoc(document_id=str(uuid.uuid4()), node_token="t1", obj_edit_time=100,
                  catalog_path="文档 t1", content_hash=None, space_id="sp1")],
        users=[_USER],
    )
    jobs = _FakeJobsRepo()
    enqueue: list[str] = []
    created = _run_sync(
        jobs, docs, _FakeClient([_node("t1", title="文档 t1", edit=200)]), enqueue=enqueue
    )
    assert created == 1
    (entry,) = jobs.created
    assert entry["trigger"] == "scheduled"
    row = jobs.rows[entry["job_id"]]
    assert row["progress"] == {  # 五数执行期才推进——建行时为零值
        "total_docs": 0,
        "processed_docs": 0,
        "imported_docs": 0,
        "failed_docs": 0,
        "skipped_docs": 0,
    }
    assert enqueue == [entry["job_id"]]


def test_scheduled_sync_creates_row_for_deletions_only() -> None:
    """仅删除（C5 差集非空）也建行（轻审 P2 钉明）：trigger=scheduled、建行五数为 0；
    P1 完成性门正臂——远端 walk 非空且完整（keep 与本地一致不变更），差集真实可信。"""
    docs = _FakeDocsRepo(
        [LocalDoc(document_id=str(uuid.uuid4()), node_token="keep", obj_edit_time=100,
                  catalog_path="保留文档", content_hash="a" * 64, space_id="sp1"),
         LocalDoc(document_id=str(uuid.uuid4()), node_token="gone", obj_edit_time=1,
                  catalog_path="旧文档", content_hash=None, space_id="sp1")],
        users=[_USER],
    )
    jobs = _FakeJobsRepo()
    enqueue: list[str] = []
    created = _run_sync(
        jobs, docs, _FakeClient([_node("keep", title="保留文档", edit=100)]), enqueue=enqueue
    )
    assert created == 1
    assert jobs.created[0]["trigger"] == "scheduled"
    assert enqueue == [jobs.created[0]["job_id"]]


def test_scheduled_sync_skips_round_when_walk_incomplete() -> None:
    """P1 完成性门（定时侧）：walk 带子树失败/截断 → 远端状态未知 → 本轮零建行
    （plan.meaningful 因差集为真也不建——不执行「未知即删」）。"""
    docs = _FakeDocsRepo(
        [LocalDoc(document_id=str(uuid.uuid4()), node_token="gone", obj_edit_time=1,
                  catalog_path="旧文档", content_hash=None, space_id="sp1")],
        users=[_USER],
    )
    jobs = _FakeJobsRepo()
    client = _FakeClient(
        [_node("keep", title="保留文档")],
        walk_errors=["depth2 parent=tokX → 飞书 API 失败：code=99991679 msg=forbidden"],
    )
    assert _run_sync(jobs, docs, client) == 0
    assert jobs.created == []

    jobs2 = _FakeJobsRepo()
    truncated = _FakeClient([_node("keep", title="保留文档")], truncated_nodes=2)
    assert _run_sync(jobs2, docs, truncated) == 0
    assert jobs2.created == []


def test_scheduled_sync_skips_round_when_remote_empty_but_local_present() -> None:
    """P1 完成性门（定时侧保守臂）：远端零文档单元而本地非空 → 不建行
    （「空响应」不判「已清空」——真清空走手动 §4.5）。"""
    docs = _FakeDocsRepo(
        [LocalDoc(document_id=str(uuid.uuid4()), node_token="gone", obj_edit_time=1,
                  catalog_path="旧文档", content_hash=None, space_id="sp1")],
        users=[_USER],
    )
    jobs = _FakeJobsRepo()
    assert _run_sync(jobs, docs, _FakeClient([])) == 0
    assert jobs.created == []


def test_scheduled_sync_no_users_no_row() -> None:
    """无飞书来源文档用户 → 零建行（定时 diff 只服务存量导入用户）。"""
    jobs, docs = _FakeJobsRepo(), _FakeDocsRepo(users=[])
    assert _run_sync(jobs, docs, _FakeClient([_node("t1")])) == 0


def test_scheduled_sync_client_failure_does_not_raise() -> None:
    """凭据/客户端失败 → 本轮放弃零建行零抛出（cron 循环口径：失败不中断周期）。"""
    jobs, docs = _FakeJobsRepo(), _FakeDocsRepo(users=[_USER])
    assert _run_sync(jobs, docs, FeishuClientError("凭据未配置")) == 0
    assert jobs.created == []


async def test_worker_start_enqueues_requeue_rows_and_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G5 启动消费端（data-model §1.17 :534 / §4 :608——B20 验收断言）：start()
    将 status IN (pending,running) 任务行实际重入队，worker 消费至终态续跑。"""
    client = _FakeClient(
        [_node("t1", title="文档一")],
        blocks_by_obj={"obj-t1": _docx_blocks("obj-t1")},
    )
    rows = {
        "job-p": dict(_pending_job("job-p")),  # pending=已建未启动也须恢复（§1.17）
        "job-r": dict(_pending_job("job-r"), status="running"),
    }
    jobs = _FakeJobsRepo(rows)
    worker = _make_worker(jobs, _FakeDocsRepo(), client)

    await worker.start()
    await asyncio.wait_for(worker.queue.join(), timeout=5)
    await worker.stop()

    assert rows["job-p"]["status"] == "completed"
    assert rows["job-r"]["status"] == "completed"  # 断点游标续跑（running 行不重置）


def test_scheduled_sync_step4_conservative_creates_row_without_content_pull() -> None:
    """④ 单元在定时判定侧保守视为候选（不重拉内容）——建行交给 worker 执行期精确
    判定（content_hash 比对在执行期完成）。"""
    docs = _FakeDocsRepo(
        [LocalDoc(document_id=str(uuid.uuid4()), node_token="t1", obj_edit_time=100,
                  catalog_path="文档 t1", content_hash="a" * 64, space_id="sp1")],
        users=[_USER],
    )
    jobs = _FakeJobsRepo()
    client = _FakeClient([_node("t1", title="文档 t1", edit=None)])
    assert _run_sync(jobs, docs, client) == 1
    assert client.block_calls == []  # 判定侧零内容拉取


# ---------------------------------------------------------------------------
# 仓储/装配协作面
# ---------------------------------------------------------------------------


def test_resolve_space_id_prefers_bound_space_then_single_space() -> None:
    """space 裁决（P2 收紧）：settings 绑定值优先；未绑定时仅单空间直取、多空间
    显式拒绝（fail 任务要求绑定——静默取首个会把全量 diff 对着错误空间跑）、
    零空间仍拒（无可用知识空间）。"""

    class _Multi:
        async def list_spaces(self) -> list[Any]:
            return [
                type("S", (), {"space_id": "sp-8", "name": "八"})(),
                type("S", (), {"space_id": "sp-9", "name": "九"})(),
            ]

    class _Single:
        async def list_spaces(self) -> list[Any]:
            return [type("S", (), {"space_id": "sp-only", "name": "唯一"})()]

    assert asyncio.run(resolve_space_id(_Multi(), preferred_space_id="sp-7")) == "sp-7"

    with pytest.raises(FeishuClientError, match="FEISHU_SPACE_ID"):
        asyncio.run(resolve_space_id(_Multi(), preferred_space_id=""))  # 多空间未绑定

    assert asyncio.run(resolve_space_id(_Single(), preferred_space_id="")) == "sp-only"

    class _Empty:
        async def list_spaces(self) -> list[Any]:
            return []

    with pytest.raises(FeishuClientError):
        asyncio.run(resolve_space_id(_Empty(), preferred_space_id=""))


def test_repo_classes_exposed_for_production_wiring() -> None:
    """生产装配依赖面存在（真仓储由 integration 覆盖）。"""
    assert KbImportJobRepository is not None
    assert FeishuImportDocRepository is not None


async def test_process_job_cascade_delete_logged_per_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 审计留痕：差集级联删除逐文档 BusinessLog（document_id+node_token+title+
    job_id，action=kb_import_cascade_delete）——删除动作可回查（§4.5 手动删除尚有
    kb_purge 日志，后台删除不缺审计粒度）。"""
    from app.rag import import_worker as iw

    logged: list[tuple[str, str]] = []

    class _SpyLog:
        def __init__(self, *, message: str, action: str = "", **kw: Any) -> None:
            logged.append((action, message))

        def info(self, *a: Any, **k: Any) -> None:
            pass

        def warning(self, *a: Any, **k: Any) -> None:
            pass

        def error(self, *a: Any, **k: Any) -> None:
            pass

    monkeypatch.setattr(iw, "BusinessLog", _SpyLog)

    keep_row = LocalDoc(
        document_id="44444444-4444-4444-8444-444444444444",
        node_token="keep",
        obj_edit_time=100,
        catalog_path="保留文档",
        content_hash="a" * 64,
        space_id="sp1",
        title="保留文档",
    )
    gone_row = LocalDoc(
        document_id="33333333-3333-4333-8333-333333333333",
        node_token="gone",
        obj_edit_time=1,
        catalog_path="旧文档",
        content_hash=None,
        space_id="sp1",
        title="旧文档",
    )
    docs = _FakeDocsRepo([keep_row, gone_row])
    client = _FakeClient([_node("keep", title="保留文档", edit=100)])
    jobs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    await worker.process_job("job-1")

    cascade_logs = [(a, m) for a, m in logged if a == "kb_import_cascade_delete"]
    assert len(cascade_logs) == 1  # 逐文档一条（非聚合计数）
    (_, message) = cascade_logs[0]
    assert "33333333-3333-4333-8333-333333333333" in message  # document_id
    assert "gone" in message  # node_token
    assert "旧文档" in message  # title
    assert "job-1" in message  # 任务关联


# ---------------------------------------------------------------------------
# sheet(30) 装配后回填段（§1.18 v1.20 30 行：placeholder 翻转 + rows/col_count 物化；
# 超限降级留痕 parse_meta；失败保留占位不算文档失败；重入队幂等；bitable 零回填）
# ---------------------------------------------------------------------------


def _sdk_sheet(bid: str, parent: str, token: str = "shT0kEn") -> Any:
    """sheet(30) 块假体（SDK 内省 Sheet{token, row_size, column_size}）。"""
    payload = type("Sheet", (), {"token": token, "row_size": 3, "column_size": 2})()
    return type(
        "B", (), {"block_id": bid, "parent_id": parent, "children": [], "block_type": 30, "sheet": payload}
    )()


def _sdk_bitable(bid: str, parent: str, token: str = "biT0kEn") -> Any:
    """bitable(18) 块假体（SDK 内省 Bitable{token, view_type}）。"""
    payload = type("Bitable", (), {"token": token, "view_type": 0})()
    return type(
        "B", (), {"block_id": bid, "parent_id": parent, "children": [], "block_type": 18, "bitable": payload}
    )()


def _sheet_value_data(
    rows: list[list[str]], *, tr: int = 0, tc: int = 0, tcell: int = 0
) -> Any:
    from app.rag.feishu_client import SheetValuesData

    return SheetValuesData(rows=rows, truncated_rows=tr, truncated_cols=tc, truncated_cells=tcell)


def _iter_all(node: Any) -> Any:
    from collections import deque

    queue = deque([node])
    while queue:
        cur = queue.popleft()
        yield cur
        queue.extend(cur.children)


def _blocks_with_sheet() -> list[Any]:
    """page 根 + 文本块 + sheet(30) 块（sheet 在 page children 序内）。"""
    return [_sdk_page(["b0", "sh1"]), _sdk_text("b0", "段落", "page1"), _sdk_sheet("sh1", "page1")]


async def test_sheet_backfill_materializes_rows_and_flips_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回填接线主线：docx 单元含 sheet 块 → 升格占位表被扫描、sheets 值矩阵物化
    meta.rows/col_count、placeholder 翻 false；零截断零失败 → sheet_backfill 键省略、
    degraded_blocks 零 sheet 键（升格非降级——契约 30 行）。"""
    from app.rag import import_worker as iw
    from app.rag.ir_schema import IrNodeType

    client = _FakeClient(
        [_node("t1", title="含表文档")],
        blocks_by_obj={"obj-t1": _blocks_with_sheet()},
        sheet_values={"shT0kEn": _sheet_value_data([["表头A", "表头B"], ["1", "2"]])},
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    captured: dict[str, Any] = {}

    async def _fake_put(user_id: str, doc_id: str, root: Any, *, client: Any = None) -> str:
        captured["root"] = root
        return f"kb/{user_id}/{doc_id}/ir.json"

    monkeypatch.setattr(iw, "put_ir_json", _fake_put)

    outcome = await worker.process_job("job-1")

    assert outcome == "completed"
    assert client.sheet_calls == ["shT0kEn"]  # 恰一次拉取（token 寻址）
    sheet_node = next(n for n in _iter_all(captured["root"]) if n.id == "fs:blk:sh1")
    assert sheet_node.type is IrNodeType.TABLE
    assert sheet_node.meta["placeholder"] is False  # 回填完成翻转
    assert sheet_node.meta["rows"] == [["表头A", "表头B"], ["1", "2"]]  # 物化矩阵（含表头首行）
    assert sheet_node.meta["col_count"] == 2
    assert sheet_node.text == "[sheet]"  # 占位字面恒保留（纯文本消费面兜底）
    row = docs.created_rows[0]
    assert "sheet_backfill" not in row["parse_meta"]["feishu"]  # 零截断零失败 → 键省略
    assert "degraded_blocks" not in row["parse_meta"]["feishu"]  # 升格不计入（契约 30 行）


async def test_sheet_backfill_truncation_traces_to_parse_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超限降级留痕（真实落到 parse_meta，非注释声明）：护栏截断计数入
    parse_meta.feishu.sheet_backfill（0 值省略键）+ degraded_blocks 落
    sheet_backfill_truncated 计数。"""
    from app.rag import import_worker as iw

    client = _FakeClient(
        [_node("t1", title="超限表文档")],
        blocks_by_obj={"obj-t1": _blocks_with_sheet()},
        sheet_values={"shT0kEn": _sheet_value_data([["a", "b"]], tr=2, tc=1, tcell=3)},
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    async def _fake_put(user_id: str, doc_id: str, root: Any, *, client: Any = None) -> str:
        return f"kb/{user_id}/{doc_id}/ir.json"

    monkeypatch.setattr(iw, "put_ir_json", _fake_put)

    outcome = await worker.process_job("job-1")

    assert outcome == "completed"  # 截断≠文档失败（内容保留前 N）
    feishu = docs.created_rows[0]["parse_meta"]["feishu"]
    assert feishu["sheet_backfill"] == {
        "truncated_rows": 2,
        "truncated_cols": 1,
        "truncated_cells": 3,
    }
    assert feishu["degraded_blocks"] == {"sheet_backfill_truncated": 1}  # 超限降级留痕


async def test_sheet_backfill_failure_keeps_placeholder_and_job_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回填失败：占位保留（placeholder 维持 true、零 rows）+ parse_meta 留痕
    （failed + failed_categories 失败分类）+ 不算文档失败（任务照常 completed、
    imported=1——27 行 image 先例同款）。"""
    from app.rag import import_worker as iw
    from app.rag.feishu_client import FeishuSheetValuesError

    client = _FakeClient(
        [_node("t1", title="失败表文档")],
        blocks_by_obj={"obj-t1": _blocks_with_sheet()},
        sheet_errors={"shT0kEn": FeishuSheetValuesError(
            "飞书 API 失败：code=99991672 msg=denied", category="permission"
        )},
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    captured: dict[str, Any] = {}

    async def _fake_put(user_id: str, doc_id: str, root: Any, *, client: Any = None) -> str:
        captured["root"] = root
        return f"kb/{user_id}/{doc_id}/ir.json"

    monkeypatch.setattr(iw, "put_ir_json", _fake_put)

    outcome = await worker.process_job("job-1")

    assert outcome == "completed"
    row = jobs.rows["job-1"]
    assert row["progress"]["imported_docs"] == 1  # 不算文档失败
    assert row["failures"] == []  # 不进失败清单（回填失败≠单元失败）
    sheet_node = next(n for n in _iter_all(captured["root"]) if n.id == "fs:blk:sh1")
    assert sheet_node.meta["placeholder"] is True  # 占位保留
    assert "rows" not in sheet_node.meta
    feishu = docs.created_rows[0]["parse_meta"]["feishu"]
    assert feishu["sheet_backfill"] == {"failed": 1, "failed_categories": {"permission": 1}}
    assert "degraded_blocks" not in feishu  # 失败不落 degraded_blocks（保留占位非降级留痕）


async def test_bitable_placeholder_not_backfilled_this_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bitable(18)：升格占位形态保留、token 留痕即终点——本批零回填（sheets 通道
    不寻址 Base API 域，回填协议挂后续工单不钉）。"""
    from app.rag import import_worker as iw

    blocks = [
        _sdk_page(["b0", "bi1"]),
        _sdk_text("b0", "段落", "page1"),
        _sdk_bitable("bi1", "page1"),
    ]
    client = _FakeClient(
        [_node("t1", title="含多维表文档")],
        blocks_by_obj={"obj-t1": blocks},
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    captured: dict[str, Any] = {}

    async def _fake_put(user_id: str, doc_id: str, root: Any, *, client: Any = None) -> str:
        captured["root"] = root
        return f"kb/{user_id}/{doc_id}/ir.json"

    monkeypatch.setattr(iw, "put_ir_json", _fake_put)

    await worker.process_job("job-1")

    assert client.sheet_calls == []  # 零 sheets API 调用
    bitable_node = next(n for n in _iter_all(captured["root"]) if n.id == "fs:blk:bi1")
    assert bitable_node.meta["placeholder"] is True  # 占位形态保留
    assert bitable_node.meta["bitable_token"] == "biT0kEn"  # 原值留痕
    assert "rows" not in bitable_node.meta
    assert "sheet_backfill" not in docs.created_rows[0]["parse_meta"]["feishu"]


async def test_backfill_sheet_placeholders_rerun_idempotent() -> None:
    """重入队幂等（函数级）：同一树重跑回填段——已回填（placeholder=false）节点
    跳过（零重复拉取、零重复写、零重复计数）；bitable/image 占位非 sheets 通道
    恒跳过。"""
    from app.rag.ir_schema import IrNode
    from app.rag.import_worker import backfill_sheet_placeholders

    doc = IrNode(
        id="fs:node:t1",
        type="document",
        meta={"raw_type": "docx"},
        children=[
            IrNode(
                id="fs:blk:sh1",
                parent_id="fs:node:t1",
                type="table",
                meta={"raw_type": "feishu.sheet", "placeholder": True, "sheet_token": "tok1"},
            ),
            IrNode(
                id="fs:blk:bi1",
                parent_id="fs:node:t1",
                type="table",
                meta={"raw_type": "feishu.bitable", "placeholder": True, "bitable_token": "tok2"},
            ),
            IrNode(
                id="fs:blk:img1",
                parent_id="fs:node:t1",
                type="image",
                meta={"raw_type": "feishu.image", "placeholder": True, "image_token": "tok3"},
            ),
        ],
    )
    calls: list[str] = []

    async def _fetch(token: str) -> Any:
        calls.append(token)
        return _sheet_value_data([["a", "b"]], tr=1, tc=0, tcell=2)

    report1 = await backfill_sheet_placeholders(doc, _fetch)

    assert calls == ["tok1"]  # bitable/image 不经 sheets 通道
    assert report1.backfilled == 1
    assert report1.truncated_nodes == 1 and report1.truncated_rows == 1 and report1.truncated_cells == 2

    rows_before = next(n for n in doc.children if n.id == "fs:blk:sh1").meta["rows"]
    report2 = await backfill_sheet_placeholders(doc, _fetch)

    assert calls == ["tok1"]  # 重跑零重复拉取
    assert report2.backfilled == 0  # 零重复写
    assert report2.truncated_nodes == 0 and report2.truncated_rows == 0  # 零重复计数
    node = next(n for n in doc.children if n.id == "fs:blk:sh1")
    assert node.meta["placeholder"] is False and node.meta["rows"] is rows_before  # 树未被二次改写


async def test_process_job_requeue_completed_no_double_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 缝重入队幂等：终态任务重跑直返（不重复导入）→ sheets 拉取调用数
    不变（不重复写、不重复计数）。"""
    from app.rag import import_worker as iw

    client = _FakeClient(
        [_node("t1", title="含表文档")],
        blocks_by_obj={"obj-t1": _blocks_with_sheet()},
        sheet_values={"shT0kEn": _sheet_value_data([["a", "b"]])},
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)

    async def _fake_put(user_id: str, doc_id: str, root: Any, *, client: Any = None) -> str:
        return f"kb/{user_id}/{doc_id}/ir.json"

    monkeypatch.setattr(iw, "put_ir_json", _fake_put)

    await worker.process_job("job-1")
    assert client.sheet_calls == ["shT0kEn"]

    outcome2 = await worker.process_job("job-1")  # 同一任务重跑（重入队）

    assert outcome2 == "completed"  # 终态幂等直返
    assert client.sheet_calls == ["shT0kEn"]  # 零重复拉取
    assert len(docs.created_rows) == 1  # 零重复建行


# ---------------------------------------------------------------------------
# 判定序④同源比对（评审 P1：_pull_content_hash 与本地 ingest 链同组合——
# tokens/obj_map 全量供给 + sheet(30) 回填后取 hash；缺任一段则含 sheet 块或
# mention_doc 引用的文档两侧 hash 恒异 → obj_edit_time 缺失文档每轮恒判「变更」
# → 级联删+全量重导死循环）
# ---------------------------------------------------------------------------


async def test_pull_content_hash_same_source_parity_with_ingest_combo() -> None:
    """④ 兜底 hash 与本地口径同源：参照 hash 由 ingest 组合第一性构建（不经共享
    helper——避免循环自证）：map_blocks(tokens/obj_map) → build → catalog → attach →
    回填 → chunk。断言 pull 侧 hash 与之一致且回填真被触达。"""
    from app.rag.chunker import ChunkingConfig, chunk_forest
    from app.rag.feishu_client import SheetValuesData
    from app.rag.feishu_mapper import build_document_node, map_blocks
    from app.rag.import_worker import backfill_sheet_placeholders
    from app.rag.tree_assembler import attach_block_tree

    blocks = _blocks_with_sheet()
    client = _FakeClient(
        [],
        blocks_by_obj={"obj-d1": blocks},
        sheet_values={"shT0kEn": _sheet_value_data([["列甲", "列乙"], ["甲1", "乙1"]])},
    )
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)
    unit = _remote(token="d1", edit=None)
    tokens = frozenset({"d1"})
    obj_map = {"d1": "obj-d1"}

    # 参照（本地 ingest 同组合第一性构建，不经 helper）
    mapping = map_blocks(
        blocks, space_id="sp1", node_token="d1",
        known_node_tokens=tokens, node_token_by_obj_token=obj_map,
    )
    doc_node = build_document_node(
        node_token="d1", title="文档一", obj_type="docx",
        obj_edit_time=None, space_id="sp1",
    )
    doc_node.catalog_ids = ["fs:node:d1"]
    doc_node.catalog_path = ["文档一"]
    attach_block_tree(doc_node, mapping.refs, root_order=mapping.root_order)

    async def _fetch(token: str) -> SheetValuesData:
        return SheetValuesData(rows=[["列甲", "列乙"], ["甲1", "乙1"]])

    await backfill_sheet_placeholders(doc_node, _fetch)
    local_hash = chunk_forest(doc_node, config=ChunkingConfig())[0].document_content_hash

    pulled = await worker._pull_content_hash(
        unit, tokens=tokens, obj_map=obj_map, space_id="sp1"
    )

    assert client.sheet_calls == ["shT0kEn"]  # pull 侧回填真被触达
    assert pulled == local_hash
    assert pulled and pulled != "<pull-failed>"


async def test_pull_content_hash_file_uses_file_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """④ 兜底 file 分支同样走 download_file（与 _download_attachment 同通道）——
    否则 obj_edit_time 缺失的附件文档每轮恒判「变更」（下载恒 403 → 哨兵值）。"""
    import app.rag.chunker as chunker_mod
    import app.rag.normalizers as normalizers_mod

    normalized_doc = type("D", (), {})()
    monkeypatch.setattr(
        normalizers_mod,
        "normalize_document",
        lambda data, ext, **kw: type("N", (), {"document": normalized_doc})(),
    )
    monkeypatch.setattr(
        chunker_mod,
        "chunk_forest",
        lambda doc, config=None: [type("C", (), {"document_content_hash": "c" * 64})()],
    )

    client = _FakeClient([], download=b"%PDF-1.7 body")
    jobs, docs, pipeline = _FakeJobsRepo({"job-1": _pending_job()}), _FakeDocsRepo(), _FakePipeline()
    worker = _make_worker(jobs, docs, client, pipeline=pipeline)
    unit = _remote(token="f1", obj_type="file", edit=None, title="手册.pdf")

    pulled = await worker._pull_content_hash(
        unit, tokens=frozenset(), obj_map={}, space_id="sp1"
    )

    assert pulled == "c" * 64
    assert client.file_calls == ["obj-f1"]
    assert client.media_calls == []


# ---------------------------------------------------------------------------
# 回填意外异常归一（评审 P3：backfill 只捕 FeishuClientError → 意外异常经单元兜底
# 整文档失败（normalize_failed）且栈含正文片段落 error 日志——契约「回填失败保留
# 占位、不算文档失败」须无条件成立；留痕只记类型名，零 payload）
# ---------------------------------------------------------------------------


async def test_backfill_unexpected_error_counts_category_and_keeps_placeholder() -> None:
    from app.rag.ir_schema import IrNode
    from app.rag.import_worker import backfill_sheet_placeholders

    doc = IrNode(
        id="fs:node:t1",
        type="document",
        meta={"raw_type": "docx"},
        children=[
            IrNode(
                id="fs:blk:shX",
                parent_id="fs:node:t1",
                type="table",
                meta={"raw_type": "feishu.sheet", "placeholder": True, "sheet_token": "tokX"},
            ),
        ],
    )

    async def _fetch(token: str) -> Any:
        raise RuntimeError("boom 含正文片段内容")  # 非 FeishuClientError 意外异常

    report = await backfill_sheet_placeholders(doc, _fetch)

    node = doc.children[0]
    assert node.meta["placeholder"] is True  # 占位保留
    assert "rows" not in node.meta
    assert report.failed == 1
    assert report.failed_categories == {"unexpected:RuntimeError": 1}  # 类型名，零消息内容
    assert report.parse_meta_block() == {
        "failed": 1,
        "failed_categories": {"unexpected:RuntimeError": 1},
    }
