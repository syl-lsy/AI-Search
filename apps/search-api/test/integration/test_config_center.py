"""integration 层：config_center 对真 Nacos（v3.2.4，gRPC 三链路——R15 实证口径）。

凭证纪律：连接信息只取仓库根 .env.test 的 NACOS_ 组（dotenv_values 直读，不经 .env
回落）；凭据值只在内存流转，绝不打印、绝不进断言消息；缺凭证/服务不可达时 skip
（给 reason，不让命令红掉）。

变更推送换版（需发布权限）：管理员凭据走 §8 惯例的 NACOS_INIT_USER/NACOS_INIT_PASS
（临时环境变量形态，不入 .env.test）——拿不到即 skip 并在 deviations 说明。发布对象用
**探测 dataId**（ai_search.test.watch_probe.json），**绝不写五个契约 dataId**（防污染
开发环境配置），监听链路语义与五 dataId 完全一致。

日志断言：全程不出现 accessToken（R15 坑⑤——SDK log_level=ERROR + 私有日志目录）。
"""
import asyncio
import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values
from loguru import logger

from app.config_center import AssetCenter, DegradationLevel
from app.config_center.client import NacosAssetClient
from app.config_center.defaults import DATA_IDS
from app.core.config import NacosSettings

from v2.nacos import ClientConfigBuilder, ConfigParam, NacosConfigService

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_TEST_PATH = REPO_ROOT / ".env.test"

# 探测 dataId：监听换版链路专用（五契约 dataId 在本测试模块中只读不写）
PROBE_DATA_ID = "ai_search.test.watch_probe.json"
PROBE_GROUP = "ai-search"

SDK_CALLBACK_DEADLINE_S = 30  # SDK 监听为 5s 周期轮询（R15 坑④），首次回调实测 ~5.1s


def _messages(records: list) -> list[str]:
    return [r["message"] for r in records]


@pytest.fixture
def log_records() -> Iterator[list]:
    """内存捕获 sink（与 unit 层 conftest 同款；integration conftest 未内置故就地定义）。"""
    records: list = []
    sink_id = logger.add(
        lambda message: records.append(message.record), format="{message}", level=0
    )
    yield records
    try:
        logger.remove(sink_id)
    except ValueError:  # setup_logging 已 logger.remove() 全清（sink 不存在属预期）
        pass


@pytest.fixture(scope="module")
def nacos_env() -> dict[str, str]:
    """NACOS_ 组凭证：只认 .env.test；缺任一关键变量 → 本模块全部用例 skip。"""
    if not ENV_TEST_PATH.exists():
        pytest.skip("缺 .env.test（cp .env.test.example .env.test 并填 NACOS_ 凭证）——requires_nacos 用例跳过")
    values = dotenv_values(ENV_TEST_PATH)
    required = ("NACOS_SERVER_ADDR", "NACOS_NAMESPACE", "NACOS_GROUP", "NACOS_USERNAME", "NACOS_PASSWORD")
    missing = [key for key in required if not values.get(key)]
    if missing:
        pytest.skip(f".env.test 缺 {', '.join(missing)}——requires_nacos 用例跳过")
    return {key: values[key] for key in required}


@pytest.fixture
def nacos_settings(nacos_env, tmp_path) -> NacosSettings:
    """真 Nacos 连接配置：快照/SDK 日志隔离进 tmp/（gitignore），凭据经 SecretStr 内存流转。"""
    return NacosSettings(
        server_addr=nacos_env["NACOS_SERVER_ADDR"],
        namespace=nacos_env["NACOS_NAMESPACE"],
        group=nacos_env["NACOS_GROUP"],
        username=nacos_env["NACOS_USERNAME"],
        password=nacos_env["NACOS_PASSWORD"],  # SecretStr 字段直接收明文，repr 全打码
        startup_timeout_s=15,  # 真服务给宽于生产 5s 的预算（首连含登录）
        snapshot_dir=str(tmp_path / "nacos_snapshot"),
    )


def tmp_dir(settings: NacosSettings) -> Path:
    return Path(settings.snapshot_dir)


@pytest.mark.requires_nacos
class TestInitialPull:
    async def test_five_data_ids_pull_l1(self, nacos_settings, log_records, tmp_path):
        """五 dataId 初始拉取成功：全 L1、schema_version=1、磁盘快照落盘。

        **环境偏斜豁免（v1.20 一次性）**：nacos-config.md §3 v1.20 起 validator 拒收
        旧 schema prompts（`fast.rewrite` 须含 `"search_strategy"`——防热更漂移）。
        现网 Nacos prompts 若仍是 v1.18 未发布 v1.20，本 dataId 必落 L4——此处按
        「校验失败留痕」精确识别并 skip（发布 v1.20 后自动恢复全 L1 断言）；
        连接/权限/超时类失败不命中文案，照常红。
        """
        center = AssetCenter(settings=nacos_settings)
        try:
            await center.start()
            for data_id in DATA_IDS:
                if center.level(data_id) is not DegradationLevel.L1:
                    rejected = [
                        message
                        for message in _messages(log_records)
                        if "配置校验失败" in message and data_id in message
                    ]
                    if rejected:
                        pytest.skip(
                            "现网配置未发布 v1.20（validator 拒收旧 schema，落 L4）："
                            f"{rejected[-1][:160]}——发布后本断言自动恢复"
                        )
                assert center.level(data_id) is DegradationLevel.L1, data_id
                snapshot = center.snapshot(data_id)
                assert snapshot.payload["schema_version"] == 1, data_id
                disk = tmp_dir(nacos_settings) / data_id
                assert disk.is_file(), f"磁盘快照缺失：{data_id}"
            assert all("accessToken" not in m for m in _messages(log_records))
        finally:
            await center.close()

    async def test_sdk_log_files_leak_no_token(self, nacos_settings):
        """R15 坑⑤护栏：SDK 私有日志目录的落盘文件不出现 AccessToken（INFO 级日志已被压掉）。"""
        center = AssetCenter(settings=nacos_settings)
        try:
            await center.start()
        finally:
            await center.close()
        sdk_log_dir = tmp_dir(nacos_settings).parent / "nacos_sdk_logs"
        leak = [
            f.name
            for f in sdk_log_dir.glob("*.log*")
            if "AccessToken" in f.read_text(encoding="utf-8", errors="ignore")
        ]
        assert leak == []


@pytest.mark.requires_nacos
class TestListenerChain:
    async def test_listener_initial_callback(self, nacos_settings):
        """监听注册（先注册后读值口径）触发初始回调：五 dataId 各收到一次实时内容。"""
        received: dict[str, str] = {}

        async def on_change(data_id: str, raw: str) -> None:
            received[data_id] = raw

        client = NacosAssetClient(nacos_settings, on_change=on_change)
        await client.start()
        try:
            deadline = time.monotonic() + SDK_CALLBACK_DEADLINE_S
            while len(received) < len(DATA_IDS) and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
            for data_id in DATA_IDS:
                assert data_id in received, f"初始回调未到达：{data_id}"
                payload = json.loads(received[data_id])
                assert payload["schema_version"] == 1, data_id
        finally:
            await client.close()


@pytest.mark.requires_nacos
class TestVersionChangePush:
    async def test_change_push_delivers_new_version(self, nacos_settings):
        """变更推送换版（需发布权限）：管理端发布探测 dataId → 只读端监听回调收到新内容。

        只读端用**裸 NacosConfigService** 对探测 dataId 做真实订阅：NacosAssetClient.start()
        只对五契约 DATA_IDS add_listener（client.py:81-84 生产口径），探测 dataId 从未被
        订阅——复用封装层会让 admin publish 后回调永不到达（2026-11-08 真凭据首跑即
        TimeoutError 暴露）。裸 SDK 服务按与 NacosAssetClient._build_client_config 同口径
        组装连接（server_addr/namespace/只读账号/log_level=ERROR/私有日志目录/快照
        cache_dir/load_cache_at_start=False），等价复现「变更推送 → 回调」链路——R15 实证
        点关注的是服务端推送语义，而非封装层五 dataId 装配。

        发布对象为探测 dataId（非五契约 dataId，防污染开发配置）；无管理员凭据时 skip
        （§8 双账号口径：运行态只读账号无写权限，本用例凭据由执行者临时注入环境变量）。
        """
        admin_user = os.environ.get("NACOS_INIT_USER", "")
        admin_pass = os.environ.get("NACOS_INIT_PASS", "")
        if not admin_user or not admin_pass:
            pytest.skip(
                "缺管理员凭据（NACOS_INIT_USER/NACOS_INIT_PASS 环境变量，§8 临时注入惯例）"
                "——变更推送换版用例跳过，五 dataId 内容不受影响"
            )

        sdk_log_dir = tmp_dir(nacos_settings).parent / "nacos_sdk_logs"
        received: list[str] = []
        got_event = asyncio.Event()

        async def probe_listener(
            tenant: str, group: str, data_id: str, content: str
        ) -> None:
            # SDK 回调签名（R15 护栏钉住）：await listener(tenant, group, data_id, content)
            if data_id == PROBE_DATA_ID:
                received.append(content)
                got_event.set()

        # 只读端：裸 SDK 服务对探测 dataId 真实订阅（封装层只订阅五契约 dataId，见 docstring）
        readonly_config = (
            ClientConfigBuilder()
            .server_address(nacos_settings.server_addr)
            .namespace_id(nacos_settings.namespace)
            .username(nacos_settings.username)
            # SecretStr 明文的唯一出口：拼 SDK 连接配置这一处（口径同 client.py）
            .password(nacos_settings.password.get_secret_value())
            .log_level(logging.ERROR)
            .log_dir(str(sdk_log_dir))
            .cache_dir(str(tmp_dir(nacos_settings)))
            .load_cache_at_start(False)
            .build()
        )
        readonly_service = await NacosConfigService.create_config_service(readonly_config)

        # 管理端服务（发布权限；凭据与只读端同源隔离，仅本用例存活期内存在）
        admin_config = (
            ClientConfigBuilder()
            .server_address(nacos_settings.server_addr)
            .namespace_id(nacos_settings.namespace)
            .username(admin_user)
            .password(admin_pass)
            .log_level(logging.ERROR)
            .log_dir(str(sdk_log_dir))
            .cache_dir(str(tmp_dir(nacos_settings).parent / "nacos_snapshot_admin"))
            .load_cache_at_start(False)
            .build()
        )
        admin_service: NacosConfigService | None = None

        probe_content = json.dumps(
            {"schema_version": 1, "updated_at": "2026-11-07T00:00:00Z", "probe": True},
            ensure_ascii=False,
        )
        try:
            admin_service = await NacosConfigService.create_config_service(admin_config)
            # 先注册监听再发布（R15 坑③ 同源约束：后注册会漏掉本次变更窗口）
            await readonly_service.add_listener(
                PROBE_DATA_ID, PROBE_GROUP, probe_listener
            )
            published = await admin_service.publish_config(
                ConfigParam(
                    data_id=PROBE_DATA_ID, group=PROBE_GROUP, content=probe_content, type="json"
                )
            )
            assert published, (
                "publish_config 返回 False（发布未生效——核查管理员账号/权限/命名空间）"
            )
            await asyncio.wait_for(got_event.wait(), timeout=SDK_CALLBACK_DEADLINE_S)
            assert json.loads(received[0])["probe"] is True
        finally:
            # 探测配置清理（失败不掩盖主断言——残留仅在控制台可手工删）
            if admin_service is not None:
                try:
                    await admin_service.remove_config(
                        ConfigParam(data_id=PROBE_DATA_ID, group=PROBE_GROUP)
                    )
                except Exception:  # noqa: BLE001 —— 清理失败仅提示
                    print(f"[warn] 探测配置清理失败，请控制台手工删除：{PROBE_DATA_ID}")
            for name, service in (
                ("管理端", admin_service),
                ("只读端", readonly_service),
            ):
                if service is None:
                    continue
                try:
                    await service.shutdown()
                except Exception:  # noqa: BLE001 —— 关停失败仅提示
                    print(f"[warn] {name} NacosConfigService 关停失败")
