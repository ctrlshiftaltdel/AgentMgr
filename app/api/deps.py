"""受验证主体依赖：内部令牌 + 员工标识头 → ``Principal``（fail closed）。

本模块是 ``docs/research/W0-2-profile-isolation.md`` §4「身份路由协议草案」在
API 层的落点，把 ``templates/PLAN.md`` §2.1「Agent 工具请求由代理附加经过验证的
操作主体和请求上下文；不能依靠模型自行填入的 ``employee_id``」与 §9「后端对每次
调用执行授权」编码为**每次请求都要执行**的确定性校验。

安全模型（由 Master 固定，不得放宽）：

1. **内部令牌 fail closed**：环境变量 ``AJAIMGR_INTERNAL_TOKEN`` 是唯一的内部
   共享凭据。**未配置时，所有受保护端点一律返回 503**，绝不默认放行——宁可拒绝
   服务，也不允许「无令牌即放行」。
2. **常量时间比较**：``Authorization: Bearer <token>`` 的令牌用
   ``hmac.compare_digest`` 做常量时间比较，避免计时侧信道。缺失或错误的凭据
   返回 **401**。
3. **主体只来自头部**：``employee_id`` **只能**来自 ``X-Employee-Id`` 请求头。
   ``get_principal`` **绝不**从请求体、查询参数、Cookie 或模型输出中读取
   ``employee_id``——模型可以提取、解释、建议，但不能声明自己是谁
   （PLAN §2.1；W0-2 §4.2/§4.3「绑定表才是授权真源」）。
4. **绑定校验**：按 ``X-Employee-Id`` 加载员工记录，要求 ``status='active'``；
   员工不存在或非 active 一律返回 **403**（默认拒绝）。

**过渡方案说明（必须记录，不得当作最终形态）**：``X-Employee-Id`` 头**必须**由
受信任的本地工具代理在验证渠道身份后写入——即由渠道适配层按
``(channel_type, external_user_id)`` 在 ``employee_channel_bindings`` 中解析出
``employee_id``，再把它绑定进该请求头（W0-2 §4.2 步骤 3/4）。本 API 仅监听
localhost / Unix socket，不直接暴露公网。在工具代理落地之前，本依赖是**过渡
方案**：它假定调用方（工具代理）可信并已注入该头，服务端无法独立复核该头的
来源。工具代理实现后，应改为由代理注入短期调用凭据或签名断言，服务端据签名
解析主体，从而不再单纯信任一个客户端可自填的头部。
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.api.errors import AuthError, ForbiddenError, ServiceUnavailableError
from app.db.models.organization import Employee
from app.db.session import get_session
from app.identity.authorization import Principal

__all__ = ["INTERNAL_TOKEN_ENV", "get_principal", "get_request_id"]

#: 内部共享令牌的环境变量名。未配置即对所有受保护端点 fail closed（503）。
INTERNAL_TOKEN_ENV = "AJAIMGR_INTERNAL_TOKEN"

#: ``Authorization`` 头使用的认证方案前缀（大小写不敏感）。
_BEARER_PREFIX = "bearer"


def _configured_token() -> str | None:
    """返回已配置的内部令牌；未配置或为空白时返回 ``None``。"""
    token = os.environ.get(INTERNAL_TOKEN_ENV)
    if token is None or not token.strip():
        return None
    return token


def _extract_bearer(authorization: str | None) -> str | None:
    """从 ``Authorization`` 头提取 Bearer 令牌；格式不符返回 ``None``。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, credentials = parts
    if scheme.lower() != _BEARER_PREFIX or not credentials:
        return None
    return credentials.strip()


def get_request_id(
    x_request_id: str | None = Header(default=None),
) -> str | None:
    """透传可选 ``X-Request-Id`` 头，用于审计关联同一次调用（PLAN §8）。"""
    if x_request_id is None:
        return None
    value = x_request_id.strip()
    return value or None


def get_principal(
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_employee_id: str | None = Header(default=None),
) -> Principal:
    """校验内部令牌与员工标识头，返回**已验证**的 :class:`Principal`。

    校验顺序（任一步失败即按对应状态码拒绝，绝不继续）：

    1. ``AJAIMGR_INTERNAL_TOKEN`` 未配置 → ``503``（fail closed）。
    2. ``Authorization: Bearer <token>`` 缺失、格式不符或令牌不匹配
       （``hmac.compare_digest`` 常量时间比较）→ ``401``。
    3. ``X-Employee-Id`` 缺失或不是合法整数 → ``401``。

       **该头是 ``employee_id`` 的唯一来源**：本函数不读取请求体、查询参数、
       Cookie 或任何模型输出中的 ``employee_id``。
    4. 按该 id 加载员工，要求 ``status='active'``；不存在或非 active → ``403``。

    通过后返回 ``Principal(employee_id=..., company_id=...)``，其中
    ``company_id`` 取自**已核验的员工记录**，不信任任何外部输入。
    """
    token = _configured_token()
    if token is None:
        # fail closed：没有配置内部令牌时，受保护端点一律不可用。
        raise ServiceUnavailableError()

    provided = _extract_bearer(authorization)
    if provided is None or not hmac.compare_digest(
        provided.encode("utf-8"), token.encode("utf-8")
    ):
        raise AuthError()

    # employee_id 只来自 X-Employee-Id 头；缺失或非法一律 401。
    if x_employee_id is None:
        raise AuthError()
    try:
        employee_id = int(x_employee_id.strip())
    except (TypeError, ValueError):
        raise AuthError() from None

    employee = session.get(Employee, employee_id)
    if employee is None or employee.status != "active":
        raise ForbiddenError()

    return Principal(employee_id=employee.id, company_id=employee.company_id)
