"""配置中心：BaseSettingsWithEnv 基类 + 按前缀分组子类与模块级单例。

组划分与 docs/ARCHITECTURE.md §7 .env 变量清单一一对应：无前缀=通用组，
DB_/MINIO_/NACOS_/LITELLM_/MCP 凭据组（无前缀）/LANGFUSE_/SANDBOX_/SSE_/KB_/LOG_
各一组；`db_settings` 等单例全局读取，业务代码禁止另建 Settings 实例重复读环境。

compose 消费型变量**不在此声明**（注入对应容器、应用不读取，§7 例外清单）：
- NACOS_AUTH_* 四变量（注入 nacos 容器开启鉴权）；
- LITELLM_MASTER_KEY + LiteLLM DB 模式四变量 LITELLM_SALT_KEY/LITELLM_UI_USERNAME/
  LITELLM_UI_PASSWORD/LITELLM_DB_PASSWORD（注入 litellm 容器，ADR-014）；
- MINIO_ROOT_USER/MINIO_ROOT_PASSWORD（注入 minio 容器）、OPEN_SANDBOX_API_KEY、
  Langfuse 容器侧五件套与 AI_API_KEY（SALT/ENCRYPTION_KEY/NEXTAUTH_SECRET/
  DB_PASSWORD/AI_API_KEY）、ZHIPU_*/DASHSCOPE_*/OTEL_*/LANGSMITH_* 等上游组。
pydantic-settings 对未声明的环境变量按 extra="ignore" 忽略——勿按前缀全量声明进组
（单测 test_config.py 按 §7 例外清单逐组兜住）。

安全口径（项目通用纪律）：
- 密钥字段一律 SecretStr——repr/str/log/model_dump 全打码（**********），取值仅经
  get_secret_value()，且只在建立连接/拼请求头等必要出口处调用；
- 出站 URL 字段在配置层校验 scheme 必须 http/https（SSRF 面闸口）。出站请求目标
  仅取自部署配置（本文件各 *_BASE_URL/HOST），严禁以请求参数或用户输入为 URL 来源；
  第一方基建端点绑 127.0.0.1 属部署设计（ARCH §7），故此处不做内网地址拒绝。

配置三分法：改了要重启 → .env（本文件唯一读取处）；改了要不重启生效 → Nacos
（config_center，五 dataId 键位见 docs/contract/nacos-config.md）；与环境无关的
纯逻辑值 → 代码常量。密钥绝不入 Nacos。
"""
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AfterValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode


class BaseSettingsWithEnv(BaseSettings):
    # env_file 相对运行时 cwd 解析，所有命令统一从仓库根目录跑（Makefile 封装）
    # extra="ignore"：.env 中未被任何分组消费的变量（compose 消费型/上游上游组）不报错
    model_config = {"env_file": ".env", "extra": "ignore"}


# ---- 校验型注解（配置层是出站 URL 的唯一来源，scheme 在此收紧）----


def _validate_http_url(value: str) -> str:
    """http/https URL 校验；通过则原样返回（不归一化——保持 .env 原值供路径拼接）。"""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"必须是 http/https URL，得到：{value!r}")
    return value


def _validate_host_port(value: str) -> str:
    """host:port 形态校验（SDK 口径无 scheme，如 minio-py / nacos-server-addr）。"""
    host, sep, port = value.rpartition(":")
    if (
        not sep
        or not host
        or "/" in value
        or not port.isdigit()
        or not 1 <= int(port) <= 65535
    ):
        raise ValueError(f"必须是 host:port 形式（如 127.0.0.1:9100），得到：{value!r}")
    return value


HttpUrlStr = Annotated[str, AfterValidator(_validate_http_url)]
HostPortStr = Annotated[str, AfterValidator(_validate_host_port)]
Port = Annotated[int, Field(ge=1, le=65535)]


def _split_csv_list(value: object) -> object:
    """逗号分隔字符串 → 列表（剔除空段与首尾空格）；已是列表则原样放行。

    配合 NoDecode 使用：pydantic-settings 默认按 JSON 解析复杂类型，
    .env 约定为逗号分隔（CORS_ORIGINS / KB_ALLOWED_TYPES，§7），故关闭
    JSON 解析、在这里手工切分。
    """
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


# 通用配置（无前缀）：应用环境 / 服务绑定 / CORS 白名单 / MVP 默认用户
class CommonSettings(BaseSettingsWithEnv):
    app_env: Literal["dev", "production"] = "dev"  # production 隐藏 docs/redoc/openapi
    api_host: str = "127.0.0.1"  # 全部 host 端口绑 127.0.0.1（部署设计）
    api_port: Port = 8300
    cors_origins: Annotated[list[HttpUrlStr], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]  # 生产环境收敛为显式白名单（不接受 * 通配——每项都过 scheme 校验）
    default_user_id: str = "00000000-0000-4000-8000-000000000001"  # MVP 固定默认用户（启动幂等 seed）
    default_user_name: str = "Local"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        return _split_csv_list(value)


# 数据库配置（DB_）：PostgreSQL + pgvector，连接池参数默认 10/5（§3.1 db/session.py）
class DBSettings(BaseSettingsWithEnv):
    host: str = "127.0.0.1"
    port: Port = 15432  # 规避本机 5432（compose 宿主映射端口）
    user: str = "ai_search"
    password: SecretStr = SecretStr("")
    name: str = "ai_search"
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=5, ge=0)
    model_config = {"env_prefix": "DB_"}


# 日志配置（LOG_，见 app/core/logger/）
class LogSettings(BaseSettingsWithEnv):
    level: str = "INFO"  # 输出级别：TRACE / DEBUG / INFO / SUCCESS / WARNING / ERROR / CRITICAL
    file_dir: str = "logs"  # dev 环境 ERROR 日志目录（相对运行时 cwd，即仓库根）；生产只走 stdout
    slow_query_threshold: float = Field(default=200, ge=0)  # 毫秒：db/session.py 事件钩子慢查询阈值
    model_config = {"env_prefix": "LOG_"}


# MinIO 配置（MINIO_）：应用读六个——Service Account 凭据（DEV §3.2 ④，不使用 root 凭据）
class MinioSettings(BaseSettingsWithEnv):
    endpoint: HostPortStr = "127.0.0.1:9100"  # host:port 形态（minio SDK 无 scheme）
    access_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    secure: bool = False
    bucket_snapshot: str = "ai-search-snapshots"
    bucket_kb: str = "ai-search-kb"
    model_config = {"env_prefix": "MINIO_"}


# Nacos 配置（NACOS_）：运行态客户端用专用只读账号（读+监听、无写权限，nacos-config.md §8）。
# 不声明 NACOS_AUTH_* 四字段——compose 消费型（注入 nacos 容器开启鉴权），勿按前缀全量声明
class NacosSettings(BaseSettingsWithEnv):
    server_addr: HostPortStr = "127.0.0.1:8849"  # SDK 填 host:port（不带 http:// 前缀与尾斜杠）
    namespace: str = "ai-search-dev"
    group: str = "ai-search"
    username: str = ""  # 运行时必填（如 ai-search-ro）；默认空仅为无 .env 时可导入，连接时 fail-fast
    password: SecretStr = SecretStr("")
    startup_timeout_s: int = Field(default=5, ge=1)
    snapshot_dir: str = "./data/nacos_snapshot"
    model_config = {"env_prefix": "NACOS_"}


# LiteLLM 网关（LITELLM_）：chat/vision/embedding/rerank 全走这里；
# master_key 与 DB 模式四变量为 compose 消费型（注入 litellm 容器，ADR-014），不声明
class LitellmSettings(BaseSettingsWithEnv):
    base_url: HttpUrlStr = "http://127.0.0.1:4100"
    api_key: SecretStr = SecretStr("")  # 静态 config 模式下与 Master Key 同值（.env 侧对齐）
    model_config = {"env_prefix": "LITELLM_"}


# 搜索/读取 MCP 凭据组（无统一前缀：字段名与环境变量名一一对应）。
# 源清单/路由在 Nacos，凭证在此（§7 组头注）；五源 = 主位 tavily/bocha/firecrawl
# + 备位 anysearch（fallback_search）/ crawl4ai（url_reader_backup，R12）
class McpCredentialSettings(BaseSettingsWithEnv):
    mcp_tavily_url: HttpUrlStr = "https://mcp.tavily.com/mcp"
    tavily_api_key: SecretStr = SecretStr("")
    bocha_api_base: HttpUrlStr = "https://api.bochaai.com/v1"
    bocha_api_key: SecretStr = SecretStr("")
    bocha_mcp_transport: str = "stdio"  # 官方远程 MCP 可用后改 http（R1 锚定）
    bocha_mcp_command: str = "uvx"
    bocha_mcp_args: str = "bocha-search-mcp"  # R1 实测收敛；S1 装载时决定切分方式
    firecrawl_mcp_url: HttpUrlStr = "https://mcp.firecrawl.dev/mcp"
    firecrawl_api_key: SecretStr = SecretStr("")
    mcp_anysearch_url: HttpUrlStr = "https://api.anysearch.com/mcp"
    anysearch_api_key: SecretStr = SecretStr("")
    mcp_crawl4ai_url: HttpUrlStr = "http://127.0.0.1:11235/mcp/sse"
    crawl4ai_api_token: SecretStr = SecretStr("")


# Langfuse 配置（LANGFUSE_）：本服务读六个——观测出口与 webhook 验签密钥（M4）；
# 容器侧五件套 + AI_API_KEY 为 compose 消费型，不声明（§7 组注）
class LangfuseSettings(BaseSettingsWithEnv):
    host: HttpUrlStr = "http://127.0.0.1:3100"
    public_key: str = ""  # pk-lf-：项目公开标识（Langfuse 客户端可见半钥），非密钥不上 SecretStr
    secret_key: SecretStr = SecretStr("")
    trace_retention_days: int = Field(default=7, ge=1)  # crons/trace_cleanup.py 分批删除过期 trace 的依据
    webhook_alert_secret: SecretStr = SecretStr("")  # 空值=拒绝校验而非放行（M4 落地）
    webhook_experiment_secret: SecretStr = SecretStr("")
    model_config = {"env_prefix": "LANGFUSE_"}


# OpenSandbox 配置（SANDBOX_，M4）：仅连接类变量——运行参数归 Agent Spec 热调（§7）；
# OPEN_SANDBOX_API_KEY 为 compose 消费型，不声明
class SandboxSettings(BaseSettingsWithEnv):
    enabled: bool = False  # 实验功能硬口径：默认关闭
    api_base_url: HttpUrlStr = "http://127.0.0.1:8801"
    artifacts_dir: str = "./data/sandbox"
    model_config = {"env_prefix": "SANDBOX_"}


# SSE / 流式（SSE_）：事件 TTL 为滑动续期（每事件写入刷新，ADR-003 v1.7 增注）
class SseSettings(BaseSettingsWithEnv):
    event_ttl_minutes: int = Field(default=30, ge=1)
    heartbeat_seconds: int = Field(default=15, ge=1)
    buffer_max_events: int = Field(default=20000, ge=1)
    model_config = {"env_prefix": "SSE_"}


# 知识库（KB_）：embedding_dim 为 schema 常量，变更需迁移（§7 注）
class KbSettings(BaseSettingsWithEnv):
    max_file_mb: int = Field(default=20, ge=1)  # 上传白名单配套的 20MB 上限
    allowed_types: Annotated[list[str], NoDecode] = ["pdf", "md", "txt", "docx"]
    embedding_dim: int = Field(default=1024, ge=1)
    chunk_size: int = Field(default=800, ge=1)
    chunk_overlap: int = Field(default=120, ge=0)
    top_k_per_query: int = Field(default=8, ge=1)
    model_config = {"env_prefix": "KB_"}

    @field_validator("allowed_types", mode="before")
    @classmethod
    def _split_allowed_types(cls, value: object) -> object:
        return _split_csv_list(value)

    @model_validator(mode="after")
    def _check_chunk_overlap(self) -> "KbSettings":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("KB_CHUNK_OVERLAP 必须 < KB_CHUNK_SIZE（否则切片窗口不前进）")
        return self


# ---- 模块级单例：业务代码 import 即用（与分组一一对应）----

common_settings = CommonSettings()
db_settings = DBSettings()
log_settings = LogSettings()
minio_settings = MinioSettings()
nacos_settings = NacosSettings()
litellm_settings = LitellmSettings()
mcp_settings = McpCredentialSettings()
langfuse_settings = LangfuseSettings()
sandbox_settings = SandboxSettings()
sse_settings = SseSettings()
kb_settings = KbSettings()
