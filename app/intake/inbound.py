"""渠道消息入站与路由（Issue #21）：身份解析 → 幂等去重 → 创建受理作业。

本模块是**渠道适配层与业务后端之间的唯一入口**：适配器把渠道原始事件归一化为
:class:`InboundChannelMessage`（不含任何 SDK 类型），本服务再按
``docs/research/W0-2-profile-isolation.md`` §4.2 的四步协议把它变成一条
``intake_jobs`` 受理作业：

1. **结构校验**（纯内存，写库之前）：渠道类型 / 输入类型必须是受支持取值，渠道
   消息 ID 与发送者标识不得缺失——不满足即 ``rejected``，**一行都不落库**；
2. **身份解析**：调用 ``app.identity.channel_routing.resolve_principal``，主体只来自
   ``employee_channel_bindings``（未绑定 / 已解绑 / 未验证 / 歧义 / 员工非 active /
   跨公司 一律拒绝），拒绝审计由该服务自行写入并以加盐哈希表示身份；
3. **目标 Profile**：由 ``app.runtime.profile_provisioning.profile_name_for`` 计算并随
   结果返回。本模块**只算名字**：不创建目录、不读写 ``~/.hermes``、不启动或停止任何
   进程（目录创建与进程生命周期属部署层任务）；
4. **幂等去重 + 建作业**：``UNIQUE (channel_type, external_message_id)`` 是唯一真源。

幂等语义（至少一次投递下的正确性）
----------------------------------

* 同一 ``(channel_type, external_message_id)`` 重复投递**只产生一个** ``IntakeJob``：
  第二次返回 ``duplicate``，且 ``intake_job_id`` 与第一次**相同**；
* **并发下不靠「先查后插」**：先查只是快路径。真正的保证是唯一约束——插入冲突时
  捕获 ``IntegrityError``、``rollback``、回查既有行并返回 ``duplicate``，绝不把
  ``IntegrityError`` 泄漏成 500（也不会留下半个作业：作业插入与幂等锚点同事务）；
* **路由冲突即拒绝**（§4.4）：同一幂等键再次到达却携带**不同**的渠道发送者或已被
  另一个员工受理时，判定 ``rejected``（``reason="route_conflict"``），不得在第二个
  身份下静默处理，防止跨身份串单。

拒绝**不落库**的理由（重要设计决策）
------------------------------------

``rejected`` 一律**不写** ``inbound_messages``、**不建** ``IntakeJob``。若为拒绝写锚点
行，一次「绑定尚未建立」时的投递会把该消息 ID 永久占位，之后员工完成绑定、渠道重发
同一条消息时会被判成 ``duplicate`` 而永远无法受理——这是**用幂等换来了永久丢单**。
拒绝的可追溯性由 ``app.identity.channel_routing`` 在同一事务内提交的拒绝审计承担
（``action="channel_identity.rejected"``，身份只记加盐哈希）。因此：拒绝不产生受理
作业，重复投递也不会产生重复作业。

隐私与日志
----------

* 本模块**不记录任何日志**，也不把消息正文、渠道发送者明文写进审计/异常/结果字符串；
* 受理审计（``action="inbound_message.accepted"``）只含渠道类型、输入类型、内部
  ``intake_job_id`` 与渠道身份的**加盐哈希**（复用
  ``channel_routing._hash_channel_identity`` 的盐口径，盐缺失时**省略**哈希而绝不写
  明文）；渠道消息 ID 属渠道侧标识，同样只以内部 ``intake_job_id`` 代替，不落审计；
* :class:`IngestResult` 的 ``__repr__`` / ``str`` 不含 ``reason``（内部原因），也不含
  任何外部身份。

事务边界
--------

本函数是**单次投递的事务边界**：校验/身份解析通过后，作业、幂等锚点与受理审计在
**同一事务**提交；并发冲突时 ``rollback`` 后回查（另一事务的提交已可见）。身份拒绝
路径由 ``resolve_principal`` 自行提交拒绝审计（既有行为）。因此调用方**不要**在同一
会话里预先压入其他待提交的业务改动。

不引入任何渠道 SDK、不发起任何网络调用：真实渠道对接由实现
:class:`InboundChannelAdapter` 的适配器完成（后续任务），本模块只消费它归一化后的
中性消息对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.db.models.organization import Employee
from app.identity.channel_routing import (
    ChannelIdentityRejectedError,
    _hash_channel_identity,  # 复用同一套加盐哈希口径（盐、域分隔、回退顺序）
    resolve_principal,
)
from app.intake.models import InboundMessage, IntakeJob
from app.runtime.profile_provisioning import profile_name_for

__all__ = [
    "ACCEPTED_AUDIT_ACTION",
    "ACCEPTED_AUDIT_RESOURCE_TYPE",
    "INGEST_ACCEPTED",
    "INGEST_DUPLICATE",
    "INGEST_REJECTED",
    "InboundChannelAdapter",
    "InboundChannelMessage",
    "IngestResult",
    "SUPPORTED_CHANNEL_TYPES",
    "SUPPORTED_INPUT_TYPES",
    "ingest_inbound_message",
]

# --- 结果状态 ------------------------------------------------------------------

#: 新建了受理作业（首次受理）。
INGEST_ACCEPTED = "accepted"
#: 重复投递：未新建作业，``intake_job_id`` 指向首次受理的作业。
INGEST_DUPLICATE = "duplicate"
#: 拒绝：未新建作业、未写 ``inbound_messages``；``reason`` 仅供内部使用。
INGEST_REJECTED = "rejected"

#: 受支持的渠道类型（W0-2 §4.2 / W0-1 §6.5 列举的渠道；新增渠道须先扩展此表）。
#: 刻意**不**在数据库层加 CHECK：渠道是开放的注册键，新增渠道不应要求迁移。
SUPPORTED_CHANNEL_TYPES: tuple[str, ...] = (
    "weixin",
    "wecom",
    "dingtalk",
    "feishu",
)

#: 受支持的输入类型，与 ``intake_jobs.input_type`` 的 CHECK 约束逐字一致。
SUPPORTED_INPUT_TYPES: tuple[str, ...] = (
    "text",
    "image",
    "file",
    "voice",
    "spreadsheet",
)

# 内部拒绝原因（只进 ``IngestResult.reason``，不进响应/日志/repr）。
_REASON_UNKNOWN_CHANNEL = "unknown_channel_type"
_REASON_UNKNOWN_INPUT = "unknown_input_type"
_REASON_MISSING_MESSAGE_ID = "missing_message_id"
_REASON_MISSING_SENDER = "missing_sender_id"
_REASON_ROUTE_CONFLICT = "route_conflict"
_REASON_CONCURRENT_CONFLICT = "concurrent_conflict"
_REASON_EMPLOYEE_MISSING = "employee_missing"

#: 受理成功的审计动作名（拒绝审计由 ``app.identity.channel_routing`` 负责）。
ACCEPTED_AUDIT_ACTION = "inbound_message.accepted"
#: 受理成功审计的 ``resource_type``；``resource_id`` 存内部 ``intake_job_id``。
ACCEPTED_AUDIT_RESOURCE_TYPE = "inbound_message"

#: 入站幂等键的唯一约束名（并发冲突的判定依据；见 ``0012_inbound_messages``）。
_DUPLICATE_KEY_CONSTRAINT = "uq_inbound_messages_channel_external_message"


# --- 适配器契约与中性消息 ------------------------------------------------------


@dataclass(frozen=True)
class InboundChannelMessage:
    """适配器归一化后的**中性**入站消息（不含任何渠道 SDK 类型）。

    只承载路由与受理所需的键，**不含消息正文**：正文属于不可信外部数据，不进入
    本服务、不落库、不入审计（PLAN §8）。

    ``input_type`` 缺省 ``text``，与 ``intake_jobs.input_type`` 的取值集合一致。
    """

    channel_type: str
    external_message_id: str
    external_user_id: str
    input_type: str = "text"


@runtime_checkable
class InboundChannelAdapter(Protocol):
    """渠道适配器协议：把渠道侧原始事件归一化为 :class:`InboundChannelMessage`。

    实现方（真实的微信 / 企业微信 / 钉钉 / 飞书适配器，后续任务）负责：校验接入账号、
    归一化消息类型、丢弃群聊等不支持场景——这些都在**适配器层**完成，本服务只接收
    已经归一化的中性对象。因此本模块可在**无渠道 SDK、无网络**的条件下被完整测试
    （测试使用桩适配器）。
    """

    @property
    def channel_type(self) -> str:
        """该适配器对应的渠道类型（必须属于 :data:`SUPPORTED_CHANNEL_TYPES`）。"""
        ...

    def normalize(self, payload: object) -> InboundChannelMessage:
        """把一条渠道原始事件归一化为中性消息。

        ``payload`` 是渠道侧原始载荷（不透明类型）；缺失消息 ID 等情形应如实返回
        空值，由 :func:`ingest_inbound_message` 统一拒绝。
        """
        ...


# --- 结果对象 ------------------------------------------------------------------


@dataclass(frozen=True)
class IngestResult:
    """一次入站受理的结果。

    * ``status``：:data:`INGEST_ACCEPTED` / :data:`INGEST_DUPLICATE` /
      :data:`INGEST_REJECTED`；
    * ``intake_job_id``：``accepted`` / ``duplicate`` 时为受理作业 ID，``rejected``
      时为 ``None``；
    * ``profile_name``：目标 Hermes Profile 名（仅计算，未创建任何目录/进程），
      ``rejected`` 时为 ``None``；
    * ``reason``：**内部**拒绝原因（如 ``unbound`` / ``revoked`` / ``unknown_input_type``）。
      除 ``reason`` 之外不含任何外部身份字段；``reason`` 本身以 ``repr=False`` 排除在
      对象字符串表示之外，且只允许进内部审计，**绝不**进入对外响应或日志。
    """

    status: str
    intake_job_id: int | None = None
    profile_name: str | None = None
    reason: str | None = field(default=None, repr=False)


# --- 内部工具 ------------------------------------------------------------------


def _non_blank(value: object) -> bool:
    """值是否为非空白字符串（``None`` / 非 ``str`` / 纯空白一律视为缺失）。"""
    return isinstance(value, str) and bool(value.strip())


def _structural_rejection(message: InboundChannelMessage) -> str | None:
    """写库**之前**的结构校验：通过返回 ``None``，否则返回内部拒绝原因。

    纯内存判定，**不触碰数据库**：未知渠道类型 / 未知输入类型 / 缺失消息 ID /
    缺失发送者标识都不会留下任何行，也不会调用身份解析。
    """
    if message.channel_type not in SUPPORTED_CHANNEL_TYPES:
        return _REASON_UNKNOWN_CHANNEL
    if not _non_blank(message.external_message_id):
        return _REASON_MISSING_MESSAGE_ID
    if not _non_blank(message.external_user_id):
        return _REASON_MISSING_SENDER
    if message.input_type not in SUPPORTED_INPUT_TYPES:
        return _REASON_UNKNOWN_INPUT
    return None


def _find_inbound(
    session: Session, *, channel_type: str, external_message_id: str
) -> InboundMessage | None:
    """按入站幂等键查既有锚点行（快路径 + 冲突后的回查共用）。"""
    return session.scalar(
        select(InboundMessage).where(
            InboundMessage.channel_type == channel_type,
            InboundMessage.external_message_id == external_message_id,
        )
    )


def _is_duplicate_key_error(exc: IntegrityError) -> bool:
    """该 ``IntegrityError`` 是否由入站幂等键的唯一约束触发。

    只把**这一条**约束的冲突翻译成 ``duplicate``；其它完整性错误（外键、CHECK 等）
    属于真实缺陷，必须照常抛出，不得被静默吞成「重复投递」。
    """
    return _DUPLICATE_KEY_CONSTRAINT in str(getattr(exc, "orig", exc))


def _duplicate_or_conflict(
    session: Session,
    existing: InboundMessage,
    *,
    message: InboundChannelMessage,
    employee_id: int,
    profile_name: str,
) -> IngestResult:
    """把「幂等键已存在」翻译成 ``duplicate`` 或 ``rejected(route_conflict)``。

    §4.4「冲突即拒绝」：同一消息 ID 再次到达却换了渠道发送者（或该作业已归属另一个
    员工，例如绑定被改绑）时不得在第二个身份下静默处理——返回 ``rejected``。
    否则返回 ``duplicate``，``intake_job_id`` 指向首次受理的作业。
    """
    job_owner = session.scalar(
        select(IntakeJob.submitted_by).where(IntakeJob.id == existing.intake_job_id)
    )
    if existing.external_user_id != message.external_user_id or job_owner != employee_id:
        return IngestResult(status=INGEST_REJECTED, reason=_REASON_ROUTE_CONFLICT)
    return IngestResult(
        status=INGEST_DUPLICATE,
        intake_job_id=existing.intake_job_id,
        profile_name=profile_name,
    )


def _record_accepted_audit(
    session: Session,
    *,
    message: InboundChannelMessage,
    employee_id: int,
    intake_job_id: int,
) -> None:
    """追加受理成功审计（与作业同事务，不单独 ``commit``）。

    * ``actor_id`` 是**已验证**的内部员工 ID（由绑定解析得出，非模型自报）；
    * ``details`` 只含渠道类型 / 输入类型 / 内部作业 ID 与渠道身份的**加盐哈希**；
      消息正文、渠道消息 ID 与渠道发送者明文**一律不写**（W0-5 §3.1/§3.2）；
    * 盐未配置时省略哈希键——**绝不**退化为写明文（与
      ``app.identity.channel_routing`` 同一口径）。
    """
    details: dict[str, Any] = {
        "channel_type": message.channel_type,
        "input_type": message.input_type,
        "intake_job_id": intake_job_id,
    }
    identity_hash = _hash_channel_identity(
        message.channel_type, message.external_user_id
    )
    if identity_hash is not None:
        details["external_user_id_hash"] = identity_hash
    record_event(
        session,
        actor_id=employee_id,
        action=ACCEPTED_AUDIT_ACTION,
        resource_type=ACCEPTED_AUDIT_RESOURCE_TYPE,
        resource_id=str(intake_job_id),
        request_id=None,
        details=details,
    )


# --- 入口 ----------------------------------------------------------------------


def ingest_inbound_message(
    session: Session,
    message: InboundChannelMessage,
    *,
    expected_company_id: int | None = None,
) -> IngestResult:
    """受理一条入站渠道消息并返回 :class:`IngestResult`（不抛身份异常）。

    流程与保证见模块 docstring：结构校验 → 身份解析 → Profile 名计算 → 幂等去重 →
    建 ``IntakeJob`` + 写 ``inbound_messages``（同事务）→ 受理审计。

    ``expected_company_id`` 原样传给 ``resolve_principal``：调用方（工具代理）已确定
    目标公司时据此防跨公司解析；本函数**不**自行推断公司。

    身份拒绝**不抛出** ``ChannelIdentityRejectedError``：它以
    ``status="rejected"`` + ``reason`` 的形式返回，调用方据此静默丢弃该事件
    （W0-2 §4.3「不创建业务记录、不调用模型、不投递任何业务回复」）。拒绝审计已由
    ``resolve_principal`` 写入，本函数不重复写。
    """
    # 1. 结构校验：写库之前，未知渠道/输入、缺失消息 ID 一律拒绝且不落库。
    reason = _structural_rejection(message)
    if reason is not None:
        return IngestResult(status=INGEST_REJECTED, reason=reason)

    # 2. 身份解析：主体只来自绑定表；拒绝（含同事务审计提交）在此被翻译成结果。
    try:
        principal = resolve_principal(
            session,
            channel_type=message.channel_type,
            external_user_id=message.external_user_id,
            expected_company_id=expected_company_id,
        )
    except ChannelIdentityRejectedError as exc:
        return IngestResult(status=INGEST_REJECTED, reason=exc.reason)

    # 3. 目标 Profile：纯计算，不创建目录、不写 ~/.hermes、不启动进程。
    employee = session.get(Employee, principal.employee_id)
    if employee is None:  # 防御：解析刚通过，正常不可达。
        return IngestResult(status=INGEST_REJECTED, reason=_REASON_EMPLOYEE_MISSING)
    profile_name = profile_name_for(employee)

    # 4. 幂等快路径：已受理过就直接返回原作业。
    existing = _find_inbound(
        session,
        channel_type=message.channel_type,
        external_message_id=message.external_message_id,
    )
    if existing is not None:
        return _duplicate_or_conflict(
            session,
            existing,
            message=message,
            employee_id=principal.employee_id,
            profile_name=profile_name,
        )

    # 5. 建作业 + 写幂等锚点（同一事务）。
    job = IntakeJob(
        submitted_by=principal.employee_id,
        channel_type=message.channel_type,
        input_type=message.input_type,
        status="received",
    )
    session.add(job)
    session.flush()

    session.add(
        InboundMessage(
            channel_type=message.channel_type,
            external_message_id=message.external_message_id,
            external_user_id=message.external_user_id,
            intake_job_id=job.id,
            status="accepted",
        )
    )
    try:
        session.flush()
    except IntegrityError as exc:
        # 并发双投递：唯一约束兜底。回滚本事务（含刚插入的作业），回查另一事务
        # 已提交的锚点行——绝不让冲突泄漏成 500，也绝不留下孤儿作业。
        session.rollback()
        if not _is_duplicate_key_error(exc):
            raise
        existing = _find_inbound(
            session,
            channel_type=message.channel_type,
            external_message_id=message.external_message_id,
        )
        if existing is None:
            # 极少数竞态（对端随后回滚）：本事务已回滚，什么都没留下，fail closed。
            return IngestResult(status=INGEST_REJECTED, reason=_REASON_CONCURRENT_CONFLICT)
        return _duplicate_or_conflict(
            session,
            existing,
            message=message,
            employee_id=principal.employee_id,
            profile_name=profile_name,
        )

    # 6. 受理审计与作业/锚点同事务提交：审计写入失败则整笔投递失败（不静默降级，
    #    避免丢失可追溯性），但绝不会产生「有作业无锚点」的半成品。
    intake_job_id = job.id
    _record_accepted_audit(
        session,
        message=message,
        employee_id=principal.employee_id,
        intake_job_id=intake_job_id,
    )
    session.commit()

    return IngestResult(
        status=INGEST_ACCEPTED,
        intake_job_id=intake_job_id,
        profile_name=profile_name,
    )
