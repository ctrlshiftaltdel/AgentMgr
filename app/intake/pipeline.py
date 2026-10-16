"""受理解析流水线的**草稿写入与人工确认**（W1-26）。

本模块是 ``app.intake.parsing`` 的持久化层：把一次 ``ParseResult`` 的裁决结果写进
``extraction_drafts``（草稿区）并推进 ``intake_jobs.status``，同时用既有
``app.audit`` 记录**解析器版本与确认人**，使每项提取值可追溯到来源文件、解析器
版本与确认人（``templates/PLAN.md`` §4.2）。

硬边界（对齐 PLAN §4.2/§11）：

* **只写草稿区**：本模块只写 ``extraction_drafts``、只更新 ``intake_jobs.status``
  与追加审计事件——**绝不写任何正式业务表**（报销/账务），也从不 import 它们；
* **原件不被覆盖**：草稿只保存 ``source_file_id`` 引用，不触碰任何文件；
* **人工确认是进入正式记录的唯一路径**：草稿 ``review_status`` 初始恒为
  ``pending``；``confirm_draft`` / ``reject_draft`` 只改草稿状态并写审计，**不写
  正式记录**（正式写入属后续任务，须经后端校验与授权）；
* **不记录原文**：审计 ``details`` 只含解析器标识/版本、原件引用、字段名与失败码；
  确认人经审计 ``actor_id`` 承载。**附件原文/识别全文绝不入日志或审计**
  （W0-5 §3.1/§3.2）。

事务边界与 ``app.audit.service.record_event`` 一致：本模块只 ``add`` / ``flush``，
**绝不 commit**——事务边界由调用方（服务层）控制，保证「草稿 + 状态 + 审计」同事务。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from app.audit.service import record_event
from app.intake.models import ExtractionDraft, IntakeJob
from app.intake.parsing import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    ParseResult,
    build_draft_payload,
    decide_draft,
    parse_provenance,
    required_fields_for,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "confirm_draft",
    "record_parse_draft",
    "reject_draft",
]


def _decision_details(
    result: ParseResult,
    *,
    business_type: str,
    required_fields: Iterable[str],
    confidence_threshold: float,
    missing_fields: Iterable[str],
    low_confidence_fields: Iterable[str],
    requires_human_review: bool,
) -> dict[str, Any]:
    """构建写入审计的 **非敏感** 裁决明细（不含任何字段取值/原文）。"""
    details = parse_provenance(result)
    details.update(
        {
            "business_type": business_type,
            "required_fields": list(required_fields),
            "confidence_threshold": confidence_threshold,
            "missing_fields": list(missing_fields),
            "low_confidence_fields": list(low_confidence_fields),
            "requires_human_review": requires_human_review,
        }
    )
    return details


def record_parse_draft(
    session: Session,
    *,
    job: IntakeJob,
    business_type: str,
    result: ParseResult,
    required_fields: Iterable[str] | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    actor_id: int | None = None,
) -> tuple[IntakeJob, ExtractionDraft | None]:
    """裁决并落库一次解析结果，返回 ``(job, draft)``。

    * 解析成功：新建 ``ExtractionDraft``（逐字段值 + 逐字段置信度 + ``source_file_id``
      原件引用，``review_status='pending'``），并把 ``job.status`` 置为
      ``'parsed'`` 或 ``'needs_review'``（低置信度/关键字段缺失时）；
    * 解析失败：不建草稿，``job.status='failed'``，写一条失败审计；
    * 两种情况都**只**写草稿区与审计，**不写正式业务表**。

    ``required_fields`` 缺省取 ``required_fields_for(business_type)``。
    ``actor_id`` 默认 ``None``（解析由系统发起、无人员主体）；调用方可在有明确操作
    人员时传入**已验证的**员工 ID。本函数不 ``commit``。
    """
    resolved_required = tuple(
        required_fields
        if required_fields is not None
        else required_fields_for(business_type)
    )

    decision = decide_draft(
        result,
        required_fields=resolved_required,
        confidence_threshold=confidence_threshold,
    )

    # 确保 job 已有主键；后续审计与草稿都要引用它。
    session.add(job)
    job.status = decision.job_status
    session.flush()

    details = _decision_details(
        result,
        business_type=business_type,
        required_fields=resolved_required,
        confidence_threshold=confidence_threshold,
        missing_fields=decision.missing_fields,
        low_confidence_fields=decision.low_confidence_fields,
        requires_human_review=decision.requires_human_review,
    )

    if not result.ok:
        record_event(
            session,
            actor_id=actor_id,
            action="intake_parse.failed",
            resource_type="intake_job",
            resource_id=str(job.id),
            details=details,
        )
        return job, None

    payload = build_draft_payload(
        intake_job_id=job.id,
        business_type=business_type,
        result=result,
    )
    draft = ExtractionDraft(**payload)
    session.add(draft)
    session.flush()

    record_event(
        session,
        actor_id=actor_id,
        action="extraction_draft.created",
        resource_type="extraction_draft",
        resource_id=str(draft.id),
        details=details,
    )
    return job, draft


def _review_details(draft: ExtractionDraft) -> dict[str, Any]:
    """确认/否决审计的 **非敏感** 明细（只含标识与字段名，不含取值）。"""
    return {
        "intake_job_id": draft.intake_job_id,
        "business_type": draft.business_type,
        "source_file_id": draft.source_file_id,
        "field_names": sorted(draft.extracted_json or {}),
    }


def confirm_draft(
    session: Session,
    *,
    draft: ExtractionDraft,
    confirmed_by: int,
    request_id: str | None = None,
) -> ExtractionDraft:
    """把草稿标记为 ``confirmed`` 并记录**确认人**（审计 ``actor_id``）。

    ``confirmed_by`` 必须是**已验证的实际人员**员工 ID（PLAN §9；严禁填模型名/
    角色名）。权限校验由调用方服务层执行（本模块不新增 HTTP 端点、不做授权判定）。

    本函数**只**改草稿 ``review_status`` 并写审计——**不写任何正式业务记录**。
    """
    if confirmed_by is None:
        raise ValueError("confirmed_by must be a verified employee id")
    draft.review_status = "confirmed"
    session.flush()
    record_event(
        session,
        actor_id=confirmed_by,
        action="extraction_draft.confirmed",
        resource_type="extraction_draft",
        resource_id=str(draft.id),
        request_id=request_id,
        details=_review_details(draft),
    )
    return draft


def reject_draft(
    session: Session,
    *,
    draft: ExtractionDraft,
    rejected_by: int,
    request_id: str | None = None,
) -> ExtractionDraft:
    """把草稿标记为 ``rejected`` 并记录**操作人**（审计 ``actor_id``）。

    与 ``confirm_draft`` 同约定：``rejected_by`` 必须是已验证的实际人员员工 ID；
    只改草稿状态并写审计，**不写正式业务记录**。
    """
    if rejected_by is None:
        raise ValueError("rejected_by must be a verified employee id")
    draft.review_status = "rejected"
    session.flush()
    record_event(
        session,
        actor_id=rejected_by,
        action="extraction_draft.rejected",
        resource_type="extraction_draft",
        resource_id=str(draft.id),
        request_id=request_id,
        details=_review_details(draft),
    )
    return draft
