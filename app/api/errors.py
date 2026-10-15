"""统一错误码映射（API 层）。

本模块把领域层抛出的异常映射为**稳定的 HTTP 状态码 + 机器可读错误码**，并保证
所有错误响应体形状一致::

    {"error": {"code": "<stable_code>", "message": "<safe message>"}}

设计依据：

* ``templates/PLAN.md`` §9「后端对每次调用执行授权」——授权失败必须是明确的
  403，而不是静默放行或 500；
* ``templates/PLAN.md`` §2.1 / ``docs/research/W0-2-profile-isolation.md`` §4
  ——身份校验失败必须 fail closed（401/403/503），不得降级为放行；
* ``docs/DEPLOYMENT-REQUIREMENTS.md`` §7「日志禁止项」与
  ``docs/research/W0-5-security-compliance.md`` §3.2——**错误响应不得泄露内部
  信息**：不返回异常堆栈、数据库 DSN、SQL 语句或异常 ``repr``。

映射表：

============================================  ======  ==========================
异常                                          状态码  错误码
============================================  ======  ==========================
``AuthorizationDeniedError``                  403     ``authorization_denied``
``InvalidTransitionError``                    409     ``invalid_transition``
``StepAlreadyDecidedError``                   409     ``step_already_decided``
``InvalidDecisionError``                      422     ``invalid_decision``
``ValidationError``（报销服务业务校验）         422     ``validation_error``
``ClaimNotFoundError``                        404     ``not_found``
``WorkflowInstanceNotFoundError``             404     ``not_found``
``PathTraversalError`` 等文件存储闸门          400     ``bad_request``
``ApiError``（API 层显式错误）                  自定    自定
请求体结构校验失败（``RequestValidationError``） 422     ``validation_error``
其它未预期异常                                  500     ``internal_error``
============================================  ======  ==========================

所有 ``message`` 都是**受控的静态文案**，绝不拼接异常 ``repr`` 或 ``str(exc)``，
从而不会把堆栈、DSN 或 SQL 带出接口。
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.documents.storage import (
    FileTooLargeError,
    FileTypeMismatchError,
    HashMismatchError,
    PathTraversalError,
)
from app.expense.service import (
    AuthorizationDeniedError,
    ClaimNotFoundError,
    ValidationError,
)
from app.expense.state_machine import InvalidTransitionError
from app.workflow.service import (
    InvalidDecisionError,
    StepAlreadyDecidedError,
    WorkflowInstanceNotFoundError,
)

__all__ = [
    "ApiError",
    "AuthError",
    "ForbiddenError",
    "ServiceUnavailableError",
    "error_body",
    "register_exception_handlers",
]

#: 受控的静态文案，避免任何内部细节经响应体泄露。
_MESSAGE_AUTHORIZATION_DENIED = "authorization denied"
_MESSAGE_INVALID_TRANSITION = "action not allowed from current status"
_MESSAGE_STEP_ALREADY_DECIDED = "workflow step already decided"
_MESSAGE_INVALID_DECISION = "invalid decision"
_MESSAGE_VALIDATION = "request failed business validation"
_MESSAGE_NOT_FOUND = "resource not found"
_MESSAGE_BAD_FILE = "invalid file input"
_MESSAGE_UNAUTHORIZED = "authentication required"
_MESSAGE_FORBIDDEN = "access denied"
_MESSAGE_UNAVAILABLE = "service unavailable"
_MESSAGE_INTERNAL = "internal server error"


class ApiError(Exception):
    """API 层显式错误：携带 HTTP 状态码与稳定错误码。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AuthError(ApiError):
    """缺少或提供了错误的内部凭据 → 401。"""

    def __init__(self, message: str = _MESSAGE_UNAUTHORIZED) -> None:
        super().__init__(401, "unauthorized", message)


class ForbiddenError(ApiError):
    """凭据有效但主体未被授权 / 不存在或非 active → 403。"""

    def __init__(self, message: str = _MESSAGE_FORBIDDEN) -> None:
        super().__init__(403, "forbidden", message)


class ServiceUnavailableError(ApiError):
    """内部令牌未配置：受保护端点一律 fail closed → 503。"""

    def __init__(self, message: str = _MESSAGE_UNAVAILABLE) -> None:
        super().__init__(503, "service_unavailable", message)


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    """构造统一错误响应体 ``{"error": {"code", "message"}}``。"""
    return {"error": {"code": code, "message": message}}


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(code, message))


def _static_handler(status_code: int, code: str, message: str):
    """生成一个忽略异常细节、只返回受控文案的处理器。"""

    async def handler(_request: Request, _exc: Exception) -> JSONResponse:
        return _json_error(status_code, code, message)

    return handler


def register_exception_handlers(app: FastAPI) -> None:
    """在应用上注册全部统一异常处理器。"""

    # --- API 层显式错误（含认证 401 / 403 / 503） ---
    async def _api_error(_request: Request, exc: Exception) -> JSONResponse:
        api_error = cast(ApiError, exc)
        return _json_error(api_error.status_code, api_error.code, api_error.message)

    app.add_exception_handler(ApiError, _api_error)

    # --- 领域层异常 ---
    app.add_exception_handler(
        AuthorizationDeniedError,
        _static_handler(403, "authorization_denied", _MESSAGE_AUTHORIZATION_DENIED),
    )
    app.add_exception_handler(
        InvalidTransitionError,
        _static_handler(409, "invalid_transition", _MESSAGE_INVALID_TRANSITION),
    )
    app.add_exception_handler(
        StepAlreadyDecidedError,
        _static_handler(409, "step_already_decided", _MESSAGE_STEP_ALREADY_DECIDED),
    )
    app.add_exception_handler(
        InvalidDecisionError,
        _static_handler(422, "invalid_decision", _MESSAGE_INVALID_DECISION),
    )
    app.add_exception_handler(
        ValidationError,
        _static_handler(422, "validation_error", _MESSAGE_VALIDATION),
    )
    app.add_exception_handler(
        ClaimNotFoundError,
        _static_handler(404, "not_found", _MESSAGE_NOT_FOUND),
    )
    app.add_exception_handler(
        WorkflowInstanceNotFoundError,
        _static_handler(404, "not_found", _MESSAGE_NOT_FOUND),
    )

    # --- 文件存储闸门（路径穿越 / 类型伪装 / 超大 / 散列不符）→ 400 ---
    for storage_error in (
        PathTraversalError,
        FileTypeMismatchError,
        FileTooLargeError,
        HashMismatchError,
    ):
        app.add_exception_handler(
            storage_error,
            _static_handler(400, "bad_request", _MESSAGE_BAD_FILE),
        )

    # --- 请求体结构校验失败 → 422（统一响应体，不回显输入值） ---
    async def _request_validation(_request: Request, _exc: Exception) -> JSONResponse:
        return _json_error(422, "validation_error", "request body failed schema validation")

    app.add_exception_handler(RequestValidationError, _request_validation)

    # --- 框架级 HTTP 异常（404 路由不存在 / 405 方法不允许等） ---
    _HTTP_CODES = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        422: "validation_error",
    }

    async def _http_exception(_request: Request, exc: Exception) -> JSONResponse:
        http_error = cast(StarletteHTTPException, exc)
        code = _HTTP_CODES.get(http_error.status_code, "http_error")
        message = _MESSAGE_NOT_FOUND if http_error.status_code == 404 else "request failed"
        return _json_error(http_error.status_code, code, message)

    app.add_exception_handler(StarletteHTTPException, _http_exception)

    # --- 兜底：绝不把堆栈 / DSN / SQL 暴露给调用方 ---
    async def _unhandled(_request: Request, _exc: Exception) -> JSONResponse:
        return _json_error(500, "internal_error", _MESSAGE_INTERNAL)

    app.add_exception_handler(Exception, _unhandled)
