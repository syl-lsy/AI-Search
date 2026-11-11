"""unit 层：scripts/nacos_init.py 凭据解析——NACOS_INIT_* 环境变量优先，回落 .env 双账号口径。

钉住的语义（docs/contract/nacos-config.md §8 v1.9 双账号口径）：
- 契约推荐做法「临时以管理员凭据替换 .env 的 NACOS_USERNAME/NACOS_PASSWORD 跑完换回，
  不新增变量；脚本本身不感知账号形态」——脚本必须能从仓库根 .env 读取凭据，
  否则该推荐做法无法执行（2026-11-10 两次 make nacos-init usage Error 2 的根因）；
- NACOS_INIT_USER/NACOS_INIT_PASS 进程环境变量仍优先（原接口向后兼容，CI/无人值守场景）；
- NACOS_INIT_* 必须成对提供：只给其一不与 .env 值混拼（跨来源拼对难以排查，宁可拒跑）；
- 凭据值绝不打印/落盘：测试全用 fixture- 前缀假值，真实凭据不进任何断言。
"""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "nacos_init_creds", REPO_ROOT / "scripts" / "nacos_init.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # 脚本有 __main__ 守卫，导入不触发发布
    return module


@pytest.fixture(autouse=True)
def _clean_init_env(monkeypatch):
    """隔离真实环境：NACOS_INIT_* 一律先清空，用例按需注入 fixture 假值。"""
    monkeypatch.delenv("NACOS_INIT_USER", raising=False)
    monkeypatch.delenv("NACOS_INIT_PASS", raising=False)


def _write_env_file(tmp_path: Path, lines: list[str]) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_file


class TestResolveCredentials:
    def test_init_env_pair_preferred_over_env_file(self, tmp_path, monkeypatch):
        """NACOS_INIT_* 成对提供时优先——原接口向后兼容，不读 .env。"""
        monkeypatch.setenv("NACOS_INIT_USER", "fixture-admin")
        monkeypatch.setenv("NACOS_INIT_PASS", "fixture-pass")
        env_file = _write_env_file(
            tmp_path,
            ["NACOS_USERNAME=fixture-env-user", "NACOS_PASSWORD=fixture-env-pass"],
        )
        module = _load_script_module()
        assert module.resolve_credentials(env_path=env_file) == (
            "fixture-admin",
            "fixture-pass",
        )

    def test_falls_back_to_env_file_pair(self, tmp_path):
        """NACOS_INIT_* 缺失 → 回落 .env 的 NACOS_USERNAME/NACOS_PASSWORD（契约 v1.9 临时替换做法）。"""
        env_file = _write_env_file(
            tmp_path,
            ["NACOS_USERNAME=fixture-admin", "NACOS_PASSWORD=fixture-pass"],
        )
        module = _load_script_module()
        assert module.resolve_credentials(env_path=env_file) == (
            "fixture-admin",
            "fixture-pass",
        )

    def test_partial_init_pair_not_mixed_with_env_file(self, tmp_path, monkeypatch):
        """NACOS_INIT_* 只给其一 → 拒跑（None），不与 .env 值拼成跨来源凭据对。"""
        monkeypatch.setenv("NACOS_INIT_USER", "fixture-admin")  # PASS 缺失
        env_file = _write_env_file(
            tmp_path,
            ["NACOS_USERNAME=fixture-env-user", "NACOS_PASSWORD=fixture-env-pass"],
        )
        module = _load_script_module()
        assert module.resolve_credentials(env_path=env_file) is None

    def test_both_sources_missing_returns_none(self, tmp_path):
        env_file = _write_env_file(tmp_path, ["OTHER_KEY=fixture-value"])
        module = _load_script_module()
        assert module.resolve_credentials(env_path=env_file) is None

    def test_missing_env_file_returns_none(self, tmp_path):
        module = _load_script_module()
        assert module.resolve_credentials(env_path=tmp_path / "nonexistent.env") is None

    def test_env_file_inline_comment_and_spaces_stripped(self, tmp_path):
        """.env 行内尾注释与赋值空格剥离——真实 .env 存在 `KEY=value  # 注释` 形态。"""
        env_file = _write_env_file(
            tmp_path,
            [
                "# 注释行",
                "",
                "NACOS_USERNAME = fixture-admin  # 管理员（临时替换）",
                "NACOS_PASSWORD=fixture-pass   # 临时替换，跑完换回只读账号",
            ],
        )
        module = _load_script_module()
        assert module.resolve_credentials(env_path=env_file) == (
            "fixture-admin",
            "fixture-pass",
        )

    def test_default_env_path_is_repo_root_dotenv(self):
        """默认路径 = 仓库根 .env（脚本在 scripts/ 下，parent.parent 即仓库根）。"""
        module = _load_script_module()
        default_path = module._default_env_path()
        assert default_path == REPO_ROOT / ".env"
