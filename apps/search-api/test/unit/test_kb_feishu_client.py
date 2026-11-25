"""unit 层：飞书 SDK 客户端封装 feishu_client（M3 批次 3 / B20 A 线①）。

覆盖面（零网络——SDK 客户端全部注入假体；真凭据链路走 integration 显式门控冒烟，
沿 RUN_PADDLE_OCR_SMOKE 先例 = RUN_FEISHU_IMPORT_SMOKE，主会话/用户执行）：
- **lark-oapi 护栏单测（import 断言）**：==1.8.0 锁版 + Client 三域调用路径在位
  （wiki.v2.space_node / docx.v1.document_block / drive.v1.media——锁版护栏：
  上游 API 面漂移先在护栏红，R8/R16 同口径）；
- **tenant token 缓存**：默认客户端进程内单例（SDK Client 构建一次——tenant_access_token
  由 SDK 内部缓存，本层缓存=客户端实例复用，避免每调用重建触发 token 重取）；
- **凭据纪律**：凭据只从 settings 单例读；错误文本一律脱敏（app_secret/ token 零回显）；
- **wiki 全树遍历**：0.65s 节流 / 99991400 指数退避（notebook
  feishu_kb_tree_probe.ipynb 先例）/ BFS + 防御上限（深度 6 / 节点 2000 / 单层翻页
  100）/ shortcut 只计数不展开 / 环安全（token 首见为准）/ 子树失败即停该子树不中断整树；
- **docx blocks 直读**：page_size=500 分页翻全 + 翻页防御上限截断留痕；
- **file 附件下载**：云空间文件走 drive `file.download` → bytes（2026-11-25 探针实证
  media.download 对 wiki file 节点恒 403 空体）；media.download 保留给云文档内素材
  （B21 图片管线）；错误码/403 空体归一 FeishuClientError；
- **附件/图片素材入 MinIO**：附件沿 `kb/{user_id}/{doc_id}/original.{ext}`（storage
  白名单校验），图片素材键为实现面 `kb/{user_id}/{doc_id}/img/{block_id}.{ext}`。
"""

import io
import json
from typing import Any

import pytest

import app.rag.feishu_client as fc
from app.rag.feishu_client import (
    DOCX_BLOCK_PAGE_SIZE,
    FEISHU_NODE_CALL_MIN_INTERVAL_S,
    FEISHU_RATE_LIMIT_CODE,
    FeishuClient,
    FeishuClientError,
    NodeInfo,
    image_material_object_key,
    store_attachment,
    store_image_material,
)


# ---------------------------------------------------------------------------
# SDK 假体（对齐 lark_oapi 1.8.0 调用路径与响应形态）
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, code: int | None = 0, msg: str = "", data: Any = None, file: Any = None) -> None:
        self.code = code
        self.msg = msg
        self.data = data
        self.file = file


class _Page:
    def __init__(self, items: list[Any], page_token: str = "", has_more: bool = False) -> None:
        self.items = items
        self.page_token = page_token
        self.has_more = has_more


class _WikiNode:
    """wiki v2 节点条目形态（nd.node_token/obj_token/obj_type/parent_node_token/node_type/has_child/title）。"""

    def __init__(self, node_token: str, *, obj_token: str = "", obj_type: str = "docx", parent: str = "", node_type: str = "", has_child: bool = False, title: str = "") -> None:
        self.node_token = node_token
        self.obj_token = obj_token
        self.obj_type = obj_type
        self.parent_node_token = parent
        self.node_type = node_type
        self.has_child = has_child
        self.title = title


class _RecordingLister:
    """分页 list 端点假体：按 (parent_token, page_token) 出页；记录每次请求属性。"""

    def __init__(self, pages_by_parent: dict[str, list[_Page]] | None = None, error_code: int | None = None) -> None:
        self.pages_by_parent = pages_by_parent or {}
        self.error_code = error_code
        self.requests: list[Any] = []

    def list(self, request: Any) -> _Resp:
        self.requests.append(request)
        if self.error_code is not None:
            return _Resp(code=self.error_code, msg=f"boom secret={fc_get_secret()}")
        parent = getattr(request, "parent_node_token", "") or ""
        pages = self.pages_by_parent.get(parent, [])
        idx = 0
        for req in self.requests[:-1]:
            if (getattr(req, "parent_node_token", "") or "") == parent:
                idx += 1
        if idx >= len(pages):
            return _Resp(code=0, data=_Page([]))
        page = pages[idx]
        return _Resp(code=0, data=page)


def fc_get_secret() -> str:
    return "SUPERSECRET"


class _FakeLark:
    def __init__(self, *, space: Any = None, space_node: Any = None, document_block: Any = None, media: Any = None, file: Any = None, sheet: Any = None, raw: Any = None) -> None:
        self.wiki = type("W", (), {"v2": type("V2", (), {"space": space, "space_node": space_node})()})()
        self.docx = type("D", (), {"v1": type("V1", (), {"document_block": document_block})()})()
        self.drive = type("Dr", (), {"v1": type("DV1", (), {"media": media, "file": file})()})()
        self.sheets = type("SH", (), {"v3": type("V3", (), {"spreadsheet_sheet": sheet})()})()
        self._raw = raw

    def request(self, req: Any) -> Any:
        """原始通道（v2 values——真 SDK 形态：BaseRequest → BaseResponse{code,msg,raw}）。"""
        return self._raw.request(req)


def _client(sdk: Any, **kw: Any) -> FeishuClient:
    defaults: dict[str, Any] = {
        "app_id": "cli_fake_app",
        "app_secret": fc_get_secret(),
        "api_base": "https://open.feishu.cn",
        "min_interval_s": 0.0,  # 测试零节流（生产默认 0.65 常量单独断言）
        "backoff_base_s": 0.001,
    }
    defaults.update(kw)
    return FeishuClient(client=sdk, **defaults)


# ---------------------------------------------------------------------------
# lark-oapi 护栏（import 断言，锁版 + 调用路径在位）
# ---------------------------------------------------------------------------


def test_lark_oapi_version_lock() -> None:
    import importlib.metadata

    assert importlib.metadata.version("lark-oapi") == "1.8.0"


def test_lark_client_call_paths_exist() -> None:
    """Client 三域调用路径在位（构建离线零网络；假凭据仅路径探测，非真实凭据）。"""
    import lark_oapi as lark

    c = lark.Client.builder().app_id("guard-probe-app").app_secret("guard-probe-secret").build()
    assert c.wiki.v2.space_node is not None
    assert c.wiki.v2.space is not None
    assert c.docx.v1.document_block is not None
    assert c.drive.v1.media is not None
    assert c.drive is not None and c.drive.v1.file is not None  # 云空间文件下载通道（file 附件正解；media 对其 403）


# ---------------------------------------------------------------------------
# 默认客户端单例（tenant token 缓存载体）与凭据纪律
# ---------------------------------------------------------------------------


def test_default_client_singleton_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(fc, "_default_client", None)
    monkeypatch.setattr(fc.feishu_settings, "app_id", SecretStr("singleton-app"))
    monkeypatch.setattr(fc.feishu_settings, "app_secret", SecretStr("singleton-secret"))
    c1 = fc.get_default_feishu_client()
    c2 = fc.get_default_feishu_client()
    assert c1 is c2  # 进程内复用同一实例（SDK 内部缓存 tenant token，避免重建重取）
    assert c1.app_id == "singleton-app"


def test_default_client_missing_credentials_raises_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(fc, "_default_client", None)
    monkeypatch.setattr(fc.feishu_settings, "app_id", SecretStr(""))
    with pytest.raises(FeishuClientError) as exc:
        fc.get_default_feishu_client()
    assert "SUPERSECRET" not in str(exc.value)
    assert "singleton-secret" not in str(exc.value)


# ---------------------------------------------------------------------------
# wiki 全树遍历：分页 / 节流 / 退避 / BFS / shortcut / 环 / 防御上限 / 子树失败
# ---------------------------------------------------------------------------


def test_node_call_min_interval_constant() -> None:
    """0.65s 节流常量（notebook 先例：wiki nodes 100 次/分钟 → 保守 0.65s/次）。"""
    assert FEISHU_NODE_CALL_MIN_INTERVAL_S == 0.65


class _FakeClock:
    """可注入 clock + sleep（零真实等待）：sleep 推进假钟，令牌桶间隔可观测。"""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.asyncio
async def test_throttle_enforces_min_interval_with_injected_clock() -> None:
    """节流可注入 clock：min_interval_s=0.2（飞书开放平台 5 QPS 底线口径）时，相邻两次
    调用间由注入 sleep 精确补足间隔——零真实等待、间隔量可断言。"""
    clock = _FakeClock()

    class _TwoPageLister(_RecordingLister):
        def list(self, request: Any) -> _Resp:  # 定页序：首页翻页 → 次页终止（两次 list 调用）
            self.requests.append(request)
            if (getattr(request, "page_token", "") or "") == "":  # 真实 SDK request.page_token=None（非 ""）
                return _Resp(code=0, data=_Page([_WikiNode("n1", parent="")], page_token="p2", has_more=True))
            return _Resp(code=0, data=_Page([_WikiNode("n2", parent="")]))

    sdk = _FakeLark(space_node=_TwoPageLister({}))
    items, err = await _client(
        sdk, min_interval_s=0.2, sleep_fn=clock.sleep, clock=clock
    ).list_space_nodes("sp1")
    assert err == ""
    assert [n.node_token for n in items] == ["n1", "n2"]
    # 相邻调用恰补 min_interval_s（第二次调用前 sleep(0.2) 推进假钟；退避零触发）
    assert clock.sleeps == [0.2]
    assert clock.now >= 100.0 + 0.2


@pytest.mark.asyncio
async def test_list_space_nodes_pagination_full() -> None:
    pages = {
        "": [_Page([_WikiNode("n1", parent="")], page_token="pt2", has_more=True), _Page([_WikiNode("n2", parent="")])],
    }
    sdk = _FakeLark(space_node=_RecordingLister(pages))
    items, err = await _client(sdk).list_space_nodes("sp1")
    assert err == ""
    assert [n.node_token for n in items] == ["n1", "n2"]
    assert all(getattr(r, "space_id", None) == "sp1" for r in sdk.wiki.v2.space_node.requests)


@pytest.mark.asyncio
async def test_rate_limit_backoff_then_success() -> None:
    """99991400 → 指数退避重试；退避 sleep 走注入 sleep_fn（可观测）。"""
    sleeps: list[float] = []
    calls: list[Any] = []

    def once_error_list(request: Any) -> _Resp:
        calls.append(request)
        if len(calls) == 1:
            return _Resp(code=FEISHU_RATE_LIMIT_CODE, msg="rate limited")
        return _Resp(code=0, data=_Page([_WikiNode("n1")]))

    lister = _RecordingLister({})
    lister.list = once_error_list  # type: ignore[method-assign]
    sdk = _FakeLark(space_node=lister)
    items, err = await _client(sdk, sleep_fn=sleeps.append).list_space_nodes("sp1")
    assert err == ""
    assert [n.node_token for n in items] == ["n1"]
    assert sleeps == [0.001]  # backoff_base_s × 2**0，指数退避已触发


@pytest.mark.asyncio
async def test_rate_limit_retries_exhausted_returns_error() -> None:
    def always_limited(request: Any) -> _Resp:
        return _Resp(code=FEISHU_RATE_LIMIT_CODE, msg="rate limited")

    lister = _RecordingLister({})
    lister.list = always_limited  # type: ignore[method-assign]
    sdk = _FakeLark(space_node=lister)
    items, err = await _client(sdk, sleep_fn=lambda _: None).list_space_nodes("sp1")
    assert items == []
    assert "99991400" in err
    assert fc_get_secret() not in err  # 错误文本零密钥回显（脱敏）


@pytest.mark.asyncio
async def test_walk_tree_bfs_shortcut_not_expanded_and_errors_isolated() -> None:
    root = _WikiNode("root", obj_token="ot_root", parent="", has_child=True, title="根目录")
    doc = _WikiNode("doc1", obj_token="ot1", parent="root", has_child=False, obj_type="docx", title="文档一")
    shortcut = _WikiNode("sc1", parent="root", has_child=True, node_type="shortcut", title="快捷方式")
    deep = _WikiNode("deep", obj_token="otd", parent="sc1", has_child=False)  # 若误展开 shortcut 会拉到

    pages: dict[str, list[_Page]] = {
        "": [_Page([root])],
        "root": [_Page([doc, shortcut])],
        "sc1": [_Page([deep])],  # 不应被请求（shortcut 只计数不展开）
    }

    def list_by_parent(request: Any) -> _Resp:
        lister.requests.append(request)
        parent = getattr(request, "parent_node_token", "") or ""
        got = pages.get(parent, [])
        return _Resp(code=0, data=got[0] if got else _Page([]))

    lister = _RecordingLister({})
    lister.list = list_by_parent  # type: ignore[method-assign]
    sdk = _FakeLark(space_node=lister)
    result = await _client(sdk).walk_space_tree("sp1")
    tokens = [n.node_token for n in result.nodes]
    assert tokens == ["root", "doc1", "sc1"]  # shortcut 本体保留；deep 未拉
    assert result.shortcut_count == 1
    assert result.errors == []
    # shortcut 的子层未被请求
    requested_parents = [getattr(r, "parent_node_token", "") or "" for r in lister.requests]
    assert "sc1" not in requested_parents


@pytest.mark.asyncio
async def test_walk_tree_subtree_error_does_not_kill_rest() -> None:
    root = _WikiNode("root", parent="", has_child=True)
    bad = _WikiNode("bad", parent="root", has_child=True)
    good = _WikiNode("good", obj_token="otg", parent="root", has_child=False, obj_type="docx")

    def list_by_parent(request: Any) -> _Resp:
        parent = getattr(request, "parent_node_token", "") or ""
        if parent == "bad":
            return _Resp(code=131006, msg="permission denied")
        got = {"": [_Page([root])], "root": [_Page([bad, good])]}.get(parent, [])
        return _Resp(code=0, data=got[0] if got else _Page([]))

    lister = _RecordingLister({})
    lister.list = list_by_parent  # type: ignore[method-assign]
    sdk = _FakeLark(space_node=lister)
    result = await _client(sdk).walk_space_tree("sp1")
    assert [n.node_token for n in result.nodes] == ["root", "bad", "good"]
    assert len(result.errors) == 1
    assert "131006" in result.errors[0]


@pytest.mark.asyncio
async def test_walk_tree_cycle_safe_and_node_cap() -> None:
    root = _WikiNode("root", parent="", has_child=True)
    a = _WikiNode("a", parent="root", has_child=True)
    # 环：root 的子层再返回 root（首见为准）
    pages: dict[str, list[_Page]] = {
        "": [_Page([root])],
        "root": [_Page([a, root])],  # 环边
        "a": [_Page([])],
    }

    def list_by_parent(request: Any) -> _Resp:
        parent = getattr(request, "parent_node_token", "") or ""
        got = pages.get(parent, [])
        return _Resp(code=0, data=got[0] if got else _Page([]))

    lister = _RecordingLister({})
    lister.list = list_by_parent  # type: ignore[method-assign]
    sdk = _FakeLark(space_node=lister)
    result = await _client(sdk).walk_space_tree("sp1")
    assert sorted(n.node_token for n in result.nodes) == ["a", "root"]
    assert result.errors == []

    # 节点上限：max_nodes=1 → 第二个节点不计、truncated_nodes 留痕
    result2 = await _client(sdk).walk_space_tree("sp1", max_nodes=1)
    assert len(result2.nodes) == 1
    assert result2.truncated_nodes == 1


@pytest.mark.asyncio
async def test_walk_tree_depth_cap() -> None:
    n1 = _WikiNode("n1", parent="", has_child=True)
    n2 = _WikiNode("n2", parent="n1", has_child=True)
    n3 = _WikiNode("n3", parent="n2", has_child=True)  # 深度超限 → 不展开（不被拉取）

    def list_by_parent(request: Any) -> _Resp:
        lister.requests.append(request)
        parent = getattr(request, "parent_node_token", "") or ""
        got = {"": [_Page([n1])], "n1": [_Page([n2])], "n2": [_Page([n3])], "n3": [_Page([])]}.get(parent, [])
        return _Resp(code=0, data=got[0] if got else _Page([]))

    lister = _RecordingLister({})
    lister.list = list_by_parent  # type: ignore[method-assign]
    sdk = _FakeLark(space_node=lister)
    result = await _client(sdk).walk_space_tree("sp1", max_depth=2)
    assert [n.node_token for n in result.nodes] == ["n1", "n2"]  # n3 未被拉取（截断安全阀）
    assert result.truncated_depth == 1  # n2 的子层不再展开
    requested_parents = [getattr(r, "parent_node_token", "") or "" for r in lister.requests]
    assert "n2" not in requested_parents


# ---------------------------------------------------------------------------
# docx blocks 直读（分页翻全 + 截断留痕）
# ---------------------------------------------------------------------------


class _BlockStub:
    def __init__(self, bid: str) -> None:
        self.block_id = bid
        self.block_type = 2


@pytest.mark.asyncio
async def test_list_document_blocks_full_pagination() -> None:
    page1 = [_BlockStub(f"b{i}") for i in range(DOCX_BLOCK_PAGE_SIZE)]
    page2 = [_BlockStub("z_last")]

    def list_blocks(request: Any) -> _Resp:
        if (getattr(request, "page_token", "") or "") == "":  # 真实 SDK request.page_token=None（非 ""）
            return _Resp(code=0, data=_Page(page1, page_token="p2", has_more=True))
        return _Resp(code=0, data=_Page(page2))

    lister = type("LB", (), {"list": staticmethod(list_blocks)})()
    sdk = _FakeLark(document_block=lister)
    result = await _client(sdk).list_document_blocks("docTok")
    assert [b.block_id for b in result.items] == [f"b{i}" for i in range(DOCX_BLOCK_PAGE_SIZE)] + ["z_last"]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_list_document_blocks_page_cap_truncated() -> None:
    def always_more(request: Any) -> _Resp:
        token = getattr(request, "page_token", "")
        return _Resp(code=0, data=_Page([_BlockStub(f"p{token or '0'}")], page_token=f"{token or '0'}x", has_more=True))

    lister = type("LB", (), {"list": staticmethod(always_more)})()
    sdk = _FakeLark(document_block=lister)
    result = await _client(sdk, max_pages=3).list_document_blocks("docTok")
    assert len(result.items) == 3
    assert result.truncated is True


@pytest.mark.asyncio
async def test_list_document_blocks_error_raises_redacted() -> None:
    def fail(request: Any) -> _Resp:
        return _Resp(code=230002, msg=f"deny secret={fc_get_secret()} token=docTok")

    lister = type("LB", (), {"list": staticmethod(fail)})()
    sdk = _FakeLark(document_block=lister)
    with pytest.raises(FeishuClientError) as exc:
        await _client(sdk).list_document_blocks("docTok")
    assert "230002" in str(exc.value)
    assert fc_get_secret() not in str(exc.value)


# ---------------------------------------------------------------------------
# 云文档素材下载（drive media.download）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_media_returns_bytes() -> None:
    class _Media:
        def __init__(self) -> None:
            self.tokens: list[str] = []

        def download(self, request: Any) -> _Resp:
            self.tokens.append(getattr(request, "file_token", ""))
            return _Resp(code=0, file=io.BytesIO(b"PDFBYTES"))

    media = _Media()
    sdk = _FakeLark(media=media)
    data = await _client(sdk).download_media("boxcnFileTok")
    assert data == b"PDFBYTES"
    assert media.tokens == ["boxcnFileTok"]


@pytest.mark.asyncio
async def test_download_media_error_redacted() -> None:
    class _Media:
        def download(self, request: Any) -> _Resp:
            return _Resp(code=230001, msg=f"no secret={fc_get_secret()}")

    sdk = _FakeLark(media=_Media())
    with pytest.raises(FeishuClientError) as exc:
        await _client(sdk).download_media("tok")
    assert fc_get_secret() not in str(exc.value)


@pytest.mark.asyncio
async def test_download_media_empty_file_stream_is_error() -> None:
    class _Media:
        def download(self, request: Any) -> _Resp:
            return _Resp(code=0, file=None)

    sdk = _FakeLark(media=_Media())
    with pytest.raises(FeishuClientError):
        await _client(sdk).download_media("tok")


# ---------------------------------------------------------------------------
# 云空间文件附件下载（drive file.download——wiki file 节点正解通道）
#
# 2026-11-25 三通道对照探针实证：media.download 对云空间文件恒 403 空体（官方：
# 仅云文档内素材）；file.download 200 且 resp.file 形态同构（BytesIO）。以下用例
# 锁死 file 通道的四类形态（成功/403 空体/超限封顶/空流）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_returns_bytes() -> None:
    class _Files:
        def __init__(self) -> None:
            self.tokens: list[str] = []

        def download(self, request: Any) -> _Resp:
            self.tokens.append(getattr(request, "file_token", ""))
            return _Resp(code=0, file=io.BytesIO(b"%PDF-1.7..."))

    files = _Files()
    sdk = _FakeLark(file=files)
    data = await _client(sdk).download_file("boxcnFileTok")
    assert data == b"%PDF-1.7..."
    assert files.tokens == ["boxcnFileTok"]


@pytest.mark.asyncio
async def test_download_file_403_empty_body_raises_client_error() -> None:
    """403 空体（探针实证形态：code=None msg=''）→ _call_with_backoff 归一
    FeishuClientError（下载失败分类面）；错误文本零凭据回显。"""

    class _Files:
        def download(self, request: Any) -> _Resp:
            return _Resp(code=None, msg="")

    sdk = _FakeLark(file=_Files())
    with pytest.raises(FeishuClientError) as exc:
        await _client(sdk).download_file("boxcnFileTok")
    assert "code=None" in str(exc.value)
    assert fc_get_secret() not in str(exc.value)


@pytest.mark.asyncio
async def test_download_file_cap_stops_early_without_full_read() -> None:
    payload = b"x" * (3 * 1024 * 1024)  # 3MB 流
    reads: list[int] = []
    closed: list[bool] = []

    class _Counting(io.BytesIO):
        def read(self, n: int = -1) -> bytes:  # type: ignore[override]
            chunk = super().read(n)
            reads.append(len(chunk))
            return chunk

        def close(self) -> None:
            closed.append(True)
            super().close()

    class _Files:
        def download(self, request: Any) -> _Resp:
            return _Resp(code=0, file=_Counting(payload))

    sdk = _FakeLark(file=_Files())
    client = _client(sdk)
    with pytest.raises(fc.FeishuAttachmentTooLargeError):
        await client.download_file("tok", max_bytes=1024 * 1024)  # 1MB 上限
    assert sum(reads) <= 2 * 1024 * 1024  # 提前终止：未读满 3MB 全量
    assert closed == [True]  # 超限路径必弃流（防 close 分支被静默删除）
    reads.clear()
    data = await client.download_file("tok", max_bytes=None)  # 不设上限：全量
    assert data == payload
    assert sum(reads) == len(payload)


@pytest.mark.asyncio
async def test_download_file_empty_stream_is_error() -> None:
    class _NoFile:
        def download(self, request: Any) -> _Resp:
            return _Resp(code=0, file=None)

    with pytest.raises(FeishuClientError):
        await _client(_FakeLark(file=_NoFile())).download_file("tok")

    class _EmptyBytes:
        def download(self, request: Any) -> _Resp:
            return _Resp(code=0, file=io.BytesIO(b""))

    with pytest.raises(FeishuClientError):
        await _client(_FakeLark(file=_EmptyBytes())).download_file("tok")


# ---------------------------------------------------------------------------
# 附件 / 图片素材入 MinIO（键面：附件沿 original.{ext} 白名单；素材为实现面）
# ---------------------------------------------------------------------------


class _FakeMinio:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str, bytes]] = []

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def put_object(self, bucket: str, key: str, stream: Any, length: int, content_type: str = "") -> None:
        self.puts.append((bucket, key, stream.read()))


@pytest.mark.asyncio
async def test_store_attachment_uses_original_key_and_whitelist() -> None:
    minio = _FakeMinio()
    key = await store_attachment(
        "00000000-0000-4000-8000-000000000001",
        "11111111-1111-4111-8111-111111111111",
        "年度报告.PDF",
        b"data",
        minio_client=minio,
    )
    assert key == "kb/00000000-0000-4000-8000-000000000001/11111111-1111-4111-8111-111111111111/original.pdf"
    assert minio.puts and minio.puts[0][1] == key and minio.puts[0][2] == b"data"


@pytest.mark.asyncio
async def test_store_attachment_rejects_non_whitelist_ext() -> None:
    with pytest.raises(ValueError):
        await store_attachment(
            "00000000-0000-4000-8000-000000000001",
            "11111111-1111-4111-8111-111111111111",
            "evil.exe",
            b"data",
            minio_client=_FakeMinio(),
        )


@pytest.mark.asyncio
async def test_store_image_material_key_layout() -> None:
    minio = _FakeMinio()
    key = await store_image_material(
        "00000000-0000-4000-8000-000000000001",
        "11111111-1111-4111-8111-111111111111",
        "blkpng01",
        "png",
        b"img",
        minio_client=minio,
    )
    assert key == "kb/00000000-0000-4000-8000-000000000001/11111111-1111-4111-8111-111111111111/img/blkpng01.png"


def test_image_material_object_key_rejects_bad_block_id() -> None:
    with pytest.raises(ValueError):
        image_material_object_key(
            "00000000-0000-4000-8000-000000000001",
            "11111111-1111-4111-8111-111111111111",
            "../escape",
            "png",
        )


def test_node_info_defaults() -> None:
    info = NodeInfo(node_token="t", obj_token="o", obj_type="docx", parent_node_token="", node_type="", has_child=False, title="x")
    assert info.title == "x"


# ---------------------------------------------------------------------------
# 评审修复（第 1 轮）：封顶读 / 脱敏全掩码 / 图片素材 ext 白名单
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_media_cap_stops_early_without_full_read() -> None:
    """P2 内存面：max_bytes 封顶读——超过上限即提前终止弃流（不全量入内存），
    归一 FeishuAttachmentTooLargeError（worker 转 attachment_unsupported 分类）；
    max_bytes=None 保持原全量读形态（图片素材通道零影响）。"""
    payload = b"x" * (3 * 1024 * 1024)  # 3MB 流
    reads: list[int] = []
    closed: list[bool] = []

    class _Counting(io.BytesIO):
        def read(self, n: int = -1) -> bytes:  # type: ignore[override]
            chunk = super().read(n)
            reads.append(len(chunk))
            return chunk

        def close(self) -> None:
            closed.append(True)
            super().close()

    class _Media:
        def download(self, request: Any) -> _Resp:
            return _Resp(code=0, file=_Counting(payload))

    sdk = _FakeLark(media=_Media())
    client = _client(sdk)
    with pytest.raises(fc.FeishuAttachmentTooLargeError):
        await client.download_media("tok", max_bytes=1024 * 1024)  # 1MB 上限
    assert sum(reads) <= 2 * 1024 * 1024  # 提前终止：未读满 3MB 全量
    assert closed == [True]  # 超限路径必弃流（防 close 分支被静默删除）
    reads.clear()
    data = await client.download_media("tok", max_bytes=None)  # 不设上限：原形态
    assert data == payload
    assert sum(reads) == len(payload)


@pytest.mark.asyncio
async def test_download_media_cap_within_limit_returns_full() -> None:
    class _Media:
        def download(self, request: Any) -> _Resp:
            return _Resp(code=0, file=io.BytesIO(b"small"))

    sdk = _FakeLark(media=_Media())
    data = await _client(sdk).download_media("tok", max_bytes=1024)
    assert data == b"small"


def test_redact_full_mask_and_token_pattern_scrub() -> None:
    """P3 脱敏面：密钥值全掩码（不保留前缀）+ 模式化 scrub（URL 查询段/文本中
    access_token=/token= 值）——tenant token 若泄漏进错误文本同样被掩。"""
    text = "GET https://open.feishu.cn/open-apis/auth/v3/tenant_access_token?token=SECRET123 failed, secret=SECRET123, access_token=u-abcdef"
    out = fc.redact(text, "SECRET123")
    assert "SECRET123" not in out  # 全掩码：无前 4 字符残留
    assert "u-abcdef" not in out  # 模式化 scrub：access_token= 值被掩
    assert "access_token=" in out  # 键名保留（可诊断）
    assert "token=" in out


def test_redact_empty_secrets_noop() -> None:
    assert fc.redact("plain text") == "plain text"
    assert fc.redact("plain text", "") == "plain text"


def test_image_material_object_key_ext_whitelist() -> None:
    """P3 加固（接线批前置）：图片素材 ext 白名单——形校验通过的 "exe" 不可过；
    白名单内大小写归一。"""
    uid = "00000000-0000-4000-8000-000000000001"
    did = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(ValueError):
        image_material_object_key(uid, did, "blk1", "exe")
    assert image_material_object_key(uid, did, "blk1", "PNG").endswith("blk1.png")
    with pytest.raises(ValueError):
        image_material_object_key(uid, did, "blk1", "")


# ---------------------------------------------------------------------------
# fetch_sheet_values（data-model §1.18 v1.20 30 行回填通道：v3 工作表定位 +
# v2 values 分页拉值矩阵 + 护栏截断 + 失败分类；假体零网络）
# ---------------------------------------------------------------------------


def test_sheet_backfill_constants_pin_contract_values() -> None:
    """回填护栏契约定值（代码常量面——§1.18 30 行钉注「实现批不得静默改」）：
    行 ≤500 / 列 ≤64 / 单元格 ≤200 字符；分页窗 100（实现面翻页驱动）。"""
    from app.rag.feishu_client import (
        SHEET_MAX_CELL_CHARS,
        SHEET_MAX_COLS,
        SHEET_MAX_ROWS,
        SHEET_VALUES_PAGE_ROWS,
    )

    assert SHEET_MAX_ROWS == 500
    assert SHEET_MAX_COLS == 64
    assert SHEET_MAX_CELL_CHARS == 200
    assert SHEET_VALUES_PAGE_ROWS == 100


@pytest.mark.parametrize(("num", "letters"), [(1, "A"), (26, "Z"), (27, "AA"), (64, "BL")])
def test_col_letter_bijective_base26(num: int, letters: str) -> None:
    """列字母换算（v2 values range 上界段——64 列护栏 → A..BL）。"""
    from app.rag.feishu_client import _col_letter

    assert _col_letter(num) == letters


def test_classify_sheet_error_categories() -> None:
    """失败分类（§1.18 30 行「回填失败 parse_meta 留痕」的分类原料）：
    99991672=scope 缺失 / 13102*=文档权限（官方排查文档两码族）→ permission；
    1310251=Path param :spreadsheet_token invalid（token 不可解析）→ resolve_failed
    （13102 族单列——曾误归 permission 带偏归因一轮，2026-11-25 活体排查钉正）；
    99991400=频控重试耗尽 → rate_limited；连接异常（无码）→ connection；余 → other。"""
    from app.rag.feishu_client import _classify_sheet_error

    assert _classify_sheet_error(None) == "connection"
    assert _classify_sheet_error(99991400) == "rate_limited"
    assert _classify_sheet_error(99991672) == "permission"
    assert _classify_sheet_error(13102) == "permission"
    assert _classify_sheet_error(1310234) == "permission"  # 13102 族前缀
    assert _classify_sheet_error(1310251) == "resolve_failed"  # 族内单列：token 不可解析≠权限
    assert _classify_sheet_error(99991661) == "other"
    assert _classify_sheet_error(23001) == "other"


def test_call_with_backoff_attaches_structured_error_code() -> None:
    """API 失败异常携带结构化 .code（fetch_sheet_values 失败分类的原料载体）：
    FeishuClientError.code 为构造器声明属性（第 2 轮 pyright 门禁修复——动态挂载
    被静态检查禁止，改为 code= 关键字参数）；连接异常路径 code=None。"""
    client = _client(_FakeLark())

    with pytest.raises(FeishuClientError) as exc:
        client._call_with_backoff(lambda: _Resp(code=99991661, msg="boom"))
    assert exc.value.code == 99991661  # 结构化错误码随异常传递（零消息格式变化）

    with pytest.raises(FeishuClientError) as exc2:
        client._call_with_backoff(
            lambda: (_ for _ in ()).throw(OSError("conn reset"))
        )
    assert exc2.value.code is None  # 连接异常无错误码 → 分类面归 connection


class _FakeSheetQuery:
    """sheets v3 spreadsheet_sheet.query 假体（工作表定位——record 请求供断言）。"""

    def __init__(self, sheets: list[Any] | None = None, error_code: int | None = None) -> None:
        self.sheets = sheets or []
        self.error_code = error_code
        self.requests: list[Any] = []

    def query(self, request: Any) -> _Resp:
        self.requests.append(request)
        if self.error_code is not None:
            return _Resp(code=self.error_code, msg=f"boom secret={fc_get_secret()}")
        body = type("D", (), {"sheets": self.sheets})()
        return _Resp(code=0, data=body)


class _FakeRawRequester:
    """Client.request 原始通道假体（v2 values——BaseRequest 捕获 + 信封 JSON 回放）。

    pages_by_start: {起始行: 该页 rows}；缺页 = 短页（自然耗尽触发面）。"""

    def __init__(
        self,
        pages_by_start: dict[int, list[list[Any]]] | None = None,
        *,
        error_code: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.requests: list[Any] = []
        self._pages = pages_by_start or {}
        self._error_code = error_code
        self._raise = raise_exc

    def request(self, req: Any) -> _Resp:
        self.requests.append(req)
        if self._raise is not None:
            raise self._raise
        if self._error_code is not None:
            return _Resp(code=self._error_code, msg=f"boom secret={fc_get_secret()}")
        range_str = str((getattr(req, "paths", None) or {}).get("range", ""))
        start = int(range_str.split("!A")[1].split(":")[0])
        rows = self._pages.get(start, [])
        body = {"code": 0, "msg": "success", "data": {"values": rows}}
        resp = _Resp(code=0, msg="success")
        resp.raw = type("Raw", (), {"content": json.dumps(body).encode("utf-8"), "headers": {}})()
        return resp


def _worksheet(sheet_id: str, *, hidden: bool = False) -> Any:
    return type("WS", (), {"sheet_id": sheet_id, "title": "工作表", "hidden": hidden})()


def _rows(count: int, width: int = 2, fill: str = "v") -> list[list[str]]:
    return [[f"{fill}{r}_{c}" for c in range(width)] for r in range(count)]


def _sheet_client(sheet_query: _FakeSheetQuery, raw: _FakeRawRequester) -> FeishuClient:
    sdk = _FakeLark(sheet=sheet_query, raw=raw)
    return _client(sdk)


@pytest.mark.asyncio
async def test_fetch_sheet_values_paginates_and_concatenates() -> None:
    """分页翻全：250 行 × 页窗 100 → 3 页（末页短页自然耗尽），矩阵按序拼接、
    零截断计数；range = {sheet_id}!A{start}:BM{end}（B 列窗=64 护栏列+1 证实列，
    评审 P3 死计数修复）、visible 工作表定位。"""
    from app.rag.feishu_client import SheetValuesData

    sq = _FakeSheetQuery([_worksheet("sheetHide", hidden=True), _worksheet("sheetABC")])
    raw = _FakeRawRequester({1: _rows(100, fill="a"), 101: _rows(100, fill="b"), 201: _rows(50, fill="c")})
    client = _sheet_client(sq, raw)

    data = await client.fetch_sheet_values("sprdTok123")

    assert isinstance(data, SheetValuesData)
    assert len(data.rows) == 250
    assert data.rows[0] == ["a0_0", "a0_1"]  # 页序拼接：第 1 页在首
    assert data.rows[100] == ["b0_0", "b0_1"]  # 第 2 页紧随
    assert data.rows[249] == ["c49_0", "c49_1"]  # 第 3 页（短页 50 行）收尾
    assert data.truncated_rows == 0 and data.truncated_cols == 0 and data.truncated_cells == 0
    assert len(raw.requests) == 3  # 三页翻全
    ranges = [str(req.paths["range"]) for req in raw.requests]
    assert ranges == ["sheetABC!A1:BM100", "sheetABC!A101:BM200", "sheetABC!A201:BM300"]
    assert sq.requests[0].spreadsheet_token == "sprdTok123"  # 工作表定位用 spreadsheet token


@pytest.mark.asyncio
async def test_fetch_sheet_values_row_overflow_proven_page_and_cap() -> None:
    """行护栏 500：600 行表 → cap 全取 + 一页溢出证实页（共 6 页）→ 物化前 500 行、
    truncated_rows=100；超过证实窗的行不再翻页（护栏预算——计数为证实窗内下界）。"""
    raw = _FakeRawRequester({i: _rows(100) for i in (1, 101, 201, 301, 401, 501)})
    client = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), raw)

    data = await client.fetch_sheet_values("tok")

    assert len(raw.requests) == 6  # 5 页 cap 全取 + 1 页溢出证实
    assert len(data.rows) == 500
    assert data.truncated_rows == 100


@pytest.mark.asyncio
async def test_fetch_sheet_values_exact_cap_no_truncation() -> None:
    """恰好 500 行：cap 全取 + 证实页空（短页）→ truncated_rows=0（零误报）。"""
    raw = _FakeRawRequester({i: _rows(100) for i in (1, 101, 201, 301, 401)})
    client = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), raw)

    data = await client.fetch_sheet_values("tok")

    assert len(raw.requests) == 6  # 证实页（A501）仍发起、返回空
    assert len(data.rows) == 500
    assert data.truncated_rows == 0


@pytest.mark.asyncio
async def test_fetch_sheet_values_col_and_cell_truncation_with_counts() -> None:
    """列护栏 64 + 单元格护栏 200 字符：宽 70 行 → 保留前 64 列、truncated_cols=6；
    超长格截断保留前 200 字符、truncated_cells 逐格计数；None/数值格防御归一。"""
    rows = [[f"c{r}_{c}" for c in range(70)] for r in range(2)]
    rows[0][63] = "x" * 300  # 超长格（保留列内——护栏外列不计格数）
    rows[1][2] = None  # None → 空串
    rows[1][3] = 42  # 数值 → str（ToString 兜底面）
    raw = _FakeRawRequester({1: rows})
    client = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), raw)

    data = await client.fetch_sheet_values("tok")

    assert data.truncated_cols == 6  # 70 − 64
    assert all(len(r) == 64 for r in data.rows)
    assert data.rows[0][63] == "x" * 200  # 截断保留前 N 字符
    assert data.truncated_cells == 1
    assert data.rows[1][2] == "" and data.rows[1][3] == "42"
    assert all(isinstance(cell, str) for r in data.rows for cell in r)


@pytest.mark.asyncio
async def test_fetch_sheet_values_col_confirmation_window_live_counter() -> None:
    """列护栏证实列（评审 P3：fetch 窗此前恒 A..BL=64 → width−64 恒 0 死计数）：
    窗拉到第 65 列（BM）作证实列——①第 65 列有实格 → truncated_cols 计数（契约
    「列 ≤64 超限截断不丢弃+计数留痕」兑现）；②第 65 列全空（API 补齐窗宽防御）
    → 零计数零误报；③≤64 列零计数；④请求 range 上界段=证实列。"""
    # ① 65 列且第 65 列有实格 → 计数 1、保留 64 列
    rows = [[f"c{r}_{c}" for c in range(65)] for r in range(2)]
    raw = _FakeRawRequester({1: rows})
    client = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), raw)
    data = await client.fetch_sheet_values("tok")
    assert data.truncated_cols == 1
    assert all(len(r) == 64 for r in data.rows)
    assert data.rows[0][63] == "c0_63"
    # ④ 请求 range 上界段=证实列（64=BL → 65=BM）
    assert str(raw.requests[0].paths["range"]).endswith("BM100")

    # ② 第 65 列全空 → 零计数（零误报）
    rows2 = [[f"c{r}_{c}" for c in range(64)] + [None] for r in range(2)]
    raw2 = _FakeRawRequester({1: rows2})
    client2 = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), raw2)
    data2 = await client2.fetch_sheet_values("tok")
    assert data2.truncated_cols == 0

    # ③ 64 列整 → 零计数
    rows3 = [[f"c{r}_{c}" for c in range(64)] for r in range(2)]
    raw3 = _FakeRawRequester({1: rows3})
    client3 = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), raw3)
    data3 = await client3.fetch_sheet_values("tok")
    assert data3.truncated_cols == 0


@pytest.mark.asyncio
async def test_fetch_sheet_values_failure_classification_and_redaction() -> None:
    """失败分类五支 + 连接异常：scope 缺失(99991672)/文档权限(13102 族) → permission；
    1310251（Path param :spreadsheet_token invalid，token 不可解析）→ resolve_failed
    （族内单列，避免误导归因）；频控重试耗尽 → rate_limited；空工作表 → not_found；
    余码 → other；连接异常 → connection；错误文本密钥零回显。"""
    from app.rag.feishu_client import FeishuSheetValuesError

    # permission（工作表定位面 scope 缺失）
    client = _sheet_client(_FakeSheetQuery(error_code=99991672), _FakeRawRequester())
    with pytest.raises(FeishuSheetValuesError) as exc:
        await client.fetch_sheet_values("tok")
    assert exc.value.category == "permission"
    assert fc_get_secret() not in str(exc.value)

    # permission（values 面 13102 族）
    client = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), _FakeRawRequester(error_code=1310234))
    with pytest.raises(FeishuSheetValuesError) as exc:
        await client.fetch_sheet_values("tok")
    assert exc.value.category == "permission"

    # resolve_failed：1310251 Path param :spreadsheet_token invalid（v3 定位端点对
    # 不可解析/不可访问的内嵌 spreadsheet 同报此码——非 permission，2026-11-25 活体钉正）
    client = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), _FakeRawRequester(error_code=1310251))
    with pytest.raises(FeishuSheetValuesError) as exc:
        await client.fetch_sheet_values("tok")
    assert exc.value.category == "resolve_failed"

    # not_found：电子表格零工作表
    client = _sheet_client(_FakeSheetQuery([]), _FakeRawRequester())
    with pytest.raises(FeishuSheetValuesError) as exc:
        await client.fetch_sheet_values("tok")
    assert exc.value.category == "not_found"

    # other：未归类错误码
    client = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), _FakeRawRequester(error_code=99991661))
    with pytest.raises(FeishuSheetValuesError) as exc:
        await client.fetch_sheet_values("tok")
    assert exc.value.category == "other"

    # rate_limited：99991400 重试耗尽（max_retries=1 压缩重试面；sleep 已注入零等待）
    client = _sheet_client(_FakeSheetQuery([_worksheet("s1")]), _FakeRawRequester(error_code=99991400))
    client.max_retries = 1
    with pytest.raises(FeishuSheetValuesError) as exc:
        await client.fetch_sheet_values("tok")
    assert exc.value.category == "rate_limited"

    # connection：连接异常（无错误码）
    client = _sheet_client(
        _FakeSheetQuery([_worksheet("s1")]), _FakeRawRequester(raise_exc=OSError("conn reset"))
    )
    with pytest.raises(FeishuSheetValuesError) as exc:
        await client.fetch_sheet_values("tok")
    assert exc.value.category == "connection"


def test_lark_guardrail_sheet_paths_exist() -> None:
    """锁版护栏扩面：sheets v3 工作表 query 调用路径在位 + v2 values 原始通道
    （BaseRequest/HttpMethod/AccessTokenType）在位——上游 API 面漂移先在护栏红。"""
    import lark_oapi as lark
    from lark_oapi.core.enum import AccessTokenType, HttpMethod
    from lark_oapi.core.model import BaseRequest

    c = lark.Client.builder().app_id("guard-probe-app").app_secret("guard-probe-secret").build()
    assert c.sheets.v3.spreadsheet_sheet.query is not None
    req = BaseRequest.builder().http_method(HttpMethod.GET).token_types({AccessTokenType.TENANT}).build()
    assert req.http_method == HttpMethod.GET
