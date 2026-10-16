"""报销单与发票的关联（挂账）服务：attach / sync / list / 提交前校验。

本模块把发票查验域（``app.integrations``）的七状态结果接进报销流程（任务
W1-23），对齐 ``templates/PLAN.md`` §7.2/§8 与 ``docs/PROJECT-CHARTER.md`` 强制
原则 5/6：

* :func:`attach_invoice` 把一张发票（票面要素快照）挂到报销单草稿上。仅限报销人
  本人且仅限 ``draft``（与 ``add_line`` 同模式）；**状态一律由服务端推导**——按
  ``duplicate_key`` 读 ``invoice_verifications``，存在则**逐字复制**其
  ``status``，不存在则置 ``pending_verification``（明确待查验）。函数签名中没有、
  将来也不得有可由调用方传入状态的参数：``verified_pass`` 只能由被认可渠道经
  查验服务写入查验记录，报销域只做只读推导；
* **同一报销单内重复挂账**被服务层先查后拒（可读错误
  :class:`DuplicateInvoiceAttachError`），并发竞态由唯一约束
  ``uq_expense_invoices_claim_duplicate`` 兜底；
* **跨报销单重复**：同一张票出现在其它报销单上时，本行与其它行**均**置
  ``suspected_duplicate = True``（业务去重；渠道不提供重复判定），并通过
  :func:`find_cross_claim_duplicates` 提供可查询入口；
* :func:`sync_expense_invoice_status` 从查验记录同步最新状态（有行走逐字复制，
  无行置 ``pending_verification``，绝不写成「通过」以外的臆造值）；
* 提交钩子 :func:`_validate_claim_invoices_for_submit`（由
  ``app.expense.service.submit`` 在空明细校验之后、状态变更之前调用）：先对全部
  挂账行做 sync（live 读 ``invoice_verifications``），再拒绝任何
  ``verified_fail`` 或不为七状态之一的挂账行——抛**既有**
  ``ValidationError``（可被 ``app.api.errors`` 映射，不新增未映射异常类型），
  已挂账且通过/待查验/无法查验/疑似重复的行允许提交，状态原样保留。

审计与事务约定与 ``app.expense.service`` 完全一致：成功写
``expense_claim.invoice.attach``，拒绝写 ``expense_claim.deny``（``details`` 只含
非敏感结构化值：不含税号、票号明文——票面身份只以单向摘要 ``duplicate_key``
出现）；所有函数只 ``flush``，**不 commit**。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.expense.models import ExpenseClaim, ExpenseInvoice
from app.expense.service import (
    ValidationError,
    _load_claim,
    _record_deny,
    _record_success,
    _require_owner,
)
from app.identity.authorization import Principal
from app.integrations.invoice_verification import (
    InvoiceFields,
    VerificationStatus,
    compute_duplicate_key,
)
from app.integrations.models import STATUS_VALUES, InvoiceVerification

__all__ = [
    "ATTACH_ACTION",
    "DuplicateInvoiceAttachError",
    "attach_invoice",
    "find_cross_claim_duplicates",
    "list_claim_invoices",
    "sync_expense_invoice_status",
]

#: 挂账成功审计的动作名。
ATTACH_ACTION = "expense_claim.invoice.attach"


class DuplicateInvoiceAttachError(ValidationError):
    """同一张票已经在**同一张报销单**内挂账（服务层先查后拒）。

    是 ``ValidationError`` 的子类：``app.api.errors`` 按 MRO 命中既有的
    ``ValidationError`` 处理器（422），不引入未映射的异常类型。
    """


# --- 内部辅助 -----------------------------------------------------------------


def _invoice_fields_snapshot(fields: InvoiceFields) -> dict[str, Any]:
    """票面要素快照：**不含税号**；金额以字符串定点保存（禁止 float）。

    ``InvoiceFields`` 构造时已把 ``invoice_date`` 规范为 ``date``、把 ``amount``
    拒绝 ``float`` 并量化到 2 位小数，因此这里只做**无损**序列化：日期用 ISO
    字符串、金额用其十进制字符串表示。
    """
    return {
        "invoice_code": fields.invoice_code,
        "invoice_no": fields.invoice_no,
        "invoice_date": fields.invoice_date.isoformat(),
        "invoice_kind": fields.invoice_kind,
        "amount": None if fields.amount is None else str(fields.amount),
    }


def _verification_status_for(session: Session, duplicate_key: str) -> str:
    """按 ``duplicate_key`` 从查验记录推导挂账状态（attach / sync 唯一来源）。

    存在查验行 → **逐字复制**其 ``status``（该列有 CHECK 限定为七状态之一）；
    不存在 → ``pending_verification``（明确待查验）。**从不臆造取值、从不改写为
    「查验通过」**。
    """
    verification = session.scalar(
        select(InvoiceVerification).where(
            InvoiceVerification.duplicate_key == duplicate_key
        )
    )
    if verification is None:
        return VerificationStatus.PENDING_VERIFICATION.value
    return verification.status


def _claim_invoice_rows(session: Session, claim_id: int) -> list[ExpenseInvoice]:
    """某报销单的全部挂账行（按 id 升序）。"""
    return list(
        session.scalars(
            select(ExpenseInvoice)
            .where(ExpenseInvoice.claim_id == claim_id)
            .order_by(ExpenseInvoice.id)
        )
    )


# --- 服务函数 -----------------------------------------------------------------


def attach_invoice(
    session: Session,
    principal: Principal,
    claim_id: int,
    *,
    fields: InvoiceFields,
    source_file_id: int | None = None,
    request_id: str | None = None,
) -> ExpenseInvoice:
    """把一张发票挂到报销单草稿上（仅本人 + 仅 ``draft``）。

    步骤（任何一项不满足都在**写入之前**抛错并写拒绝审计）：

    1. 仅本人（``_require_owner``）；报销单不存在 → ``ClaimNotFoundError``；
       非 ``draft`` → ``claim_not_draft``（``ValidationError``，与 ``add_line``
       同模式）；
    2. ``duplicate_key = compute_duplicate_key(fields)``（复用查验域的单向身份
       摘要，不重写算法）；
    3. 同一报销单内该票已挂账 → :class:`DuplicateInvoiceAttachError`（先查后拒；
       并发由唯一约束 ``uq_expense_invoices_claim_duplicate`` 兜底）；
    4. 跨报销单存在该票 → 本行与其它行**均**置 ``suspected_duplicate=True``；
    5. ``verification_status`` 由 :func:`_verification_status_for` 推导（逐字
       复制查验记录或无记录时 ``pending_verification``）——**函数签名中没有任何
       可传入状态的参数**。

    只 ``flush``，不 ``commit``；成功写一条 ``expense_claim.invoice.attach``
    审计（details 不含税号/票号明文）。
    """
    claim = _load_claim(session, claim_id)
    _require_owner(
        session,
        principal,
        claim,
        attempted_action="attach_invoice",
        request_id=request_id,
    )

    if claim.status != "draft":
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action="attach_invoice",
            reason="claim_not_draft",
            request_id=request_id,
        )
        raise ValidationError(
            attempted_action="attach_invoice",
            reason="claim_not_draft",
            claim_id=claim.id,
        )

    duplicate_key = compute_duplicate_key(fields)

    # 同一报销单内重复挂账：服务层先查后拒（可读错误）；并发由唯一约束兜底
    # （约束违反会以 IntegrityError 失败，绝不会产生第二行）。
    already_attached = session.scalar(
        select(ExpenseInvoice.id)
        .where(
            ExpenseInvoice.claim_id == claim.id,
            ExpenseInvoice.duplicate_key == duplicate_key,
        )
        .limit(1)
    )
    if already_attached is not None:
        _record_deny(
            session,
            principal,
            resource_id=str(claim.id),
            attempted_action="attach_invoice",
            reason="invoice_already_attached",
            request_id=request_id,
        )
        raise DuplicateInvoiceAttachError(
            attempted_action="attach_invoice",
            reason="invoice_already_attached",
            claim_id=claim.id,
        )

    # 跨报销单重复：本行与其它报销单上的既有行**均**标记 suspected_duplicate。
    cross_rows = list(
        session.scalars(
            select(ExpenseInvoice).where(
                ExpenseInvoice.duplicate_key == duplicate_key,
                ExpenseInvoice.claim_id != claim.id,
            )
        )
    )
    for other in cross_rows:
        other.suspected_duplicate = True

    status_value = _verification_status_for(session, duplicate_key)

    row = ExpenseInvoice(
        claim_id=claim.id,
        source_file_id=source_file_id,
        invoice_fields_json=_invoice_fields_snapshot(fields),
        verification_status=status_value,
        duplicate_key=duplicate_key,
        suspected_duplicate=bool(cross_rows),
    )
    session.add(row)
    session.flush()

    _record_success(
        session,
        principal,
        claim,
        action=ATTACH_ACTION,
        from_status=claim.status,
        to_status=claim.status,
        request_id=request_id,
        details={
            "claim_id": claim.id,
            "duplicate_key": duplicate_key,
            "has_source_file": source_file_id is not None,
            "verification_status": status_value,
            "suspected_duplicate": row.suspected_duplicate,
        },
    )
    return row


def sync_expense_invoice_status(session: Session, row: ExpenseInvoice) -> ExpenseInvoice:
    """从 ``invoice_verifications`` 按 ``duplicate_key`` 同步挂账行状态。

    有查验行 → 逐字复制其**当前** ``status``；无查验行 → 置
    ``pending_verification``。绝不改写为臆造值（尤其是绝不置为「查验通过」——
    通过只能来自查验记录本身）。
    """
    row.verification_status = _verification_status_for(session, row.duplicate_key)
    return row


def list_claim_invoices(session: Session, claim: ExpenseClaim) -> list[ExpenseInvoice]:
    """列出某报销单的全部挂账行（按 id 升序）。"""
    return _claim_invoice_rows(session, claim.id)


def find_cross_claim_duplicates(
    session: Session, duplicate_key: str
) -> list[ExpenseInvoice]:
    """「可查询」入口：返回该票（``duplicate_key``）跨报销单的全部挂账行。

    按 id 升序；同一张票挂到 N 张报销单时返回 N 行。
    """
    return list(
        session.scalars(
            select(ExpenseInvoice)
            .where(ExpenseInvoice.duplicate_key == duplicate_key)
            .order_by(ExpenseInvoice.id)
        )
    )


def _validate_claim_invoices_for_submit(
    session: Session,
    principal: Principal,
    claim: ExpenseClaim,
    *,
    request_id: str | None,
) -> None:
    """提交钩子（仅由 ``app.expense.service.submit`` 调用）。

    先对全部挂账行做 sync（live 读 ``invoice_verifications``，把「挂账时的状态」
    推进到「当前状态」），再校验：任一（同步后）``verification_status`` 为
    ``verified_fail`` → 以 ``invoice_verification_failed`` 拒绝；不为七状态之一
    → 以 ``invoice_status_unknown`` 拒绝。两者都写拒绝审计并抛**既有**
    ``ValidationError``（映射为 422，不引入未映射异常）。

    ``verified_pass`` 与各「待…」/``unable_to_verify``/``suspected_duplicate``
    状态允许提交，且**原样保留**，绝不改写。
    """
    rows = _claim_invoice_rows(session, claim.id)
    for row in rows:
        sync_expense_invoice_status(session, row)
    for row in rows:
        if row.verification_status == VerificationStatus.VERIFIED_FAIL.value:
            _record_deny(
                session,
                principal,
                resource_id=str(claim.id),
                attempted_action="submit",
                reason="invoice_verification_failed",
                request_id=request_id,
            )
            raise ValidationError(
                attempted_action="submit",
                reason="invoice_verification_failed",
                claim_id=claim.id,
            )
    for row in rows:
        if row.verification_status not in STATUS_VALUES:
            _record_deny(
                session,
                principal,
                resource_id=str(claim.id),
                attempted_action="submit",
                reason="invoice_status_unknown",
                request_id=request_id,
            )
            raise ValidationError(
                attempted_action="submit",
                reason="invoice_status_unknown",
                claim_id=claim.id,
            )
