"""unit 层：统一异常处理器（app/exception/handler/）全分支覆盖。

覆盖点：
- `_lookup`：MRO 命中已注册类型 / 未注册子类回退 / 非 Exception 防御分支；
- `exception_handler` 直 await：协议错误（ERROR_MAP 与 PROTOCOL_ERROR_MAP 两路，
  含 detail 不可用时的归一）、领域异常、校验错误、未知异常兜底（哨兵断言敏感信息不泄漏）；
- 日志承诺（**每请求恰一条的异常侧出口**）：有 request_log 时走 loguru——先补
  status_code 再按级落（协议/校验/业务 warning，未知异常 error 带栈），业务异常 detail
  进 message、仅 original_exception 存在时带栈；request_log 缺失时回退标准库 logger，
  severity/message 口径一致；
- TestClient 走真实注册链路：`/nonexistent` 404、非法参数 400001、未捕获异常 500001、
  BusinessException 在默认 TestClient 下内联返回统一 JSON（不冒泡）；
  **404/422/500 各恰好一条对应级别日志、都没有 SUCCESS**（请求日志由中间件/处理器二选一）；
- `generate_error_docs()` 与 `app.description`（错误码表自动进文档）；
- 防漂移：`app.exception` 导出的领域异常必须已在 ERROR_MAP 注册。

直调用例的 request 用最小 stub（处理器只读 `request.state.request_log` 并写
`exception_handled` 标志）；loguru 捕获 fixture（log_records）见 test/unit/conftest.py。
"""
import json
import logging
from types import SimpleNamespace

from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

import app.exception as exception_pkg
from app.core.logger import RequestLog
from app.exception import (
    BusinessException,
    ConflictException,
    DatabaseException,
    NotFoundException,
    TurnTerminalException,
)
from app.exception.handler import exception_handler, generate_error_docs
from app.exception.handler.errors import ERROR_MAP
from app.exception.handler.handlers import _lookup
from app.main import app

HANDLER_LOGGER = "app.exception.handler.handlers"


def _request_with_log(request_log: RequestLog | None = None) -> SimpleNamespace:
    """最小 request stub：state.request_log 为 None 时覆盖标准库 logger 兜底路径。"""
    return SimpleNamespace(state=SimpleNamespace(request_log=request_log))


def _remove_test_route(path: str) -> None:
    """移除测试临时注册的路由，不污染其他用例。"""
    app.router.routes = [
        r for r in app.router.routes if getattr(r, "path", None) != path
    ]


def _records_at(records: list[dict], level: str) -> list[dict]:
    """按 loguru 级别名过滤捕获到的原始 record。"""
    return [r for r in records if r["level"].name == level]


# ---- _lookup：MRO 查表 ----

def test_lookup_hits_registered_type_exactly():
    assert _lookup(NotFoundException(message="x")) == ("404001", 404, "资源不存在")


def test_lookup_conflict_exceptions_use_409_codes():
    """REQ-042 同线程并发策略的 409 码：ConflictException=409001、TurnTerminalException=409002。"""
    assert _lookup(ConflictException(message="线程已有进行中轮次")) == (
        "409001",
        409,
        "线程已有进行中轮次",
    )
    assert _lookup(TurnTerminalException(message="轮次已处于终态，不可取消")) == (
        "409002",
        409,
        "轮次已处于终态，不可取消",
    )


def test_lookup_database_exception_maps_to_internal_error():
    """DatabaseException → 500001/500（api.md §0 错误码表「内部错误」；
    422001 让位给 api.md 46 行「URL 非法」M3 口径，本批不实现 KB）。"""
    assert _lookup(DatabaseException(message="unique violation")) == (
        "500001",
        500,
        "内部错误",
    )


def test_lookup_falls_back_to_nearest_registered_parent():
    """现场定义的未注册领域异常：MRO 里没有自己，回退到 Exception 兜底配置。"""

    class UnregisteredError(BusinessException):
        """未在 ERROR_MAP 注册的子类。"""

    assert _lookup(UnregisteredError(message="x")) == ERROR_MAP[Exception]
    assert ERROR_MAP[Exception] == ("500001", 500, "服务器内部错误")


def test_lookup_defensive_fallback_when_exception_not_in_mro():
    """防御分支：MRO 里连 Exception 都没有的对象，也返回兜底配置（_lookup 最后一行）。"""
    assert _lookup(object()) == ("500001", 500, "服务器内部错误")


def test_exported_domain_exceptions_are_registered_in_error_map():
    """防漂移：exception 包导出的领域异常（除基类外）必须已在 ERROR_MAP 注册。"""
    for name in exception_pkg.__all__:
        exc_type = getattr(exception_pkg, name)
        # 基类本身不映射错误码，由子类各自注册
        if exc_type is BusinessException:
            continue
        assert exc_type in ERROR_MAP, f"{name} 未在 ERROR_MAP 注册"


# ---- exception_handler：直 await 各分支的响应 ----

async def test_handler_protocol_error_uses_friendly_message():
    resp = await exception_handler(_request_with_log(), HTTPException(status_code=404))
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"code": "404", "message": "路径不存在"}


async def test_handler_protocol_error_falls_back_to_detail():
    resp = await exception_handler(
        _request_with_log(), HTTPException(status_code=418, detail="teapot")
    )
    assert resp.status_code == 418
    assert json.loads(resp.body) == {"code": "418", "message": "teapot"}


async def test_handler_protocol_error_falls_back_to_starlette_detail():
    """协议表未收录的状态码回退 detail：starlette 会把缺省 detail 归一为 HTTP 短语并原样透传。"""
    exc = HTTPException(status_code=400)
    resp = await exception_handler(_request_with_log(), exc)
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["code"] == "400"
    assert body["message"] == exc.detail == "Bad Request"


async def test_handler_protocol_error_with_unusable_detail_yields_empty_message():
    """防御分支：detail 为 None / 非字符串（下游改写或自定义子类）时归一为空串，不出现 null、不抛序列化错误。"""
    for bad_detail in (None, {"nested": "object"}):
        exc = HTTPException(status_code=400)
        exc.detail = bad_detail
        resp = await exception_handler(_request_with_log(), exc)
        assert json.loads(resp.body) == {"code": "400", "message": ""}


async def test_handler_business_exception_uses_message():
    exc = NotFoundException(message="商品不存在")
    resp = await exception_handler(_request_with_log(), exc)
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"code": "404001", "message": "商品不存在"}


async def test_handler_validation_error_formats_locations():
    error = {
        "loc": ("query", "n"),
        "msg": "Input should be a valid integer",
        "type": "int_parsing",
    }
    resp = await exception_handler(_request_with_log(), RequestValidationError([error]))
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["code"] == "400001"
    assert body["message"] == "query.n: Input should be a valid integer"


async def test_handler_conflict_exceptions_return_409():
    """REQ-042 新 409 领域异常经统一处理器出 {code, message}（409001/409002）。"""
    resp = await exception_handler(
        _request_with_log(), ConflictException(message="线程已有进行中轮次")
    )
    assert resp.status_code == 409
    assert json.loads(resp.body) == {"code": "409001", "message": "线程已有进行中轮次"}

    resp = await exception_handler(
        _request_with_log(), TurnTerminalException(message="轮次已处于终态，不可取消")
    )
    assert resp.status_code == 409
    assert json.loads(resp.body) == {
        "code": "409002",
        "message": "轮次已处于终态，不可取消",
    }


async def test_handler_unknown_exception_does_not_leak_internals():
    """哨兵断言：内部信息绝不进响应体。"""
    resp = await exception_handler(_request_with_log(), Exception("secret internal detail"))
    assert resp.status_code == 500
    body = json.loads(resp.body)
    assert body == {"code": "500001", "message": "服务器内部错误"}
    assert "secret" not in resp.body.decode()


# ---- 日志出口之一：有 request_log → loguru 单管线（补状态码 + 按级落）----

async def test_handler_protocol_error_logs_warning_via_request_log(log_records):
    log = RequestLog(message="请求完成", method="GET", path="/nope")
    resp = await exception_handler(_request_with_log(log), HTTPException(status_code=404))

    assert resp.status_code == 404
    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "请求异常: 路径不存在"
    assert record["extra"]["status_code"] == 404  # 处理器补上真实状态码
    assert record["extra"]["path"] == "/nope"  # 请求上下文（request_id/method/path）随记录带出
    assert record["exception"] is None  # 协议错误是预期控制流，不带栈


async def test_handler_validation_error_logs_warning_via_request_log(log_records):
    """新行为：校验失败（400001）此前不记日志，现经 request_log 落一条 warning。"""
    error = {
        "loc": ("query", "n"),
        "msg": "Input should be a valid integer",
        "type": "int_parsing",
    }
    log = RequestLog(message="请求完成")
    resp = await exception_handler(
        _request_with_log(log), RequestValidationError([error])
    )

    assert resp.status_code == 400
    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "请求异常: query.n: Input should be a valid integer"
    assert record["extra"]["status_code"] == 400
    assert record["exception"] is None


async def test_handler_business_exception_logs_detail_via_request_log(log_records):
    """detail 只进日志：同一哨兵值在日志里找得到、响应体里找不到；预期控制流走 WARNING 且不带栈。"""
    exc = DatabaseException(
        message="数据保存失败", detail="psycopg: unique_violation on orders_pkey"
    )
    resp = await exception_handler(_request_with_log(RequestLog(message="请求完成")), exc)

    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert (
        record["message"]
        == "业务异常: 数据保存失败 | detail=psycopg: unique_violation on orders_pkey"
    )
    assert record["exception"] is None  # 预期控制流不带栈，避免应用日志层再造噪音
    # DatabaseException → 500001/500（api.md「内部错误」；message 仍用异常自身面向用户的消息）
    assert json.loads(resp.body) == {"code": "500001", "message": "数据保存失败"}
    assert "unique_violation" not in resp.body.decode()


async def test_handler_business_exception_with_original_exception_logs_stack(log_records):
    """包装了基础设施故障（original_exception）时，WARNING 日志才附原始异常栈。"""
    exc = DatabaseException(
        message="数据保存失败",
        detail="unique_violation",
        original_exception=ValueError("db boom"),
    )
    await exception_handler(_request_with_log(RequestLog(message="请求完成")), exc)

    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "业务异常: 数据保存失败 | detail=unique_violation"
    assert record["exception"] is not None  # 根因（原始异常）带栈
    assert isinstance(record["exception"].value, ValueError)


async def test_handler_unknown_exception_logs_error_with_stack(log_records):
    """未知异常走 ERROR 且带栈（进日志），内部信息绝不进响应体。"""
    resp = await exception_handler(
        _request_with_log(RequestLog(message="请求完成")), Exception("secret internal detail")
    )

    record = log_records[-1]
    assert record["level"].name == "ERROR"
    assert record["message"] == "未处理异常: secret internal detail"
    assert record["exception"] is not None  # 未知异常必须带栈，供根因排查
    assert record["extra"]["status_code"] == 500
    assert "secret" not in resp.body.decode()


# ---- 日志出口之二：request_log 缺失 → 标准库 logger 兜底（同口径）----

async def test_handler_stdlib_fallback_warns_for_protocol_error(caplog):
    """不在中间件链内直调处理器（无 request_log）时回退标准库 logger，severity 口径一致。"""
    with caplog.at_level(logging.WARNING, logger=HANDLER_LOGGER):
        resp = await exception_handler(_request_with_log(), HTTPException(status_code=404))

    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is None
    assert "请求异常: 路径不存在" in caplog.text
    assert json.loads(resp.body) == {"code": "404", "message": "路径不存在"}


async def test_handler_stdlib_fallback_omits_empty_detail_suffix(caplog):
    """兜底路径的消息口径与 loguru 分支一致：detail 为空时不追加 " | detail=" 尾巴。"""
    with caplog.at_level(logging.WARNING, logger=HANDLER_LOGGER):
        await exception_handler(_request_with_log(), NotFoundException(message="商品不存在"))

    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert "业务异常: 商品不存在" in caplog.text
    assert "| detail=" not in caplog.text  # 条件拼接：detail 为空就没有尾巴


async def test_handler_stdlib_fallback_keeps_business_detail_out_of_body(caplog):
    """兜底路径同样只把 detail 写日志：哨兵在 caplog 里、不在响应体里。"""
    exc = DatabaseException(
        message="数据保存失败", detail="psycopg: unique_violation on orders_pkey"
    )
    with caplog.at_level(logging.WARNING, logger=HANDLER_LOGGER):
        resp = await exception_handler(_request_with_log(), exc)

    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is None
    assert "unique_violation on orders_pkey" in caplog.text
    assert "unique_violation" not in resp.body.decode()


async def test_handler_stdlib_fallback_logs_traceback_for_original_exception(caplog):
    """兜底路径：original_exception 存在时 warn 级也带原始异常栈。"""
    exc = DatabaseException(
        message="数据保存失败",
        detail="unique_violation",
        original_exception=ValueError("db boom"),
    )
    with caplog.at_level(logging.WARNING, logger=HANDLER_LOGGER):
        await exception_handler(_request_with_log(), exc)

    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is not None


async def test_handler_stdlib_fallback_errors_with_stack_for_unknown(caplog):
    with caplog.at_level(logging.ERROR, logger=HANDLER_LOGGER):
        resp = await exception_handler(_request_with_log(), Exception("boom"))

    records = [r for r in caplog.records if r.name == HANDLER_LOGGER]
    assert len(records) == 1
    assert records[0].levelname == "ERROR"
    assert records[0].exc_info is not None
    assert resp.status_code == 500


# ---- TestClient：走 main.py 的真实注册链路（响应 + 日志两条断言线）----

def test_nonexistent_path_returns_unified_error_body():
    resp = TestClient(app, raise_server_exceptions=False).get("/nonexistent")
    assert resp.status_code == 404
    assert resp.json() == {"code": "404", "message": "路径不存在"}


def test_client_404_logs_exactly_one_warning(log_records):
    """404：恰一条 warning（经 request_log），且没有 SUCCESS（异常侧不重复记请求完成）。"""
    resp = TestClient(app, raise_server_exceptions=False).get("/nonexistent")
    assert resp.status_code == 404

    warnings = _records_at(log_records, "WARNING")
    assert len(warnings) == 1
    assert warnings[0]["message"] == "请求异常: 路径不存在"
    assert warnings[0]["extra"]["status_code"] == 404
    assert _records_at(log_records, "SUCCESS") == []


def test_invalid_query_returns_validation_error_code():
    @app.get("/_test_validate")
    async def _validate(n: int):
        return {"n": n}

    client = TestClient(app, raise_server_exceptions=False)
    try:
        resp = client.get("/_test_validate", params={"n": "not-an-int"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "400001"
        assert "query.n" in body["message"]
        assert client.get("/_test_validate", params={"n": 3}).json() == {"n": 3}
    finally:
        _remove_test_route("/_test_validate")


def test_client_400_logs_exactly_one_warning(log_records):
    @app.get("/_test_validate_log")
    async def _validate_log(n: int):
        return {"n": n}

    try:
        resp = TestClient(app, raise_server_exceptions=False).get(
            "/_test_validate_log", params={"n": "not-an-int"}
        )
        assert resp.status_code == 400

        warnings = _records_at(log_records, "WARNING")
        assert len(warnings) == 1
        assert warnings[0]["message"].startswith("请求异常: query.n")
        assert warnings[0]["extra"]["status_code"] == 400
        assert _records_at(log_records, "SUCCESS") == []
    finally:
        _remove_test_route("/_test_validate_log")


def test_conflict_exception_route_returns_409001():
    """ConflictException 经真实注册链路返回 409001（REQ-042 同线程并发拒绝）。"""

    @app.get("/_test_conflict")
    async def _conflict():
        raise ConflictException(message="线程已有进行中轮次")

    client = TestClient(app)
    try:
        resp = client.get("/_test_conflict")
        assert resp.status_code == 409
        assert resp.json() == {"code": "409001", "message": "线程已有进行中轮次"}
    finally:
        _remove_test_route("/_test_conflict")


def test_business_exception_route_works_with_default_test_client():
    """BusinessException 已显式注册：默认 TestClient（raise_server_exceptions=True）
    不抛异常，直接拿到统一 JSON（回归守卫：漏注册会冒泡到 ServerErrorMiddleware 并 re-raise）。"""

    @app.get("/_test_not_found")
    async def _not_found():
        raise NotFoundException(message="商品不存在")

    client = TestClient(app)  # 故意保留默认 raise_server_exceptions=True
    try:
        resp = client.get("/_test_not_found")
        assert resp.status_code == 404
        assert resp.json() == {"code": "404001", "message": "商品不存在"}
    finally:
        _remove_test_route("/_test_not_found")


def test_uncaught_exception_returns_500001():
    @app.get("/_test_boom")
    async def _boom():
        raise Exception("secret internal detail")  # 故意触发兜底处理器

    # 未知异常走 ServerErrorMiddleware（构造响应后仍会 re-raise），必须关掉 raise_server_exceptions
    client = TestClient(app, raise_server_exceptions=False)
    try:
        resp = client.get("/_test_boom")
        assert resp.status_code == 500
        assert resp.json() == {"code": "500001", "message": "服务器内部错误"}
        assert "secret" not in resp.text
    finally:
        _remove_test_route("/_test_boom")


def test_client_500_logs_exactly_one_error_with_stack(log_records):
    @app.get("/_test_boom_log")
    async def _boom_log():
        raise Exception("secret internal detail")

    try:
        resp = TestClient(app, raise_server_exceptions=False).get("/_test_boom_log")
        assert resp.status_code == 500

        errors = _records_at(log_records, "ERROR")
        assert len(errors) == 1
        assert errors[0]["message"] == "未处理异常: secret internal detail"
        assert errors[0]["exception"] is not None  # 带栈
        assert errors[0]["extra"]["status_code"] == 500
        assert _records_at(log_records, "SUCCESS") == []
        assert "secret" not in resp.text
    finally:
        _remove_test_route("/_test_boom_log")


# ---- 错误码文档 ----

def test_generate_error_docs_contains_both_tables():
    docs = generate_error_docs()
    assert "## 错误码说明" in docs
    # 业务错误表 + 协议错误表各一张
    assert docs.count("| 业务错误码 | HTTP状态码 | 消息 |") == 2
    assert "| 404001 | 404 | 资源不存在 |" in docs
    assert "| 404 | 404 | 路径不存在 |" in docs


def test_generate_error_docs_lists_contract_codes_and_hides_fallback():
    docs = generate_error_docs()
    # 契约错误码表（api.md §0）：校验 400001、并发 409001/409002、数据库 500001 各列一次
    assert "| 400001 | 400 | 请求参数错误 |" in docs
    assert "| 409001 | 409 | 线程已有进行中轮次 |" in docs
    assert "| 409002 | 409 | 轮次已处于终态，不可取消 |" in docs
    assert "| 500001 | 500 | 内部错误 |" in docs  # DatabaseException 注册 500001 后进文档
    assert "422001" not in docs  # 422001 让位给「URL 非法」（api.md 46 行，M3 口径，本批不实现）
    assert "服务器内部错误" not in docs  # Exception 兜底行仍不进文档（客户端无需区分内部成因）


def test_app_description_includes_error_docs():
    assert "## 错误码说明" in app.description
