#!/usr/bin/env python3
"""按 git 变更跑关联测试：变更测试文件 ∩ 配置的测试目录，逐文件串行执行。

行为约定：
- 变更文件 = staged（已暂存）+ unstaged（工作区）+ untracked（未跟踪）三路并集；
  仓库还没有首个提交（unborn HEAD）时，暂存区退化为列出全部已暂存文件；
- 测试目录清单读根 pyproject.toml 的 `[tool.test-runner] test_dirs`（glob，如 "apps/*/test"）；
- 变更文件落在测试目录内、且是测试文件（test_*.py / *_test.py，conftest/__init__ 除外）
  的，才逐个 `uv run pytest <file> -q` 串行执行，任一失败最终 exit 1；
- **非 git 仓库直接清晰报错并 exit 1**（本脚本依赖 git 定位变更集，不做猜测）；
- 无匹配变更时打印提示并 exit 0。

Makefile 入口：`make test-changed`。定位是开发时的快速回归（改了哪个测试跑哪个），
不能替代 `make test` 的门禁（unit + integration + 覆盖率 80%）。
"""
import subprocess
import sys
import tomllib
from pathlib import Path

# git 命令失败（非 git 仓库、unborn HEAD 等）由调用方按场景处理
GIT_REPO_CHECK_ARGS = ["rev-parse", "--is-inside-work-tree"]


def _git(repo_root: Path, args: list[str]) -> list[str] | None:
    """执行 git 命令并返回非空输出行；命令失败返回 None（由调用方决定是否忽略）。

    注意不 raise：unborn HEAD（仓库尚无首个提交）会让 diff 类命令失败，
    这属于正常开发早期状态，不应中断脚本。
    `-c core.quotepath=false`：中文等非 ASCII 文件名原样输出——git 默认会把它们
    转义成带引号的八进制（`"test_\\344\\270\\255..."`），路径匹配会静默失配漏跑测试。
    """
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def get_repo_root() -> Path:
    """定位仓库根；非 git 仓库打印清晰提示并退出（课程版在此裸抛 CalledProcessError）。"""
    result = subprocess.run(
        ["git", *GIT_REPO_CHECK_ARGS], capture_output=True, text=True
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        print("错误: 当前目录不在 git 仓库内，无法定位变更文件。", file=sys.stderr)
        print("提示: 先 `git init`（并提交一次）后重试；本命令只按 git 变更跑关联测试。", file=sys.stderr)
        sys.exit(1)
    top_level = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(top_level.stdout.strip())


def get_changed_files(repo_root: Path) -> set[Path]:
    """staged + unstaged + untracked 三路并集（相对仓库根解析为绝对路径）。"""
    changed: set[Path] = set()

    has_head = _git(repo_root, ["rev-parse", "--verify", "--quiet", "HEAD"]) is not None
    commands: list[list[str]] = []
    if has_head:
        commands.append(["diff", "--name-only", "--cached", "HEAD"])  # 已暂存
        commands.append(["diff", "--name-only", "HEAD"])  # 工作区未暂存
    else:
        # unborn HEAD：还没有可比的提交，暂存区视为全部「新增」（intersect 后仍只跑测试文件）
        commands.append(["ls-files", "--cached"])
    commands.append(["ls-files", "--others", "--exclude-standard"])  # 未跟踪

    for cmd in commands:
        for line in _git(repo_root, cmd) or []:
            changed.add(repo_root / line)

    return changed


def load_test_dirs(repo_root: Path) -> list[Path]:
    """读根 pyproject 的 [tool.test-runner] test_dirs 并展开为已存在的目录。"""
    config_path = repo_root / "pyproject.toml"
    if not config_path.exists():
        print("错误: 根目录缺少 pyproject.toml，无法读取 [tool.test-runner] test_dirs。", file=sys.stderr)
        return []

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    patterns: list[str] = config.get("tool", {}).get("test-runner", {}).get("test_dirs", [])
    if not patterns:
        print("错误: pyproject.toml 的 [tool.test-runner] 中未配置 test_dirs。", file=sys.stderr)
        return []

    dirs: list[Path] = []
    for pattern in patterns:
        for p in sorted(repo_root.glob(pattern)):
            if p.is_dir():
                dirs.append(p)

    return dirs


def _is_test_file(file: Path) -> bool:
    """只有测试文件才值得单文件直跑；conftest/__init__ 由 pytest 收集时自动加载。"""
    if file.suffix != ".py":
        return False
    name = file.stem
    if name in ("__init__", "conftest"):
        return False
    return name.startswith("test_") or name.endswith("_test")


def find_test_files(changed_files: set[Path], test_dirs: list[Path]) -> list[Path]:
    """变更文件 ∩ 测试目录 ∩ 测试文件命名，按路径排序保证串行顺序稳定。"""
    test_files: list[Path] = []
    for f in sorted(changed_files):
        if not _is_test_file(f):
            continue
        for test_dir in test_dirs:
            try:
                f.relative_to(test_dir)
                test_files.append(f)
                break
            except ValueError:
                continue
    return test_files


def run_test(test_file: Path, repo_root: Path) -> bool:
    rel_path = test_file.relative_to(repo_root)
    # flush=True：本脚本的 stdout 被管道接走时是块缓冲，不刷新会把头部信息排到子进程输出之后
    print(f"\n{'=' * 60}", flush=True)
    print(f"运行: {rel_path}", flush=True)
    print(f"{'=' * 60}", flush=True)

    result = subprocess.run(
        ["uv", "run", "pytest", str(test_file), "-q"],
        cwd=repo_root,
    )
    return result.returncode == 0


def main() -> None:
    repo_root = get_repo_root()
    changed_files = get_changed_files(repo_root)

    if not changed_files:
        print("没有变更的文件，无需跑关联测试。")
        return

    test_dirs = load_test_dirs(repo_root)
    if not test_dirs:
        sys.exit(1)

    test_files = find_test_files(changed_files, test_dirs)
    if not test_files:
        print("变更文件中没有测试文件（变更集里无 test_*.py / *_test.py），无需跑关联测试。")
        return

    print(f"发现 {len(test_files)} 个变更的测试文件:", flush=True)
    for f in test_files:
        print(f"  {f.relative_to(repo_root)}")

    failed: list[Path] = []
    passed = 0
    for test_file in test_files:
        if run_test(test_file, repo_root):
            passed += 1
        else:
            failed.append(test_file)

    print(f"\n{'=' * 60}")
    print(f"结果: {passed} 通过, {len(failed)} 失败")
    if failed:
        print("失败的测试:")
        for f in failed:
            print(f"  {f.relative_to(repo_root)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
