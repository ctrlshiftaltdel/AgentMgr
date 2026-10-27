"""受信 Task Intake 与闭集动作注册表（Issue #104，增量 B1）。

本模块是**渠道 Bridge 与任务核心之间的唯一入口**。Bridge 只发送渠道侧键
（``channel_type`` / ``external_user_id`` / ``external_message_id``）与一个
**显式动作 ID**；它不发送、也不允许发送执行模式、owner、approver、employee id、
capability 或 policy set。服务端据此完成：

1. **闭集动作查找**：``action_id`` 必须在 :class:`ActionRegistry` 中，且
   ``enabled``。未知动作 -> ``unknown_action``；已登记但停用 ->
   ``action_not_available``（两者都是稳定拒绝码，不进入任务核心）。
2. **身份解析**：调用 :func:`app.identity.channel_routing.resolve_principal`，
   主体只来自 ``employee_channel_bindings``。未绑定 / 已解绑 / 未验证 / 歧义 /
   员工非 active / 跨公司一律拒绝；再经
   :func:`app.governance.principal.resolve_employee_principal` 转成任务核心需要的
   :class:`~app.governance.principal.GovernancePrincipal`。
3. **最终模式只来自规格**：``execution_mode`` 直接取 :attr:`ActionSpec.final_mode`；
   调用方没有这个参数，payload 中出现该字段会被拒绝。
4. **幂等**：幂等键由 ``(channel_type, external_user_id, external_message_id)``
   确定性派生（``idem_wechat_`` 前缀），**绝不使用消息正文**。同一事件重复投递
   只会得到一个任务；不同事件得到不同的键。
5. **提交**：requester / owner / executor 全部是**服务端解析出的同一名员工**；
   ``requested_capabilities`` 只含规格里的 capability；不自动执行、不调用
   dispatcher、不写审批回调。

B1 的生产注册表（:data:`B1_ACTION_REGISTRY`）**没有任何启用的建任务动作**：微信
侧只能使用由 Bridge 自身处理的只读 ``/help``、``/whoami``。设计稿中的
``policy_lookup`` / ``document_forward`` / ``inventory_entry`` 以停用条目登记，等
B2/B3/B4 落地后再逐项启用，而不是在客户端或模型侧“临时放行”。

不做的事（B1 边界）
------------------

* 不回答 advisory 问题、不调用模型；
* 不执行 governed / managed 动作，不创建审批、不写审批回调；
* 不新增数据库表或迁移（幂等依赖 ``tasks`` 既有的
  ``(requester_principal_id, idempotency_key)`` 唯一约束）；
* 不接受调用方自报的 ``employee_id`` / ``principal_id`` / owner / approver /
  ``execution_mode`` / capability —— 这些字段在函数签名中不存在，payload 里出现
  也会被 :data:`CLIENT_AUTHORITY_FIELDS` 拒绝。

事务边界
--------

本函数**不提交**成功任务：调用方拥有事务边界，必须在 ``accepted`` /
``duplicate`` 后 ``commit``。身份拒绝路径由 ``resolve_principal`` 在同一事务内
提交拒绝审计（既有行为），因此调用方**不要**在同一会话里预先压入其他待提交的
业务改动。任务核心拒绝时，调用方应 ``rollback`` 以丢弃可能已插入的草稿行。

重复投递的严格语义
------------------

同一事件键只有在 **canonical request 相同**时才返回 ``duplicate``。若同一事件
携带了不同的 goal/payload，任务核心的幂等检查会给出
``task_idempotency_conflict``，本模块以 ``rejected`` 返回——不会让第二个 payload
静默覆盖或复用第一次的任务。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.governance.errors import GovernanceError, PrincipalResolutionError
from app.governance.policies import EXECUTION_MODES
from app.governance.principal import (
    GovernancePrincipal,
    resolve_employee_principal,
)
from app.identity.channel_routing import (
    ChannelIdentityRejectedError,
    resolve_principal,
)
from app.tasks.errors import TaskError
from app.tasks.models import Task
from app.tasks.service import submit_task

__all__ = [
    "ACTION_NOT_AVAILABLE",
    "B1_ACTION_REGISTRY",
    "CLIENT_AUTHORITY_FIELDS",
    "IDEMPOTENCY_KEY_PREFIX",
    "INTAKE_ACCEPTED",
    "INTAKE_DUPLICATE",
    "INTAKE_REJECTED",
    "ActionRegistry",
    "ActionRegistryError",
    "ActionSpec",
    "TaskIntakeResult",
    "channel_idempotency_key",
    "submit_action",
]

# --- 结果状态与稳定拒绝码 ------------------------------------------------------

#: 新建了一个任务。
INTAKE_ACCEPTED = "accepted"
#: 同一渠道事件重复投递：未新建任务，``task_id`` 指向首次提交的任务。
INTAKE_DUPLICATE = "duplicate"
#: 拒绝：未新建任务；``reason`` 仅供内部审计 / 调试，``repr`` 中不出现。
INTAKE_REJECTED = "rejected"

#: 动作已登记但当前未启用（B1 的生产条目都是这一状态）。
ACTION_NOT_AVAILABLE = "action_not_available"

#: 渠道幂等键的固定前缀（``submit_task`` 要求 ``idem_*``）。
IDEMPOTENCY_KEY_PREFIX = "idem_wechat_"

#: 调用方**绝不能**出现在 payload 里的权威字段。它们要么由服务端从绑定表解析
#: （employee / owner / approver / principal），要么由闭集规格固定（mode /
#: capability / policy set / action id）。出现即拒绝，避免“先收下再解释”。
CLIENT_AUTHORITY_FIELDS: frozenset[str] = frozenset(
    {
        "action_id",
        "approver",
        "approver_employee_id",
        "approver_principal_id",
        "capabilities",
        "capability",
        "channel_type",
        "employee_id",
        "execution_mode",
        "executor",
        "executor_employee_id",
        "executor_principal_id",
        "external_message_id",
        "external_user_id",
        "final_mode",
        "idempotency_key",
        "owner",
        "owner_employee_id",
        "owner_principal_id",
        "policy_set",
        "policy_set_id",
        "policy_version",
        "principal_id",
        "requester",
        "requester_employee_id",
        "requester_principal_id",
        "requested_capabilities",
        "resource_scope",
        "service_identity_id",
    }
)

# 内部拒绝原因（只进 ``TaskIntakeResult.reason``；绝不进入任务 / 异常文案）。
_REASON_UNKNOWN_ACTION = "unknown_action"
_REASON_UNSUPPORTED_CHANNEL = "unsupported_channel"
_REASON_MISSING_EVENT_KEY = "missing_event_key"
_REASON_MISSING_SENDER = "missing_sender_id"
_REASON_EMPLOYEE_MISSING = "employee_missing"
_REASON_PRINCIPAL_UNRESOLVED = "principal_unresolved"
_REASON_INVALID_PAYLOAD = "invalid_payload"
_REASON_CLIENT_AUTHORITY_FIELD = "client_authority_field"
_REASON_APPROVAL_UNAVAILABLE = "approval_not_supported"


# --- 动作规格与闭集注册表 ------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """一个动作的**唯一**规则来源。

    ``action_id`` 是客户端唯一可以选择的键；其余字段全部由服务端决定。
    ``final_mode`` 必须是治理层已知的执行模式；任务提交时直接写入该值，策略规则
    只允许这个模式（由 :attr:`policy_set` 指向的已发布策略保证），不依赖 PDP
    “自动升级”。
    """

    action_id: str
    capability: str
    final_mode: str
    policy_set: str
    approval_required: bool = False
    allowed_scope: Mapping[str, Any] | None = None
    enabled: bool = True


class ActionRegistryError(Exception):
    """闭集查找失败（未知或未启用）。``reason`` 是内部稳定码。"""

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ActionRegistry:
    """动作 ID 的**闭集**：不在构造时给出的 ID 一律找不到。

    构造时校验规格（非空 ID / 合法 capability / 已知执行模式 / 非空 policy set），
    并拒绝重复 ID。查找只走私有映射，不提供“临时追加”或“按 capability 搜索”
    的入口，避免调用方绕过闭集。
    """

    def __init__(self, specs: Iterable[ActionSpec]) -> None:
        table: dict[str, ActionSpec] = {}
        for spec in specs:
            self._validate(spec)
            if spec.action_id in table:
                raise ValueError(f"duplicate action_id: {spec.action_id!r}")
            table[spec.action_id] = spec
        self._specs = table

    @staticmethod
    def _validate(spec: ActionSpec) -> None:
        if not isinstance(spec.action_id, str) or not spec.action_id.strip():
            raise ValueError("action_id must be a non-empty string")
        if not isinstance(spec.capability, str) or "@" not in spec.capability:
            raise ValueError(
                f"capability must be a versioned '<name>@<version>' string: {spec.capability!r}"
            )
        if spec.final_mode not in EXECUTION_MODES:
            raise ValueError(f"unknown execution mode: {spec.final_mode!r}")
        if not isinstance(spec.policy_set, str) or not spec.policy_set.strip():
            raise ValueError("policy_set must be a non-empty string")
        if not isinstance(spec.approval_required, bool):
            raise ValueError("approval_required must be a boolean")
        if not isinstance(spec.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if spec.enabled:
            if not isinstance(spec.allowed_scope, Mapping):
                raise ValueError(
                    "an enabled action must declare its server-side allowed_scope"
                )

    def lookup(self, action_id: object) -> ActionSpec:
        """返回启用中的规格；未知 / 停用均抛出 :class:`ActionRegistryError`。"""
        if not isinstance(action_id, str) or not action_id:
            raise ActionRegistryError(reason=_REASON_UNKNOWN_ACTION)
        spec = self._specs.get(action_id)
        if spec is None:
            raise ActionRegistryError(reason=_REASON_UNKNOWN_ACTION)
        if not spec.enabled:
            raise ActionRegistryError(reason=ACTION_NOT_AVAILABLE)
        return spec

    def action_ids(self) -> tuple[str, ...]:
        """已登记的动作 ID（含停用条目），顺序稳定，便于运维核对。"""
        return tuple(sorted(self._specs))


#: B1 生产注册表：**没有启用的建任务动作**。
#:
#: 设计稿中的三个动作以停用状态登记，让运维与测试能明确看到“已登记但 B1 不可用”
#: 与“完全未知”是两种不同的稳定拒绝；启用它们属于 B2/B3/B4。
B1_ACTION_REGISTRY = ActionRegistry(
    (
        ActionSpec(
            action_id="policy_lookup",
            capability="policy.read@1.0.0",
            final_mode="advisory",
            policy_set="wechat_b2_readonly_pending",
            enabled=False,
        ),
        ActionSpec(
            action_id="document_forward",
            capability="document.forward@1.0.0",
            final_mode="governed_resource_access",
            policy_set="wechat_b3_governed_pending",
            approval_required=True,
            enabled=False,
        ),
        ActionSpec(
            action_id="inventory_entry",
            capability="inventory.write@1.0.0",
            final_mode="managed_execution",
            policy_set="wechat_b4_managed_pending",
            approval_required=True,
            enabled=False,
        ),
    )
)


# --- 结果对象 ------------------------------------------------------------------


@dataclass(frozen=True)
class TaskIntakeResult:
    """一次动作提交的结果。

    * ``status``：``accepted`` / ``duplicate`` / ``rejected``；
    * ``task_id``：``accepted`` / ``duplicate`` 时为任务 ID，``rejected`` 时为
      ``None``；
    * ``reason``：**内部**拒绝原因（身份拒绝原因、注册表稳定码或任务层的
      ``reason_code``）。``repr=False`` 保证对象字符串表示不泄漏内部原因。
    """

    status: str
    task_id: str | None = None
    reason: str | None = field(default=None, repr=False)


# --- 幂等键 --------------------------------------------------------------------


def channel_idempotency_key(
    *,
    channel_type: str,
    external_user_id: str,
    external_message_id: str,
) -> str:
    """从渠道事件键确定性派生任务幂等键（不使用消息正文）。

    使用不可歧义的域分隔 ``\\x1f`` 拼接三个键，避免
    ``("a", "b:c", "d")`` 与 ``("a", "b", "c:d")`` 撞键。摘要只含渠道侧事件标识，
    不含正文、附件或任何员工明文。
    """
    material = "\x1f".join(
        (channel_type, external_user_id, external_message_id)
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    # task.idempotency_key has a DB CHECK of ``idem_`` + 1..? (max 69 chars):
    # keep the channel component short while retaining 192 bits of the digest.
    return f"{IDEMPOTENCY_KEY_PREFIX}{digest[:48]}"


# --- 内部工具 ------------------------------------------------------------------


def _non_blank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _find_existing_task(
    session: Session, *, requester_principal_id: str, idempotency_key: str
) -> Task | None:
    return session.scalar(
        select(Task).where(
            Task.requester_principal_id == requester_principal_id,
            Task.idempotency_key == idempotency_key,
        )
    )


def _reject(reason: str) -> TaskIntakeResult:
    return TaskIntakeResult(status=INTAKE_REJECTED, reason=reason)


def _client_authority_conflict(payload: Mapping[str, Any]) -> str | None:
    found = sorted(CLIENT_AUTHORITY_FIELDS.intersection(payload))
    return found[0] if found else None


def _goal_request(
    payload: Mapping[str, Any] | None,
    *,
    spec: ActionSpec,
) -> tuple[dict[str, Any], Mapping[str, Any] | None, str | None] | None:
    """从客户端 payload 中取出 goal / execution payload / summary。

    ``payload`` 只承载**业务描述**，权威字段一律拒绝。资源范围由
    :attr:`ActionSpec.allowed_scope` 固定，客户端不能提供 ``resource_scope``；
    客户端尝试提供会被 :data:`CLIENT_AUTHORITY_FIELDS` 拒绝，而不是静默覆盖。
    """
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        return None
    conflict = _client_authority_conflict(payload)
    if conflict is not None:
        raise _ClientAuthorityError(conflict)
    goal = payload.get("goal")
    if not isinstance(goal, Mapping):
        return None
    execution_payload = payload.get("execution_payload")
    if execution_payload is not None and not isinstance(execution_payload, Mapping):
        return None
    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        return None
    return dict(goal), execution_payload, summary


class _ClientAuthorityError(Exception):
    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(field)


# --- 入口 ----------------------------------------------------------------------


def submit_action(
    session: Session,
    *,
    channel_type: str = "weixin",
    external_user_id: str,
    external_message_id: str,
    action_id: str,
    payload: Mapping[str, Any] | None = None,
    expected_company_id: int | None = None,
    registry: ActionRegistry | None = None,
) -> TaskIntakeResult:
    """把一个显式渠道动作提交为任务（B1 契约，详见模块 docstring）。

    参数全部是渠道侧事实或服务端配置：

    * ``channel_type``：B1 只接受 ``weixin``；
    * ``external_user_id`` / ``external_message_id``：渠道身份与稳定事件键；
    * ``action_id``：闭集动作 ID；
    * ``payload``：可选的业务描述（``goal`` / ``execution_payload`` /
      ``summary``）。**不得**包含任何权威字段，包括 ``resource_scope``；
    * ``expected_company_id``：调用方已确定目标公司时传给身份解析，防跨公司；
    * ``registry``：默认使用 :data:`B1_ACTION_REGISTRY`；测试可注入自己的闭集。

    函数**不抛**身份拒绝异常：拒绝以 ``status="rejected"`` + 内部 ``reason``
    返回。身份拒绝审计由 ``resolve_principal`` 自行写入；本函数不重复写，也不
    记录任何消息正文。
    """
    active_registry = registry if registry is not None else B1_ACTION_REGISTRY

    if channel_type != "weixin":
        return _reject(_REASON_UNSUPPORTED_CHANNEL)
    if not _non_blank(external_message_id):
        return _reject(_REASON_MISSING_EVENT_KEY)
    if not _non_blank(external_user_id):
        return _reject(_REASON_MISSING_SENDER)

    try:
        spec = active_registry.lookup(action_id)
    except ActionRegistryError as exc:
        return _reject(exc.reason)

    try:
        request = _goal_request(payload, spec=spec)
    except _ClientAuthorityError:
        return _reject(_REASON_CLIENT_AUTHORITY_FIELD)
    if request is None:
        return _reject(_REASON_INVALID_PAYLOAD)

    # B1 没有受信 approver 解析器，审批类动作必须等 B3；绝不能因此从客户端取
    # approver，也不能创建“看起来已受理”的审批任务。
    if spec.approval_required:
        return _reject(_REASON_APPROVAL_UNAVAILABLE)

    try:
        identity = resolve_principal(
            session,
            channel_type=channel_type,
            external_user_id=external_user_id,
            expected_company_id=expected_company_id,
        )
    except ChannelIdentityRejectedError as exc:
        return _reject(exc.reason)

    try:
        principal: GovernancePrincipal = resolve_employee_principal(
            session, employee_id=identity.employee_id
        )
    except PrincipalResolutionError as exc:
        return _reject(exc.reason_code)
    except GovernanceError as exc:  # pragma: no cover - defensive
        return _reject(exc.reason_code)

    goal, execution_payload, summary = request

    idempotency_key = channel_idempotency_key(
        channel_type=channel_type,
        external_user_id=external_user_id,
        external_message_id=external_message_id,
    )

    existing = _find_existing_task(
        session,
        requester_principal_id=principal.principal_id,
        idempotency_key=idempotency_key,
    )

    try:
        task = submit_task(
            session,
            requester=principal,
            owner=principal,
            executor=principal,
            idempotency_key=idempotency_key,
            goal=goal,
            summary=summary,
            execution_mode=spec.final_mode,
            resource_scope=spec.allowed_scope,
            requested_capabilities=(spec.capability,),
            policy_set_id=spec.policy_set,
            payload=execution_payload,
        )
    except TaskError as exc:
        # 任务层的确定性拒绝（策略拒绝、缺输入、payload 冲突等）一律翻译成
        # 内部原因码；不把异常文案或上下文带到渠道回复。即使 ``existing`` 非空，
        # 也仍调用核心：同一幂等键配不同请求会得到 ``task_idempotency_conflict``，
        # 而不是被静默当成重复投递。
        return _reject(exc.reason_code)

    if existing is not None:
        return TaskIntakeResult(status=INTAKE_DUPLICATE, task_id=task.task_id)
    return TaskIntakeResult(status=INTAKE_ACCEPTED, task_id=task.task_id)
