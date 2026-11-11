"""integration：fuse_rerank 真 rerank 冒烟（requires_llm——LiteLLM 网关 /rerank 透传
qwen3-rerank，S0 落档 cohere 兼容通道直连百炼）。

口径：断结构（scores 与 documents 等长、数值型）不断言分值内容；网关不可达 skip。
"""

import httpx
import pytest

from app.agent.nodes.fuse_rerank import rerank_via_gateway

pytestmark = pytest.mark.requires_llm


def _gateway_ready() -> str | None:
    from app.core.config import litellm_settings

    try:
        resp = httpx.get(
            str(litellm_settings.base_url).rstrip("/") + "/v1/models",
            headers={
                "Authorization": "Bearer "
                + litellm_settings.api_key.get_secret_value()
            },
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 —— 探活失败统一 skip
        return f"LiteLLM 网关不可达（{type(exc).__name__}）"
    return None


async def test_rerank_real_structure() -> None:
    """真 rerank 冒烟：3 文档小样本 → scores 等长数值列表（结构断言）。"""
    ready = _gateway_ready()
    if ready:
        pytest.skip(ready)
    scores = await rerank_via_gateway(
        "个人知识库向量数据库怎么选",
        [
            "pgvector：PostgreSQL 的向量扩展，适合既有 PG 技术栈",
            "Milvus：独立分布式向量数据库，面向大规模场景",
            "今天天气不错，适合出门散步",
        ],
        model_alias="qwen3-rerank",
        timeout_s=10.0,
    )
    assert len(scores) == 3
    for score in scores:
        assert score is None or isinstance(score, (int, float))
