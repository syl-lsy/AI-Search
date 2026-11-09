"""unit 层：models 路由（app/api/v1/models.py，api.md §3.1 GET /api/v1/models）。

覆盖点：
- chat 模型切换器数据：读 model.mapping AssetSnapshot 的 chat.options/default →
  options[{key,label}] + default；label 为**服务端常量**（api.md §3.1 v1.6：key→品牌
  显示名映射在后端一处定义，不进 Nacos；未收录键兜底显示 key 本身）；
- 资产不可用 → ConfigCenterException（503001）自然冒泡：路由层零捕获，经全局
  业务异常处理器出 503 {"code":"503001"}；
- get_model_mapping 依赖自身的转译语义（依赖直测）：资产中心未启动（RuntimeError
  fail-fast）/ 快照缺失（None）均转 503001——API 请求路径上按「配置面不可用」呈现。

外部依赖全替换（dependency_overrides 假 mapping / monkeypatch 快照出口）——零网络。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import dependencies
from app.exception import ConfigCenterException
from app.main import app

_MAPPING: dict[str, Any] = {
    "chat": {
        "options": {
            "qwen": "chat-standard",
            "glm": "chat-standard-glm",
            "deepseek": "chat-standard-deepseek",
        },
        "default": "qwen",
    }
}


@pytest.fixture
def overrides() -> dict[Any, Any]:
    yield app.dependency_overrides  # type: ignore[misc]
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---- GET /api/v1/models（§3.1）----


async def test_get_models_success(overrides: dict) -> None:
    app.dependency_overrides[dependencies.get_model_mapping] = lambda: _MAPPING
    async with _client() as client:
        response = await client.get("/api/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    # label 服务端常量（qwen→Qwen / glm→GLM / deepseek→DeepSeek，api.md §3.1 v1.6）
    assert body["data"] == {
        "options": [
            {"key": "qwen", "label": "Qwen"},
            {"key": "glm", "label": "GLM"},
            {"key": "deepseek", "label": "DeepSeek"},
        ],
        "default": "qwen",
    }


async def test_get_models_unmapped_key_falls_back_to_key(overrides: dict) -> None:
    """chat.options 热更新增键先于常量表维护时：label 兜底显示 key 本身（不拼品牌名）。"""
    app.dependency_overrides[dependencies.get_model_mapping] = lambda: {
        "chat": {"options": {"newkey": "chat-new"}, "default": "newkey"}
    }
    async with _client() as client:
        response = await client.get("/api/v1/models")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "options": [{"key": "newkey", "label": "newkey"}],
        "default": "newkey",
    }


async def test_get_models_asset_unavailable_503001(overrides: dict) -> None:
    """资产不可用：ConfigCenterException 自然冒泡 → 503 {"code":"503001"}（路由零捕获）。"""

    def _raise() -> Any:
        raise ConfigCenterException("配置中心不可达且无缓存")

    app.dependency_overrides[dependencies.get_model_mapping] = _raise
    async with _client() as client:
        response = await client.get("/api/v1/models")
    assert response.status_code == 503
    assert response.json()["code"] == "503001"


# ---- get_model_mapping 依赖直测（转译语义：RuntimeError/None → 503001）----


def test_get_model_mapping_translates_not_started_to_503001(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """资产中心未启动（get_asset_center fail-fast 的 RuntimeError）→ 503001：
    API 请求路径上按可预期运行态「配置面不可用」呈现，不落 500001。"""

    def _raise(data_id: str) -> Any:
        raise RuntimeError("配置中心尚未启动：请在 lifespan 启动钩子调用 start_asset_center()")

    monkeypatch.setattr(dependencies, "get_asset_snapshot", _raise)
    with pytest.raises(ConfigCenterException):
        dependencies.get_model_mapping()


def test_get_model_mapping_translates_missing_snapshot_to_503001(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动后 dataId 快照缺失（正常不发生——五 dataId 全量装内置默认）→ 503001。"""
    monkeypatch.setattr(dependencies, "get_asset_snapshot", lambda data_id: None)
    with pytest.raises(ConfigCenterException):
        dependencies.get_model_mapping()


def test_get_model_mapping_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常路径：透传快照 payload（threads 建轮模型解析与 /models 共用同源）。"""
    snapshot = SimpleNamespace(payload=_MAPPING)
    monkeypatch.setattr(dependencies, "get_asset_snapshot", lambda data_id: snapshot)
    assert dependencies.get_model_mapping() == _MAPPING
