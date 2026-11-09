"""错误码表：异常类型 → (业务错误码, HTTP 状态码, 默认消息)。

- ERROR_MAP 是错误码的唯一事实源：新增领域异常必须在此注册，否则按 MRO 回退到
  最近已注册的父类（最终兜底 Exception → 500001）；
- PROTOCOL_ERROR_MAP 覆盖框架层协议错误（路径/方法/媒体类型），code 用 HTTP 状态码字符串。
"""
from fastapi.exceptions import RequestValidationError

from app.exception.bad_request import BadRequestException
from app.exception.config_center import ConfigCenterException
from app.exception.conflict import ConflictException, TurnTerminalException
from app.exception.database import DatabaseException
from app.exception.not_found import NotFoundException

ERROR_MAP: dict[type, tuple[str, int, str]] = {
    # 校验失败（api.md §0 400001「请求参数错误」：query 为空/超 2000 字、枚举非法等）
    RequestValidationError: ("400001", 400, "请求参数错误"),
    # 运行期参数校验失败（TurnRunner 模型解析等确定性拒绝——同一 400001 错误码域）
    BadRequestException: ("400001", 400, "请求参数错误"),
    # 数据库故障=内部错误（api.md §0 500001；422001 让位给「URL 非法」M3 口径，本批不实现 KB）
    DatabaseException: ("500001", 500, "内部错误"),
    NotFoundException: ("404001", 404, "资源不存在"),
    # REQ-042 同线程并发策略（api.md §0 / ARCHITECTURE ADR-010）
    ConflictException: ("409001", 409, "线程已有进行中轮次"),
    TurnTerminalException: ("409002", 409, "轮次已处于终态，不可取消"),
    ConfigCenterException: ("503001", 503, "配置中心不可达且无缓存"),
    Exception: ("500001", 500, "服务器内部错误"),
}

PROTOCOL_ERROR_MAP: dict[int, str] = {
    404: "路径不存在",
    405: "请求方法不允许",
    415: "不支持的媒体类型",
}
