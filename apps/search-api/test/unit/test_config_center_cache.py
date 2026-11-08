"""unit 层：config_center/cache.py——不可变快照与磁盘快照（docs/contract/nacos-config.md §6）。

钉住的语义：
- AssetSnapshot 不可变（frozen dataclass + payload 只读映射）——TurnRunner 轮次捕获的是
  引用，换版必须是「整体引用切换」而不是原地改（OQ-11：进行中轮次不受热更影响）；
- 版本递增、按 dataId 独立（§7：单 dataId 校验失败不影响其余四个——独立快照对象）；
- 磁盘快照原子覆写（临时文件 + rename，§6「磁盘快照覆写」防半写损坏——同 nacos raft
  半写教训，s0-batch1-handoff.md 续 11）。
"""
import json

import pytest

from app.config_center.cache import (
    AssetSnapshot,
    AssetStore,
    read_disk_snapshot,
    write_disk_snapshot,
)


def _payload(version_note: str = "v1") -> dict:
    return {"schema_version": 1, "updated_at": "2026-11-01T12:00:00Z", "note": version_note}


class TestAssetSnapshot:
    def test_frozen_cannot_mutate_attribute(self):
        snap = AssetSnapshot(
            data_id="ai_search.agent.spec.json",
            version=1,
            md5="abc",
            updated_at="2026-11-01T12:00:00Z",
            payload=_payload(),
        )
        with pytest.raises(Exception):  # noqa: B017 —— frozen dataclass 置值抛 FrozenInstanceError
            snap.version = 99

    def test_payload_is_read_only_mapping(self):
        snap = AssetSnapshot(
            data_id="ai_search.agent.spec.json",
            version=1,
            md5="abc",
            updated_at="2026-11-01T12:00:00Z",
            payload=_payload(),
        )
        with pytest.raises(TypeError):
            snap.payload["schema_version"] = 2  # type: ignore[index]


class TestAssetStore:
    def test_install_defaults_version_one(self):
        store = AssetStore()
        snap = store.install_defaults("ai_search.agent.spec.json", _payload())
        assert snap.version == 1
        assert snap.data_id == "ai_search.agent.spec.json"
        assert snap.md5  # md5 已计算（防抖比对基准）
        assert snap.updated_at == "2026-11-01T12:00:00Z"

    def test_replace_bumps_version_and_swaps_whole_reference(self):
        store = AssetStore()
        first = store.install_defaults("ai_search.agent.spec.json", _payload("first"))
        second = store.replace(
            "ai_search.agent.spec.json",
            _payload("second"),
            md5_hex="deadbeef",
        )
        # 整体引用切换：旧快照对象原样不动（轮内捕获的引用不被热更污染）
        assert first.version == 1 and first.payload["note"] == "first"
        assert second.version == 2
        assert second.md5 == "deadbeef"
        assert store.get("ai_search.agent.spec.json") is second

    def test_get_unknown_data_id_returns_none(self):
        store = AssetStore()
        assert store.get("ai_search.agent.unknown.json") is None

    def test_per_data_id_independence(self):
        store = AssetStore()
        store.install_defaults("ai_search.agent.spec.json", _payload("spec"))
        store.install_defaults("ai_search.agent.mcp.json", _payload("mcp"))
        store.replace("ai_search.agent.mcp.json", _payload("mcp-new"), md5_hex="m1")

        spec = store.get("ai_search.agent.spec.json")
        mcp = store.get("ai_search.agent.mcp.json")
        assert spec.version == 1  # mcp 换版不影响 spec（独立快照对象）
        assert mcp.version == 2


class TestDiskSnapshot:
    def test_write_read_roundtrip(self, tmp_path):
        write_disk_snapshot(tmp_path, "ai_search.agent.spec.json", _payload())
        loaded = read_disk_snapshot(tmp_path, "ai_search.agent.spec.json")
        assert loaded == _payload()

    def test_overwrite_leaves_no_tmp_file(self, tmp_path):
        """原子覆写：临时文件 + rename，覆写后目录无 .tmp 残留（防半写损坏）。"""
        write_disk_snapshot(tmp_path, "ai_search.agent.spec.json", _payload("old"))
        write_disk_snapshot(tmp_path, "ai_search.agent.spec.json", _payload("new"))
        assert read_disk_snapshot(tmp_path, "ai_search.agent.spec.json") == _payload("new")
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_read_missing_returns_none(self, tmp_path):
        assert read_disk_snapshot(tmp_path, "ai_search.agent.none.json") is None

    def test_read_corrupt_raises(self, tmp_path):
        (tmp_path / "ai_search.agent.spec.json").write_text("{not-json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            read_disk_snapshot(tmp_path, "ai_search.agent.spec.json")
