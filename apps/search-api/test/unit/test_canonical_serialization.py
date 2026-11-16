"""unit 层：Nacos 配置内容的规范化序列化口径（发布端与防抖 md5 的单一事实源）。

钉住的语义：
- 口径 = `json.dumps(ensure_ascii=False, indent=2)`——缩进形态让 Nacos 控制台可读、
  中文不转义；解析侧 `json.loads` 对空白不敏感，内容语义与紧凑形态完全等价；
- 防抖 md5（`payload_md5`）与磁盘快照文本必须同经 `canonical_text` 一个出口：发布端
  与消费端口径分裂会让「内容等价但字节不同」在每次启动各触发一次假换版 + 落盘 + INFO
  噪音，违背 docs/contract/nacos-config.md §6 的 md5 防抖设计意图；
- `scripts/nacos_init.py` 是独立标准库脚本（不 import app 包），两侧一致性只能靠本文件
  的跨端护栏锁定：脚本侧 `serialize_payload` 必须与 `canonical_text` 逐字等价，且发布
  内容（content 字段）与覆写保护的对比基准都用它。
"""
import importlib.util
import json
from pathlib import Path

from app.config_center import AssetCenter, DegradationLevel
from app.config_center.cache import (
    canonical_text,
    content_md5,
    payload_md5,
    read_disk_snapshot,
    write_disk_snapshot,
)
from app.config_center.defaults import BUILTIN_DEFAULTS
from app.core.config import NacosSettings

REPO_ROOT = Path(__file__).resolve().parents[4]

# 代表性 payload：含中文（ensure_ascii 差异可见）、嵌套结构、数组——覆盖五 dataId 共同形态
_SAMPLE_PAYLOAD = {
    "schema_version": 1,
    "updated_at": "2026-11-16T12:00:00Z",
    "fast": {"fuse_top_n": 10, "turn_timeout_s": 120, "notes": ["中文说明", "多源融合"]},
    "deep": {"turn_timeout_s": 300},
}

_SPEC_DATA_ID = "ai_search.agent.spec.json"


def _load_script_module():
    """从仓库根加载 scripts/nacos_init.py（脚本有 __main__ 守卫，导入不触发发布）。"""
    spec = importlib.util.spec_from_file_location(
        "nacos_init_serialize", REPO_ROOT / "scripts" / "nacos_init.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings(tmp_path: Path) -> NacosSettings:
    """AssetCenter 构造参数（假体 client 不触网；凭据为 fixture 假值）。"""
    return NacosSettings(
        server_addr="127.0.0.1:8849",
        namespace="ai-search-dev",
        group="ai-search",
        username="ro-user",
        password="ro-pass",  # 测试夹具值，非真实凭据
        startup_timeout_s=1,
        snapshot_dir=str(tmp_path / "snapshot"),
    )


class _FakeAssetClient:
    """NacosAssetClient 假体：只实现 start/fetch/close 三个注入面，零网络。"""

    def __init__(self, raw_by_data_id: dict[str, str]) -> None:
        self.raw_by_data_id = dict(raw_by_data_id)

    async def start(self) -> None:
        return None

    async def fetch(self, data_id: str) -> str:
        return self.raw_by_data_id.get(data_id, "")

    async def close(self) -> None:
        return None


class TestCanonicalTextShape:
    def test_is_indent_two_and_keeps_chinese_unescaped(self):
        """口径形态：两空格缩进（控制台可读）+ 中文原样（不转义）。"""
        text = canonical_text(_SAMPLE_PAYLOAD)
        assert '\n  "schema_version"' in text, "缺两空格缩进——Nacos 控制台会挤成一行"
        assert "中文说明" in text, "中文被转义为 \\uXXXX，失去可读性"
        assert r"\u" not in text

    def test_parse_reserialize_is_md5_stable(self):
        """内容等价断言：解析后重新规范化，md5 不变（空白不承载语义）。"""
        reparsed = json.loads(canonical_text(_SAMPLE_PAYLOAD))
        assert reparsed == _SAMPLE_PAYLOAD
        assert payload_md5(reparsed) == payload_md5(_SAMPLE_PAYLOAD)

    def test_not_byte_identical_to_compact_form(self):
        """反例锁死形态声明：规范口径必须与紧凑形态字节不同（否则本文件全绿无意义）。"""
        assert canonical_text(_SAMPLE_PAYLOAD) != json.dumps(
            _SAMPLE_PAYLOAD, ensure_ascii=False
        )


class TestMd5FollowsCanonicalText:
    def test_payload_md5_equals_content_md5_of_canonical_text(self):
        """a) 防抖基准 md5 == 规范化文本的 md5（同一口径，发布端与消费端才能对上）。"""
        assert payload_md5(_SAMPLE_PAYLOAD) == content_md5(canonical_text(_SAMPLE_PAYLOAD))


class TestDiskSnapshotUsesCanonicalText:
    def test_disk_snapshot_text_equals_canonical_text(self, tmp_path):
        """b) 磁盘快照写盘文本逐字等于 canonical_text（L3 预热读回与口径一致）。"""
        write_disk_snapshot(tmp_path, "ai_search.agent.spec.json", _SAMPLE_PAYLOAD)
        written = (tmp_path / "ai_search.agent.spec.json").read_text(encoding="utf-8")
        assert written == canonical_text(_SAMPLE_PAYLOAD)


class TestScriptSerializationParity:
    def test_script_serialize_payload_matches_canonical_text(self):
        """c) 跨端一致性：脚本发布序列化 == 消费端 canonical_text（逐字，含中文/嵌套）。"""
        module = _load_script_module()
        assert module.serialize_payload(_SAMPLE_PAYLOAD) == canonical_text(_SAMPLE_PAYLOAD)

    def test_script_publish_sends_canonical_content(self, monkeypatch):
        """发布 content 字段走 serialize_payload（缩进形态送达 Nacos，不回落紧凑）。"""
        module = _load_script_module()
        captured: dict = {}

        def fake_post_form(url, fields, headers=None):
            captured.update(fields)
            return 200, b'{"code":0}'

        monkeypatch.setattr(module, "_post_form", fake_post_form)
        ok, status = module.publish(
            "fixture-token", "ai_search.agent.spec.json", _SAMPLE_PAYLOAD
        )

        assert (ok, status) == (True, 200)
        assert captured["content"] == canonical_text(_SAMPLE_PAYLOAD)

    def test_script_skip_guard_compares_canonical_text(self, monkeypatch):
        """覆写保护对比基准同口径：现值 == 规范口径初值 → 判定「内容相同」照常发布。

        反证：对比基准若回落紧凑形态，则规范化送达后的现值每次都被误判「内容不同」，
        默认跳过发布（FORCE 才覆盖）——运维侧表现为「初值明明没改却被跳过」。
        """
        module = _load_script_module()
        payloads = dict(module.DATA_IDS)
        monkeypatch.delenv("NACOS_INIT_FORCE", raising=False)
        monkeypatch.setattr(
            module, "resolve_credentials", lambda: ("fixture-user", "fixture-pass")
        )
        monkeypatch.setattr(module, "login", lambda user, password: "fixture-token")
        monkeypatch.setattr(
            module,
            "fetch_current",
            lambda token, data_id: (canonical_text(payloads[data_id]), 200),
        )
        published: list[str] = []

        def fake_publish(token, data_id, payload):
            published.append(data_id)
            return True, 200

        monkeypatch.setattr(module, "publish", fake_publish)

        assert module.main() == 0
        assert published == [data_id for data_id, _ in module.DATA_IDS]


class TestL3ToL1DebounceSteadyState:
    """重启稳态：L3 磁盘快照预热 + 远端同内容（规范口径）→ L1 防抖旁路不换版。

    阶梯版本语义（§6/§7）：L4 内置默认安装 version=1 → L3 磁盘快照接管 version=2 →
    L1 拉取。拉取内容与磁盘快照同出 `canonical_text` ⇒ digest 恒等 ⇒ 防抖旁路：不动
    version、不重发换版 INFO，仅层级升 L1 且磁盘快照保持存在（「置 L1 ⇒ 磁盘快照存在」
    是 L3 阶梯的前提不变量）。

    口径分裂的反证（正则边界）：发布端口径若偏离 `payload_md5`，此处变成 version=3 的
    真实换版——每次重启一次假换版 + 落盘 + INFO 噪音，正是本文件要防的回归。
    既有用例只钉了 L4→L1（内置默认 → 拉取），本类补 L3→L1 这一条重启路径。
    """

    async def test_l3_warm_then_identical_l1_pull_keeps_version(self, tmp_path, log_records):
        snapshot_dir = tmp_path / "snapshot"
        # 上一次 L1 落盘的磁盘快照（写盘层即规范口径，无需手工拼文本）
        for data_id, payload in BUILTIN_DEFAULTS.items():
            write_disk_snapshot(snapshot_dir, data_id, payload)
        # 远端同内容（Nacos 发布端规范口径送达）
        raws = {
            data_id: canonical_text(payload) for data_id, payload in BUILTIN_DEFAULTS.items()
        }

        center = AssetCenter(
            settings=_settings(tmp_path), app_env="dev", client=_FakeAssetClient(raws)
        )
        await center.start()

        assert center.level(_SPEC_DATA_ID) is DegradationLevel.L1, "L3 未升级到 L1"
        assert center.snapshot(_SPEC_DATA_ID).version == 2, (
            "L4(1) → L3(2)：同内容 L1 拉取必须停在 2（走了换版路径说明口径分裂）"
        )
        assert dict(center.snapshot(_SPEC_DATA_ID).payload) == BUILTIN_DEFAULTS[_SPEC_DATA_ID]
        assert (snapshot_dir / _SPEC_DATA_ID).is_file(), (
            "L1 成立但磁盘快照缺失：L3 前提不变量被破坏（重启+断网即跌 L4）"
        )
        assert read_disk_snapshot(snapshot_dir, _SPEC_DATA_ID) == BUILTIN_DEFAULTS[_SPEC_DATA_ID]
        assert not any(
            _SPEC_DATA_ID in str(record["message"]) and "换版" in str(record["message"])
            for record in log_records
        ), "同内容 L1 拉取出现换版日志——本该走防抖旁路"
        await center.close()
