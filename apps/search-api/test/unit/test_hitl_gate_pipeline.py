"""unit 层：kb_gate 门内「单 query 检索到 Top K」管线 + 多信号确定性地板（B22 批次 2 第二段）。

契约锚点（见各用例 docstring）：
- ARCH §4.1:485 抽取位置钉注：逐子问题「双通道→RRF→rerank→Top K」可复用封装；
- A3 硬约束（批次 0 探针 §4）：RRF 空 → 跳过 rerank 直接判空（零 rerank 调用）；
- OQ-10：门内只 rerank 预览所需少量条目（preview_rerank_cap）；
- 多信号确定性地板（nacos-config §3 hitl.noise_floor）：向量相似度腿主防线 +
  rerank 分腿与组合；缺省（enabled=false+双阈值 0.0）行为等价「不设」；
  阈值 0.0 的腿不激活（沿 kb.route.min_top1_score 0=门条件关闭语义）；
- A2 病态 query（批次 0 探针 §4：纯标点 rerank 分 0.499 反高于真实 0.368）——
  向量腿为主防线，组合地板可地板「高 rerank 分但低向量相似度」的病态子问题；
- 失效方向纪律（plan §4.0/R38）：通道全败=channel_failed 回落现状、部分通道
  成功照常参与判定、rerank 失败=异常上抛（异常与空结果类型上分开，不落空列表）。

全部假体零网络：retriever 为内存假体、rerank_via_gateway 经 monkeypatch 注入。
"""

from typing import Any

import pytest

from app.agent.nodes import kb_gate
from app.agent.nodes.kb_gate import evaluate_noise_floor, retrieve_top_k_single_query

# ---- 假体 ----


def _row(
    chunk_id: str,
    *,
    channel: str,
    score: float,
    title: str,
    doc_id: str = "d1",
    rank: int | None = None,
) -> dict[str, Any]:
    """通道行（app/rag/retrieval.py 行投影形态；rank 由管道按序分配）。

    heading_path 前缀按 chunk 区分——rrf_fuse_kb 同章节归并（同 kb_doc_id+去叶端
    同前缀仅留最高分）会把同前缀兄弟块折叠，本套件需多 chunk 共存形态。
    """
    return {
        "chunk_id": chunk_id,
        "kb_doc_id": doc_id,
        "content": f"内容 {chunk_id}",
        "score": score,
        "heading_path": [f"文档{chunk_id}", title],
        "node_id": None,
        "channel": channel,
        "_rank": rank,
    }


class _FakeRetriever:
    """双通道假体：预设行集或异常，记录调用次数。"""

    def __init__(
        self,
        *,
        bm25_rows: list[dict[str, Any]] | None = None,
        vec_rows: list[dict[str, Any]] | None = None,
        bm25_exc: BaseException | None = None,
        vec_exc: BaseException | None = None,
    ) -> None:
        self.bm25_rows = bm25_rows or []
        self.vec_rows = vec_rows or []
        self.bm25_exc = bm25_exc
        self.vec_exc = vec_exc
        self.bm25_calls = 0
        self.vec_calls = 0

    async def bm25_search(self, query: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        self.bm25_calls += 1
        if self.bm25_exc is not None:
            raise self.bm25_exc
        return list(self.bm25_rows)

    async def vector_search(self, query_text: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        self.vec_calls += 1
        if self.vec_exc is not None:
            raise self.vec_exc
        return list(self.vec_rows)


class _FakeRerank:
    """rerank_via_gateway 假体：按 documents 长度回预设分数，记录调用面。"""

    def __init__(self, scores: list[float | None], *, exc: BaseException | None = None) -> None:
        self.scores = scores
        self.exc = exc
        self.calls: list[list[str]] = []

    async def __call__(self, query: str, documents: list[str], *, model_alias: str, timeout_s: float) -> list[float | None]:
        self.calls.append(list(documents))
        if self.exc is not None:
            raise self.exc
        assert len(documents) == len(self.scores), (
            f"rerank 文档数 {len(documents)} ≠ 预设分数 {len(self.scores)}"
        )
        return list(self.scores)


def _pipeline_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user_id": "u1",
        "query_index": 0,
        "es_on": True,
        "vec_on": True,
        "per_query_top_k": 30,
        "rrf_k": 60,
        "kb_fuse_top_k": 20,
        "type_weights": (0.5, 0.5),
        "dual_channel_timeout_s": 3.0,
        "rerank_alias": "qwen3-rerank",
        "rerank_timeout_s": 2.0,
        "preview_rerank_cap": 3,
        "noise_floor": None,
        "guards": None,
    }
    base.update(overrides)
    return base


def _dual_channel_retriever(vec_score: float = 0.83) -> _FakeRetriever:
    """bm25 两行（a rank1/b rank2）+ vector 两行（c rank1/a rank2）——RRF 融合序
    [a, c, b] 互异分数的确定性形态：a=0.5/61+0.5/62=0.01646 > c=0.5/61=0.00820
    > b=0.5/62=0.00806。"""
    return _FakeRetriever(
        bm25_rows=[
            _row("a", channel="bm25", score=12.0, title="检索融合"),
            _row("b", channel="bm25", score=9.0, title="代码块"),
        ],
        vec_rows=[
            _row("c", channel="vector", score=0.41, title="训练技巧"),
            _row("a", channel="vector", score=vec_score, title="检索融合"),
        ],
    )


# ---- 管线：双通道→RRF→rerank→Top K ----


async def test_pipeline_fuses_dual_channel_and_reranks_preview_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OQ-10+ARCH :485：RRF 融合序为 Top K 全序；rerank 仅 preview_rerank_cap
    条（预览所需少量）；preview 按 rerank 分降序并附 rerank_score；top1 向量
    相似度取自向量通道原始行。"""
    retriever = _dual_channel_retriever(vec_score=0.83)
    fake = _FakeRerank([0.2, 0.8, 0.5])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)

    result = await retrieve_top_k_single_query(
        retriever, "RRF 融合怎么做的？", **_pipeline_kwargs()
    )

    assert result.status == "ok"
    assert result.floored is False
    # RRF 序：a 双通道同命 rank1 融合分最高；c（vec rank1）> b（bm25 rank2）
    assert [entry["chunk_id"] for entry in result.entries] == ["a", "c", "b"]
    assert result.top_k_count == 3
    # rerank 恰一次、文档=preview 子集 title 形态（与图级 fuse_rerank :326-328 同构）
    assert len(fake.calls) == 1
    assert fake.calls[0] == ["检索融合", "训练技巧", "代码块"]
    # preview 按 rerank 分降序 + 分数附着
    assert [entry["chunk_id"] for entry in result.preview_entries] == ["c", "b", "a"]
    assert [entry["rerank_score"] for entry in result.preview_entries] == [0.8, 0.5, 0.2]
    assert result.top1_rerank == 0.8
    assert result.top1_vector_sim == 0.83
    assert result.filter_errors == []


async def test_rrf_empty_skips_rerank_direct_zero_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3 硬约束（批次 0 探针 §4：空 documents=网关 400）：双通道零召回 → RRF 空
    → 跳过 rerank 直接判空（零 rerank 调用），状态 ok、floored=False（真零命中
    路径，非异常回落）。"""
    retriever = _FakeRetriever(bm25_rows=[], vec_rows=[])
    fake = _FakeRerank([0.5])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)

    result = await retrieve_top_k_single_query(
        retriever, "冷门问题", **_pipeline_kwargs()
    )

    assert fake.calls == []  # A3：零 rerank 调用
    assert result.status == "ok"
    assert result.entries == []
    assert result.top_k_count == 0
    assert result.preview_entries == []
    assert result.floored is False
    assert result.top1_rerank is None


async def test_all_channels_failed_channel_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失效方向纪律（plan §4.0 原则 3）：启用通道全败=「不知道」≠「没有」——
    channel_failed 状态（调用方回落现状 hybrid，不判无），零 rerank 调用。"""
    retriever = _FakeRetriever(bm25_exc=ValueError("es down"), vec_exc=ValueError("pg down"))
    fake = _FakeRerank([0.5])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)

    result = await retrieve_top_k_single_query(
        retriever, "任意问题", **_pipeline_kwargs()
    )

    assert result.status == "channel_failed"
    assert result.entries == [] and result.top_k_count == 0
    assert fake.calls == []
    assert result.floored is False


async def test_partial_channel_failure_keeps_success_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分通道成功照常参与判定（plan §4.0 原则 3）：bm25 腿失败、向量腿成功 →
    ok + 向量单通道结果（不因失败腿判无/回落）。"""
    retriever = _FakeRetriever(
        bm25_exc=ValueError("es down"),
        vec_rows=[_row("a", channel="vector", score=0.7, title="检索融合")],
    )
    fake = _FakeRerank([0.6])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)

    result = await retrieve_top_k_single_query(
        retriever, "任意问题", **_pipeline_kwargs()
    )

    assert result.status == "ok"
    assert [entry["chunk_id"] for entry in result.entries] == ["a"]
    assert result.top_k_count == 1
    assert result.top1_vector_sim == 0.7
    assert result.floored is False


async def test_rerank_failure_propagates_not_judged_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R38：rerank 失败=异常上抛（调用方门节点捕获回落现状 hybrid）——异常与空
    结果在类型上分开，绝不落「空列表」分支被解释成零命中。"""
    retriever = _dual_channel_retriever()
    fake = _FakeRerank([], exc=RuntimeError("rerank 网关返回 500"))
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)

    with pytest.raises(RuntimeError, match="500"):
        await retrieve_top_k_single_query(
            retriever, "任意问题", **_pipeline_kwargs()
        )


# ---- 多信号确定性地板（hitl.noise_floor）----


async def test_noise_floor_missing_or_disabled_never_floors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺省回落=不判无（OQ-17 缺省态）：noise_floor=None（节缺失）与 enabled=False
    两种形态下，任何低分都不视同空——「Top K 有值就弹窗」现状口径。"""
    for floor in (None, {"enabled": False, "vector_sim_min": 0.9, "rerank_top1_min": 0.9}):
        retriever = _dual_channel_retriever(vec_score=0.31)
        fake = _FakeRerank([0.2, 0.2, 0.2])
        monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)
        result = await retrieve_top_k_single_query(
            retriever, "任意问题", **_pipeline_kwargs(noise_floor=floor)
        )
        assert result.floored is False, f"floor={floor!r} 不应地板"
        assert result.top_k_count == 3


async def test_floor_both_legs_empty_floors_subquery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """与组合（nacos §3 :358）：向量腿空 ∧ rerank 腿空 → 该子问题视同空——
    entries 清空、top_k_count=0、preview 清空（floored=True）。"""
    retriever = _FakeRetriever(
        bm25_rows=[_row("a", channel="bm25", score=5.0, title="噪声标题")],
        vec_rows=[_row("a", channel="vector", score=0.31, title="噪声标题")],
    )
    fake = _FakeRerank([0.40])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)
    floor = {"enabled": True, "vector_sim_min": 0.5, "rerank_top1_min": 0.45}

    result = await retrieve_top_k_single_query(
        retriever, "任意问题", **_pipeline_kwargs(noise_floor=floor)
    )

    assert result.floored is True
    assert result.entries == []
    assert result.top_k_count == 0
    assert result.preview_entries == []


async def test_floor_vector_leg_keeps_real_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """向量腿不空 → 不地板（与组合：rerank 腿单独不构成地板）——真相关弱 rerank
    分（0.368 档）不被砍（批次 0 探针 §3：单一 rerank 阈值 T≥0.55 砍真相关的教训）。"""
    retriever = _FakeRetriever(
        bm25_rows=[_row("a", channel="bm25", score=5.0, title="真相关")],
        vec_rows=[_row("a", channel="vector", score=0.66, title="真相关")],
    )
    fake = _FakeRerank([0.40])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)
    floor = {"enabled": True, "vector_sim_min": 0.5, "rerank_top1_min": 0.45}

    result = await retrieve_top_k_single_query(
        retriever, "任意问题", **_pipeline_kwargs(noise_floor=floor)
    )

    assert result.floored is False
    assert result.top_k_count == 1


async def test_floor_rerank_leg_keeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rerank 腿不空 → 不地板（向量腿单独不构成地板——与组合对称面）。"""
    retriever = _FakeRetriever(
        bm25_rows=[_row("a", channel="bm25", score=5.0, title="标题")],
        vec_rows=[_row("a", channel="vector", score=0.31, title="标题")],
    )
    fake = _FakeRerank([0.60])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)
    floor = {"enabled": True, "vector_sim_min": 0.5, "rerank_top1_min": 0.45}

    result = await retrieve_top_k_single_query(
        retriever, "任意问题", **_pipeline_kwargs(noise_floor=floor)
    )

    assert result.floored is False
    assert result.top_k_count == 1


async def test_pathological_query_floored_via_vector_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2（批次 0 探针 §4）：病态 query rerank 分反高于真实 query（0.499 vs
    0.368）——纯 rerank 阈值地板挡不住它；多信号组合下向量腿主防线地板病态
    （低向量相似度）、真实 query（高向量相似度）同 rerank 档不被砍。"""
    floor = {"enabled": True, "vector_sim_min": 0.5, "rerank_top1_min": 0.5}

    # 病态：rerank 0.499（高于真实档）但向量相似度 0.31 → floored
    pathological = _FakeRetriever(
        bm25_rows=[_row("a", channel="bm25", score=5.0, title="！！！？？？")],
        vec_rows=[_row("a", channel="vector", score=0.31, title="！！！？？？")],
    )
    fake_p = _FakeRerank([0.499])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake_p)
    result_p = await retrieve_top_k_single_query(
        pathological, "！！！？？？", **_pipeline_kwargs(noise_floor=floor)
    )
    assert result_p.floored is True

    # 真实：rerank 0.368（低于病态）但向量相似度 0.66 → 不地板
    real = _FakeRetriever(
        bm25_rows=[_row("a", channel="bm25", score=5.0, title="真相关")],
        vec_rows=[_row("a", channel="vector", score=0.66, title="真相关")],
    )
    fake_r = _FakeRerank([0.368])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake_r)
    result_r = await retrieve_top_k_single_query(
        real, "正常问题", **_pipeline_kwargs(noise_floor=floor)
    )
    assert result_r.floored is False
    assert result_r.top_k_count == 1


async def test_floor_vector_zero_rows_positive_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """向量通道启用但零召回=top1 None——向量腿判空（真实检索结果「什么都没有」，
    非管道缺口）；rerank 腿亦空 → floored。对照：rerank 未产分（None）腿不空
    （管道缺口≠证据，纯函数矩阵另行覆盖）。"""
    retriever = _FakeRetriever(
        bm25_rows=[_row("a", channel="bm25", score=5.0, title="标题")],
        vec_rows=[],
    )
    fake = _FakeRerank([0.2])
    monkeypatch.setattr(kb_gate, "rerank_via_gateway", fake)
    floor = {"enabled": True, "vector_sim_min": 0.5, "rerank_top1_min": 0.45}

    result = await retrieve_top_k_single_query(
        retriever, "任意问题", **_pipeline_kwargs(noise_floor=floor)
    )
    assert result.floored is True


# ---- 纯函数 evaluate_noise_floor（矩阵）----


@pytest.mark.parametrize(
    (
        "floor_enabled",
        "vector_sim_min",
        "rerank_top1_min",
        "vector_channel_enabled",
        "vector_top1_sim",
        "rerank_top1",
        "expected",
    ),
    [
        # 缺省态：enabled=false → 永不地板（行为等价「不设」）
        (False, 0.5, 0.45, True, 0.1, 0.1, False),
        # enabled=true 但双阈值 0.0 → 两腿均不激活 → 永不地板（行为等价「不设」）
        (True, 0.0, 0.0, True, 0.1, 0.1, False),
        # 单腿配置不构成地板（与组合要求两腿均激活且均判空）
        (True, 0.5, 0.0, True, 0.1, 0.9, False),
        (True, 0.0, 0.45, True, 0.1, 0.1, False),
        # 两腿激活且均判空 → 地板
        (True, 0.5, 0.45, True, 0.31, 0.40, True),
        # 向量腿不空（≥阈值）→ 不地板
        (True, 0.5, 0.45, True, 0.66, 0.40, False),
        # rerank 腿不空 → 不地板
        (True, 0.5, 0.45, True, 0.31, 0.60, False),
        # 向量通道关闭 → 向量腿不激活 → 不地板（不判无）
        (True, 0.5, 0.45, False, None, 0.40, False),
        # 向量零召回（top1=None）= 真实「什么都没有」→ 腿判空；rerank 亦空 → 地板
        (True, 0.5, 0.45, True, None, 0.20, True),
        # rerank 未产分（None）= 管道缺口≠证据 → 腿不空 → 不地板（不判无）
        (True, 0.5, 0.45, True, 0.31, None, False),
        # 恰等值不判空（低于阈值才空——边界）
        (True, 0.5, 0.45, True, 0.5, 0.45, False),
    ],
)
def test_evaluate_noise_floor_matrix(
    floor_enabled: bool,
    vector_sim_min: float,
    rerank_top1_min: float,
    vector_channel_enabled: bool,
    vector_top1_sim: float | None,
    rerank_top1: float | None,
    expected: bool,
) -> None:
    assert (
        evaluate_noise_floor(
            floor_enabled=floor_enabled,
            vector_sim_min=vector_sim_min,
            rerank_top1_min=rerank_top1_min,
            vector_channel_enabled=vector_channel_enabled,
            vector_top1_sim=vector_top1_sim,
            rerank_top1=rerank_top1,
        )
        is expected
    )
