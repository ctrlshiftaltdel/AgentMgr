"""文件上传端点的 API 级测试（Issue #60，端点 ``POST /documents``）。

覆盖验收标准：

a. 合法上传（multipart）→ 201 + ``source_files`` 元数据（``id`` / ``sha256`` /
   ``size_bytes`` / ``mime_type`` / ``scan_status``）；落库归属为**已验证主体**；
   物理 blob 按内容寻址落盘；成功审计存在且 ``details`` 不含文件名原文；
b. 内容寻址去重：相同内容重复上传 → 两行元数据、一个物理 blob（第二次
   ``deduped=True``）；
c. 类型伪装（声明扩展名与真实魔数不符）→ 400 ``bad_request``，不落盘、不落库；
d. 超过大小上限 → 400 ``bad_request``（先于读取内容拒绝），不落盘、不落库；
e. 无上传权限（``sysadmin`` 不持有 ``expense_claim/create/own``）→ 403
   ``authorization_denied`` + 拒绝审计，不落盘、不落库；
f. 认证既有行为：无令牌 → 503；错令牌 / 缺 ``X-Employee-Id`` → 401；
g. 缺 ``file`` 字段 → 422；
h. 端到端：上传得到的 ``source_file_id`` 可被挂账端点引用（``GET
   /claims/{id}/invoices`` 回显该 id）。

全部跑在真实 ``ajaimgr_test`` 库 + 真实 ``TestClient`` 上；存储根用
``AJAIMGR_STORAGE_ROOT`` 指向 pytest 的 ``tmp_path``，因此**不会**污染仓库内
``var/storage``。``source_files`` 不在 ``app/tests/api/conftest.py`` 的清理列表里，
且 ``owner_id`` 外键引用 ``employees.id``，因此本文件自建夹具在每个测试后清掉自己
写入的挂账行与源文件行（须先于 ``world`` 夹具删员工执行）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.documents.models import SourceFile
from app.documents.storage import DEFAULT_MAX_BYTES
from app.expense.models import ExpenseInvoice
from app.tests.api.conftest import TEST_TOKEN, World

_ENDPOINT = "/documents"

#: A minimal, well-formed-enough PDF: only the leading magic matters here.
_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

#: Plain UTF-8 text (the storage layer's text fallback).
_TEXT_BYTES = "报销票据说明".encode("utf-8")


@pytest.fixture()
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the storage root at ``tmp_path`` (never the repository's var/storage)."""
    monkeypatch.setenv("AJAIMGR_STORAGE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_source_files(engine: Engine, world: World) -> Iterator[None]:
    """每个测试后清掉本模块写入的挂账行与源文件行（须先于 ``world`` 删员工执行）。"""
    yield
    with Session(engine) as session:
        # 先删引用 ``source_files.id`` 的挂账行，再删源文件行（避免外键悬空）。
        session.execute(delete(ExpenseInvoice))
        session.execute(delete(SourceFile))
        session.commit()


def _upload(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    employee,  # type: ignore[no-untyped-def]
    *,
    request_id: str,
    content: bytes = _PDF_BYTES,
    filename: str = "fapiao.pdf",
    token: str | None = None,
):  # type: ignore[no-untyped-def]
    headers = {**auth(employee, token=token), "X-Request-Id": request_id}
    return client.post(
        _ENDPOINT, files={"file": (filename, content)}, headers=headers
    )


def _rows(engine: Engine) -> list[SourceFile]:
    with Session(engine) as session:
        return list(session.scalars(select(SourceFile).order_by(SourceFile.id)))


def _events(
    engine: Engine, *, action: str, request_id: str | None = None
) -> list[AuditEvent]:
    with Session(engine) as session:
        stmt = select(AuditEvent).where(AuditEvent.action == action)
        if request_id is not None:
            stmt = stmt.where(AuditEvent.request_id == request_id)
        return list(session.scalars(stmt.order_by(AuditEvent.id)))


def _blobs(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


# --- a. 合法上传 ---------------------------------------------------------------


def test_upload_returns_metadata_and_stores_blob(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    storage_root: Path,
) -> None:
    response = _upload(
        client, auth, world.claimant, request_id="issue-60-upload-ok-1"
    )
    assert response.status_code == 201, response.text
    body = response.json()

    sha = hashlib.sha256(_PDF_BYTES).hexdigest()
    assert set(body) == {
        "id",
        "sha256",
        "size_bytes",
        "mime_type",
        "scan_status",
        "original_name",
        "deduped",
    }
    assert body["sha256"] == sha
    assert body["size_bytes"] == len(_PDF_BYTES)
    assert body["mime_type"] == "application/pdf"
    assert body["scan_status"] == "pending"
    assert body["original_name"] == "fapiao.pdf"
    assert body["deduped"] is False
    # 响应绝不泄露物理存储键或路径。
    assert "storage_key" not in response.text
    assert str(storage_root) not in response.text

    rows = _rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == body["id"]
    # 归属取自已验证主体（X-Employee-Id 头），不取表单里的任何字段。
    assert row.owner_id == world.claimant.id
    assert row.storage_key == f"{sha[:2]}/{sha[2:4]}/{sha}"
    assert row.sha256 == sha
    assert row.mime_type == "application/pdf"
    assert row.size_bytes == len(_PDF_BYTES)
    assert row.original_name == "fapiao.pdf"
    assert row.scan_status == "pending"

    # 内容寻址落盘：blob 位于 <root>/<sha[:2]>/<sha[2:4]>/<sha>。
    blob = storage_root / sha[:2] / sha[2:4] / sha
    assert blob.is_file()
    assert blob.read_bytes() == _PDF_BYTES
    assert _blobs(storage_root) == [blob]

    events = _events(
        engine, action="document.upload", request_id="issue-60-upload-ok-1"
    )
    assert len(events) == 1
    event = events[0]
    assert event.actor_id == world.claimant.id
    assert event.resource_type == "source_file"
    assert event.resource_id == str(row.id)
    assert event.details == {
        "sha256": sha,
        "size_bytes": len(_PDF_BYTES),
        "mime_type": "application/pdf",
        "scan_status": "pending",
        "deduped": False,
    }
    # 显式断言：审计 details 不含文件名原文（不可信输入不进审计）。
    assert "fapiao.pdf" not in str(event.details)


def test_upload_of_text_payload_is_accepted_as_plain_text(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    storage_root: Path,
) -> None:
    """文本载荷按真实内容判定为 ``text/plain``（类型只认魔数，不认扩展名）。"""
    response = _upload(
        client,
        auth,
        world.claimant,
        request_id="issue-60-upload-text-1",
        content=_TEXT_BYTES,
        filename="note.txt",
    )
    assert response.status_code == 201, response.text
    assert response.json()["mime_type"] == "text/plain"
    assert _rows(engine)[0].mime_type == "text/plain"


# --- b. 内容寻址去重 -----------------------------------------------------------


def test_same_content_twice_shares_one_blob(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    storage_root: Path,
) -> None:
    first = _upload(
        client, auth, world.claimant, request_id="issue-60-upload-dedup-1"
    )
    second = _upload(
        client, auth, world.manager, request_id="issue-60-upload-dedup-2"
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["deduped"] is False
    assert second.json()["deduped"] is True

    rows = _rows(engine)
    # 每次上传各留一行元数据（owner 不同），物理 blob 只有一份。
    assert [row.owner_id for row in rows] == [world.claimant.id, world.manager.id]
    assert len({row.storage_key for row in rows}) == 1
    assert len(_blobs(storage_root)) == 1


# --- c. 类型伪装 → 400 ---------------------------------------------------------


def test_declared_extension_mismatch_is_rejected(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    storage_root: Path,
) -> None:
    cases = (
        # PDF 字节声明为 .png。
        (_PDF_BYTES, "receipt.png"),
        # 文本字节声明为 .pdf（二进制容器扩展名 + 非二进制载荷）。
        (_TEXT_BYTES, "receipt.pdf"),
    )
    for index, (content, filename) in enumerate(cases):
        response = _upload(
            client,
            auth,
            world.claimant,
            request_id=f"issue-60-upload-mismatch-{index}",
            content=content,
            filename=filename,
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "bad_request"
    assert _rows(engine) == []
    assert _blobs(storage_root) == []


# --- d. 超限 → 400 -------------------------------------------------------------


def test_oversized_upload_is_rejected_without_writing(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    storage_root: Path,
) -> None:
    oversized = b"%PDF-" + b"0" * DEFAULT_MAX_BYTES
    assert len(oversized) > DEFAULT_MAX_BYTES
    response = _upload(
        client,
        auth,
        world.claimant,
        request_id="issue-60-upload-too-large-1",
        content=oversized,
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "bad_request"
    assert _rows(engine) == []
    assert _blobs(storage_root) == []
    # 未授权/超限都不落任何审计（不是权限拒绝，也不改变业务状态）。
    assert _events(engine, action="document.upload.deny") == []


# --- e. 越权 → 403 + 拒绝审计 --------------------------------------------------


def test_principal_without_create_permission_gets_403(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    storage_root: Path,
) -> None:
    response = _upload(
        client, auth, world.sysadmin, request_id="issue-60-upload-deny-1"
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"
    assert _rows(engine) == []
    assert _blobs(storage_root) == []

    denials = _events(
        engine, action="document.upload.deny", request_id="issue-60-upload-deny-1"
    )
    assert len(denials) == 1
    assert denials[0].actor_id == world.sysadmin.id
    assert denials[0].details == {
        "attempted_action": "upload_document",
        "reason": "missing_permission",
    }
    # 拒绝路径不得写出任何成功审计。
    assert (
        _events(
            engine,
            action="document.upload",
            request_id="issue-60-upload-deny-1",
        )
        == []
    )


# --- f. 认证既有行为 -----------------------------------------------------------


def test_missing_internal_token_returns_503(
    client_without_token: TestClient, world: World, storage_root: Path
) -> None:
    response = client_without_token.post(
        _ENDPOINT,
        files={"file": ("fapiao.pdf", _PDF_BYTES)},
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "X-Employee-Id": str(world.claimant.id),
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_wrong_bearer_returns_401(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    response = _upload(
        client,
        auth,
        world.claimant,
        request_id="issue-60-upload-auth-1",
        token="not-the-right-token",
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_missing_employee_header_returns_401(
    client: TestClient, storage_root: Path
) -> None:
    response = client.post(
        _ENDPOINT,
        files={"file": ("fapiao.pdf", _PDF_BYTES)},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# --- g. 缺 file 字段 → 422 -----------------------------------------------------


def test_missing_file_field_is_422(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    response = client.post(
        _ENDPOINT,
        data={"note": "no file here"},
        headers=auth(world.claimant),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- h. 端到端：上传 → 挂账引用 ------------------------------------------------


def test_uploaded_source_file_can_be_attached_to_a_claim(
    client: TestClient,
    world: World,
    auth: Callable[..., dict[str, str]],
    engine: Engine,
    storage_root: Path,
) -> None:
    headers = auth(world.claimant)
    uploaded = _upload(
        client, auth, world.claimant, request_id="issue-60-upload-e2e-1"
    )
    assert uploaded.status_code == 201, uploaded.text
    source_file_id = uploaded.json()["id"]

    claim = client.post("/claims", json={"currency": "CNY"}, headers=headers)
    assert claim.status_code == 201, claim.text
    claim_id = claim.json()["id"]

    attached = client.post(
        f"/claims/{claim_id}/invoices",
        json={
            "invoice_no": "12345678",
            "invoice_date": "2026-10-16",
            "invoice_code": "011002000211",
            "source_file_id": source_file_id,
        },
        headers={**headers, "X-Request-Id": "issue-60-upload-e2e-2"},
    )
    assert attached.status_code == 201, attached.text
    assert attached.json()["source_file_id"] == source_file_id

    listed = client.get(f"/claims/{claim_id}/invoices", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row["source_file_id"] for row in listed.json()] == [source_file_id]

    with Session(engine) as session:
        assert session.get(SourceFile, source_file_id) is not None
