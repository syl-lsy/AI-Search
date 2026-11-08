"""Nacos 资产中心（组装层）：启动降级阶梯 + 热更换版 + 进程级持有者。

三层分工：client.py（SDK 连接与监听）/ cache.py（不可变快照与磁盘快照）/
validator.py（结构校验与密钥嗅探）——本文件把三者串成 docs/contract/nacos-config.md
§6 启动流程与 §7 四级降级阶梯：

    启动：读内置默认（L4，defaults.py）→ 读磁盘快照（L3）→ 拉 Nacos（L1，阻塞 ≤
    NACOS_STARTUP_TIMEOUT_S，失败降级继续启动）；单 dataId 校验失败保留旧版、
    其余四个独立生效、日志逐级（L2/L3 WARN、L4/校验失败 ERROR，§7 不静默）；
    production 落到 L4 拒绝启动（503001，ConfigCenterException）。

    运行：监听回调（md5 防抖 → 校验 → 原子换版 version++ → 磁盘覆写 → INFO 留痕）
    与初始拉取共用 apply_remote 一条通路；gRPC 裸 JSON 口径（R15）——内容按裸
    JSON 解析，HTTP 的 data.content 包装形态不拆包、整份按非法结构拒收。

    不变量：dataId 置 L1（含 md5 防抖旁路）必然落磁盘快照——L3 预热依赖稳态下
    每次 L1 落盘；防抖分支若跳过落盘，重启+断网即跌 L4（production 拒启动）。

消费方式：main.py lifespan 调 start_asset_center()（B2 接线）；业务代码经
app/dependencies.py 的 get_asset_center / get_asset_snapshot 取快照（api/ 层唯一
出口），TurnRunner 轮次发起时一次性捕获 AssetSnapshot 引用整轮沿用（OQ-11）。

连接配置一律经 core/config 单例（NACOS_ 组）读取，本包不硬编码地址/凭据；密钥绝不
进 Nacos（validator 密钥嗅探拒收疑似密钥字段）。
"""
import asyncio
import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.config_center.cache import (
    AssetSnapshot,
    AssetStore,
    content_md5,
    payload_md5,
    read_disk_snapshot,
    write_disk_snapshot,
)
from app.config_center.client import NacosAssetClient
from app.config_center.defaults import BUILTIN_DEFAULTS, DATA_IDS
from app.config_center.validator import AssetValidationError, validate_asset
from app.core.config import NacosSettings, common_settings, nacos_settings
from app.core.logger import ConfigLog
from app.exception import ConfigCenterException

__all__ = [
    "DATA_IDS",
    "AssetCenter",
    "AssetSnapshot",
    "DegradationLevel",
    "get_asset_center",
    "start_asset_center",
]


class DegradationLevel(StrEnum):
    """四级降级阶梯（docs/contract/nacos-config.md §7）：当前快照的来源层级。"""

    L1 = "L1"  # Nacos 实时（拉取或推送）
    L2 = "L2"  # 内存快照（推送/拉取失败，保留上次已知值）
    L3 = "L3"  # 磁盘快照（进程重启且 Nacos 不可达）
    L4 = "L4"  # 代码内置默认（L2/L3 均无；仅 dev——production 拒绝启动）


class AssetCenter:
    """五 dataId 资产中心：持有内存快照、驱动降级阶梯与热更换版。

    client 经构造注入（生产默认 NacosAssetClient；单测注入假体），连接配置从
    settings 读——构造时不触网，全部网络动作在 start() 内且受 startup_timeout_s 约束。
    """

    def __init__(
        self,
        *,
        settings: NacosSettings | None = None,
        app_env: str | None = None,
        client: NacosAssetClient | None = None,
    ) -> None:
        self._settings = settings or nacos_settings
        self._app_env = app_env if app_env is not None else common_settings.app_env
        self._store = AssetStore()
        self._levels: dict[str, DegradationLevel] = {}
        self._snapshot_dir = Path(self._settings.snapshot_dir)
        self._client = client or NacosAssetClient(
            self._settings, on_change=self.apply_remote
        )

    # ---- 启动流程（§6：默认 → 磁盘 → Nacos）----

    async def start(self) -> "AssetCenter":
        """按降级阶梯启动：L4 安装 → L3 磁盘预热 → L1 拉取（阻塞 ≤ startup_timeout_s）。"""
        self._install_l4_defaults()
        self._load_l3_disk_snapshots()
        await self._pull_l1_within_budget()
        self._log_degradations()
        self._enforce_production_floor()
        return self

    def _install_l4_defaults(self) -> None:
        for data_id in DATA_IDS:
            self._store.install_defaults(data_id, BUILTIN_DEFAULTS[data_id])
            self._levels[data_id] = DegradationLevel.L4

    def _load_l3_disk_snapshots(self) -> None:
        """磁盘快照预热（L3）：校验通过才接管；损坏/校验失败 ERROR 留痕后留在 L4。"""
        for data_id in DATA_IDS:
            try:
                payload = read_disk_snapshot(self._snapshot_dir, data_id)
            except (json.JSONDecodeError, OSError) as exc:
                ConfigLog(
                    message=f"磁盘快照损坏，忽略并回退内置默认：{data_id}（{exc}）",
                    data_id=data_id,
                    source=DegradationLevel.L3,
                ).error()
                continue
            if payload is None:
                continue
            try:
                validate_asset(data_id, payload)
            except AssetValidationError as exc:
                ConfigLog(
                    message=f"磁盘快照校验失败，忽略并回退内置默认：{data_id}（{exc}）",
                    data_id=data_id,
                    source=DegradationLevel.L3,
                ).error()
                continue
            self._store.replace(data_id, payload, md5_hex=payload_md5(payload))
            self._levels[data_id] = DegradationLevel.L3

    async def _pull_l1_within_budget(self) -> None:
        """注册监听后拉取五 dataId（阻塞 ≤ NACOS_STARTUP_TIMEOUT_S；失败降级继续启动）。

        超时/异常一律吞掉并留痕——降级决策不在客户端（client 原样上抛），在这里收口；
        晚到的监听回调仍可经 apply_remote 把 dataId 升级到 L1（SDK 最终一致，§6）。
        """
        try:
            async with asyncio.timeout(self._settings.startup_timeout_s):
                await self._client.start()  # 先注册全部监听（R15 坑③）
                for data_id in DATA_IDS:
                    raw = await self._client.fetch(data_id)
                    await self.apply_remote(data_id, raw)
        except Exception as exc:  # noqa: BLE001 —— 任何 Nacos 侧故障都走降级而非启动失败（§7）
            ConfigLog(
                message=f"Nacos 启动拉取失败，按降级阶梯继续启动：{type(exc).__name__}",
                source=DegradationLevel.L1,
            ).warning(exc=exc)

    def _log_degradations(self) -> None:
        """逐 dataId 记录最终降级态（§7 不静默：L2/L3 WARN、L4 ERROR）。"""
        level_log = {
            DegradationLevel.L2: "warning",
            DegradationLevel.L3: "warning",
            DegradationLevel.L4: "error",
        }
        for data_id in DATA_IDS:
            level = self._levels[data_id]
            if level not in level_log:
                continue
            record = ConfigLog(
                message=f"配置降级至 {level}：{data_id}",
                data_id=data_id,
                source=level,
            )
            getattr(record, level_log[level])()

    def _enforce_production_floor(self) -> None:
        """§7 L4：production 落到内置默认（无任何缓存）→ 拒绝启动（503001）。"""
        if self._app_env != "production":
            return
        at_l4 = [d for d in DATA_IDS if self._levels[d] is DegradationLevel.L4]
        if at_l4:
            raise ConfigCenterException(
                message="配置中心不可达且无缓存",
                detail=f"production 环境以下 dataId 降级到 L4 内置默认，拒绝启动：{', '.join(at_l4)}",
            )

    # ---- 运行期换版（初始拉取与监听推送共用通路）----

    async def apply_remote(self, data_id: str, raw: str) -> None:
        """远端内容落地：md5 防抖 → 解析 → 校验 → 原子换版 → 磁盘覆写 → INFO 留痕。

        失败语义（§6）：解析/校验失败保留旧快照、ERROR 留痕（含校验错误定位），
        该 dataId 不生效、其余 dataId 独立生效。

        两条生效路径（防抖旁路 / 完整换版）都经 `_persist_l1_disk` 落盘——「置 L1 ⇒
        磁盘快照存在」是 L3 阶梯的前提不变量（内置默认 == 契约初值时初始拉取恒走
        防抖旁路，此处漏落盘会让磁盘快照永不落地）。
        """
        if data_id not in DATA_IDS:  # 防御式：监听只注册五个，意外 dataId 静默忽略
            ConfigLog(message=f"忽略未注册 dataId 的推送：{data_id}", data_id=data_id).warning()
            return

        digest = content_md5(raw)
        current = self._store.get(data_id)
        if current is not None and current.md5 == digest:
            # md5 比对防抖（§6）：同内容重复送达（初始拉取+初始回调并存）不重复换版；
            # 但本通路只承接 Nacos 侧内容——来源层级仍确认为 L1（§7），且必须补落
            # 磁盘快照：md5 相等保证 current.payload 与 raw 解析结果全等
            self._persist_l1_disk(data_id, current.payload)
            self._levels[data_id] = DegradationLevel.L1
            return

        try:
            payload = json.loads(raw)
            validate_asset(data_id, payload)
        except (json.JSONDecodeError, AssetValidationError) as exc:
            ConfigLog(
                message=f"配置校验失败，保留旧版：{data_id}（{exc}）",
                data_id=data_id,
                source=str(self._levels[data_id]),
            ).error()
            return

        snapshot = self._store.replace(data_id, payload, md5_hex=digest)
        self._persist_l1_disk(data_id, payload)
        self._levels[data_id] = DegradationLevel.L1
        ConfigLog(
            message=f"配置换版生效：{data_id} → v{snapshot.version}",
            data_id=data_id,
            version=snapshot.version,
            md5=snapshot.md5,
            source=DegradationLevel.L1,
        ).info()

    def _persist_l1_disk(self, data_id: str, payload: Mapping[str, Any]) -> None:
        """L1 落盘（两条生效路径唯一出口）：保证置 L1 时磁盘快照存在（§7 L3 预热）。

        payload 可能是快照里的 MappingProxyType（只读包裹，json 不可直接序列化），
        dict() 浅转一次交给写盘层（快照内容来自 json 解析/内置常量，浅转即可）。

        写盘失败只 ERROR 留痕不上抛——落盘是 L3 预热的手段而非换版前提，监听链不得
        因落盘故障中断；内存新版照常生效，待下一次成功换版自然收敛磁盘快照。捕获面：

        - OSError：磁盘侧失败（磁盘满/只读挂载等）；
        - TypeError：内置默认混入不可序列化常量属编程错误，防御性捕获避免监听回调链
          断裂（换版/L1/ERROR 留痕语义不变）；defaults.py 另有单测护栏前置拦截该类错误。
        """
        try:
            write_disk_snapshot(self._snapshot_dir, data_id, dict(payload))
        except (OSError, TypeError) as exc:
            ConfigLog(
                message=f"磁盘快照写入失败，内存新版保留：{data_id}（{exc}）",
                data_id=data_id,
                source=DegradationLevel.L1,
            ).error()

    # ---- 消费面 ----

    def snapshot(self, data_id: str) -> AssetSnapshot | None:
        """当前快照（不可变引用；未知 dataId 返回 None）。"""
        return self._store.get(data_id)

    def level(self, data_id: str) -> DegradationLevel:
        """当前生效层级（/health/ready 的 nacos 组件 degraded 判定依据，§7）。"""
        return self._levels[data_id]

    async def close(self) -> None:
        """关停客户端并清掉进程级持有者（若持有者即本实例）。"""
        await self._client.close()
        if _asset_center is self:
            _set_asset_center(None)


# ---- 进程级持有者（main.py lifespan 启动时注册；dependencies 经此取）----

_asset_center: AssetCenter | None = None


def _set_asset_center(center: AssetCenter | None) -> None:
    global _asset_center
    _asset_center = center


def get_asset_center() -> AssetCenter:
    """取进程级资产中心（未启动时 fail-fast——组装层尚未 start 即消费属编程错误）。"""
    if _asset_center is None:
        raise RuntimeError("配置中心尚未启动：请在 lifespan 启动钩子调用 start_asset_center()")
    return _asset_center


async def start_asset_center(
    *,
    settings: NacosSettings | None = None,
    app_env: str | None = None,
    client: NacosAssetClient | None = None,
) -> AssetCenter:
    """启动资产中心并注册进程级持有者（main.py lifespan 调用；B2 接线）。

    参数仅供单测注入假体/隔离目录，生产调用方一律不传（连接配置走 NACOS_ 组单例）。
    """
    center = AssetCenter(settings=settings, app_env=app_env, client=client)
    await center.start()
    _set_asset_center(center)
    return center
