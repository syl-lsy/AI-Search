"""unit 层示例：settings 分组的实例化、前缀隔离与默认值断言（零网络）。"""
from app.core.config import CommonSettings, DBSettings, LogSettings, WebSettings


def test_common_settings_default(monkeypatch):
    """通用组（无前缀）：默认值直接生效。"""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = CommonSettings(_env_file=None)  # 跳过 .env，只用进程环境变量
    assert settings.environment == "development"


def test_web_settings_reads_prefixed_env(monkeypatch):
    """WEB_ 前缀：WEB_APP_NAME 映射到 app_name。"""
    monkeypatch.setenv("WEB_APP_NAME", "订单服务 API")
    settings = WebSettings(_env_file=None)
    assert settings.app_name == "订单服务 API"


def test_db_settings_reads_prefixed_env(monkeypatch):
    """DB_ 前缀：DB_HOST 映射到 host。"""
    monkeypatch.setenv("DB_HOST", "db.internal")
    settings = DBSettings(_env_file=None)
    assert settings.host == "db.internal"


def test_db_settings_pool_defaults(monkeypatch):
    """DB_ 组池化参数默认值：DB_POOL_SIZE=10 / DB_MAX_OVERFLOW=5（ARCH §7）。"""
    for key in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW"):
        monkeypatch.delenv(key, raising=False)
    settings = DBSettings(_env_file=None)
    assert settings.pool_size == 10
    assert settings.max_overflow == 5


def test_log_settings_defaults(monkeypatch):
    """LOG_ 组默认值：INFO / 200ms 慢查询阈值 / logs 目录。"""
    for key in ("LOG_LEVEL", "LOG_SLOW_QUERY_THRESHOLD", "LOG_FILE_DIR"):
        monkeypatch.delenv(key, raising=False)
    settings = LogSettings(_env_file=None)
    assert settings.level == "INFO"
    assert settings.slow_query_threshold == 200
    assert settings.file_dir == "logs"


def test_log_settings_reads_prefixed_env(monkeypatch):
    """LOG_ 前缀：LOG_LEVEL / LOG_SLOW_QUERY_THRESHOLD / LOG_FILE_DIR 三件套。"""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_SLOW_QUERY_THRESHOLD", "500")
    monkeypatch.setenv("LOG_FILE_DIR", "var/logs")
    settings = LogSettings(_env_file=None)
    assert settings.level == "DEBUG"
    assert settings.slow_query_threshold == 500
    assert settings.file_dir == "var/logs"


def test_prefix_isolation(monkeypatch):
    """前缀隔离：WEB_ 组的变量不会落入 DB_ 组（同名后缀互不串扰）。"""
    monkeypatch.setenv("WEB_NAME", "should-not-leak")
    monkeypatch.delenv("DB_NAME", raising=False)
    assert DBSettings(_env_file=None).name == ""


def test_log_prefix_isolation(monkeypatch):
    """前缀隔离（LOG_ ↔ 其他组）：LOG_LEVEL 只进 LogSettings，不串到通用/Web 组。"""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert CommonSettings(_env_file=None).environment == "development"
    assert WebSettings(_env_file=None).app_name == "Web Service API"
    assert LogSettings(_env_file=None).level == "DEBUG"
    # 反向：DB_ 组的变量不落进 LOG_ 组
    monkeypatch.setenv("DB_LEVEL", "CRITICAL")
    assert LogSettings(_env_file=None).level == "DEBUG"


def test_web_settings_cors_defaults(monkeypatch):
    """CORS 默认值：开发期放开；生产环境经 WEB_CORS_ORIGINS 收敛为白名单。"""
    monkeypatch.delenv("WEB_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("WEB_CORS_EXPOSE_HEADERS", raising=False)
    settings = WebSettings(_env_file=None)
    assert settings.cors_origins == ["*"]
    assert settings.cors_expose_headers == ["*"]
