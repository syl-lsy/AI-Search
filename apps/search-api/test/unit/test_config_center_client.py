"""unit 层：config_center/client.py——SDK 封装连接配置与监听（全 mock SDK，零网络）。

钉住的语义（docs/s0-batch1-handoff.md 续 10 R15 五坑）：
- ① 导入名 v2.nacos、全 asyncio 协程——本封装 async；
- ③ 快照双坑——显式 cache_dir（禁 SDK 默认 ~/nacos/cache 家目录污染）+
  load_cache_at_start=False + 先注册监听后读值（否则初始回调永不触发）；
- ⑤ 安全坑——SDK INFO 级日志打印原始 accessToken：client log_level 必须 ERROR、
  log_dir 指向私有目录（不进版本库、不落家目录）；
- R15 护栏三链路：登录（连接配置+SDK 源码护栏）、读配置（gRPC 裸 JSON 不拆包）、
  监听换版（回调签名转发）。
"""
import asyncio
import importlib.metadata
import inspect
import logging
from pathlib import Path

import pytest

from app.config_center import client as client_module
from app.config_center.client import NacosAssetClient
from app.config_center.defaults import DATA_IDS
from app.core.config import NacosSettings


def _settings(tmp_path: Path) -> NacosSettings:
    return NacosSettings(
        server_addr="127.0.0.1:8849",
        namespace="ai-search-dev",
        group="ai-search",
        username="ro-user",
        password="ro-pass",  # 测试夹具值，非真实凭据
        startup_timeout_s=5,
        snapshot_dir=str(tmp_path / "snapshot"),
    )


class FakeService:
    """SDK NacosConfigService 假体：记录调用序，不触网。"""

    last_config = None

    def __init__(self):
        self.listeners: list[tuple[str, str, object]] = []
        self.get_config_calls: list[str] = []
        self.raw_by_data_id: dict[str, str] = {}
        self.shutdown_called = False

    @staticmethod
    async def create_config_service(config):
        FakeService.last_config = config
        return FakeService()

    async def add_listener(self, data_id: str, group: str, listener) -> None:
        self.listeners.append((data_id, group, listener))

    async def get_config(self, param) -> str:
        self.get_config_calls.append(param.data_id)
        return self.raw_by_data_id.get(param.data_id, "")

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture
def fake_service(monkeypatch) -> FakeService:
    """替换 SDK 服务工厂：捕获 ClientConfig，返回记录型假体。"""
    service = FakeService()

    async def _factory(config):
        FakeService.last_config = config
        return service

    monkeypatch.setattr(client_module, "NacosConfigService", _FactoryHolder(_factory))
    return service


class _FactoryHolder:
    """容纳 async create_config_service 工厂的最小替身（按名字被 client 引用）。"""

    def __init__(self, factory):
        self.create_config_service = staticmethod(factory)


async def test_client_config_carries_r15_settings(fake_service, tmp_path):
    """连接配置必须带上 R15 五坑对应的全部口径（⑤日志级别/私有目录、③快照双坑、凭据）。"""
    settings = _settings(tmp_path)
    client = NacosAssetClient(settings, on_change=_noop_on_change)
    await client.start()

    config = FakeService.last_config
    assert config.server_list == ["127.0.0.1:8849"]
    assert config.namespace_id == "ai-search-dev"
    assert config.username == "ro-user"
    assert config.password == "ro-pass"  # SecretStr 只在拼连接配置这一个出口取明文
    assert config.log_level == logging.ERROR  # R15 坑⑤：INFO 会打印 accessToken
    assert config.load_cache_at_start is False  # R15 坑③：先监听后读值才有初始回调
    # R15 坑③：显式 cache_dir（SDK 空值默认落 ~/nacos/cache 家目录）
    assert Path(config.cache_dir.rstrip("/\\")) == (tmp_path / "snapshot")
    # log_dir 私有目录（SDK 空值默认 ~/logs/nacos 家目录）：派生自快照目录同侧，不进版本库语义由 data/ gitignore 保证
    assert Path(config.log_dir.rstrip("/\\")).is_relative_to(tmp_path)
    assert not Path(config.log_dir.rstrip("/\\")).is_relative_to(Path.home())


async def test_client_start_registers_five_listeners_without_get(fake_service, tmp_path):
    """start() 只注册监听（五个 dataId、group=ai-search），不拉值——读值由组装层做。"""
    settings = _settings(tmp_path)
    client = NacosAssetClient(settings, on_change=_noop_on_change)
    await client.start()

    assert [d for d, _, _ in fake_service.listeners] == list(DATA_IDS)
    assert all(g == "ai-search" for _, g, _ in fake_service.listeners)
    assert fake_service.get_config_calls == []


async def test_listener_callback_forwards_to_on_change(fake_service, tmp_path):
    """监听回调签名 (tenant, group, data_id, content) → on_change(data_id, content)。"""
    received: list[tuple[str, str]] = []

    async def on_change(data_id: str, raw: str) -> None:
        received.append((data_id, raw))

    settings = _settings(tmp_path)
    client = NacosAssetClient(settings, on_change=on_change)
    await client.start()

    _, _, listener = fake_service.listeners[0]
    await listener("ns", "ai-search", "ai_search.agent.spec.json", '{"schema_version": 1}')
    assert received == [("ai_search.agent.spec.json", '{"schema_version": 1}')]


async def test_fetch_returns_raw_content_unwrapped(fake_service, tmp_path):
    """读配置链路：gRPC 裸 JSON 口径——原样返回，不做 HTTP 的 data.content 拆包（R15）。"""
    raw = '{"schema_version": 1, "updated_at": "2026-11-01T12:00:00Z"}'
    fake_service.raw_by_data_id["ai_search.agent.spec.json"] = raw
    settings = _settings(tmp_path)
    client = NacosAssetClient(settings, on_change=_noop_on_change)
    await client.start()
    assert await client.fetch("ai_search.agent.spec.json") == raw


async def test_fetch_unknown_data_id_rejected(fake_service, tmp_path):
    settings = _settings(tmp_path)
    client = NacosAssetClient(settings, on_change=_noop_on_change)
    await client.start()
    with pytest.raises(ValueError):
        await client.fetch("ai_search.agent.unknown.json")


async def test_client_close_shuts_down_service(fake_service, tmp_path):
    settings = _settings(tmp_path)
    client = NacosAssetClient(settings, on_change=_noop_on_change)
    await client.start()
    await client.close()
    assert fake_service.shutdown_called


async def test_client_start_failure_raises(monkeypatch, tmp_path):
    """建连失败（登录/网络）原样上抛——降级决策在组装层，不在客户端。"""

    async def _boom(config):
        raise RuntimeError("login failed")

    monkeypatch.setattr(client_module, "NacosConfigService", _FactoryHolder(_boom))
    client = NacosAssetClient(_settings(tmp_path), on_change=_noop_on_change)
    with pytest.raises(RuntimeError):
        await client.start()


async def _noop_on_change(data_id: str, raw: str) -> None:
    return None


# ---- R15 SDK 护栏（锁版本 + 源码级断言，R8 同口径；零网络）----


def test_r15_guard_sdk_version_locked():
    """锁版本护栏：SDK 版本被升级行 → 此处红（升级前必须先跑护栏，R8 口径）。"""
    assert importlib.metadata.version("nacos-sdk-python") == "3.2.0"


def test_r15_guard_login_v1_compat_endpoint_still_exists():
    """登录链路护栏：SDK 登录硬编码 v1 兼容端点 /nacos/v1/auth/users/login（R15 坑②）——
    上游未来收掉 v1 auth 即断，源码级断言让升级在单测层显式红掉。"""
    import v2.nacos.transport.auth_client as auth_client

    source = inspect.getsource(auth_client)
    assert "/v1/auth/users/login" in source


def test_r15_guard_config_service_api_surface():
    """读配置/监听链路护栏：SDK 3.2.0 的方法面（全 asyncio）必须仍在。"""
    from v2.nacos import NacosConfigService

    for method in ("add_listener", "get_config", "server_health", "shutdown"):
        assert callable(getattr(NacosConfigService, method)), f"缺少 {method}"


def test_r15_guard_listener_invocation_signature():
    """监听换版护栏：SDK 以 await listener(tenant, group, data_id, content) 四参回调——
    本封装的监听闭包签名依赖该形态（源码级钉住）。"""
    import v2.nacos.config.model.config as config_model

    source = inspect.getsource(config_model.SubscribeCacheData.execute_listener)
    assert "listener(self.tenant, self.group, self.data_id, decrypted_content)" in source


def test_r15_guard_client_config_defaults():
    """SDK 默认值护栏：cache_dir 空值默认家目录、load_cache_at_start 默认 True——
    两个默认值是 R15 坑③的根源，若上游改默认（比如改安全）本护栏提示复核封装参数。"""
    from v2.nacos import ClientConfig

    config = ClientConfig(server_addresses="127.0.0.1:8849")
    assert config.load_cache_at_start is True
    assert config.cache_dir == ""
