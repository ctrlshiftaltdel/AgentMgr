"""发票查验 HTTP 端点：人工辅助查验结果录入（任务 W1-22）。

本模块是 ``app.integrations.invoice_service.record_manual_verification`` 的**薄
HTTP 适配层**：只负责「解析请求 → 调用服务层 → 序列化响应」，**绝不自行重写授权、
状态或证据逻辑**（``templates/PLAN.md`` §9「后端对每次调用执行授权」）。授权判定、
状态合并、证据落库与审计都由服务层执行。

关键约束（与 ``app.api.claims`` 同一约定）：

* **主体只来自受验证的头部**：每个端点的 ``principal`` 都来自
  ``app.api.deps.get_principal``（内部令牌 + ``X-Employee-Id``）。请求体中的
  ``employee_id`` 字段**仅为兼容性而声明，且被显式忽略**；任何 ``actor`` 之类的
  身份字段同样不参与——操作人绝不采信请求体（PLAN §2.1 / W0-2 §4）。
* **显式录入 ≠ provider 闸门**：本端点只承载「授权人员经官方平台核验后的显式
  录入结果」这一个动作（W0-4 §8）；``manual_assisted`` 渠道在 ``verify_invoice``
  路径下**永远**不能产出「查验通过」，该闸门语义由 W1-20 固定，本模块不改动。
* **事务边界**：服务层只 ``flush``，本层在每次调用结束时 ``commit``。业务拒绝
  （``InvoiceServiceError``）发生时，服务层已写入拒绝审计（若有），本层先
  ``commit`` 持久化该审计再向上抛出，由 ``app.api.errors`` 映射为统一错误码
  （权限拒绝 → 403、缺证据/校验 → 422、与渠道终态冲突 → 409）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_principal, get_request_id
from app.api.errors import ApiError
from app.db.session import get_session
from app.identity.authorization import Principal
from app.integrations.invoice_service import (
    InvoiceServiceError,
    record_manual_verification,
)
from app.integrations.invoice_verification import InvoiceFields, parse_invoice_date
from app.integrations.models import InvoiceVerification

__all__ = ["router"]

router = APIRouter(tags=["invoices"])


# --- 请求体模型 ---------------------------------------------------------------


class ManualVerificationRequest(BaseModel):
    """人工录入一笔查验结果的请求体。

    * ``invoice_no``（发票号码）/ ``invoice_date``（开票日期，``YYYY-MM-DD`` 或
      ``YYYYMMDD``）/ ``evidence``（非空证据说明）必填；
    * ``invoice_code``（发票代码，全电票为空）与 ``source_file_id``（原件引用）可选。

    ``employee_id`` 字段**仅为兼容性而声明，且被显式忽略**：操作人与审计主体一律
    取自已验证的 ``principal``（``X-Employee-Id`` 头），请求体里的任何身份字段
    （``employee_id``/``actor`` 等）都不能改变它（PLAN §2.1）。
    """

    invoice_no: str
    invoice_date: str
    evidence: str
    invoice_code: str | None = None
    source_file_id: int | None = None
    # 显式忽略：身份只能来自 X-Employee-Id 头（PLAN §2.1 / W0-2 §4）。
    employee_id: int | None = None


# --- 事务与序列化辅助 ---------------------------------------------------------


@contextmanager
def _unit_of_work(session: Session) -> Iterator[None]:
    """统一事务边界：成功或业务拒绝都 ``commit``，未预期异常则 ``rollback``。

    业务拒绝（``InvoiceServiceError``）意味着服务层已写入一条拒绝审计事件（若有）
    且业务状态未变；此时 ``commit`` 只持久化该审计，随后原样抛出交由
    ``app.api.errors`` 映射状态码。
    """
    try:
        yield
    except InvoiceServiceError:
        # 服务层已写拒绝审计：持久化它，业务状态不受影响。
        session.commit()
        raise
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()


def _fields_from_body(body: ManualVerificationRequest) -> InvoiceFields:
    """把请求体解析为 ``InvoiceFields``；格式非法（如开票日期）→ 422。

    发票身份的规范形式（``YYYY-MM-DD``/``YYYYMMDD``）以契约层
    ``parse_invoice_date`` 为唯一判定来源；校验失败只回受控文案，绝不回显输入原文。
    """
    parsed_date = parse_invoice_date(body.invoice_date)
    if parsed_date is None:
        raise ApiError(
            422, "validation_error", "request failed business validation"
        )
    try:
        return InvoiceFields(
            invoice_no=body.invoice_no,
            invoice_date=parsed_date,
            invoice_code=body.invoice_code,
        )
    except (TypeError, ValueError) as exc:
        raise ApiError(
            422, "validation_error", "request failed business validation"
        ) from exc


def _verification_dict(row: InvoiceVerification) -> dict[str, object]:
    """序列化录入结果（响应体**不回显**税号/票号明文）。"""
    return {
        "id": row.id,
        "status": row.status,
        "provider_type": row.provider_type,
        "provider_name": row.provider_name,
        "verified_at": (
            None if row.verified_at is None else row.verified_at.isoformat()
        ),
        "verified_by": row.verified_by,
        "duplicate_check_result": row.duplicate_check_result,
        "evidence_ref": row.evidence_ref,
    }


# --- 路由：人工辅助查验结果录入 -----------------------------------------------


@router.post("/invoice-verifications/manual", status_code=201)
def record_manual_verification_endpoint(
    body: ManualVerificationRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """显式录入一笔人工辅助查验结果（201）。

    仅授权财务人员（``invoice/verify/company``）可调用，必须带非空证据说明；操作
    人取自已验证主体。无权限 → 403（含拒绝审计）；缺证据/校验失败 → 422；既有
    渠道结论为「查验不通过」→ 409；同一张票重复录入幂等（不产生第二条记录）。
    """
    fields = _fields_from_body(body)
    with _unit_of_work(session):
        row = record_manual_verification(
            session,
            principal,
            fields,
            evidence=body.evidence,
            source_file_id=body.source_file_id,
            request_id=request_id,
        )
    return _verification_dict(row)
