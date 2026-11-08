"""资产快照缓存层：不可变 AssetSnapshot + 内存快照原子替换 + 磁盘快照原子覆写。

语义（docs/contract/nacos-config.md §6/§7）：
- AssetSnapshot 不可变（frozen dataclass，payload 经只读映射包裹）——消费方（TurnRunner
  OQ-11 / GET /api/v1/suggestions）捕获的是引用，换版走「整体引用切换」而非原地修改，
  进行中轮次持有的旧快照不受热更污染；
- version 在进程内按 dataId 单调递增（L4 安装=1，此后每次换版 +1），携带 md5/updated_at
  供防抖比对与换版留痕；
- 磁盘快照（L3）写盘走「临时文件 + os.replace 同目录原子 rename」——进程被杀也不会留下
  半写损坏的快照文件（教训同源：nacos plugin_state raft 半写即崩，s0-batch1-handoff.md 续 11）。

单线程事件循环内的同步 replace（无 await）天然原子——监听回调与请求处理共享同一线程，
不存在并发交错窗口，无需加锁。
"""
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any


def content_md5(raw: str) -> str:
    """原始配置内容（Nacos 推送文本 / 内置默认序列化文本）的 md5——防抖比对基准（§6）。

    安全注：此处 md5 非安全用途——它是契约 §6 明文的「md5 比对（防抖动）」变更指纹，
    新旧两侧均由本进程自行计算（非对抗方供给），且任何新内容须先过 validator 才会生效；
    碰撞抗性在此场景无安全语义，不构成缺陷（弱哈希告警不适用）。
    """
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def payload_md5(payload: Mapping[str, Any]) -> str:
    """payload 的确定性 md5（内置默认/磁盘快照无原始推送文本时的等价口径）。"""
    return content_md5(json.dumps(payload, ensure_ascii=False))


@dataclass(frozen=True)
class AssetSnapshot:
    """单个 dataId 的不可变资产快照（§6「构建不可变 AssetSnapshot」）。

    payload 以 MappingProxyType 包裹：快照对象与内容双层只读，杜绝消费方误改。
    """

    data_id: str
    version: int
    md5: str
    updated_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen dataclass 经 __post_init__ 用 object.__setattr__ 包只读映射
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class AssetStore:
    """内存快照容器：按 dataId 独立持有 AssetSnapshot，换版原子替换引用（§7 独立快照对象）。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, AssetSnapshot] = {}

    def get(self, data_id: str) -> AssetSnapshot | None:
        return self._snapshots.get(data_id)

    def install_defaults(self, data_id: str, payload: Mapping[str, Any]) -> AssetSnapshot:
        """L4 内置默认安装（版本起点 1；updated_at 取 payload 自带值，缺省空串）。"""
        return self.replace(
            data_id,
            payload,
            md5_hex=payload_md5(payload),
            version=1,
        )

    def replace(
        self,
        data_id: str,
        payload: Mapping[str, Any],
        *,
        md5_hex: str,
        version: int | None = None,
    ) -> AssetSnapshot:
        """原子换版：构建新快照后一次性切换引用（旧快照对象原样保留）。

        version 缺省 = 旧版本 + 1（首版时 1）；updated_at 优先取 payload 的
        顶层 updated_at（契约每个 dataId 必带），缺省空串由组装层补齐语义。
        """
        current = self._snapshots.get(data_id)
        next_version = version if version is not None else (current.version + 1 if current else 1)
        snapshot = AssetSnapshot(
            data_id=data_id,
            version=next_version,
            md5=md5_hex,
            updated_at=str(payload.get("updated_at", "")),
            payload=payload,
        )
        self._snapshots[data_id] = snapshot
        return snapshot


def write_disk_snapshot(snapshot_dir: Path, data_id: str, payload: Mapping[str, Any]) -> None:
    """磁盘快照原子覆写（L3 来源）：临时文件 + 同目录 rename（os.replace 幂等跨平台）。

    临时文件与目标同目录——保证同一文件系统，rename 才是原子操作。
    快照文件名 = dataId 本身（契约 dataId 以 .json 结尾，§0 命名规则，不再二次加后缀）。
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / data_id
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=snapshot_dir, prefix=f".{data_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(text)
        os.replace(tmp_path, target)
    except BaseException:
        # 落盘失败清掉半写临时文件（rename 前任何一步异常都不留残骸）
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_disk_snapshot(snapshot_dir: Path, data_id: str) -> dict[str, Any] | None:
    """读磁盘快照（L3 预热）：文件缺失返回 None；内容损坏抛 JSONDecodeError 由调用方定级。"""
    target = snapshot_dir / data_id
    if not target.is_file():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
