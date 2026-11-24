"""错误码表：异常类型 → (业务错误码, HTTP 状态码, 默认消息)。

- ERROR_MAP 是错误码的唯一事实源：新增领域异常必须在此注册，否则按 MRO 回退到
  最近已注册的父类（最终兜底 Exception → 500001）；
- PROTOCOL_ERROR_MAP 覆盖框架层协议错误（路径/方法/媒体类型），code 用 HTTP 状态码字符串。
"""
from fastapi.exceptions import RequestValidationError

from app.exception.bad_request import BadRequestException
from app.exception.config_center import ConfigCenterException
from app.exception.conflict import (
    ConflictException,
    ThreadBusyException,
    TurnNotRegenerableException,
    TurnTerminalException,
)
from app.exception.database import DatabaseException
from app.exception.export import ExportNotAvailableException
from app.exception.feedback import FeedbackTurnNotFoundException
from app.exception.kb import (
    FileTooLargeException,
    FileUnsupportedTypeException,
    KbUrlInvalidException,
)
from app.exception.not_found import NotFoundException
from app.exception.snapshot import SnapshotNotFoundException

ERROR_MAP: dict[type, tuple[str, int, str]] = {
    # 校验失败（api.md §0 400001「请求参数错误」：query 为空/超 2000 字、枚举非法等）
    RequestValidationError: ("400001", 400, "请求参数错误"),
    # 运行期参数校验失败（TurnRunner 模型解析等确定性拒绝——同一 400001 错误码域）
    BadRequestException: ("400001", 400, "请求参数错误"),
    # 数据库故障=内部错误（api.md §0 500001；422001 让位给「URL 非法」M3 口径，本批不实现 KB）
    DatabaseException: ("500001", 500, "内部错误"),
    NotFoundException: ("404001", 404, "资源不存在"),
    # 该来源无快照（api.md §0 404002 行：快速模式来源、或深度来源未抓原文——REQ-015）
    SnapshotNotFoundException: ("404002", 404, "该来源无快照"),
    # 不满足导出条件（api.md §0 409003 行：非深度轮、或轮次未到 completed/degraded——REQ-016）
    ExportNotAvailableException: ("409003", 409, "不满足导出条件"),
    # 轮反馈端点专属（api.md §0 v1.16 增：语义同 404001 turn 维度，独立码便于反馈面
    # 埋点/客户端区分——轮被删或失效时的单独处置）
    FeedbackTurnNotFoundException: ("404004", 404, "轮次不存在"),
    # REQ-042 同线程并发策略（api.md §0 / ARCHITECTURE ADR-010）
    ConflictException: ("409001", 409, "线程已有进行中轮次"),
    TurnTerminalException: ("409002", 409, "轮次已处于终态，不可取消"),
    # 线程删除撞进行中轮（api.md §0 v1.18 增 / §2.11 / D-11：独立码不复用 409001）
    ThreadBusyException: ("409005", 409, "线程有进行中轮次，不可删除"),
    # regenerate 前置不满足（api.md §0 v1.19 增 / §2.12：单码覆盖非 {degraded,failed}
    # 终态 / 零来源 / deep 轮 / 进行中重复点击——沿 409005 独立码先例不拆码）
    TurnNotRegenerableException: ("409006", 409, "轮次不支持重新生成"),
    ConfigCenterException: ("503001", 503, "配置中心不可达且无缓存"),
    # KB 上传面（api.md §0 v1.30 M3 三码——§4.1 :527 / §4.2 :555）：
    # 413001 大小上限（KB_MAX_FILE_MB）/ 415001 类型白名单外（音视频同码，message
    # 提示「暂不支持」由异常方携带）/ 422001 URL 非法（scheme/解析/SSRF 拒收）
    FileTooLargeException: ("413001", 413, "文件超过大小上限"),
    FileUnsupportedTypeException: ("415001", 415, "不支持的文件类型"),
    KbUrlInvalidException: ("422001", 422, "URL 非法"),
    Exception: ("500001", 500, "服务器内部错误"),
}

PROTOCOL_ERROR_MAP: dict[int, str] = {
    404: "路径不存在",
    405: "请求方法不允许",
    415: "不支持的媒体类型",
}
