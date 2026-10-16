"""渠道身份路由：``(channel_type, external_user_id) -> Principal`` 解析服务（任务 W1-19）。

本模块实现 ``docs/research/W0-2-profile-isolation.md`` §4「身份路由协议草案」中被
W1-14 负面测试标记为**缺失**的那一环（缺陷 #5）：把渠道侧发送者标识解析成后端
**已验证的操作主体** :class:`~app.identity.authorization.Principal`。

授权真源
--------

``employee_channel_bindings`` 表是授权真源（W0-2 §4.2 步骤 3、§4.3）：路由以
``(channel_type, external_user_id)`` 复合键（**绝不**退化为仅 ``external_user_id``，
也绝不跨渠道复用）在业务绑定表中查 ``employee_id``。渠道用户 ID 不是凭据，也不是
身份——它只是一个**待解析的键**，只有命中一条**已核验且有效**的绑定才可能成为主体。

与 ``X-Employee-Id`` 的关系（必须遵守）
----------------------------------------

受信任的**本地工具代理**（渠道适配层）是唯一允许构造主体的调用方，其正确顺序是：

1. 收到渠道消息后，调用本服务 :func:`resolve_principal` 得到 ``Principal``；
2. 用解析出的 ``employee_id`` 注入下游 API 请求的 ``X-Employee-Id`` 头；
3. 模型**不得**自报身份——``employee_id`` 永远来自绑定表解析，而非模型输出、
   请求体或查询参数（``templates/PLAN.md`` §2.1、§9）。

``app/api/deps.py`` 目前仍是**过渡方案**（它信任调用方已注入的头，服务端无法独立
复核该头来源）；工具代理落地后应改为由代理注入短期调用凭据 / 签名断言，服务端据
签名解析主体。无论哪种形态，**主体只能源自本服务对绑定表的解析**。

拒绝语义（fail closed，对外不可区分）
--------------------------------------

任一下列情况一律拒绝，且**对外不可区分**——抛出**同一个** :class:`ChannelIdentityRejectedError`
且 ``str(exc)`` **完全相同**（见该异常与 :data:`REJECTION_MESSAGE` 的说明）：

1. 未绑定：``(channel_type, external_user_id)`` 无对应绑定；
2. 歧义：命中**不止一条**绑定（数据异常）——即便库层有
   ``UNIQUE (channel_type, external_user_id)`` 使歧义在结构上不可能，本服务仍**显式**
   做「必须恰好一条」的防御性检查，绝不静默取第一条；
3. 已解绑：绑定 ``status != 'active'``（``revoked``）；
4. 未验证：绑定 ``verified_at IS NULL``——未经核验的绑定不得认证任何人；
5. 员工非 active：绑定指向的员工不存在或 ``status != 'active'``；
6. 公司不匹配：传入 ``expected_company_id`` 且与员工所属公司不一致（防跨公司解析）。

区分信息（具体原因）只进 :attr:`ChannelIdentityRejectedError.reason`，供**内部审计 /
调试日志**使用，**绝不**并入异常消息或响应体，避免据此枚举渠道用户（W0-2 §4.3
「只记录一条审计事件（含身份哈希而非明文）」）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.organization import Employee
from app.identity.authorization import Principal
from app.identity.models import EmployeeChannelBinding

__all__ = [
    "REJECTION_MESSAGE",
    "ChannelIdentityRejectedError",
    "resolve_principal",
]

#: 对外统一拒绝文案。**所有**拒绝原因都必须返回这**一条**文案，使调用方无法据
#: 错误信息区分「未绑定 / 已解绑 / 未验证 / 员工非 active / 公司不匹配 / 歧义」，
#: 从而无法枚举渠道用户。
REJECTION_MESSAGE = "channel identity is not authorized"


class ChannelIdentityRejectedError(Exception):
    """渠道身份未获授权（fail closed）。

    对外契约（**不得放宽**）：

    * **单一异常类型**：无论拒绝原因为何，解析失败一律抛出本异常；调用方**无法**
      依据异常类型区分具体原因。
    * **单一文案**：``str(exc)`` 恒等于 :data:`REJECTION_MESSAGE`，不拼接渠道用户
      ID、员工 ID 或任何具体原因。

    :attr:`reason` 只承载**内部**拒绝原因（如 ``"unbound"`` / ``"revoked"`` /
    ``"unverified"`` / ``"employee_inactive"`` / ``"company_mismatch"`` /
    ``"ambiguous"``），供审计或调试日志使用；**绝不**对外暴露，也**不得**并入异常
    消息或 HTTP 响应体。
    """

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(REJECTION_MESSAGE)


def resolve_principal(
    session: Session,
    *,
    channel_type: str,
    external_user_id: str,
    expected_company_id: int | None = None,
) -> Principal:
    """把渠道身份解析成**已验证**的 :class:`Principal`，否则拒绝。

    参数：

    * ``channel_type`` / ``external_user_id``：渠道侧身份键，**跨渠道不复用**
      （微信 ``from_user_id`` ≠ 企业微信 userid ≠ 钉钉 staffId ≠ 飞书 open_id）。
    * ``expected_company_id``：可选。调用方（如工具代理已确定的目标公司）期望的
      公司；非 ``None`` 时员工所属公司必须与之相符，否则拒绝（防跨公司解析）。

    返回的 :class:`Principal` 的 ``employee_id`` 与 ``company_id`` **只取自数据库
    记录**（绑定 → 员工），**绝不**取自调用方参数。

    任一拒绝情形一律抛出 :class:`ChannelIdentityRejectedError`（同一类型、同一文案，
    对外不可区分）；具体原因仅在 ``exc.reason`` 中，供内部审计 / 调试使用。
    """
    bindings = session.scalars(
        select(EmployeeChannelBinding).where(
            EmployeeChannelBinding.channel_type == channel_type,
            EmployeeChannelBinding.external_user_id == external_user_id,
        )
    ).all()

    # 规则 1：必须**恰好一条**。0 条 = 未绑定；>1 条 = 歧义。即便库层有 UNIQUE
    # (channel_type, external_user_id) 约束，这里也显式做防御性检查，绝不静默取
    # 第一条（歧义即拒绝）。
    if len(bindings) != 1:
        raise ChannelIdentityRejectedError(
            reason="unbound" if not bindings else "ambiguous"
        )

    binding = bindings[0]

    # 规则 2：已解绑（revoked）一律拒绝。
    if binding.status != "active":
        raise ChannelIdentityRejectedError(reason="revoked")

    # 规则 3：未核验（verified_at IS NULL）的绑定不得认证任何人。
    if binding.verified_at is None:
        raise ChannelIdentityRejectedError(reason="unverified")

    # 规则 4：员工必须存在且 status == 'active'。
    employee = session.get(Employee, binding.employee_id)
    if employee is None or employee.status != "active":
        raise ChannelIdentityRejectedError(reason="employee_inactive")

    # 规则 5：防跨公司解析。
    if expected_company_id is not None and employee.company_id != expected_company_id:
        raise ChannelIdentityRejectedError(reason="company_mismatch")

    # 规则 6：主体只来自数据库记录。
    return Principal(employee_id=employee.id, company_id=employee.company_id)
