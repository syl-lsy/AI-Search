"""unit 层：embedding 输入构造与网关客户端（app/rag/embedding.py，plan B19 §2.1 A1）。

契约锚点：
- plan-m3-b19 §3.2（heading_path 前置进 embedding 输入）：输入文本 =
  `标题轨迹：{" > ".join(heading_path)}\\n{content}`；轨迹串截断 ≤
  `EMBEDDING_TRAJECTORY_MAX_CHARS = 200` 字符**保叶端**（leaf 侧语义最具体；root
  侧文档题名由 doc 级承担——B18 plan §9① 原裁决护栏字面）；常量为代码常量（配置
  三分法第三行——纯逻辑防御值不入 Nacos，沿 IMAGES_MAX_COUNT 判例）；
- plan §3.3 / §5 必答项②：模型别名 = 轮内映射快照 `model.mapping.embedding`
  （缺省回落内置 text-embedding-v4——nacos-config §5 :379 已钉）；维度 =
  kb_settings.embedding_dim（1024，data-model §1.8 已钉）；响应维度≠1024 → 异常
  （防静默脏向量）；批窗切 ≤10（代码常量）；429/5xx/连接类重试恰 1 次；失败异常
  上抛且消息零 content 回显；
- 消费面单一事实源：pipeline `_run_chunking`（后续接线）与 A5 回填脚本与查询侧
  向量化统一走 `build_embedding_input`。

TDD 红绿：unit 全 mock 零网络（httpx MockTransport 先例——test_config_center_
client.py 同款；网关凭据为零意义假值）。
"""

from typing import Any

import httpx
import pytest

from app.config_center.defaults import MODEL_MAPPING_CONFIG
from app.rag import embedding as emb


def _mock_client(handler: Any) -> httpx.AsyncClient:
    """MockTransport 注入缝（embed_texts client 形参——unit 零网络）。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_response(request: httpx.Request, dim: int = 4) -> httpx.Response:
    """OpenAI /embeddings 成功形态：data[i].index 对位 input 序。"""
    body = request.read()
    import json

    payload = json.loads(body)
    inputs: list[str] = payload["input"]
    if not isinstance(inputs, list):
        inputs = [inputs]
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": i, "embedding": [0.1] * dim} for i in range(len(inputs))
            ]
        },
    )


# ---------------------------------------------------------------------------
# build_embedding_input：标题轨迹前置 + 200 字符保叶端护栏（plan §3.2）
# ---------------------------------------------------------------------------


class TestBuildEmbeddingInput:
    def test_prefix_shape_exact_bytes(self) -> None:
        """① 输入 = `标题轨迹：{" > ".join(heading_path)}\\n{content}` 逐字节。"""
        assert (
            emb.build_embedding_input("正文一段。", ["面试题库", "LLMs", "架构"])
            == "标题轨迹：面试题库 > LLMs > 架构\n正文一段。"
        )

    def test_trajectory_over_200_truncated_keep_leaf(self) -> None:
        """② 轨迹 >200 字符 → 截断保叶端（root 侧截去、leaf 侧完整保留）。"""
        leaf = "叶端章节名"
        root_padding = "根" * 210
        heading_path = [root_padding, "中段", leaf]
        trajectory = " > ".join(heading_path)
        assert len(trajectory) > emb.EMBEDDING_TRAJECTORY_MAX_CHARS
        result = emb.build_embedding_input("正文。", heading_path)
        expected_tail = trajectory[-emb.EMBEDDING_TRAJECTORY_MAX_CHARS:]
        assert result == f"标题轨迹：{expected_tail}\n正文。"
        # 保叶端：leaf 段完整在输入内；root 侧 padding 首段被截去
        assert result.startswith("标题轨迹：根")
        assert leaf in result
        assert len(result.split("\n", 1)[0]) == (
            len("标题轨迹：") + emb.EMBEDDING_TRAJECTORY_MAX_CHARS
        )

    def test_trajectory_exactly_200_unchanged(self) -> None:
        """护栏边界：轨迹恰 200 → 不截断（≤ 语义）。"""
        trajectory = "标" * 200
        result = emb.build_embedding_input("正文。", [trajectory])
        assert result == f"标题轨迹：{trajectory}\n正文。"

    def test_empty_heading_path_content_only(self) -> None:
        """③ 空 heading_path → 仅 content（零前缀——查询侧向量化的同款形态）。"""
        assert emb.build_embedding_input("只有正文。", []) == "只有正文。"

    def test_constant_pinned(self) -> None:
        """护栏常量 = 200（plan §3.2 钉字；防漂移）。"""
        assert emb.EMBEDDING_TRAJECTORY_MAX_CHARS == 200
        assert emb.EMBEDDING_BATCH_SIZE == 10


# ---------------------------------------------------------------------------
# resolve_embedding_alias：轮内映射快照 → 内置回落（plan §3.3 模型面）
# ---------------------------------------------------------------------------


def test_resolve_alias_builtin_fallback() -> None:
    """mapping 缺失/无 embedding 键 → 内置 text-embedding-v4（nacos §5 :379）。"""
    assert emb.resolve_embedding_alias(None) == "text-embedding-v4"
    assert emb.resolve_embedding_alias({}) == "text-embedding-v4"
    assert (
        emb.resolve_embedding_alias(MODEL_MAPPING_CONFIG) == "text-embedding-v4"
    )


def test_resolve_alias_snapshot_override() -> None:
    """轮内映射快照 embedding 键优先（OQ-11 同源——快照只读不回写）。"""
    assert emb.resolve_embedding_alias({"embedding": "my-embed-alias"}) == (
        "my-embed-alias"
    )


# ---------------------------------------------------------------------------
# embed_texts：网关客户端（批窗切 / 重试 / 维度护栏 / 失败上抛）
# ---------------------------------------------------------------------------


_DIM = 1024


async def test_embed_texts_request_shape() -> None:
    """① 请求形态：POST {base_url}/embeddings，body {model, input}，Bearer 头；
    出参按 data[i].index 对位还原输入序。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response(request, dim=_DIM)

    vectors = await emb.embed_texts(
        ["文本甲", "文本乙"],
        model="my-alias",
        client=_mock_client(handler),
    )
    assert len(vectors) == 2
    assert vectors[0] == [0.1] * _DIM
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path.endswith("/embeddings")
    import json

    payload = json.loads(request.read())
    assert payload == {"model": "my-alias", "input": ["文本甲", "文本乙"]}
    assert request.headers["Authorization"].startswith("Bearer ")


async def test_embed_texts_default_alias_from_builtin_mapping() -> None:
    """model 形参缺省 → resolve_embedding_alias 内置回落（text-embedding-v4）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.read())["model"] == "text-embedding-v4"
        return _ok_response(request, dim=_DIM)

    await emb.embed_texts(["文本甲"], mapping=None, client=_mock_client(handler))


async def test_embed_texts_batch_window() -> None:
    """② 批窗切：25 条 → 3 批（10/10/5）逐批 POST，向量按输入序拼接。"""
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        inputs = json.loads(request.read())["input"]
        sizes.append(len(inputs))
        return _ok_response(request, dim=_DIM)

    texts = [f"文本{i}" for i in range(25)]
    vectors = await emb.embed_texts(texts, client=_mock_client(handler))
    assert sizes == [10, 10, 5]
    assert len(vectors) == 25
    assert all(v == [0.1] * _DIM for v in vectors)


async def test_embed_texts_empty_input_no_request() -> None:
    """空输入 → 空 results、零请求（防御：不空转网关）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("空输入不应发起请求")

    assert await emb.embed_texts([], client=_mock_client(handler)) == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_embed_texts_retryable_status_retries_once(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """③ 429/5xx → 重试恰 1 次（共 2 次尝试）后成功。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(status)
        return _ok_response(request, dim=_DIM)

    monkeypatch.setattr(emb, "_retry_sleep", _async_noop)
    vectors = await emb.embed_texts(
        ["文本甲"], mapping=None, client=_mock_client(handler)
    )
    assert vectors == [[0.1] * _DIM]
    assert len(calls) == 2


async def test_embed_texts_transport_error_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接类（TransportError）→ 重试恰 1 次后成功（退避 sleep 缝零等待）。"""
    monkeypatch.setattr(emb, "_retry_sleep", _async_noop)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return _ok_response(request, dim=_DIM)

    vectors = await emb.embed_texts(
        ["文本甲"], mapping=None, client=_mock_client(handler)
    )
    assert vectors == [[0.1] * _DIM]
    assert len(calls) == 2


async def _async_noop(_: float) -> None:
    return None


@pytest.mark.parametrize("status", [429, 503])
async def test_embed_texts_retry_exhausted_raises(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """⑤ 失败耗尽（重试 1 次后仍 429/5xx）→ EmbeddingError 上抛；attempts=2；
    消息零 content 回显。"""
    monkeypatch.setattr(emb, "_retry_sleep", _async_noop)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(emb.EmbeddingError) as excinfo:
        await emb.embed_texts(["机密正文内容甲"], mapping=None, client=_mock_client(handler))
    message = str(excinfo.value)
    assert "attempts=2" in message
    assert "机密正文内容甲" not in message
    assert "文本" not in message


async def test_embed_texts_non_retryable_4xx_no_retry() -> None:
    """401/400 等不可重试 → 单次尝试即上抛（重试无收益）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401)

    with pytest.raises(emb.EmbeddingError):
        await emb.embed_texts(["文本甲"], mapping=None, client=_mock_client(handler))
    assert len(calls) == 1


async def test_embed_texts_dim_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """④ 响应维度≠1024 → EmbeddingError（防静默脏向量；确定性失败零重试）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(request, dim=512)

    with pytest.raises(emb.EmbeddingError) as excinfo:
        await emb.embed_texts(["文本甲"], mapping=None, client=_mock_client(handler))
    assert "512" in str(excinfo.value)
    assert str(emb.kb_settings.embedding_dim) in str(excinfo.value)


async def test_embed_texts_malformed_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 但出参形态异常（data 缺失/数量不符）→ EmbeddingError（零重试）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with pytest.raises(emb.EmbeddingError):
        await emb.embed_texts(["文本甲", "文本乙"], mapping=None, client=_mock_client(handler))
