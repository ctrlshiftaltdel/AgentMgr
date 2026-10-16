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

拒绝审计（任务 W1-21）
----------------------

上面**每一种**拒绝情形都会在**同一事务内**追加一条审计事件（``action =
``channel_identity.rejected``，见 :data:`REJECTION_AUDIT_ACTION`），并**在抛出**
:class:`ChannelIdentityRejectedError` **之前显式 ``commit``**：拒绝以异常收尾，若
等到调用方的事务边界再提交，这条审计会随回滚丢失（与
``app.api.admin._record_denial`` 同一模式，W0-2 §4.3「所有拒绝写审计」）。

* **身份只记加盐哈希，绝不落明文**：渠道用户 ID 以 :data:`IDENTITY_HASH_SALT_ENV`
  （``AJAIMGR_IDENTITY_HASH_SALT``）为盐做 HMAC-SHA256。盐未配置时**回退复用现有
  做法**——以内部令牌（``AJAIMGR_INTERNAL_TOKEN``）派生（与
  ``app.api.admin._external_id_hash`` 同源）。**两者都未配置时不得退化为写明文**：
  此时只记 ``channel_type`` 与 ``reason``，省略身份哈希（宁可少记身份标识，也绝不
  把渠道用户 ID 写进只增不改的账本）。
* **对外仍不可区分**：审计是**内部**账本；``reason`` **只**进审计 ``details`` 与
  ``exc.reason``，**绝不**进入异常消息、响应体或任何对外通道。异常类型与文案一字
  不变（见 :data:`REJECTION_MESSAGE`）。
* **写入放大控制**：``audit_events`` 只增不改，未认证流量可对其造成无界写入。因此
  对**同一渠道身份**的重复拒绝在 :data:`REJECTION_DEDUPE_WINDOW_SECONDS` 秒窗口内
  只记一条（去重键 = ``channel_type`` + 身份哈希 + ``reason``）；**首次拒绝一定记录**
  （窗口内查不到匹配行即写入）。盐未配置时身份不可区分，去重键退化为
  ``channel_type`` + ``reason``。
* **审计失败不改变拒绝结果**：写入 / 提交抛出的任何异常都被吞掉并 ``rollback``，
  随后仍抛 :class:`ChannelIdentityRejectedError`——不得变成 500，更不得放行。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import NoReturn

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import INTERNAL_TOKEN_ENV
from app.audit.models import AuditEvent
from app.audit.service import record_event
from app.db.models.organization import Employee
from app.identity.authorization import Principal
from app.identity.models import EmployeeChannelBinding

__all__ = [
    "IDENTITY_HASH_SALT_ENV",
    "REJECTION_AUDIT_ACTION",
    "REJECTION_AUDIT_RESOURCE_TYPE",
    "REJECTION_DEDUPE_WINDOW_SECONDS",
    "REJECTION_MESSAGE",
    "ChannelIdentityRejectedError",
    "resolve_principal",
]

#: 对外统一拒绝文案。**所有**拒绝原因都必须返回这**一条**文案，使调用方无法据
#: 错误信息区分「未绑定 / 已解绑 / 未验证 / 员工非 active / 公司不匹配 / 歧义」，
#: 从而无法枚举渠道用户。
REJECTION_MESSAGE = "channel identity is not authorized"

#: 专用身份哈希盐的环境变量名（优先使用）。**盐绝不硬编码在仓库**：由部署注入。
#: 未配置时回退复用内部令牌派生（见 :func:`_identity_hash_salt`）。
IDENTITY_HASH_SALT_ENV = "AJAIMGR_IDENTITY_HASH_SALT"

#: 拒绝审计的事件动作名。单一动作名（不区分具体原因），审计层因此**不是**枚举面；
#: 具体原因只进 ``details["reason"]``。
REJECTION_AUDIT_ACTION = "channel_identity.rejected"

#: 拒绝审计事件的 ``resource_type``；``resource_id`` 存身份哈希（或 ``None``）。
REJECTION_AUDIT_RESOURCE_TYPE = "channel_identity"

#: 写入放大控制的去重窗口（秒）：同一 ``(channel_type, 身份哈希, reason)`` 在此窗口
#: 内只记一条拒绝审计，避免未认证流量对只增不改的 ``audit_events`` 造成无界写入。
#: 取 60 秒的理由：足够粗以吸收重放 / 重试 / 消息风暴，又足够细以在持续攻击中仍按
#: 分钟留痕（每个被拒身份每窗口至少一条）；且**不依赖任何跨进程状态**——判定完全在
#: 库内完成，多 worker / 进程重启后仍然一致。
REJECTION_DEDUPE_WINDOW_SECONDS = 60


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


# --- 拒绝审计（任务 W1-21） ----------------------------------------------------


def _identity_hash_salt() -> str | None:
    """返回身份哈希的盐；未配置任何来源时返回 ``None``。

    优先 :data:`IDENTITY_HASH_SALT_ENV`（``AJAIMGR_IDENTITY_HASH_SALT``，部署注入的
    专用盐）；未配置时**回退复用现有做法**——内部令牌（``AJAIMGR_INTERNAL_TOKEN``），
    与 ``app.api.admin._external_id_hash`` 同源。两者都未配置（或为空白）时返回
    ``None``，调用方据此**省略**身份标识而**绝不**写明文。
    """
    for env_name in (IDENTITY_HASH_SALT_ENV, INTERNAL_TOKEN_ENV):
        value = os.environ.get(env_name)
        if value is not None and value.strip():
            return value
    return None


def _hash_channel_identity(channel_type: str, external_user_id: str) -> str | None:
    """把 ``(channel_type, external_user_id)`` 加盐哈希；盐缺失时返回 ``None``。

    用不可歧义的域分隔（``\\x1f`` 单元分隔符）拼接渠道类型与渠道用户 ID，避免
    ``("a", "b:c")`` 与 ``("a:b", "c")`` 撞哈希。盐缺失时返回 ``None``——**绝不**
    退化为写明文或用固定盐。
    """
    salt = _identity_hash_salt()
    if salt is None:
        return None
    message = f"{channel_type}\x1f{external_user_id}".encode("utf-8")
    return hmac.new(salt.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _recent_rejection_exists(
    session: Session,
    *,
    channel_type: str,
    reason: str,
    identity_hash: str | None,
) -> bool:
    """窗口内是否已有同一渠道身份的同一拒绝审计（写入放大去重判定）。

    去重键 = ``channel_type`` + 身份哈希 + ``reason``（盐缺失时身份哈希为 ``None``，
    退化为 ``channel_type`` + ``reason``，因为此时身份不可区分）。只查
    :data:`REJECTION_DEDUPE_WINDOW_SECONDS` 秒内、动作与资源类型相符的事件；命中即
    认为重复、跳过本次写入。判定在**库内**完成，不依赖任何进程内状态。
    """
    stmt = (
        select(AuditEvent.id)
        .where(
            AuditEvent.action == REJECTION_AUDIT_ACTION,
            AuditEvent.resource_type == REJECTION_AUDIT_RESOURCE_TYPE,
            AuditEvent.created_at
            >= func.now()
            - text(f"interval '{REJECTION_DEDUPE_WINDOW_SECONDS} seconds'"),
            AuditEvent.details["channel_type"].astext == channel_type,
            AuditEvent.details["reason"].astext == reason,
        )
        .limit(1)
    )
    if identity_hash is None:
        stmt = stmt.where(AuditEvent.resource_id.is_(None))
    else:
        stmt = stmt.where(AuditEvent.resource_id == identity_hash)
    return session.scalar(stmt) is not None


def _record_rejection_audit(
    session: Session,
    *,
    channel_type: str,
    external_user_id: str,
    reason: str,
) -> None:
    """在同一事务内写一条拒绝审计并**提交**它（W0-2 §4.3）。

    与 ``app.api.admin._record_denial`` 同一模式：拒绝以异常收尾，若等到调用方事务
    边界再提交就会随回滚丢失，故必须在此处 ``commit``。

    * ``actor_id=None``：渠道身份**未通过解析**，此刻没有可归因的**已核验**人员
      （与 ``admin.login_failed`` 同理），绝不把一次匿名拒绝伪装成有主体的事件。
    * ``details`` 只含结构化的 ``channel_type`` / ``reason`` 与**加盐哈希**
      （``external_user_id_hash``，盐缺失时省略）；**绝不**写渠道用户 ID 明文。
    * **写入放大控制**：窗口内已有同一 ``(channel_type, 身份哈希, reason)`` 的拒绝
      时跳过写入（**首次一定写**）。
    * **默认拒绝优先**：写入 / 提交 / 去重查询抛出的任何异常都被吞掉并 ``rollback``，
      调用方随后仍抛 :class:`ChannelIdentityRejectedError`。审计后端故障**不得**把
      拒绝变成 500，更不得放行。
    """
    try:
        identity_hash = _hash_channel_identity(channel_type, external_user_id)
        if _recent_rejection_exists(
            session,
            channel_type=channel_type,
            reason=reason,
            identity_hash=identity_hash,
        ):
            return
        details: dict[str, str] = {"channel_type": channel_type, "reason": reason}
        if identity_hash is not None:
            details["external_user_id_hash"] = identity_hash
        record_event(
            session,
            actor_id=None,
            action=REJECTION_AUDIT_ACTION,
            resource_type=REJECTION_AUDIT_RESOURCE_TYPE,
            resource_id=identity_hash,
            request_id=None,
            details=details,
        )
        session.commit()
    except Exception:  # noqa: BLE001 - 审计失败一律降级为「仍然拒绝」
        session.rollback()


def _reject(
    session: Session,
    *,
    channel_type: str,
    external_user_id: str,
    reason: str,
) -> NoReturn:
    """记录拒绝审计（同事务提交）后抛 :class:`ChannelIdentityRejectedError`。

    对外**不可区分**：审计写入不改变异常类型与文案；``reason`` 只进审计，不进异常
    消息。
    """
    _record_rejection_audit(
        session,
        channel_type=channel_type,
        external_user_id=external_user_id,
        reason=reason,
    )
    raise ChannelIdentityRejectedError(reason=reason)


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
    对外不可区分）；具体原因仅在 ``exc.reason`` 中，供内部审计 / 调试使用。**每种
    拒绝情形都在同一事务内写一条审计事件（含身份哈希而非明文）并在抛出前 ``commit``**
    （见模块 docstring「拒绝审计」）。
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
        _reject(
            session,
            channel_type=channel_type,
            external_user_id=external_user_id,
            reason="unbound" if not bindings else "ambiguous",
        )

    binding = bindings[0]

    # 规则 2：已解绑（revoked）一律拒绝。
    if binding.status != "active":
        _reject(
            session,
            channel_type=channel_type,
            external_user_id=external_user_id,
            reason="revoked",
        )

    # 规则 3：未核验（verified_at IS NULL）的绑定不得认证任何人。
    if binding.verified_at is None:
        _reject(
            session,
            channel_type=channel_type,
            external_user_id=external_user_id,
            reason="unverified",
        )

    # 规则 4：员工必须存在且 status == 'active'。
    employee = session.get(Employee, binding.employee_id)
    if employee is None or employee.status != "active":
        _reject(
            session,
            channel_type=channel_type,
            external_user_id=external_user_id,
            reason="employee_inactive",
        )

    # 规则 5：防跨公司解析。
    if expected_company_id is not None and employee.company_id != expected_company_id:
        _reject(
            session,
            channel_type=channel_type,
            external_user_id=external_user_id,
            reason="company_mismatch",
        )

    # 规则 6：主体只来自数据库记录。
    return Principal(employee_id=employee.id, company_id=employee.company_id)
