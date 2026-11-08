"""Nacos 客户端层：nacos-sdk-python 3.2.0（导入名 v2.nacos）封装——连接、监听、拉取。

R15 五坑口径（docs/s0-batch1-handoff.md 续 10，均在本文件对号落位）：
- ① API 面全换代：导入名 `v2.nacos`，方法全 asyncio 协程——本封装 async，与 TurnRunner
  的事件循环体系相容；
- ② 登录硬编码 v1 兼容端点 `/nacos/v1/auth/users/login`（v3.2.4 存活）——锁版本 + 源码级
  单测护栏（test_config_center_client.py 的 R15 guard 用例），升级前必跑；
- ③ 快照双坑：cache_dir 显式指向 NACOS_SNAPSHOT_DIR（SDK 空值默认落 ~/nacos/cache 家目录
  污染）、load_cache_at_start=False（SDK 默认 True 会让「先 get 后 listen」的初始回调
  永不触发）——start() 内先注册全部监听、拉值由组装层在其后进行；
- ④ 热更生效延迟 ≥5s（SDK 5s 周期轮询）——消费方按秒级而非毫秒级推送设计（§6）；
- ⑤ 安全坑：SDK INFO 级日志打印原始 accessToken（transport/auth_client.py:43）——
  client log_level 钉死 ERROR、log_dir 指向私有目录（快照目录同侧派生，data/ 已
  gitignore，不落家目录、不进版本库）。

解析口径（R15）：SDK gRPC 通道返回裸 JSON（无 HTTP 口径的 data.content 包装）——本封装
只做透明转发，不拆包；组装层按裸 JSON 解析，包装形态会被校验拒收（护栏见组装层单测）。

契约措辞映射：nacos-config.md §6 的「add_config_watcher 监听」在 SDK 3.2.0 的实装 API
为 `NacosConfigService.add_listener`（R15 坑① API 全换代），行为语义同一条。
"""
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from v2.nacos import ClientConfigBuilder, ConfigParam, NacosConfigService

from app.config_center.defaults import DATA_IDS
from app.core.config import NacosSettings

# SDK 通道日志级别钉死 ERROR（R15 坑⑤：INFO 会打印 accessToken，见模块 docstring）
SDK_LOG_LEVEL = logging.ERROR

# 监听/拉取回调：(dataId, 原始内容文本)
OnChangeCallback = Callable[[str, str], Awaitable[None]]


class NacosAssetClient:
    """五 dataId 配置客户端：start() 建连 + 注册监听；fetch() 单次拉取（组装层调度）。

    生命周期：start() → （组装层拉值与消费）→ close()；监听回调经构造注入的
    on_change 上抛，内容解析/校验/换版决策全部留在组装层（客户端不持有业务语义）。
    """

    def __init__(self, settings: NacosSettings, on_change: OnChangeCallback) -> None:
        self._settings = settings
        self._on_change = on_change
        self._service: NacosConfigService | None = None

    @property
    def snapshot_dir(self) -> Path:
        return Path(self._settings.snapshot_dir)

    def _sdk_log_dir(self) -> Path:
        """SDK 通道日志私有目录：快照目录同侧派生（data/ 已 gitignore），不落家目录。"""
        return self.snapshot_dir.parent / "nacos_sdk_logs"

    def _build_client_config(self):
        builder = ClientConfigBuilder()
        return (
            builder.server_address(self._settings.server_addr)
            .namespace_id(self._settings.namespace)
            .username(self._settings.username)
            # SecretStr 明文的唯一出口：拼 SDK 连接配置这一处（口径同 db/session.py）
            .password(self._settings.password.get_secret_value())
            .log_level(SDK_LOG_LEVEL)
            .log_dir(str(self._sdk_log_dir()))
            .cache_dir(str(self.snapshot_dir))
            .load_cache_at_start(False)
            .build()
        )

    async def start(self) -> None:
        """建连（gRPC，登录在连接握手内）并注册五 dataId 监听——**不拉值**。

        先监听后读值的顺序约束（R15 坑③）由本方法与组装层的调用顺序共同保证：
        start() 只注册监听，fetch() 必须在其后调用。
        """
        config = self._build_client_config()
        self._service = await NacosConfigService.create_config_service(config)
        for data_id in DATA_IDS:
            await self._service.add_listener(
                data_id, self._settings.group, self._make_listener(data_id)
            )

    def _make_listener(self, data_id: str):
        async def _listener(tenant: str, group: str, changed_data_id: str, content: str) -> None:
            # SDK 回调签名（R15 护栏钉住）：await listener(tenant, group, data_id, content)
            await self._on_change(changed_data_id, content)

        return _listener

    async def fetch(self, data_id: str) -> str:
        """单次拉取（gRPC 裸 JSON 原样返回，不拆包）；意外 dataId fail-fast。"""
        if data_id not in DATA_IDS:
            raise ValueError(f"未知 dataId：{data_id}（清单见 config_center.defaults.DATA_IDS）")
        if self._service is None:
            raise RuntimeError("NacosAssetClient 尚未 start()，禁止 fetch()")
        return await self._service.get_config(
            ConfigParam(data_id=data_id, group=self._settings.group)
        )

    async def close(self) -> None:
        """释放 gRPC 连接与监听任务（lifespan 关停时调用；幂等）。"""
        if self._service is not None:
            await self._service.shutdown()
            self._service = None
