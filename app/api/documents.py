"""文件上传 HTTP 端点：原件入库（复用内容寻址存储层的四道闸门）。

本模块是 ``app.documents.storage`` 的**薄 HTTP 适配层**：只负责「解析上传 →
调用存储层 → 落 ``source_files`` 元数据 → 审计 → 序列化响应」，**绝不自行实现
类型 / 大小 / 路径 / 散列判定**。四道闸门（大小、真实魔数类型、路径安全、哈希）
全部由 ``store_file`` 执行（``docs/research/W0-5-security-compliance.md`` §4.3
恶意文件上传与 §5.5 路径拼接禁止规则；``templates/PLAN.md`` §8「文件路径不能由
用户输入直接拼接」「对附件实施文件类型/大小限制、恶意文件扫描、授权下载和散列
校验」）。

关键约束：

* **主体只来自受验证的头部**：``principal`` 来自 ``app.api.deps.get_principal``
  （内部令牌 + ``X-Employee-Id``）。表单里出现的任何身份字段都不参与——上传归属
  一律取自已验证主体（PLAN §2.1 / W0-2 §4）。
* **附件一律视为不可信输入**（W0-5 §4.3）：声明的文件名**只**用于读取扩展名与
  落 ``original_name`` 展示元数据，**绝不用于拼接文件路径**——物理路径由存储层按
  内容寻址 ``sha[:2]/sha[2:4]/sha`` 生成；真实类型只按内容魔数判定。
* **先鉴权、后落盘**：无上传权限时**不写任何字节**（不落盘、不落库），只写一条
  拒绝审计并返回 403（默认拒绝，PLAN §9）。
* **审计与业务同事务**：成功写 ``document.upload``（``details`` 只含
  ``sha256`` / 大小 / MIME / 扫描状态 / 去重标记等非敏感结构化值，**不含文件名
  原文**）；拒绝写 ``document.upload.deny``。两者都与业务写入一起提交，拒绝不会
  因异常回滚而丢失（PLAN §12「权限拒绝可审计」）。
* **大小闸门先于缓冲**：multipart 解析把文件写入 ``SpooledTemporaryFile``（超过
  1 MiB 即落临时文件），因此可以先用 ``UploadFile.size`` 拒绝超限请求，避免把
  超大文件整体读入内存（W0-5 §4.3「超大文件」）。``store_file`` 内部的同名闸门
  仍是**权威判定**，本层只是提前拒绝，二者使用同一个上限常量。

权限口径（已知限制，见 PR 说明）：上传闸门为 ``expense_claim/create/own``——能为
自己创建报销单者，才能上传该报销所需票据原件，与「挂账仅限报销人本人」的服务层
语义一致。``app.identity.permissions`` 的 20 条权限目录是**封闭集合**，本任务允许
路径只有 ``app/api/``，因此财务角色（不持有 create 权限）暂不能使用本端点上传查验
证据；如需放开，应由独立任务新增目录权限后一并调整此处。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_principal, get_request_id
from app.api.errors import ApiError
from app.audit.service import record_event
from app.db.session import get_session
from app.documents.models import SourceFile
from app.documents.storage import (
    DEFAULT_MAX_BYTES,
    FileTooLargeError,
    default_storage_root,
    store_file,
)
from app.identity.authorization import Principal, has_permission

__all__ = [
    "UPLOAD_ACTION",
    "UPLOAD_DENY_ACTION",
    "UPLOAD_PERMISSION",
    "router",
]

router = APIRouter(tags=["documents"])

#: 上传所需权限三元组：能为自己创建报销单者才可上传原件（见模块 docstring）。
UPLOAD_PERMISSION: tuple[str, str, str] = ("expense_claim", "create", "own")

#: 成功 / 拒绝审计的动作名（沿用 ``<资源>.<动作>[.deny]`` 的既有命名）。
UPLOAD_ACTION = "document.upload"
UPLOAD_DENY_ACTION = "document.upload.deny"

#: 审计与拒绝事件使用的资源类型。
RESOURCE_TYPE = "source_file"

#: 拒绝审计里的动作名（与其它域的 ``attempted_action`` 口径一致）。
ATTEMPTED_ACTION = "upload_document"

#: 与 ``app.api.errors`` 中 ``authorization_denied`` 的受控文案保持一致：本模块
#: 只映射状态码与稳定错误码，**不新增**错误映射语义，响应体与既有 API 完全相同。
_MESSAGE_AUTHORIZATION_DENIED = "authorization denied"


# --- 事务与序列化辅助 ---------------------------------------------------------


@contextmanager
def _unit_of_work(session: Session) -> Iterator[None]:
    """统一事务边界：成功或权限拒绝都 ``commit``，未预期异常则 ``rollback``。

    ``ApiError`` 意味着权限拒绝——此时已写入一条拒绝审计而业务状态未变，因此
    ``commit`` 只持久化该审计，随后原样抛出交由 ``app.api.errors`` 映射状态码。
    """
    try:
        yield
    except ApiError:
        # 权限拒绝：持久化拒绝审计，业务状态不受影响。
        session.commit()
        raise
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()


def _source_file_dict(row: SourceFile, *, deduped: bool) -> dict[str, object]:
    """序列化 ``source_files`` 元数据行（响应**不含**物理存储键/路径）。

    ``deduped`` 来自存储层：相同内容已在库中时物理 blob 不重写（内容寻址去重），
    但每次上传仍各留一行元数据。
    """
    return {
        "id": row.id,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "mime_type": row.mime_type,
        "scan_status": row.scan_status,
        "original_name": row.original_name,
        "deduped": deduped,
    }


def _record_upload_deny(
    session: Session,
    principal: Principal,
    *,
    request_id: str | None,
    reason: str,
) -> None:
    """写入一条上传拒绝审计（不 commit；由 ``_unit_of_work`` 随拒绝一起提交）。"""
    record_event(
        session,
        actor_id=principal.employee_id,
        action=UPLOAD_DENY_ACTION,
        resource_type=RESOURCE_TYPE,
        resource_id=None,
        request_id=request_id,
        details={"attempted_action": ATTEMPTED_ACTION, "reason": reason},
    )


# --- 路由：上传原件 -----------------------------------------------------------


@router.post("/documents", status_code=201)
def upload_document_endpoint(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
    request_id: str | None = Depends(get_request_id),
) -> dict[str, object]:
    """上传一份原件（multipart ``file`` 字段）→ 201 + ``source_files`` 元数据。

    流程（任一步失败即在写入之前拒绝）：

    1. **鉴权**：需 ``expense_claim/create/own``；无权限 → 403
       ``authorization_denied`` + 拒绝审计，**不落盘、不落库**；
    2. **大小**：``UploadFile.size`` 超 ``DEFAULT_MAX_BYTES``（20 MiB）→ 400
       ``bad_request``，**不读取内容**；
    3. **存储层四道闸门**：``store_file`` 依次执行大小、真实魔数类型、路径安全、
       内容寻址哈希；类型伪装 / 超限 / 路径穿越 → 400 ``bad_request``；
    4. **元数据**：写一行 ``source_files``（``owner_id`` 取自已验证主体，
       ``scan_status`` 初始 ``pending``——未扫描文件不得进入业务）；
    5. **审计**：写 ``document.upload``（``details`` 不含文件名原文）。

    响应为 ``id`` / ``sha256`` / ``size_bytes`` / ``mime_type`` / ``scan_status`` /
    ``original_name`` / ``deduped``；**不返回**物理存储键与路径。相同内容重复上传
    时各留一行元数据（``deduped=True`` 表示物理 blob 未重写）。
    """
    with _unit_of_work(session):
        if not has_permission(session, principal, *UPLOAD_PERMISSION):
            _record_upload_deny(
                session,
                principal,
                request_id=request_id,
                reason="missing_permission",
            )
            raise ApiError(403, "authorization_denied", _MESSAGE_AUTHORIZATION_DENIED)

        # 先按已解析出的真实大小拒绝，避免把超大文件整体读入内存；store_file
        # 内部的同一闸门仍是权威判定（本层只是提前拒绝，用的是同一个常量）。
        declared_size = file.size
        if declared_size is not None and declared_size > DEFAULT_MAX_BYTES:
            raise FileTooLargeError(
                f"content size {declared_size} exceeds limit {DEFAULT_MAX_BYTES}"
            )

        content = file.file.read()
        stored = store_file(
            default_storage_root(),
            owner_id=principal.employee_id,
            content=content,
            declared_name=file.filename,
        )

        row = SourceFile(
            owner_id=principal.employee_id,
            storage_key=stored.storage_key,
            sha256=stored.sha256,
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            original_name=file.filename,
        )
        session.add(row)
        session.flush()

        record_event(
            session,
            actor_id=principal.employee_id,
            action=UPLOAD_ACTION,
            resource_type=RESOURCE_TYPE,
            resource_id=str(row.id),
            request_id=request_id,
            details={
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
                "mime_type": stored.mime_type,
                "scan_status": row.scan_status,
                "deduped": stored.deduped,
            },
        )

    return _source_file_dict(row, deduped=stored.deduped)
