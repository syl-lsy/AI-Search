"""unit 层：core/config.py 分组配置（ARCH §7 变量清单的测试侧镜像，零网络）。

覆盖面（与 .env.example / ARCH §7 一一对应）：
- 分组隔离：前缀同名后缀互不串扰；MCP 凭据组无前缀、字段名=变量名精确命中；
- 精确变量名命中：每组的全部字段逐一以真实变量名注入验证；
- 默认值：与 .env.example 非密钥值一致（密钥默认空）；
- compose 消费型变量不声明：NACOS_AUTH_* 四变量、MINIO_ROOT_*、LITELLM_MASTER_KEY +
  LiteLLM DB 模式四变量、Langfuse 容器侧五件套 + AI_API_KEY——注入对应容器、
  应用不读取（ARCH §7 例外清单），任何组不得声明（pydantic-settings 对未声明
  env 按 extra="ignore" 忽略，防「按前缀全量声明」回归）；
- SecretStr 不泄漏：repr / str / model_dump(json) 全部打码，取值须显式
  get_secret_value()；
- 校验失败路径：端口范围、URL scheme（仅 http/https，SSRF 面闸口）、host:port
  形态、数值下限、KB_CHUNK_OVERLAP < KB_CHUNK_SIZE。

所有实例 `_env_file=None`（跳过仓库根 .env，只看进程环境变量）；断言默认值的
变量先 monkeypatch.delenv（进程环境如 conftest 的 LOG_FILE_DIR 不干扰）。
测试值一律为无意义假值，禁止写入任何真实凭据。
"""

import pytest
from pydantic import ValidationError

from app.core.config import (
    CommonSettings,
    DBSettings,
    KbSettings,
    LangfuseSettings,
    LitellmSettings,
    LogSettings,
    McpCredentialSettings,
    MinioSettings,
    NacosSettings,
    SandboxSettings,
    SseSettings,
)

# ---- 通用组（无前缀）----


def test_common_defaults(monkeypatch):
    """通用组默认值 = .env.example 非密钥值（dev / 127.0.0.1:8300 / 本地双源 CORS）。"""
    for key in (
        "APP_ENV",
        "API_HOST",
        "API_PORT",
        "CORS_ORIGINS",
        "DEFAULT_USER_ID",
        "DEFAULT_USER_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = CommonSettings(_env_file=None)
    assert settings.app_env == "dev"
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8300
    assert settings.cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    assert settings.default_user_id == "00000000-0000-4000-8000-000000000001"
    assert settings.default_user_name == "Local"


def test_common_reads_exact_env_names(monkeypatch):
    """通用组精确变量名命中：六个变量逐一映射到对应字段。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_HOST", "127.0.0.2")
    monkeypatch.setenv("API_PORT", "9000")
    monkeypatch.setenv("DEFAULT_USER_ID", "00000000-0000-4000-8000-000000000002")
    monkeypatch.setenv("DEFAULT_USER_NAME", "Tester")
    settings = CommonSettings(_env_file=None)
    assert settings.app_env == "production"
    assert settings.api_host == "127.0.0.2"
    assert settings.api_port == 9000
    assert settings.default_user_id == "00000000-0000-4000-8000-000000000002"
    assert settings.default_user_name == "Tester"


def test_cors_origins_comma_split_and_strip(monkeypatch):
    """CORS_ORIGINS 逗号分隔解析（非 JSON）：空段与首尾空格剔除。"""
    monkeypatch.setenv(
        "CORS_ORIGINS", " https://a.example , https://b.example ,,https://c.example"
    )
    settings = CommonSettings(_env_file=None)
    assert settings.cors_origins == [
        "https://a.example",
        "https://b.example",
        "https://c.example",
    ]


def test_cors_origins_rejects_non_http_origin(monkeypatch):
    """CORS 白名单每项都必须是 http/https URL（SSRF 面闸口：scheme 收紧）。"""
    monkeypatch.setenv("CORS_ORIGINS", "https://ok.example,ftp://bad.example")
    with pytest.raises(ValidationError):
        CommonSettings(_env_file=None)


def test_app_env_rejects_unknown_value(monkeypatch):
    """APP_ENV 只接受 dev | production（.env.example 注释的封闭集合）。"""
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(ValidationError):
        CommonSettings(_env_file=None)


def test_api_port_out_of_range_rejected(monkeypatch):
    monkeypatch.setenv("API_PORT", "65536")
    with pytest.raises(ValidationError):
        CommonSettings(_env_file=None)


def test_api_port_non_numeric_rejected(monkeypatch):
    monkeypatch.setenv("API_PORT", "eighty-three-hundred")
    with pytest.raises(ValidationError):
        CommonSettings(_env_file=None)


# ---- DB_ 组 ----


def test_db_reads_exact_env_names(monkeypatch):
    """DB_ 组七个变量逐一命中；密码收 SecretStr；端口收敛为 int。"""
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PORT", "15433")
    monkeypatch.setenv("DB_USER", "other_user")
    monkeypatch.setenv("DB_PASSWORD", "db-pass-dummy")
    monkeypatch.setenv("DB_NAME", "other_db")
    monkeypatch.setenv("DB_POOL_SIZE", "20")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "7")
    settings = DBSettings(_env_file=None)
    assert settings.host == "db.internal"
    assert settings.port == 15433
    assert settings.user == "other_user"
    assert settings.password.get_secret_value() == "db-pass-dummy"
    assert settings.name == "other_db"
    assert settings.pool_size == 20
    assert settings.max_overflow == 7


def test_db_defaults(monkeypatch):
    """DB_ 组默认值 = .env.example 非密钥值（127.0.0.1:15432 / ai_search / 10+5）。"""
    for key in (
        "DB_HOST",
        "DB_PORT",
        "DB_USER",
        "DB_PASSWORD",
        "DB_NAME",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = DBSettings(_env_file=None)
    assert settings.host == "127.0.0.1"
    assert settings.port == 15432
    assert settings.user == "ai_search"
    assert settings.password.get_secret_value() == ""
    assert settings.name == "ai_search"
    assert settings.pool_size == 10
    assert settings.max_overflow == 5


def test_db_pool_size_lower_bound(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    with pytest.raises(ValidationError):
        DBSettings(_env_file=None)


# ---- LOG_ 组 ----


def test_log_defaults(monkeypatch):
    """LOG_ 组默认值：INFO / 200ms 慢查询阈值 / logs 目录（conftest 的 LOG_FILE_DIR 先剔除）。"""
    for key in ("LOG_LEVEL", "LOG_SLOW_QUERY_THRESHOLD", "LOG_FILE_DIR"):
        monkeypatch.delenv(key, raising=False)
    settings = LogSettings(_env_file=None)
    assert settings.level == "INFO"
    assert settings.slow_query_threshold == 200
    assert settings.file_dir == "logs"


def test_log_reads_exact_env_names(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_SLOW_QUERY_THRESHOLD", "500")
    monkeypatch.setenv("LOG_FILE_DIR", "var/logs")
    settings = LogSettings(_env_file=None)
    assert settings.level == "DEBUG"
    assert settings.slow_query_threshold == 500
    assert settings.file_dir == "var/logs"


def test_log_slow_query_threshold_non_negative(monkeypatch):
    monkeypatch.setenv("LOG_SLOW_QUERY_THRESHOLD", "-1")
    with pytest.raises(ValidationError):
        LogSettings(_env_file=None)


# ---- MINIO_ 组（root 凭据为 compose 消费型，不声明）----


def test_minio_reads_exact_env_names(monkeypatch):
    """MINIO_ 组应用读六变量逐一命中（endpoint/access_key/secret_key/secure/两桶名）。"""
    monkeypatch.setenv("MINIO_ENDPOINT", "minio.internal:9100")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "sa-key-dummy")
    monkeypatch.setenv("MINIO_SECRET_KEY", "sa-secret-dummy")
    monkeypatch.setenv("MINIO_SECURE", "true")
    monkeypatch.setenv("MINIO_BUCKET_SNAPSHOT", "other-snapshots")
    monkeypatch.setenv("MINIO_BUCKET_KB", "other-kb")
    settings = MinioSettings(_env_file=None)
    assert settings.endpoint == "minio.internal:9100"
    assert settings.access_key.get_secret_value() == "sa-key-dummy"
    assert settings.secret_key.get_secret_value() == "sa-secret-dummy"
    assert settings.secure is True
    assert settings.bucket_snapshot == "other-snapshots"
    assert settings.bucket_kb == "other-kb"


def test_minio_defaults(monkeypatch):
    for key in (
        "MINIO_ENDPOINT",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "MINIO_SECURE",
        "MINIO_BUCKET_SNAPSHOT",
        "MINIO_BUCKET_KB",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = MinioSettings(_env_file=None)
    assert settings.endpoint == "127.0.0.1:9100"
    assert settings.access_key.get_secret_value() == ""
    assert settings.secret_key.get_secret_value() == ""
    assert settings.secure is False
    assert settings.bucket_snapshot == "ai-search-snapshots"
    assert settings.bucket_kb == "ai-search-kb"


def test_minio_endpoint_requires_host_port_form(monkeypatch):
    """MINIO_ENDPOINT 是 host:port 形态（minio SDK 口径，无 scheme）。"""
    monkeypatch.setenv("MINIO_ENDPOINT", "http://127.0.0.1:9100")
    with pytest.raises(ValidationError):
        MinioSettings(_env_file=None)


# ---- NACOS_ 组（NACOS_AUTH_* 为 compose 消费型，严禁声明——ARCH §7 组头注）----


def test_nacos_reads_exact_env_names(monkeypatch):
    """NACOS_ 组仅七个应用读变量逐一命中；密码收 SecretStr。"""
    monkeypatch.setenv("NACOS_SERVER_ADDR", "nacos.internal:8849")
    monkeypatch.setenv("NACOS_NAMESPACE", "ai-search-test")
    monkeypatch.setenv("NACOS_GROUP", "other-group")
    monkeypatch.setenv("NACOS_USERNAME", "ro-account-dummy")
    monkeypatch.setenv("NACOS_PASSWORD", "nacos-pass-dummy")
    monkeypatch.setenv("NACOS_STARTUP_TIMEOUT_S", "9")
    monkeypatch.setenv("NACOS_SNAPSHOT_DIR", "var/nacos_snapshot")
    settings = NacosSettings(_env_file=None)
    assert settings.server_addr == "nacos.internal:8849"
    assert settings.namespace == "ai-search-test"
    assert settings.group == "other-group"
    assert settings.username == "ro-account-dummy"
    assert settings.password.get_secret_value() == "nacos-pass-dummy"
    assert settings.startup_timeout_s == 9
    assert settings.snapshot_dir == "var/nacos_snapshot"


def test_nacos_auth_variables_not_declared(monkeypatch):
    """NACOS_AUTH_* 四变量同时存在：不被声明、不进 model_fields、实例化不报错。

    它们是 nacos 容器鉴权参数（compose 消费型，注入容器开启鉴权），应用
    core/config.py 不读取；勿按前缀全量声明进本组（ARCH §7 组头注）。
    """
    monkeypatch.setenv("NACOS_AUTH_ENABLE", "true")
    monkeypatch.setenv("NACOS_AUTH_TOKEN", "compose-side-only")
    monkeypatch.setenv("NACOS_AUTH_IDENTITY_KEY", "nacos-identity")
    monkeypatch.setenv("NACOS_AUTH_IDENTITY_VALUE", "compose-side-only")
    settings = NacosSettings(_env_file=None)
    for banned in (
        "auth_enable",
        "auth_token",
        "auth_identity_key",
        "auth_identity_value",
    ):
        assert banned not in NacosSettings.model_fields
        assert not hasattr(settings, banned)
    # 组自身字段照常读取（鉴权参数存在不干扰应用侧只读账号字段）
    assert settings.username == ""


def test_nacos_server_addr_rejects_scheme_prefix(monkeypatch):
    """NACOS_SERVER_ADDR 必须 host:port（SDK 口径：不带 http:// 前缀与尾斜杠，§7 v1.13 注）。"""
    monkeypatch.setenv("NACOS_SERVER_ADDR", "http://127.0.0.1:8849")
    with pytest.raises(ValidationError):
        NacosSettings(_env_file=None)


def test_nacos_startup_timeout_lower_bound(monkeypatch):
    monkeypatch.setenv("NACOS_STARTUP_TIMEOUT_S", "0")
    with pytest.raises(ValidationError):
        NacosSettings(_env_file=None)


# ---- LITELLM_ 组（master_key 与 DB 模式四变量为 compose 消费型，不声明——ADR-014）----


def test_litellm_reads_exact_env_names(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "http://gateway.internal:4100")
    monkeypatch.setenv("LITELLM_API_KEY", "gateway-key-dummy")
    settings = LitellmSettings(_env_file=None)
    assert settings.base_url == "http://gateway.internal:4100"
    assert settings.api_key.get_secret_value() == "gateway-key-dummy"


def test_litellm_defaults(monkeypatch):
    for key in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = LitellmSettings(_env_file=None)
    assert settings.base_url == "http://127.0.0.1:4100"
    assert settings.api_key.get_secret_value() == ""


def test_litellm_base_url_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "ftp://127.0.0.1:4100")
    with pytest.raises(ValidationError):
        LitellmSettings(_env_file=None)


# ---- MCP 凭据组（无统一前缀：字段名与环境变量名一一对应）----


def test_mcp_group_reads_exact_field_names(monkeypatch):
    """MCP 凭据组不设 env_prefix：十三个字段名=变量名，逐一精确命中。"""
    env = {
        "mcp_tavily_url": "https://mcp.tavily.test/mcp",
        "tavily_api_key": "tvly-dummy",
        "bocha_api_base": "https://api.bocha.test/v1",
        "bocha_api_key": "bocha-dummy",
        "bocha_mcp_transport": "http",
        "bocha_mcp_command": "uvx",
        "bocha_mcp_args": "bocha-search-mcp",
        "firecrawl_mcp_url": "https://mcp.firecrawl.test/mcp",
        "firecrawl_api_key": "fc-dummy",
        "mcp_anysearch_url": "https://api.anysearch.test/mcp",
        "anysearch_api_key": "as_sk-dummy",
        "mcp_crawl4ai_url": "http://127.0.0.1:11235/mcp/sse",
        "crawl4ai_api_token": "crawl4ai-dummy",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = McpCredentialSettings(_env_file=None)
    assert settings.mcp_tavily_url == "https://mcp.tavily.test/mcp"
    assert settings.tavily_api_key.get_secret_value() == "tvly-dummy"
    assert settings.bocha_api_base == "https://api.bocha.test/v1"
    assert settings.bocha_api_key.get_secret_value() == "bocha-dummy"
    assert settings.bocha_mcp_transport == "http"
    assert settings.bocha_mcp_command == "uvx"
    assert settings.bocha_mcp_args == "bocha-search-mcp"
    assert settings.firecrawl_mcp_url == "https://mcp.firecrawl.test/mcp"
    assert settings.firecrawl_api_key.get_secret_value() == "fc-dummy"
    assert settings.mcp_anysearch_url == "https://api.anysearch.test/mcp"
    assert settings.anysearch_api_key.get_secret_value() == "as_sk-dummy"
    assert settings.mcp_crawl4ai_url == "http://127.0.0.1:11235/mcp/sse"
    assert settings.crawl4ai_api_token.get_secret_value() == "crawl4ai-dummy"


def test_mcp_group_without_prefix_stays_untouched_by_wrong_names(monkeypatch):
    """无前缀组只认字段名本身：缺 MCP_ 前缀的同名变量不命中。"""
    monkeypatch.delenv("mcp_tavily_url", raising=False)
    monkeypatch.setenv("TAVILY_URL", "https://wrong.example/mcp")  # 少了 MCP_ 前缀
    settings = McpCredentialSettings(_env_file=None)
    assert settings.mcp_tavily_url == "https://mcp.tavily.com/mcp"  # 默认值未被动


def test_mcp_group_url_fields_require_http_scheme(monkeypatch):
    monkeypatch.setenv("MCP_TAVILY_URL", "mcp.tavily.com/mcp")  # 无 scheme
    with pytest.raises(ValidationError):
        McpCredentialSettings(_env_file=None)


def test_mcp_group_defaults(monkeypatch):
    """MCP 组默认值与 .env.example 一致（密钥默认空；stdio 三件套为 R1 实测收敛值）。"""
    for name in (
        "mcp_tavily_url",
        "tavily_api_key",
        "bocha_api_base",
        "bocha_api_key",
        "bocha_mcp_transport",
        "bocha_mcp_command",
        "bocha_mcp_args",
        "firecrawl_mcp_url",
        "firecrawl_api_key",
        "mcp_anysearch_url",
        "anysearch_api_key",
        "mcp_crawl4ai_url",
        "crawl4ai_api_token",
    ):
        # 大写一并清：pydantic-settings 大小写不敏感，os.environ 里的 TAVILY_API_KEY
        # 等真值会盖掉默认值（外层 shell 导出或其他层 conftest 注入都算）
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    settings = McpCredentialSettings(_env_file=None)
    assert settings.mcp_tavily_url == "https://mcp.tavily.com/mcp"
    assert settings.bocha_api_base == "https://api.bochaai.com/v1"
    assert settings.bocha_mcp_transport == "stdio"
    assert settings.bocha_mcp_command == "uvx"
    assert settings.bocha_mcp_args == "bocha-search-mcp"
    assert settings.firecrawl_mcp_url == "https://mcp.firecrawl.dev/mcp"
    assert settings.mcp_anysearch_url == "https://api.anysearch.com/mcp"
    assert settings.mcp_crawl4ai_url == "http://127.0.0.1:11235/mcp/sse"
    assert settings.tavily_api_key.get_secret_value() == ""
    assert settings.crawl4ai_api_token.get_secret_value() == ""


# ---- LANGFUSE_ 组（容器侧五件套 + AI_API_KEY 为 compose 消费型，不声明）----


def test_langfuse_reads_exact_env_names(monkeypatch):
    """LANGFUSE_ 组六个应用读变量逐一命中；public_key 是公开半钥、不上 SecretStr。"""
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse.internal:3100")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-dummy")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-dummy")
    monkeypatch.setenv("LANGFUSE_TRACE_RETENTION_DAYS", "14")
    monkeypatch.setenv("LANGFUSE_WEBHOOK_ALERT_SECRET", "alert-dummy")
    monkeypatch.setenv("LANGFUSE_WEBHOOK_EXPERIMENT_SECRET", "experiment-dummy")
    settings = LangfuseSettings(_env_file=None)
    assert settings.host == "http://langfuse.internal:3100"
    assert settings.public_key == "pk-lf-dummy"  # 项目公开标识（客户端可见），非密钥
    assert settings.secret_key.get_secret_value() == "sk-lf-dummy"
    assert settings.trace_retention_days == 14
    assert settings.webhook_alert_secret.get_secret_value() == "alert-dummy"
    assert settings.webhook_experiment_secret.get_secret_value() == "experiment-dummy"


def test_langfuse_defaults(monkeypatch):
    for key in (
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_TRACE_RETENTION_DAYS",
        "LANGFUSE_WEBHOOK_ALERT_SECRET",
        "LANGFUSE_WEBHOOK_EXPERIMENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = LangfuseSettings(_env_file=None)
    assert settings.host == "http://127.0.0.1:3100"
    assert settings.public_key == ""
    assert settings.trace_retention_days == 7
    assert settings.secret_key.get_secret_value() == ""
    assert settings.webhook_alert_secret.get_secret_value() == ""
    assert settings.webhook_experiment_secret.get_secret_value() == ""


def test_langfuse_host_requires_http_scheme(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "127.0.0.1:3100")  # 无 scheme
    with pytest.raises(ValidationError):
        LangfuseSettings(_env_file=None)


# ---- SANDBOX_ / SSE_ / KB_ 组 ----


def test_sandbox_reads_exact_env_names_and_defaults(monkeypatch):
    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_API_BASE_URL", "http://sandbox.internal:8801")
    monkeypatch.setenv("SANDBOX_ARTIFACTS_DIR", "var/sandbox")
    settings = SandboxSettings(_env_file=None)
    assert settings.enabled is True
    assert settings.api_base_url == "http://sandbox.internal:8801"
    assert settings.artifacts_dir == "var/sandbox"

    for key in ("SANDBOX_ENABLED", "SANDBOX_API_BASE_URL", "SANDBOX_ARTIFACTS_DIR"):
        monkeypatch.delenv(key, raising=False)
    defaults = SandboxSettings(_env_file=None)
    assert defaults.enabled is False  # M4 前默认关闭（实验功能硬口径）
    assert defaults.api_base_url == "http://127.0.0.1:8801"
    assert defaults.artifacts_dir == "./data/sandbox"


def test_sse_reads_exact_env_names_and_defaults(monkeypatch):
    monkeypatch.setenv("SSE_EVENT_TTL_MINUTES", "60")
    monkeypatch.setenv("SSE_HEARTBEAT_SECONDS", "10")
    monkeypatch.setenv("SSE_BUFFER_MAX_EVENTS", "40000")
    settings = SseSettings(_env_file=None)
    assert settings.event_ttl_minutes == 60
    assert settings.heartbeat_seconds == 10
    assert settings.buffer_max_events == 40000

    for key in (
        "SSE_EVENT_TTL_MINUTES",
        "SSE_HEARTBEAT_SECONDS",
        "SSE_BUFFER_MAX_EVENTS",
    ):
        monkeypatch.delenv(key, raising=False)
    defaults = SseSettings(_env_file=None)
    assert defaults.event_ttl_minutes == 30
    assert defaults.heartbeat_seconds == 15
    assert defaults.buffer_max_events == 20000


def test_sse_values_have_lower_bounds(monkeypatch):
    monkeypatch.setenv("SSE_HEARTBEAT_SECONDS", "0")
    with pytest.raises(ValidationError):
        SseSettings(_env_file=None)


def test_kb_reads_exact_env_names(monkeypatch):
    monkeypatch.setenv("KB_MAX_FILE_MB", "50")
    monkeypatch.setenv("KB_ALLOWED_TYPES", "pdf,md")
    monkeypatch.setenv("KB_EMBEDDING_DIM", "768")
    monkeypatch.setenv("KB_CHUNK_SIZE", "500")
    monkeypatch.setenv("KB_CHUNK_OVERLAP", "50")
    monkeypatch.setenv("KB_TOP_K_PER_QUERY", "3")
    settings = KbSettings(_env_file=None)
    assert settings.max_file_mb == 50
    assert settings.allowed_types == ["pdf", "md"]
    assert settings.embedding_dim == 768
    assert settings.chunk_size == 500
    assert settings.chunk_overlap == 50
    assert settings.top_k_per_query == 3


def test_kb_defaults(monkeypatch):
    for key in (
        "KB_MAX_FILE_MB",
        "KB_ALLOWED_TYPES",
        "KB_EMBEDDING_DIM",
        "KB_CHUNK_SIZE",
        "KB_CHUNK_OVERLAP",
        "KB_TOP_K_PER_QUERY",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = KbSettings(_env_file=None)
    assert settings.max_file_mb == 20
    assert settings.allowed_types == ["pdf", "md", "txt", "docx"]
    assert settings.embedding_dim == 1024
    assert settings.chunk_size == 800
    assert settings.chunk_overlap == 120
    assert settings.top_k_per_query == 8


def test_kb_chunk_overlap_must_be_less_than_chunk_size(monkeypatch):
    """切片窗口不变量：overlap ≥ size 会产生死循环窗口，配置层直接拒绝。"""
    monkeypatch.setenv("KB_CHUNK_SIZE", "100")
    monkeypatch.setenv("KB_CHUNK_OVERLAP", "100")  # 相等同样非法（≥ 即拒）
    with pytest.raises(ValidationError):
        KbSettings(_env_file=None)


def test_kb_top_k_lower_bound(monkeypatch):
    monkeypatch.setenv("KB_TOP_K_PER_QUERY", "0")
    with pytest.raises(ValidationError):
        KbSettings(_env_file=None)


# ---- compose 消费型变量：任何组都不得声明（ARCH §7 例外清单全量巡检）----


@pytest.mark.parametrize(
    ("settings_cls", "forbidden_fields"),
    [
        (
            NacosSettings,
            ("auth_enable", "auth_token", "auth_identity_key", "auth_identity_value"),
        ),
        (MinioSettings, ("root_user", "root_password")),
        (
            LitellmSettings,
            ("master_key", "salt_key", "ui_username", "ui_password", "db_password"),
        ),
        (
            LangfuseSettings,
            (
                "salt",
                "encryption_key",
                "nextauth_secret",
                "db_password",
                "ai_api_key",
            ),
        ),
        (SandboxSettings, ("api_key",)),  # OPEN_SANDBOX_API_KEY：无 SANDBOX_ 前缀可命中
    ],
)
def test_compose_consumed_variables_not_declared(settings_cls, forbidden_fields, monkeypatch):
    """容器侧变量塞入进程环境：组不得声明这些字段（防「按前缀全量声明」回归）。

    变量名按「组前缀 + 字段大写」拼出（如 LANGFUSE_ + DB_PASSWORD）——若组误声明，
    实例化即命中真实值；此处按 model_fields 白名单断言兜住。
    """
    env_prefix = settings_cls.model_config.get("env_prefix", "")
    for field in forbidden_fields:
        monkeypatch.setenv(env_prefix + field.upper(), "compose-side-only")
        assert field not in settings_cls.model_fields
    settings_cls(_env_file=None)  # 未声明变量按 extra="ignore" 吞掉，不报错
    for field in forbidden_fields:
        assert field not in settings_cls.model_fields


# ---- SecretStr 不泄漏 ----


def test_secret_fields_never_leak_via_repr_or_str(monkeypatch):
    """repr / str / model_dump(json) 全打码：密钥明文只经 get_secret_value() 显式取出。"""
    monkeypatch.setenv("DB_PASSWORD", "super-secret-db")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-super-secret")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-super-secret")
    db = DBSettings(_env_file=None)
    mcp = McpCredentialSettings(_env_file=None)
    langfuse = LangfuseSettings(_env_file=None)

    for settings in (db, mcp, langfuse):
        rendered = repr(settings) + str(settings) + repr(settings.model_dump(mode="json"))
        for secret in ("super-secret-db", "tvly-super-secret", "sk-lf-super-secret"):
            assert secret not in rendered
        # 打码占位符在位（pydantic SecretStr 约定输出 **********）
        assert "**********" in rendered

    assert db.password.get_secret_value() == "super-secret-db"
    assert mcp.tavily_api_key.get_secret_value() == "tvly-super-secret"
    assert langfuse.secret_key.get_secret_value() == "sk-lf-super-secret"


# ---- 分组隔离（前缀同名后缀互不串扰）----


def test_prefix_isolation_across_groups(monkeypatch):
    """同名后缀不串组：DB_GROUP 不进 NACOS 组、NACOS_USER 不进 DB 组、MINIO_NAME 无效。"""
    monkeypatch.setenv("DB_GROUP", "should-not-leak")
    monkeypatch.setenv("NACOS_USER", "should-not-leak")
    monkeypatch.setenv("MINIO_NAME", "should-not-leak")
    assert NacosSettings(_env_file=None).group == "ai-search"
    assert DBSettings(_env_file=None).user == "ai_search"
    assert MinioSettings(_env_file=None).bucket_kb == "ai-search-kb"


def test_db_password_does_not_leak_into_other_groups(monkeypatch):
    """密钥同名后缀隔离：DB_PASSWORD 只进 DB 组，其他组密码字段保持默认空。"""
    # 被断言的密钥字段先清环境（大小写都清——pydantic-settings 大小写不敏感）：
    # 外层 shell 导出或其他层 conftest 注入的真值会把「默认空」断言误杀
    for name in ("NACOS_PASSWORD", "MINIO_SECRET_KEY", "LITELLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("DB_PASSWORD", "db-only-secret")
    assert NacosSettings(_env_file=None).password.get_secret_value() == ""
    assert MinioSettings(_env_file=None).secret_key.get_secret_value() == ""
    assert LitellmSettings(_env_file=None).api_key.get_secret_value() == ""
    assert DBSettings(_env_file=None).password.get_secret_value() == "db-only-secret"


# ---- 模块级单例 ----


def test_module_singletons_instantiated():
    """十一个分组单例随模块导入即实例化（业务代码 import 即用，无第二实例化路径）。"""
    from app.core import config as config_module

    pairs = (
        ("common_settings", CommonSettings),
        ("db_settings", DBSettings),
        ("log_settings", LogSettings),
        ("minio_settings", MinioSettings),
        ("nacos_settings", NacosSettings),
        ("litellm_settings", LitellmSettings),
        ("mcp_settings", McpCredentialSettings),
        ("langfuse_settings", LangfuseSettings),
        ("sandbox_settings", SandboxSettings),
        ("sse_settings", SseSettings),
        ("kb_settings", KbSettings),
    )
    for name, cls in pairs:
        assert isinstance(getattr(config_module, name), cls), name
