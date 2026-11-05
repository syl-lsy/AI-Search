from pydantic_settings import BaseSettings


class BaseSettingsWithEnv(BaseSettings):
    # env_file 相对运行时 cwd 解析，所有命令统一从仓库根目录跑（Makefile 封装）
    # extra="ignore"：.env 中未被任何分组消费的变量（如 OSS_*）不报错
    model_config = {"env_file": ".env", "extra": "ignore"}


# 通用配置（无前缀）
class CommonSettings(BaseSettingsWithEnv):
    environment: str = "development"


# Web 服务配置（WEB_ 前缀）
class WebSettings(BaseSettingsWithEnv):
    app_name: str = "Web Service API"  # 实际读取 WEB_APP_NAME
    cors_origins: list[str] = ["*"]  # 允许跨域来源（JSON 数组），生产环境收敛为白名单
    cors_expose_headers: list[str] = ["*"]  # 暴露给浏览器 JS 可读的响应头
    model_config = {"env_prefix": "WEB_"}


# 数据库配置（DB_ 前缀）
class DBSettings(BaseSettingsWithEnv):
    host: str = ""
    port: str = ""
    name: str = ""
    user: str = ""
    password: str = ""
    # 连接池参数（ARCH §3.1 db/session.py：DB_POOL_SIZE/DB_MAX_OVERFLOW，§7 默认 10/5）
    pool_size: int = 10
    max_overflow: int = 5
    model_config = {"env_prefix": "DB_"}


# 日志配置（LOG_ 前缀，见 app/core/logger/）
class LogSettings(BaseSettingsWithEnv):
    level: str = "INFO"  # 输出级别：TRACE / DEBUG / INFO / SUCCESS / WARNING / ERROR / CRITICAL
    slow_query_threshold: float = 200  # 毫秒：SQL 执行超过该阈值记 DBLog.warning（慢查询）
    file_dir: str = "logs"  # dev 环境 ERROR 日志目录（相对运行时 cwd，即仓库根）；生产只走 stdout
    model_config = {"env_prefix": "LOG_"}


common_settings = CommonSettings()
web_settings = WebSettings()
db_settings = DBSettings()
log_settings = LogSettings()
