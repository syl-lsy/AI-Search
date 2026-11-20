"""unit 层：A6 images 数据面（REQ-004 / api.md §7.2 v1.12 / OQ-6 / ARCH §4.1 images_raw）。

四处断点的红绿循环测试（批次 7 前置小修）：
- registry 透传（tools/registry._normalize_search_output）：SearXNG `img_src` 主通道
  （逐条附带、天然带相关性）；Tavily 顶层 `images` 数组次级（无逐条来源页绑定 →
  作 query_images 池挂载到首个候选，**不伪造逐条绑定**）；BoCha/AnySearch 文本形态
  无图字段（自然不产图，禁止硬造）；现有键 url/title/snippet/score/origin 一律不动；
- merge_dedupe 填充 images_raw（ARCH §4.1：候选图片投影，候选附带优先保归并序、
  query_images 池殿后）；描述符 = api.md §7.2 形态（url 恒在；source_url/title/
  width/height 键可省——v1.12 钉文字：缺失则省略该键、不置 null、不伪造）；
- signals.emit_images 信号形状（type=images + images 数组，TurnRunner 一对一翻译）；
- fuse_rerank 选图 + 发射（ARCH:413「按相关度取 3（OQ-6）」落地）：沿 fused 序取前
  ≤IMAGES_MAX_COUNT（=3 语义化代码常量）、URL 协议白名单 http/https、URL 去重、
  过滤后为空则不发射（轮内零 images 事件合法）、发射点 = emit_sources 之后
  （§7.4 快速序列 images(可选) 位于 sources 与 stage(write,running) 之间）。

payload 断言形态（api.md v1.12 裁决口径）：断 url 恒在 + 缺值键**不存在**
（`assert "width" not in item` 形态），不断言缺键的值。unit 全 mock 零网络。
"""

from typing import Any

import pytest

from app.agent.nodes import merge_dedupe as merge_module
from app.agent.nodes import fuse_rerank as fuse_module
from app.agent.graphs import signals as signals_module
from app.agent.tools.registry import _normalize_search_output, _search_args


def _cand(
    url: str,
    *,
    img_src: str | None = None,
    width: int | None = None,
    height: int | None = None,
    origin: list[str] | str = "tavily",
    score: float = 0.9,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "url": url,
        "title": f"t:{url}",
        "snippet": "s",
        "score": score,
        "origin": origin,
    }
    if img_src is not None:
        item["img_src"] = img_src
    if width is not None:
        item["img_width"] = width
    if height is not None:
        item["img_height"] = height
    return item


# ---- 断点 1：registry 出参归一透传（SearXNG img_src 主通道 / Tavily 池次级）----


def test_normalize_searxng_img_src_passthrough_keeps_existing_keys() -> None:
    """SearXNG 逐条 img_src 透传为候选键；现有键 url/title/snippet/score/origin 原样不动。"""
    raw = {
        "results": [
            {
                "url": "https://page.example/a",
                "title": "页面 A",
                "content": "正文",
                "score": 0.9,
                "img_src": "https://img.example/a.png",
            }
        ]
    }
    out = _normalize_search_output("searxng", raw)
    assert out[0]["img_src"] == "https://img.example/a.png"
    # 现有键零改动（增量透传，不重排不重命名）
    assert out[0]["url"] == "https://page.example/a"
    assert out[0]["title"] == "页面 A"
    assert out[0]["snippet"] == "正文"
    assert out[0]["score"] == 0.9
    assert out[0]["origin"] == "searxng"


def test_normalize_no_image_keys_when_upstream_absent() -> None:
    """上游无图字段 → 候选不携带任何图片键（键可省，不置 null 不造空键）。"""
    raw = {"results": [{"url": "https://a/1", "title": "A", "content": "c", "score": 0.5}]}
    out = _normalize_search_output("tavily", raw)
    assert "img_src" not in out[0]
    assert "query_images" not in out[0]


def test_normalize_drops_non_string_img_src() -> None:
    """img_src 非字符串（畸形上游）→ 丢弃该图片键，候选本体保留。"""
    raw = {"results": [{"url": "https://a/1", "title": "A", "img_src": 123}]}
    out = _normalize_search_output("searxng", raw)
    assert "img_src" not in out[0]
    assert out[0]["url"] == "https://a/1"


def test_normalize_img_dimensions_passthrough_only_when_numeric() -> None:
    """img_width/img_height 数值型才透传（字符串畸形丢弃）；键名保持上游原样。"""
    raw = {
        "results": [
            {
                "url": "https://a/1",
                "title": "A",
                "img_src": "https://i/1.png",
                "img_width": 800,
                "img_height": 600.0,
            },
            {"url": "https://a/2", "title": "B", "img_src": "https://i/2.png", "img_width": "wide"},
        ]
    }
    out = _normalize_search_output("searxng", raw)
    assert out[0]["img_width"] == 800 and out[0]["img_height"] == 600.0
    assert "img_width" not in out[1]


def test_normalize_tavily_top_level_images_pool_mounts_on_first_candidate() -> None:
    """Tavily 顶层 images 池（无逐条来源绑定）：str 条目与 {url, description} 对象条目
    归一后挂载到**首个候选**的 query_images 键；其余候选不携带。"""
    raw = {
        "results": [
            {"url": "https://a/1", "title": "A", "content": "c1", "score": 0.9},
            {"url": "https://a/2", "title": "B", "content": "c2", "score": 0.8},
        ],
        "images": [
            "https://i/1.png",
            {"url": "https://i/2.png", "description": "图二说明"},
            {"no_url": True},  # 无 url 条目丢弃
        ],
    }
    out = _normalize_search_output("tavily", raw)
    assert out[0]["query_images"] == [
        {"url": "https://i/1.png"},
        {"url": "https://i/2.png", "title": "图二说明"},
    ]
    assert "query_images" not in out[1]


def test_normalize_tavily_images_pool_dropped_when_zero_candidates() -> None:
    """全部条目缺 url 被过滤为零候选时，顶层 images 池无处挂载 → 随空集丢弃
    （池不独立成候选，禁止硬造）。"""
    raw = {"results": [{"title": "无 url 条目"}], "images": ["https://i/1.png"]}
    assert _normalize_search_output("tavily", raw) == []


def test_normalize_bocha_text_form_naturally_has_no_image_keys() -> None:
    """BoCha 文本形态无图字段 → 该源自然不产图（只允许出现归一键集，禁止硬造图片键）。"""
    text = "Title: 结果一\nURL: https://a/1\nDescription: 描述一"
    out = _normalize_search_output("bocha", text)
    assert out and set(out[0]) == {"url", "title", "snippet", "score", "origin"}


def test_search_args_tavily_requests_images() -> None:
    """Tavily 调用参数开启 include_images（顶层 images 次级通道的数据前提）；
    其余源参数面不含该键（searxng 扁平 q / anysearch 最小面不变）。"""
    assert _search_args("tavily", "q", None, 3) == {
        "query": "q",
        "max_results": 3,
        "include_images": True,
    }
    assert _search_args("tavily", "q", "day", 3) == {
        "query": "q",
        "max_results": 3,
        "time_range": "day",
        "include_images": True,
    }
    assert _search_args("searxng", "q", None, 3) == {"q": "q"}
    assert _search_args("anysearch", "q", None, 3) == {"query": "q"}


# ---- 断点 2：merge_dedupe 填充 images_raw（候选图片投影）----


def test_merge_dedupe_fills_images_raw_in_merged_order() -> None:
    """候选附带 img_src 投影进 images_raw（保归并序）；描述符含 url/source_url/title，
    width/height 上游携带才携带（键可省）。"""
    merged = merge_module.merge_candidates(
        [
            _cand("https://a/1", img_src="https://i/1.png", width=800, height=600),
            _cand("https://a/2"),  # 无图候选
            _cand("https://a/3", img_src="https://i/3.png"),
        ]
    )
    update = merge_module.merge_dedupe({"candidates": merged, "errors": []}, {})
    assert update["images_raw"] == [
        {
            "url": "https://i/1.png",
            "source_url": "https://a/1",
            "title": "t:https://a/1",
            "width": 800,
            "height": 600,
        },
        {
            "url": "https://i/3.png",
            "source_url": "https://a/3",
            "title": "t:https://a/3",
        },
    ]


def test_merge_dedupe_images_raw_appends_query_pool_after_candidate_images() -> None:
    """query_images 池（Tavily 次级）殿后：候选附带图（归并序）在前、池条目在后；
    池条目无 source_url 键（无逐条来源绑定——不伪造）。"""
    merged = merge_module.merge_candidates(
        [
            {**_cand("https://a/1", img_src="https://i/from-cand.png"),
             "query_images": [{"url": "https://i/pool-1.png", "title": "池图一"}]},
            {**_cand("https://a/2"), "query_images": [{"url": "https://i/pool-2.png"}]},
        ]
    )
    update = merge_module.merge_dedupe({"candidates": merged, "errors": []}, {})
    assert update["images_raw"] == [
        {
            "url": "https://i/from-cand.png",
            "source_url": "https://a/1",
            "title": "t:https://a/1",
        },
        {"url": "https://i/pool-1.png", "title": "池图一"},
        {"url": "https://i/pool-2.png"},
    ]
    assert all("source_url" not in item for item in update["images_raw"][-2:])


def test_merge_dedupe_images_raw_empty_when_no_images() -> None:
    """全候选无图 → images_raw 空列表（不失败语义；fuse_rerank 侧空则不发事件）。"""
    merged = merge_module.merge_candidates([_cand("https://a/1"), _cand("https://a/2")])
    update = merge_module.merge_dedupe({"candidates": merged, "errors": []}, {})
    assert update["images_raw"] == []


def test_merge_dedupe_images_raw_tolerates_garbage() -> None:
    """不失败语义：非 dict 候选 / 非 dict 池条目 / 缺 url 池条目静默跳过。"""
    merged = merge_module.merge_candidates(
        [_cand("https://a/1", img_src="https://i/1.png"), {**_cand("https://a/2"), "query_images": "junk"}]
    )
    merged.append("junk")  # 归并后的残留垃圾（防御面）
    update = merge_module.merge_dedupe({"candidates": merged, "errors": []}, {})
    assert update["images_raw"] == [
        {"url": "https://i/1.png", "source_url": "https://a/1", "title": "t:https://a/1"}
    ]


# ---- 断点 3：signals.emit_images 信号形状 ----


def test_emit_images_signal_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """emit_images → custom 流 dict：{"type": "images", "images": [...]}（TurnRunner
    _translate_custom elif ptype == "images" 一对一翻译的入参形态）。"""
    captured: list[dict[str, Any]] = []

    class _FakeWriter:
        def __call__(self, payload: dict[str, Any]) -> None:
            captured.append(payload)

    monkeypatch.setattr(signals_module, "get_stream_writer", lambda: _FakeWriter())
    items = [{"url": "https://i/1.png", "source_url": "https://a/1"}]
    signals_module.emit_images(items)
    assert captured == [{"type": "images", "images": items}]


# ---- 断点 4：fuse_rerank 选图 + 发射（OQ-6：沿 fused 序取前 ≤3）----


def test_images_max_count_is_three_semantic_constant() -> None:
    """≤3 上限为语义化命名代码常量（口径已裁决不进 Nacos）；值=3（OQ-6）。"""
    assert fuse_module.IMAGES_MAX_COUNT == 3


def test_select_images_takes_first_three_along_fused_order() -> None:
    """沿 fused 序取前 ≤3：5 个带图候选（fused 已按相关度定序）→ 恰取前 3、保序。"""
    fused = [_cand(f"https://a/{i}", img_src=f"https://i/{i}.png") for i in range(5)]
    selected = fuse_module.select_images(fused, [])
    assert [item["url"] for item in selected] == [
        "https://i/0.png",
        "https://i/1.png",
        "https://i/2.png",
    ]


def test_select_images_protocol_whitelist_http_https_only() -> None:
    """URL 协议白名单：http/https 保留；ftp/data/javascript/相对路径丢弃。"""
    fused = [
        _cand("https://a/1", img_src="https://i/ok.png"),
        _cand("https://a/2", img_src="ftp://i/nope.png"),
        _cand("https://a/3", img_src="data:image/png;base64,AAAA"),
        _cand("https://a/4", img_src="javascript:alert(1)"),
        _cand("https://a/5", img_src="//relative/nope.png"),
        _cand("https://a/6", img_src="http://i/ok-too.png"),
    ]
    selected = fuse_module.select_images(fused, [])
    assert [item["url"] for item in selected] == ["https://i/ok.png", "http://i/ok-too.png"]


def test_select_images_dedupes_by_url() -> None:
    """URL 去重：同图多候选引用只保留首见（沿序首见）。"""
    fused = [
        _cand("https://a/1", img_src="https://i/dup.png"),
        _cand("https://a/2", img_src="https://i/dup.png"),
        _cand("https://a/3", img_src="https://i/unique.png"),
    ]
    selected = fuse_module.select_images(fused, [])
    assert [item["url"] for item in selected] == ["https://i/dup.png", "https://i/unique.png"]


def test_select_images_skips_candidates_without_images_keeping_order() -> None:
    """无图候选跳过不打断序：出图候选仍按 fused 相对次序。"""
    fused = [
        _cand("https://a/1"),  # 无图
        _cand("https://a/2", img_src="https://i/first.png"),
        _cand("https://a/3"),  # 无图
        _cand("https://a/4", img_src="https://i/second.png"),
    ]
    selected = fuse_module.select_images(fused, [])
    assert [item["url"] for item in selected] == ["https://i/first.png", "https://i/second.png"]


def test_select_images_payload_url_always_present_other_keys_omissible() -> None:
    """payload 形状（api.md v1.12）：url 恒在；可省键上游缺则**键不存在**（断言缺键形态，
    不断言缺键的值）。候选附带路径 source_url/title 恒在（来源页=候选页）；width/height
    上游缺则省略；池条目（无逐条绑定）source_url 键省略由次级池用例覆盖。"""
    fused = [
        _cand("https://a/1", img_src="https://i/full.png", width=1200, height=750),
        _cand("https://a/2", img_src="https://i/bare.png"),
    ]
    full, bare = fuse_module.select_images(fused, [])
    assert full["url"] == "https://i/full.png"
    assert full["source_url"] == "https://a/1"
    assert full["title"] == "t:https://a/1"
    assert full["width"] == 1200 and full["height"] == 750
    assert bare["url"] == "https://i/bare.png"
    assert bare["source_url"] == "https://a/2"  # 候选附带图绑定真实来源页
    assert "width" not in bare  # 上游缺则省略（不置 null）
    assert "height" not in bare


def test_select_images_secondary_pool_fills_after_fused_candidates() -> None:
    """次级池殿后：fused 候选图取尽后不足 3 → images_raw 池按序补位；与主通道重复的
    URL 去重跳过；池条目（无逐条绑定）不伪造 source_url 键。"""
    fused = [_cand("https://a/1", img_src="https://i/main.png")]
    images_raw = [
        {"url": "https://i/main.png", "source_url": "https://a/1", "title": "t"},  # 与主通道重复
        {"url": "https://i/pool-extra.png", "title": "池补位"},
        {"url": "https://i/pool-nobind.png"},  # 无 source_url（不伪造）
        {"url": "ftp://i/bad.png"},  # 池条目同样过协议白名单
    ]
    selected = fuse_module.select_images(fused, images_raw)
    assert selected == [
        {
            "url": "https://i/main.png",
            "source_url": "https://a/1",
            "title": "t:https://a/1",  # 主通道塑形以候选字段为准（池内同 URL 条目被去重跳过）
        },
        {"url": "https://i/pool-extra.png", "title": "池补位"},
        {"url": "https://i/pool-nobind.png"},
    ]
    assert "source_url" not in selected[-1]


def test_select_images_pool_malformed_optional_keys_omitted() -> None:
    """池条目畸形可省键直喂 select_images（旁路 descriptor 塑形直进 _image_item）→
    不合规键省略（api.md v1.12「缺则省略、不置 null 不伪造」）：source_url 限 str、
    width/height 限 int/float 且非 bool——与 merge_dedupe.image_descriptor_from_candidate
    类型门口径对齐（评审 P3）；合法类型条目照常透传。"""
    images_raw = [
        {
            "url": "https://i/malformed.png",
            "source_url": 123,  # 非 str → 省略
            "width": "1200",  # 非 int/float → 省略
            "height": True,  # bool 冒充数值 → 省略
            "title": "畸形条目",
        },
        {
            "url": "https://i/ok-types.png",
            "source_url": "https://a/ok",
            "width": 1200,
            "height": 75.5,
        },
    ]
    malformed, ok = fuse_module.select_images([], images_raw)
    assert malformed == {"url": "https://i/malformed.png", "title": "畸形条目"}
    assert ok == {
        "url": "https://i/ok-types.png",
        "source_url": "https://a/ok",
        "width": 1200,
        "height": 75.5,
    }


def test_select_images_respects_limit_parameter() -> None:
    """limit 参数化（默认 IMAGES_MAX_COUNT；语义化入口便于 M4 评测复用）。"""
    fused = [_cand(f"https://a/{i}", img_src=f"https://i/{i}.png") for i in range(5)]
    assert len(fuse_module.select_images(fused, [], limit=2)) == 2


# ---- fuse_rerank 节点发射面：位置（sources 之后）+ 空不发 ----


def _capture_emits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """同时捕获 emit_sources / emit_images / emit_stage（保调用次序——§7.4 位置断言）。"""
    captured: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        fuse_module,
        "emit_sources",
        lambda batch, total: captured.append(("sources", {"batch": batch, "total": total})),
    )
    monkeypatch.setattr(
        fuse_module,
        "emit_images",
        lambda images: captured.append(("images", images)),
    )
    monkeypatch.setattr(
        fuse_module,
        "emit_stage",
        lambda key, status, **kw: captured.append(("stage", {"stage_key": key, "status": status, **kw})),
    )
    return captured


async def test_fuse_rerank_emits_images_right_after_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7.4 快速序列：images 发射紧邻 emit_sources 之后（topN 截断后同节点尾部）——
    TurnRunner 按发射次序翻译，sources → images → stage(write,running) 落位成立。"""
    # fixture URL 用点分域名（B15 起节点尾部对 images 条目过 url_guard——无点短主机名
    # 会被保留主机名禁令拒收；纯 select_images 直调用例不受影响，仍可用短形态）
    fused = [_cand(f"https://a.example/{i}", img_src=f"https://i.example/{i}.png") for i in range(5)]
    captured = _capture_emits(monkeypatch)

    update = await fuse_module.fuse_rerank(
        {"fused": fused, "query": "q", "spec": {"fast": {"fuse_top_n": 10}}}, None
    )

    kinds = [kind for kind, _ in captured]
    # 紧邻性：images 恰在 sources 之后一席（本节点常态路径零 stage——write(running) 归
    # synthesis；sources → images → write(running) 的完整位置由 integration stub 用例
    # 按 §7.4 断言）
    assert kinds.index("images") == kinds.index("sources") + 1
    (images_payload,) = [payload for kind, payload in captured if kind == "images"]
    assert [item["url"] for item in images_payload] == [
        "https://i.example/0.png",
        "https://i.example/1.png",
        "https://i.example/2.png",
    ]
    assert "images" not in update  # 选图发射不产 state 写入（images_raw 归 merge_dedupe）


async def test_fuse_rerank_no_images_emits_no_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """无图不发（api.md §7.2：无图不发本事件，不渲染空区块）：候选与池均无图 →
    emit_images 零调用、sources 照常。"""
    fused = [_cand(f"https://a/{i}") for i in range(3)]
    captured = _capture_emits(monkeypatch)

    await fuse_module.fuse_rerank(
        {"fused": fused, "images_raw": [], "query": "q", "spec": {"fast": {"fuse_top_n": 10}}},
        None,
    )

    assert [kind for kind, _ in captured] == ["sources"]


async def test_fuse_rerank_images_follow_post_rerank_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KB 命中 rerank 重排后：选图跟随重排后的 fused 序（§7.4「沿 fused 序」口径）。"""
    # fixture URL 用点分域名（B15 起节点尾部 images 条目过 url_guard——同 emits 测试注）
    fused = [
        _cand("https://low.example/1", img_src="https://i.example/low.png", origin=["kb", "tavily"]),
        _cand("https://kb.example/2", img_src="https://i.example/kb.png", origin=["kb"]),
        _cand("https://mid.example/3", img_src="https://i.example/mid.png", origin=["kb"]),
    ]
    seen: list[list[str]] = []

    async def _fake_rerank(query: str, documents: list[str], *, model_alias: str, timeout_s: float) -> list[float]:
        seen.append(list(documents))
        return [0.1, 0.95, 0.5]  # 重排 → kb/2, mid/3, low/1

    monkeypatch.setattr(fuse_module, "rerank_via_gateway", _fake_rerank)
    captured = _capture_emits(monkeypatch)

    await fuse_module.fuse_rerank(
        {
            "fused": fused,
            "images_raw": [
                {"url": "https://i.example/low.png", "source_url": "https://low.example/1", "title": "t:low"},
                {"url": "https://i.example/kb.png", "source_url": "https://kb.example/2", "title": "t:kb"},
                {"url": "https://i.example/mid.png", "source_url": "https://mid.example/3", "title": "t:mid"},
            ],
            "query": "用户原始问题",
            "spec": {"fast": {"fuse_top_n": 10}, "kb": {"rerank_timeout_s": 2}},
        },
        {"configurable": {"model_mapping": {"rerank": "qwen3-rerank"}}},
    )

    (images_payload,) = [payload for kind, payload in captured if kind == "images"]
    assert [item["url"] for item in images_payload] == [
        "https://i.example/kb.png",
        "https://i.example/mid.png",
        "https://i.example/low.png",
    ]


async def test_fuse_rerank_still_writes_only_faststate_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """images 发射不扩 state 写入面（images_raw 归 merge_dedupe 填充——断点 2 单一落点）。"""
    fused = [_cand("https://a.example/1", img_src="https://i.example/1.png")]
    _capture_emits(monkeypatch)

    update = await fuse_module.fuse_rerank(
        {"fused": fused, "images_raw": [{"url": "https://i.example/1.png"}], "query": "q",
         "spec": {"fast": {"fuse_top_n": 10}}},
        None,
    )

    from app.agent.state import FastState

    assert set(update) <= set(FastState.__annotations__)


# ---- B15 内容侧护栏：图片收口（images 事件出侧 url_guard 全判据）----


async def test_fuse_rerank_image_source_url_metadata_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """池条目 source_url 指向元数据端点 → 该图剔除（url 已校验面之外的新判据——
    此前只查主 url）；errors url_rejected 留痕；干净图照常发射。"""
    fused = [_cand("https://a.example/1", img_src="https://i.example/ok.png")]
    images_raw = [
        {"url": "https://i.example/bad.png", "source_url": "http://169.254.169.254/x", "title": "t:bad"},
    ]
    captured = _capture_emits(monkeypatch)

    update = await fuse_module.fuse_rerank(
        {"fused": fused, "images_raw": images_raw, "query": "q",
         "spec": {"fast": {"fuse_top_n": 10}}},
        None,
    )

    (images_payload,) = [payload for kind, payload in captured if kind == "images"]
    assert [item["url"] for item in images_payload] == ["https://i.example/ok.png"]
    assert update["errors"] == [{"stage": "search", "reason": "url_rejected", "count": 1}]
    assert "degraded" not in update  # 护栏动作不置 degraded（errors 路径零 degraded 键）


async def test_fuse_rerank_image_main_url_private_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 url 字面私网 → 剔除（既有协议白名单之外的 SSRF 判据）；全图被剔 → 零
    images 事件（合法）。"""
    fused = [
        _cand("https://a.example/1", img_src="http://10.0.0.5/x.png"),
        _cand("https://a.example/2", img_src="https://i.example/ok.png"),
    ]
    captured = _capture_emits(monkeypatch)

    await fuse_module.fuse_rerank(
        {"fused": fused, "images_raw": [], "query": "q",
         "spec": {"fast": {"fuse_top_n": 10}}},
        None,
    )

    (images_payload,) = [payload for kind, payload in captured if kind == "images"]
    assert [item["url"] for item in images_payload] == ["https://i.example/ok.png"]


# ---- 模块防呆：registry 导出面不被误改 ----

#: registry 公共导出集字面量快照（显式枚举现有导出；sorted 后相等断言——修正原
#: 「registry_all == registry_module.__all__」恒真断言：registry_module 即被测模块
#: 自身，两边引用同一列表对象、比较永真，锁不住导出面漂移，评审 P3）。
#: M2 批次 1（2026-11-16）：读取链面有意增量——READ_SOURCE_NAMES/READ_PRIMARY_SOURCES/
#: READ_BACKUP_CHAIN/READ_TOOL_NAMES/ReadChain/resolve_read_chain（firecrawl_read
#: 工具面配套，url_reader.py 消费）；搜索面既有导出零删减零改名。
EXPECTED_REGISTRY_ALL = sorted(
    [
        "BACKUP_SEARCH_CHAIN",
        "CircuitBreaker",
        "READ_BACKUP_CHAIN",
        "READ_PRIMARY_SOURCES",
        "READ_SOURCE_NAMES",
        "READ_TOOL_NAMES",
        "SEARCH_SOURCE_NAMES",
        "SOURCE_RETRY_DELAY_S",
        "ReadChain",
        "SearchChain",
        "SearchChainUnavailable",
        "SourceCallError",
        "ToolRegistry",
        "guarded_source_call",
        "resolve_read_chain",
        "resolve_search_chain",
    ]
)


def test_registry_module_exports_unchanged_surface() -> None:
    """registry 公共面字面量快照断言：导出面漂移（新增/删减/改名）即红——
    增量必须伴随本清单显式更新（M2 批次 1 读取链增量已随批登记）。"""
    from app.agent.tools.registry import __all__ as registry_all

    assert sorted(registry_all) == EXPECTED_REGISTRY_ALL


# ---- 评审修复第 1 轮：发现 7（registry 归一层 URL 协议白名单统一收口）----


def test_normalize_filters_non_http_url_schemes() -> None:
    """发现 7：归一层逐条过滤非常规 scheme（file/ftp/javascript/data 等）——fast/
    deep/supplement 三消费面一次覆盖（与 fuse_rerank._url_allowed 同判据：http/https
    +netloc）；白名单外条目丢弃不抛（缺 url 条目 continue 同款），其余候选保留。"""
    raw = {
        "results": [
            {"url": "https://ok.example/1", "title": "合法", "content": "c", "score": 0.9},
            {"url": "file:///etc/passwd", "title": "本地文件", "content": "c", "score": 0.8},
            {"url": "javascript:alert(1)", "title": "脚本", "content": "c", "score": 0.7},
            {"url": "ftp://files.example/x", "title": "FTP", "content": "c", "score": 0.6},
            {"url": "data:text/html;base64,PHNjcmlwdD4=", "title": "data", "content": "c", "score": 0.5},
            {"url": "https://ok.example/2", "title": "合法二", "content": "c", "score": 0.4},
        ]
    }
    out = _normalize_search_output("searxng", raw)
    urls = [item["url"] for item in out]
    assert urls == ["https://ok.example/1", "https://ok.example/2"]  # 白名单外全部过滤
    assert all(item["url"].startswith("https://") for item in out)


def test_normalize_filters_scheme_only_and_relative_urls() -> None:
    """无 netloc（相对路径/纯 scheme）同判据过滤——绝对 URL 才入册。"""
    raw = {
        "results": [
            {"url": "/relative/path", "title": "相对", "content": "c", "score": 0.9},
            {"url": "https:", "title": "纯 scheme", "content": "c", "score": 0.8},
            {"url": "HTTPS://UPPER.EXAMPLE/ok", "title": "大写 scheme", "content": "c", "score": 0.7},
        ]
    }
    out = _normalize_search_output("searxng", raw)
    assert [item["url"] for item in out] == ["HTTPS://UPPER.EXAMPLE/ok"]  # 大写 scheme 放行（判据 lower()）
