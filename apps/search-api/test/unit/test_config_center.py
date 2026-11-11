"""unit 层：config_center 组装（__init__.py）——四级降级启动流程与热更换版。

钉住的语义（docs/contract/nacos-config.md §6/§7）：
- 启动：内置默认（L4）→ 磁盘快照（L3）→ 拉 Nacos（阻塞 ≤ NACOS_STARTUP_TIMEOUT_S）；
  失败降级继续启动（dev）；production 落到 L4 拒绝启动（503001）；
- 单 dataId 校验失败保留旧版、其余四个独立生效、日志逐级（L2/L3 WARN、L4/校验失败 ERROR）；
- 热更：md5 防抖 → 校验 → 原子换版（version++）→ 磁盘覆写 → INFO 留痕；
- gRPC 裸 JSON 口径（R15）：内容按裸 JSON 解析，HTTP 的 data.content 包装形态不拆包
  ——整份当非法结构拒收。
"""
import asyncio
import json
from pathlib import Path

import pytest

import app.config_center as config_center_module
from app.config_center import (
    AssetCenter,
    DegradationLevel,
    get_asset_center,
    start_asset_center,
)
from app.config_center.cache import read_disk_snapshot, write_disk_snapshot
from app.config_center.defaults import BUILTIN_DEFAULTS, DATA_IDS
from app.core.config import NacosSettings
from app.exception import ConfigCenterException
from app.exception.handler.errors import ERROR_MAP

MCP_DATA_ID = "ai_search.agent.mcp.json"
SPEC_DATA_ID = "ai_search.agent.spec.json"
PROMPTS_DATA_ID = "ai_search.agent.prompts.json"


def _settings(tmp_path) -> NacosSettings:
    return NacosSettings(
        server_addr="127.0.0.1:8849",
        namespace="ai-search-dev",
        group="ai-search",
        username="ro-user",
        password="ro-pass",  # 测试夹具值，非真实凭据
        startup_timeout_s=1,
        snapshot_dir=str(tmp_path / "snapshot"),
    )


class FakeClient:
    """NacosAssetClient 假体：可控 start/fetch/延迟，不触网。"""

    def __init__(self, raw_by_data_id=None, start_error=None, start_delay=0.0):
        self.started = False
        self.closed = False
        self.raw_by_data_id = dict(raw_by_data_id or {})
        self.start_error = start_error
        self.start_delay = start_delay

    async def start(self) -> None:
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def fetch(self, data_id: str) -> str:
        return self.raw_by_data_id.get(data_id, "")

    async def close(self) -> None:
        self.closed = True


def _raw(payload: dict) -> str:
    # indent=2 模拟 Nacos 发布格式（与内置默认的紧凑序列化不同字节 → md5 不同，走真实换版路径）
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _messages(log_records) -> list[str]:
    return [r["message"] for r in log_records]


# ---- 启动降级阶梯 ----


class TestStartupDegradation:
    async def test_dev_all_l4_when_client_fails(self, tmp_path):
        center = AssetCenter(
            settings=_settings(tmp_path),
            app_env="dev",
            client=FakeClient(start_error=RuntimeError("连接失败")),
        )
        await center.start()
        for data_id in DATA_IDS:
            assert center.level(data_id) is DegradationLevel.L4
            snap = center.snapshot(data_id)
            assert snap.version == 1
            assert dict(snap.payload) == BUILTIN_DEFAULTS[data_id]
        await center.close()

    async def test_l3_disk_snapshot_beats_l4(self, tmp_path, log_records):
        """进程重启且 Nacos 不可达 → 磁盘快照（L3）顶上（§7 阶梯）。"""
        snapshot_dir = tmp_path / "snapshot"
        disk_payload = dict(BUILTIN_DEFAULTS[MCP_DATA_ID])
        disk_payload["updated_at"] = "2026-11-02T00:00:00Z"
        write_disk_snapshot(snapshot_dir, MCP_DATA_ID, disk_payload)
        center = AssetCenter(
            settings=_settings(tmp_path),
            app_env="dev",
            client=FakeClient(start_error=RuntimeError("连接失败")),
        )
        await center.start()
        assert center.level(MCP_DATA_ID) is DegradationLevel.L3
        assert center.snapshot(MCP_DATA_ID).payload["updated_at"] == "2026-11-02T00:00:00Z"
        assert center.level(SPEC_DATA_ID) is DegradationLevel.L4  # 未落盘的仍 L4
        assert any(MCP_DATA_ID in m for m in _messages(log_records))
        await center.close()

    async def test_corrupt_disk_snapshot_falls_back_l4_with_error(self, tmp_path, log_records):
        snapshot_dir = tmp_path / "snapshot"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / MCP_DATA_ID).write_text("{broken", encoding="utf-8")
        center = AssetCenter(
            settings=_settings(tmp_path),
            app_env="dev",
            client=FakeClient(start_error=RuntimeError("连接失败")),
        )
        await center.start()
        assert center.level(MCP_DATA_ID) is DegradationLevel.L4
        assert any(MCP_DATA_ID in m for m in _messages(log_records))
        await center.close()

    async def test_l1_full_pull(self, tmp_path):
        """Nacos 正常：五个 dataId 全部 L1，磁盘快照落盘。"""
        raws = {data_id: _raw(payload) for data_id, payload in BUILTIN_DEFAULTS.items()}
        center = AssetCenter(settings=_settings(tmp_path), app_env="dev", client=FakeClient(raws))
        await center.start()
        for data_id in DATA_IDS:
            assert center.level(data_id) is DegradationLevel.L1
            snap = center.snapshot(data_id)
            assert snap.version == 2  # L4 安装(1) → L1 拉取换版(2)
            assert dict(snap.payload) == BUILTIN_DEFAULTS[data_id]
            assert read_disk_snapshot(tmp_path / "snapshot", data_id) == BUILTIN_DEFAULTS[data_id]
        await center.close()

    async def test_single_data_id_invalid_keeps_old_others_l1(self, tmp_path, log_records):
        """单 dataId 校验失败保留旧版、其余四个独立生效（§7）。"""
        raws = {data_id: _raw(payload) for data_id, payload in BUILTIN_DEFAULTS.items()}
        broken = dict(BUILTIN_DEFAULTS[MCP_DATA_ID])
        broken.pop("schema_version")
        raws[MCP_DATA_ID] = _raw(broken)
        center = AssetCenter(settings=_settings(tmp_path), app_env="dev", client=FakeClient(raws))
        await center.start()
        assert center.level(MCP_DATA_ID) is DegradationLevel.L4  # 保留 L4 旧版
        assert center.snapshot(MCP_DATA_ID).version == 1
        assert center.level(SPEC_DATA_ID) is DegradationLevel.L1  # 其余独立生效
        assert any(
            MCP_DATA_ID in m for m in _messages(log_records)
        ), "校验失败必须 ERROR 留痕"
        await center.close()

    async def test_startup_timeout_degrades_instead_of_crashing(self, tmp_path):
        """拉取阻塞超过 NACOS_STARTUP_TIMEOUT_S → 降级继续启动（§6，dev 不拒启）。"""
        center = AssetCenter(
            settings=_settings(tmp_path),
            app_env="dev",
            client=FakeClient(start_delay=5.0),  # 远超 1s 预算
        )
        await center.start()
        assert center.level(SPEC_DATA_ID) is DegradationLevel.L4
        await center.close()

    async def test_late_callback_upgrades_to_l1(self, tmp_path):
        """拉取超时降级后，监听晚到回调仍可升级到 L1（SDK 最终一致，§6）。"""
        raws = {data_id: _raw(payload) for data_id, payload in BUILTIN_DEFAULTS.items()}
        client = FakeClient(raws, start_delay=5.0)
        center = AssetCenter(settings=_settings(tmp_path), app_env="dev", client=client)
        await center.start()
        assert center.level(SPEC_DATA_ID) is DegradationLevel.L4
        await center.apply_remote(SPEC_DATA_ID, raws[SPEC_DATA_ID])
        assert center.level(SPEC_DATA_ID) is DegradationLevel.L1
        await center.close()


class TestDiskSnapshotInvariant:
    """不变量：dataId 置 L1 ⇒ 磁盘快照存在（§7 阶梯：L3 预热依赖上一次 L1 落盘）。"""

    async def test_initial_pull_identical_to_defaults_still_persists_disk(self, tmp_path):
        """初始拉取内容与内置默认逐字相同（md5 防抖旁路）也必须落盘。

        生产实证（2026-11-08 真凭证首跑）：defaults.py 内置默认 = 契约初值逐字投影，
        scripts/nacos_init.py 以 json.dumps(payload, ensure_ascii=False) 紧凑形态发布，
        与 payload_md5 同一口径——Nacos 现值 == 初值时 digest 恒等，防抖分支直接置 L1
        返回；旧实现不写磁盘快照，稳态下 L3 永不落盘，重启+断网即跌 L4（production 拒启动）。
        """
        raws = {
            data_id: json.dumps(payload, ensure_ascii=False)  # 与 payload_md5 同口径
            for data_id, payload in BUILTIN_DEFAULTS.items()
        }
        center = AssetCenter(
            settings=_settings(tmp_path), app_env="dev", client=FakeClient(raws)
        )
        await center.start()

        assert center.level(PROMPTS_DATA_ID) is DegradationLevel.L1
        assert center.snapshot(PROMPTS_DATA_ID).version == 1  # 防抖：同内容不重复换版
        snapshot_dir = tmp_path / "snapshot"
        assert (snapshot_dir / PROMPTS_DATA_ID).is_file(), (
            "L1 成立但磁盘快照缺失：重启+断网会跌 L4（production 拒启动）"
        )
        assert read_disk_snapshot(snapshot_dir, PROMPTS_DATA_ID) == BUILTIN_DEFAULTS[PROMPTS_DATA_ID]
        await center.close()

    async def test_disk_write_oserror_does_not_break_swap(
        self, tmp_path, log_records, monkeypatch
    ):
        """磁盘写失败（OSError）不中断换版/监听链：不上抛、内存新版保留、level 仍 L1、ERROR 留痕。

        落盘是 L3 预热的手段而非换版前提——磁盘满/只读挂载时监听回调不能把整条链路打断；
        内存新版照常生效，等下一次成功换版自然收敛磁盘快照。
        """
        center = AssetCenter(
            settings=_settings(tmp_path),
            app_env="dev",
            client=FakeClient(start_error=RuntimeError("连接失败")),  # L4 起步
        )
        await center.start()
        assert center.level(SPEC_DATA_ID) is DegradationLevel.L4
        log_records.clear()  # 只留本次推送的日志，排除启动期 L4 降级 ERROR 干扰

        def boom(snapshot_dir, data_id, payload):
            raise OSError("磁盘满")

        monkeypatch.setattr(config_center_module, "write_disk_snapshot", boom)
        updated = dict(BUILTIN_DEFAULTS[SPEC_DATA_ID])
        updated["updated_at"] = "2026-11-08T00:00:00Z"
        await center.apply_remote(SPEC_DATA_ID, _raw(updated))  # 不上抛

        assert center.level(SPEC_DATA_ID) is DegradationLevel.L1
        assert (
            center.snapshot(SPEC_DATA_ID).payload["updated_at"] == "2026-11-08T00:00:00Z"
        )
        assert not (tmp_path / "snapshot" / SPEC_DATA_ID).exists()
        errors = [r for r in log_records if r["level"].name == "ERROR"]
        assert any(SPEC_DATA_ID in r["message"] for r in errors), (
            "磁盘写失败必须 ERROR 留痕且含 dataId（§7 不静默）"
        )
        await center.close()

    async def test_disk_write_typeerror_does_not_break_swap(
        self, tmp_path, log_records, monkeypatch
    ):
        """payload 不可序列化（TypeError）同样不上抛：换版/监听链不断、L1 仍置位、ERROR 留痕。

        编程错误（内置默认混入不可序列化常量）属防御性捕获面：落盘层 json.dumps 抛
        TypeError 时若逃逸，会经 apply_remote 冒泡打断 Nacos 监听回调链；语义与磁盘侧
        失败一致——内存新版保留，待下次成功换版收敛磁盘快照。
        """
        center = AssetCenter(
            settings=_settings(tmp_path),
            app_env="dev",
            client=FakeClient(start_error=RuntimeError("连接失败")),  # L4 起步
        )
        await center.start()
        assert center.level(SPEC_DATA_ID) is DegradationLevel.L4
        log_records.clear()  # 只留本次推送的日志，排除启动期 L4 降级 ERROR 干扰

        updated = dict(BUILTIN_DEFAULTS[SPEC_DATA_ID])
        updated["updated_at"] = "2026-11-08T00:00:00Z"
        raw = _raw(updated)  # 先序列化，再让 json.dumps 整体抛错（模拟不可序列化常量）

        def boom(*args, **kwargs):
            raise TypeError("Object of type object is not JSON serializable")

        monkeypatch.setattr(json, "dumps", boom)
        await center.apply_remote(SPEC_DATA_ID, raw)  # 不上抛

        assert center.level(SPEC_DATA_ID) is DegradationLevel.L1
        assert (
            center.snapshot(SPEC_DATA_ID).payload["updated_at"] == "2026-11-08T00:00:00Z"
        )
        assert not (tmp_path / "snapshot" / SPEC_DATA_ID).exists()
        errors = [r for r in log_records if r["level"].name == "ERROR"]
        assert any(SPEC_DATA_ID in r["message"] for r in errors), (
            "不可序列化（TypeError）必须 ERROR 留痕且含 dataId（§7 不静默）"
        )
        await center.close()


# ---- 内置默认可序列化护栏（defaults.py：L4 常量的 json 可写性）----


class TestBuiltinDefaultsSerializable:
    """BUILTIN_DEFAULTS 五份常量必须可 json 序列化——防「写入路径夹带不可序列化常量」。

    这类编程错误若漏进内置默认，会在首次启动/监听推送时崩（payload_md5 或落盘层
    json.dumps 抛 TypeError），此处前置固化：增改内置默认时立即红，不必等真栈验证。
    """

    @pytest.mark.parametrize("data_id", DATA_IDS)
    def test_builtin_default_is_json_serializable(self, data_id):
        text = json.dumps(BUILTIN_DEFAULTS[data_id], ensure_ascii=False)
        assert json.loads(text) == BUILTIN_DEFAULTS[data_id], (
            f"{data_id} 内置默认 json 往返不等价（含不可序列化常量或语义漂移）"
        )


class TestNacosInitScriptParity:
    """scripts/nacos_init.py 五份初值 ≡ defaults.py BUILTIN_DEFAULTS（同一契约 §1~§5 的双投影）。

    两处均声称【逐字】取自契约——投影一旦单侧漂移（某批次只改 defaults 或只改脚本），
    FORCE=1 重跑 nacos-init 会把旧结构初值覆盖发布（线上热更值被回滚为过时结构）。
    本测试锁死双投影一致：契约增量必须两处同步后才算闭环。
    """

    @staticmethod
    def _load_script_module():
        import importlib.util

        script_path = Path(__file__).resolve().parents[4] / "scripts" / "nacos_init.py"
        spec = importlib.util.spec_from_file_location("nacos_init_parity", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # 脚本有 __main__ 守卫，导入不触发发布
        return module

    def test_all_five_payloads_identical_to_defaults(self):
        module = self._load_script_module()
        script_payloads = dict(module.DATA_IDS)
        for data_id, defaults_payload in BUILTIN_DEFAULTS.items():
            assert script_payloads.get(data_id) == defaults_payload, (
                f"{data_id}：nacos_init.py 初值与 defaults.py 内置默认漂移——"
                "契约变更须两处同步（逐字投影），先修齐再发布"
            )


class TestProductionFloor:
    async def test_production_l4_refuses_start_503001(self, tmp_path):
        """§7 L4：production 拒绝启动（503001）。"""
        center = AssetCenter(
            settings=_settings(tmp_path),
            app_env="production",
            client=FakeClient(start_error=RuntimeError("连接失败")),
        )
        with pytest.raises(ConfigCenterException):
            await center.start()

    async def test_production_l3_allowed(self, tmp_path):
        """production 落到 L3 磁盘快照属合法降级——不拒启。"""
        snapshot_dir = tmp_path / "snapshot"
        for data_id, payload in BUILTIN_DEFAULTS.items():
            write_disk_snapshot(snapshot_dir, data_id, payload)
        center = AssetCenter(
            settings=_settings(tmp_path),
            app_env="production",
            client=FakeClient(start_error=RuntimeError("连接失败")),
        )
        await center.start()
        assert all(center.level(d) is DegradationLevel.L3 for d in DATA_IDS)
        await center.close()

    def test_error_code_503001_registered(self):
        """错误码表：ConfigCenterException → ("503001", 503, ...)（api.md §0）。"""
        assert ERROR_MAP[ConfigCenterException] == ("503001", 503, "配置中心不可达且无缓存")


# ---- 热更换版 ----


class TestHotUpdate:
    async def _started_center(self, tmp_path, log_records):
        raws = {data_id: _raw(payload) for data_id, payload in BUILTIN_DEFAULTS.items()}
        center = AssetCenter(settings=_settings(tmp_path), app_env="dev", client=FakeClient(raws))
        await center.start()
        return center, raws

    async def test_valid_push_swaps_snapshot(self, tmp_path, log_records):
        center, raws = await self._started_center(tmp_path, log_records)
        before = center.snapshot(SPEC_DATA_ID)
        updated = json.loads(raws[SPEC_DATA_ID])
        updated["fast"]["fuse_top_n"] = 20
        updated["updated_at"] = "2026-11-07T00:00:00Z"
        await center.apply_remote(SPEC_DATA_ID, _raw(updated))

        after = center.snapshot(SPEC_DATA_ID)
        assert after.version == before.version + 1
        assert after.payload["fast"]["fuse_top_n"] == 20
        assert before.payload["fast"]["fuse_top_n"] == 10  # 旧引用不被污染
        assert read_disk_snapshot(tmp_path / "snapshot", SPEC_DATA_ID)["fast"]["fuse_top_n"] == 20
        assert any(
            SPEC_DATA_ID in m and str(after.version) in m
            for m in _messages(log_records)
        ), "换版 INFO 日志须含 dataId 与 version（§6 留痕）"
        await center.close()

    async def test_md5_debounce_skips_identical_content(self, tmp_path, log_records):
        """§6：md5 比对防抖动——同内容重复推送不换版。"""
        center, raws = await self._started_center(tmp_path, log_records)
        before = center.snapshot(SPEC_DATA_ID)
        await center.apply_remote(SPEC_DATA_ID, raws[SPEC_DATA_ID])
        assert center.snapshot(SPEC_DATA_ID) is before
        await center.close()

    async def test_invalid_push_keeps_old_with_error(self, tmp_path, log_records):
        center, raws = await self._started_center(tmp_path, log_records)
        before = center.snapshot(SPEC_DATA_ID)
        broken = json.loads(raws[SPEC_DATA_ID])
        broken["fast"]["recursion_limit"] = -1
        await center.apply_remote(SPEC_DATA_ID, _raw(broken))
        assert center.snapshot(SPEC_DATA_ID) is before
        assert any(SPEC_DATA_ID in m for m in _messages(log_records))
        await center.close()

    async def test_wrapped_content_rejected_not_unwrapped(self, tmp_path, log_records):
        """R15 裸 JSON 口径：HTTP 形态的 data.content 包装不拆包——整份拒收。"""
        center, raws = await self._started_center(tmp_path, log_records)
        before = center.snapshot(SPEC_DATA_ID)
        wrapped = {"data": {"content": json.dumps(BUILTIN_DEFAULTS[SPEC_DATA_ID])}}
        await center.apply_remote(SPEC_DATA_ID, _raw(wrapped))
        assert center.snapshot(SPEC_DATA_ID) is before
        await center.close()

    async def test_unknown_data_id_ignored(self, tmp_path):
        center, _ = await self._started_center(tmp_path, None)
        await center.apply_remote("ai_search.agent.unknown.json", "{}")
        assert center.snapshot("ai_search.agent.unknown.json") is None
        await center.close()

    async def test_listen_registered_before_get(self, tmp_path):
        """R15 坑③：先注册监听后读值（否则初始回调永不触发）。"""
        order: list[str] = []

        class OrderRecordingClient(FakeClient):
            async def start(self) -> None:
                order.append("listeners-registered")
                await super().start()

            async def fetch(self, data_id: str) -> str:
                order.append(f"fetch:{data_id}")
                return self.raw_by_data_id.get(data_id, "")

        raws = {data_id: _raw(payload) for data_id, payload in BUILTIN_DEFAULTS.items()}
        center = AssetCenter(
            settings=_settings(tmp_path), app_env="dev", client=OrderRecordingClient(raws)
        )
        await center.start()
        assert order[0] == "listeners-registered"
        assert order[1:].count("fetch:ai_search.agent.prompts.json") == 1
        await center.close()


# ---- 持有者与依赖出口 ----


class TestHolderAndDependencies:
    async def test_get_asset_center_before_start_raises(self, monkeypatch):
        monkeypatch.setattr(config_center_module, "_asset_center", None)
        with pytest.raises(RuntimeError):
            get_asset_center()

    async def test_start_asset_center_registers_holder(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_center_module, "_asset_center", None)
        center = await start_asset_center(
            settings=_settings(tmp_path), app_env="dev", client=FakeClient()
        )
        assert get_asset_center() is center
        await center.close()
        with pytest.raises(RuntimeError):
            get_asset_center()  # close 后持有者清空

    def test_dependencies_reexport(self, monkeypatch, tmp_path):
        """dependencies.get_asset_snapshot/get_asset_center 与 config_center 单一来源。"""
        from app import dependencies

        monkeypatch.setattr(config_center_module, "_asset_center", None)
        center = asyncio.run(_start(tmp_path))
        monkeypatch.setattr(config_center_module, "_asset_center", center)

        assert dependencies.get_asset_center is get_asset_center
        snap = dependencies.get_asset_snapshot(SPEC_DATA_ID)
        assert snap is center.snapshot(SPEC_DATA_ID)
        assert dependencies.get_asset_snapshot("ai_search.agent.unknown.json") is None

    async def test_snapshot_unknown_data_id_returns_none(self, tmp_path):
        center = await _start(tmp_path)
        assert center.snapshot("ai_search.agent.unknown.json") is None
        await center.close()


async def _start(tmp_path) -> AssetCenter:
    center = AssetCenter(settings=_settings(tmp_path), app_env="dev", client=FakeClient())
    await center.start()
    return center
