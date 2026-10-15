"""管理后台认证：签名会话 cookie 与登录凭据校验（fail closed）。

本模块把 ``templates/PLAN.md`` §3「Web 管理界面：主要用于审核、查询、权限、配置
和审计」、§9「权限模型」与 §10 阶段 1「最小化 Web 管理后台」落成一套**服务端
渲染、浏览器可用**的认证机制。它只负责「谁登录了」，**不负责**任何业务授权判定
——逐页/逐动作的权限校验仍由 ``app.identity.authorization`` 在每个请求里执行
（见 ``app.api.admin``）。

安全模型（由 Master 固定，不得放宽）
--------------------------------------------------------------------------------

1. **内部令牌 fail closed**：唯一共享凭据是环境变量 ``AJAIMGR_INTERNAL_TOKEN``。
   未配置时登录一律 **503**，绝不默认放行（与 ``app.api.deps`` 的模型一致）。
2. **常量时间比较**：提交的 ``token`` 用 ``hmac.compare_digest`` 与已配置令牌做
   常量时间比较，不符即 **401**，避免计时侧信道。
3. **员工号解析默认拒绝**：``employee_no`` 必须命中**恰好一位** ``status='active'``
   的员工；不存在、非 active 或歧义匹配一律 **401**，且**不区分**「不存在」与
   「非 active」——避免通过错误信息枚举员工。
4. **签名会话 cookie**：通过后下发 cookie，其值是 ``HMAC-SHA256`` 签名的载荷
   （至少含 ``employee_id`` 与过期时间），**密钥由内部令牌派生**。cookie 属性
   ``HttpOnly + SameSite=Strict + Path=/admin``；签名不符或已过期一律拒绝，并由
   上层重定向到登录页（``app.api.admin``）。令牌轮换会改变派生密钥，从而使既有
   会话立即失效。

过渡方案（**必须记录，不得当作最终形态**）
--------------------------------------------------------------------------------

浏览器路径**没有** ``(channel_type, external_user_id)`` 渠道绑定可依据——渠道
绑定服务于工具代理 / 微信通道，而人类管理员是通过浏览器直连本服务的。因此：

* 操作员的员工号由**本人声明**（登录表单的 ``employee_no``），服务端只校验「该
  员工号存在且 active」以及「共享内部令牌正确」；
* 信任边界是**服务器端内部令牌**（``AJAIMGR_INTERNAL_TOKEN``，由部署注入环境，
  **模型不可读**），而非任何客户端可自填的字段；
* 即便如此，**权限校验仍然逐页生效**：登录只回答「是谁」，每次页面/动作请求都
  重新调用 ``app.identity.authorization`` 的权限函数，登录本身**不授予任何业务
  权限**。

**生产环境必须接入公司 SSO / 账号体系**（OIDC / 企业目录 / 双向 TLS 等），由
身份提供方完成人员认证，本模块的「共享令牌 + 自报员工号」只是阶段 1 的**过渡
方案**，不得作为最终形态上线。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import INTERNAL_TOKEN_ENV
from app.api.errors import ServiceUnavailableError
from app.db.models.organization import Employee
from app.db.session import get_session
from app.identity.authorization import Principal

__all__ = [
    "AdminAuthRequired",
    "AdminForbidden",
    "COOKIE_PATH",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL_SECONDS",
    "configured_token",
    "create_session_token",
    "get_admin_principal",
    "session_cookie_kwargs",
    "verify_login",
    "verify_session_token",
]

#: 会话 cookie 名。
SESSION_COOKIE_NAME = "ajaimgr_admin_session"

#: 会话有效期（秒）：默认 8 小时。
SESSION_TTL_SECONDS = 8 * 60 * 60

#: cookie 作用路径：只随 ``/admin`` 下的请求发送。
COOKIE_PATH = "/admin"

#: 派生签名密钥时使用的固定上下文串（域分隔，避免跨用途复用密钥）。
_SIGNING_CONTEXT = b"ajaimgr-admin-session-v1"


class AdminAuthRequired(Exception):
    """会话缺失 / 篡改 / 过期 / 主体失效：上层应重定向到登录页。

    本异常**不携带任何内部细节**（不含 DSN、堆栈或令牌），只表达「需要重新登录」。
    """


class AdminForbidden(Exception):
    """已登录但无权访问该页面 / 动作：上层渲染 403 页面，且不泄露任何数据。"""

    def __init__(self, message: str = "无权访问该页面") -> None:
        self.message = message
        super().__init__(message)


def configured_token() -> str | None:
    """返回已配置的内部令牌；未配置或为空白时返回 ``None``（fail closed 前置）。"""
    token = os.environ.get(INTERNAL_TOKEN_ENV)
    if token is None or not token.strip():
        return None
    return token


def _signing_key(token: str) -> bytes:
    """由内部令牌派生会话签名密钥（域分隔的 HMAC-SHA256）。"""
    return hmac.new(token.encode("utf-8"), _SIGNING_CONTEXT, hashlib.sha256).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_session_token(
    employee_id: int,
    *,
    ttl: int = SESSION_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """构造签名会话值 ``<payload_b64>.<signature>``。

    载荷是紧凑 JSON ``{"employee_id": <int>, "exp": <int>}``；签名是
    ``HMAC-SHA256(key, payload_b64)`` 的十六进制。``ttl`` 允许为负（仅用于测试
    构造过期会话）。内部令牌未配置时抛 ``RuntimeError``（调用方应先 fail closed）。
    """
    token = configured_token()
    if token is None:  # pragma: no cover - 调用方已前置检查
        raise RuntimeError("internal token is not configured")
    issued = time.time() if now is None else now
    payload = {"employee_id": int(employee_id), "exp": int(issued) + int(ttl)}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64encode(raw)
    signature = hmac.new(
        _signing_key(token), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{body}.{signature}"


def verify_session_token(value: str | None, *, now: float | None = None) -> int | None:
    """校验签名会话值，返回 ``employee_id``；任何异常一律返回 ``None``。

    拒绝条件：值为空、内部令牌未配置、结构不符、签名不符（常量时间比较）或已
    过期。绝不抛出可被用于区分的异常。
    """
    if not value:
        return None
    token = configured_token()
    if token is None:
        return None
    try:
        body, signature = value.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(
        _signing_key(token), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
        employee_id = int(payload["employee_id"])
        expires_at = int(payload["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    current = time.time() if now is None else now
    if current >= expires_at:
        return None
    return employee_id


def verify_login(session: Session, *, token: str, employee_no: str) -> Employee | None:
    """校验登录凭据，成功返回员工记录，失败返回 ``None``。

    内部令牌未配置时抛 ``ServiceUnavailableError``（503，fail closed）。令牌不符
    返回 ``None``；员工号必须命中**恰好一位** active 员工，否则返回 ``None``
    （不区分「不存在」与「非 active」）。
    """
    configured = configured_token()
    if configured is None:
        raise ServiceUnavailableError()

    if not hmac.compare_digest(
        token.encode("utf-8"), configured.encode("utf-8")
    ):
        return None

    normalized = employee_no.strip()
    if not normalized:
        return None
    candidates = session.scalars(
        select(Employee).where(
            Employee.employee_no == normalized,
            Employee.status == "active",
        )
    ).all()
    if len(candidates) != 1:
        return None
    return candidates[0]


def get_admin_principal(
    request: Request,
    session: Session = Depends(get_session),
) -> Principal:
    """从签名会话 cookie 解析并复核主体；失败抛 ``AdminAuthRequired``。

    内部令牌未配置时抛 ``ServiceUnavailableError``（503，fail closed）。会话有效
    但员工已不存在或非 active 时同样按「需要重新登录」处理。返回的
    ``Principal`` 的 ``company_id`` 取自**已核验的员工记录**，不信任 cookie 内容。
    """
    if configured_token() is None:
        raise ServiceUnavailableError()

    employee_id = verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))
    if employee_id is None:
        raise AdminAuthRequired()

    employee = session.get(Employee, employee_id)
    if employee is None or employee.status != "active":
        raise AdminAuthRequired()

    return Principal(employee_id=employee.id, company_id=employee.company_id)


def session_cookie_kwargs() -> dict[str, object]:
    """``set_cookie`` 的安全属性：HttpOnly + SameSite=Strict + Path=/admin。"""
    return {
        "key": SESSION_COOKIE_NAME,
        "path": COOKIE_PATH,
        "httponly": True,
        "samesite": "strict",
        "max_age": SESSION_TTL_SECONDS,
    }
