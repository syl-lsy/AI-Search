"""unit 层共享 fixture（本目录下所有测试模块可见）。

日志文件隔离（**必须放在任何 app 模块 import 之前**）：unit 测试读的是空的 `.env`
（不装载 `.env.test`），`LOG_FILE_DIR` 会落到默认值 `logs/`——而单测故意触发异常
（500 兜底、service_logger 带栈等），错误文件会污染仓库根。这里在 conftest 导入期
（先于测试模块收集）把目录改到 `tmp/logs`（`tmp/` 已 gitignore）；integration/e2e
由各自 conftest 与 `.env.test` 的 `LOG_FILE_DIR=tmp/logs` 覆盖。
"""
import os

os.environ.setdefault("LOG_FILE_DIR", "tmp/logs")  # 见模块 docstring：先于 app import

from collections.abc import Iterator  # noqa: E402 —— 见上一行的顺序要求

import pytest  # noqa: E402
from loguru import logger  # noqa: E402


@pytest.fixture
def log_records() -> Iterator[list[dict]]:
    """内存捕获 sink：收集 loguru 原始 record 字典，测试结束移除。

    两个注意点：
    - `level=0`（低于 TRACE）与 `format="{message}"`：全级别都收到，且格式串不引用 extra
      （不依赖 setup_logging 装的 patcher，单测断言保持独立）；
    - teardown 按 id 移除 sink 包 try/except：`setup_logging()` 会 `logger.remove()`
      清掉全部 sink，测试若重配过日志系统，按 id 移除会找不到——属预期，不算失败。
    """
    records: list[dict] = []
    sink_id = logger.add(
        lambda message: records.append(message.record), format="{message}", level=0
    )
    yield records
    try:
        logger.remove(sink_id)
    except ValueError:  # setup_logging 已 logger.remove() 全清（sink 不存在属预期）
        pass


@pytest.fixture(autouse=True)
def _offline_url_guard_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """B15 内容侧护栏：URL 校验 DNS 注入缝的套件级离线化（unit 零网络纪律）。

    `content_guard.DEFAULT_RESOLVER` 是接线面 `validate_url_async` 的缺省 resolver
    （生产 = socket.getaddrinfo）；测试统一换成确定性公网假体（任意主机 → 公网 IP）。
    护栏的字面 IP / 短主机名 / 保留后缀判定不经 DNS、不受本假体影响；需要 NXDOMAIN /
    私网解析语义的用例在本测内对同一缝后置覆盖（monkeypatch 后写生效）。
    """
    from app.agent.safety import content_guard  # noqa: PLC0415 —— fixture 局部导入

    monkeypatch.setattr(
        content_guard, "DEFAULT_RESOLVER", lambda host: ["93.184.216.34"]
    )
