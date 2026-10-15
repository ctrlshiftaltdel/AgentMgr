"""审计域写入服务（只增不改）。

本模块**只暴露追加写入能力**：``record_event``。这里刻意不提供任何
``update_*`` / ``delete_*`` 之类的公开函数——审计事件一经写入即不可修改，这一约束
同时在数据库层以 BEFORE UPDATE OR DELETE 触发器强制（见迁移 ``0004_audit_events``），
应用层与数据库层双重保证（``docs/research/W0-5-security-compliance.md`` §7.3）。

``record_event`` 只把事件 ``add`` 到传入的 session 并 ``flush``，**绝不 commit**：
事务边界由调用方控制，审计写入应与被审计的业务变更处于同一事务，避免出现
「业务成功但审计丢失」或反之的偏差。

**部署前提（本防护有效性的必要条件）**：应用连接数据库所用的角色**不得拥有任何
DDL 权限**（不得是表/函数/触发器的属主，不得是超级用户，不得有 ``CREATE`` /
``ALTER`` 权限）。数据库层只增不改的强制依赖于 ``audit_events`` 上的 BEFORE
UPDATE OR DELETE 触发器；而拥有 DDL 权限的角色可以执行
``ALTER TABLE audit_events DISABLE TRIGGER ...`` 或 ``DROP TRIGGER`` 直接绕过它。
因此数据库迁移（建表、建函数、建触发器）**必须由独立的、仅迁移时使用的 DDL 角色
执行**；运行期应用角色只被授予 ``INSERT`` / ``SELECT``（外加 ``audit_events_id_seq``
的 ``USAGE``），不得被授予 ``UPDATE`` / ``DELETE``，更不得被授予 DDL 权限。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.audit.models import AuditEvent

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session

__all__ = ["record_event"]


def record_event(
    session: Session,
    *,
    actor_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    request_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """追加写入一条审计事件并返回它（不 commit）。

    参数约束（对齐 ``templates/PLAN.md`` §8/§9 与
    ``docs/research/W0-5-security-compliance.md`` §3.1/§3.2）：

    * ``actor_id`` 必须是**已验证的实际人员**的内部员工 ID。严禁填入模型名字、
      角色名或任何非人员标识——审批与操作记录必须关联实际人员而非模型名字
      （PLAN §9）。当操作由系统自身发起且确无人员主体时，才允许传 ``None``。
    * ``details`` 只允许放**结构化、非敏感**字段。**禁止放入 A 类敏感字段原文**，
      包括但不限于：身份证号、银行卡号、CVC/卡校验码、账号密码与各类凭据、
      完整手机号与住址、发票查验凭证原文、完整银行流水明细（W0-5 §3.1）。
      需要引用这类对象时，只写脱敏值、内部引用 ID 或哈希。
    * 同时**禁止把完整渠道用户 ID 明文写入** ``details``/``request_id``：渠道用户
      标识只允许以加盐哈希或不可逆短标识的形式出现（W0-5 §3.2 日志禁止项）。

    本函数不提供 ``update`` / ``delete`` 能力；审计事件只增不改。
    """
    event = AuditEvent(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        details=dict(details) if details is not None else None,
    )
    session.add(event)
    session.flush()
    return event
