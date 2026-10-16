"""发票查验（W1-20）测试的共享辅助：固定时钟、桩 provider、构造器与清理。

本模块**不含 pytest 夹具**（夹具在 ``conftest.py``），只放被测试与夹具共同引用的
纯辅助，避免同一模块被以两个不同名字重复导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models.organization import Company, Department, Employee
from app.documents.models import SourceFile
from app.identity.authorization import Principal
from app.identity.models import EmployeeRole
from app.identity.seed import clear_permission_catalog
from app.integrations.invoice_verification import (
    InvoiceFields,
    InvoiceVerificationProvider,
    ProviderType,
    VerificationResult,
    VerificationStatus,
)
from app.integrations.models import InvoiceVerification

REPO_ROOT = Path(__file__).resolve().parents[3]

#: 固定时钟：让签名与时间相关断言完全可复现。
FIXED_NOW = datetime(2026, 10, 16, 3, 30, 45, tzinfo=UTC)
FIXED_TODAY = date(2026, 10, 16)


def alembic_config() -> Config:
    """指向本仓库 migrations 的 Alembic 配置。"""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(REPO_ROOT))
    return config


def cleanup(session: Session) -> None:
    """删除本次测试创建的全部业务行（先子后父），并清掉权限目录种子。

    **不删除** ``audit_events``：该表由数据库触发器强制只增不改。``source_files``
    与 ``invoice_verifications`` 也要清掉，因为 ``app/tests/conftest.py`` 的
    ``BUSINESS_TABLES``（本任务不允许改动）未包含 ``invoice_verifications``。
    """
    session.execute(delete(InvoiceVerification))
    session.execute(delete(SourceFile))
    session.execute(delete(EmployeeRole))
    clear_permission_catalog(session)
    session.execute(delete(Employee))
    session.execute(delete(Department))
    session.execute(delete(Company))


@dataclass
class World:
    """一套测试组织：公司、部门、员工（含财务角色）与两份原件。"""

    company: Company
    department: Department
    claimant: Employee
    finance_operator: Employee
    outsider: Employee
    source_file_a: SourceFile
    source_file_b: SourceFile

    def principal(self, employee: Employee) -> Principal:
        return Principal(employee_id=employee.id, company_id=self.company.id)


@dataclass
class StubProvider(InvoiceVerificationProvider):
    """可编程的本地桩渠道：按队列返回预设结果，并记录调用次数。

    ``_provider_type`` 可被显式设为任意值（含 ``ProviderType.MANUAL_ASSISTED`` 或
    未识别字符串），用于验证「只有被认可渠道能进入查验通过」的闸门。**绝不触网**。
    """

    results: list[VerificationResult] = field(default_factory=list)
    _provider_type: object = ProviderType.AUTHORIZED_PROVIDER
    name: str = "stub/authorized"
    calls: list[InvoiceFields] = field(default_factory=list)

    @property
    def provider_type(self):  # type: ignore[override]
        return self._provider_type

    @property
    def provider_name(self) -> str:
        return self.name

    def verify(self, invoice_fields: InvoiceFields) -> VerificationResult:
        self.calls.append(invoice_fields)
        if not self.results:
            raise AssertionError("stub provider has no queued result")
        # 只剩一个结果时重复使用，便于「重复查验」类断言。
        return self.results[0] if len(self.results) == 1 else self.results.pop(0)


def make_fields(**overrides: object) -> InvoiceFields:
    """构造一份默认发票要素（可用关键字覆盖任意字段）。"""
    base: dict[str, object] = {
        "invoice_no": "12345678",
        "invoice_date": FIXED_TODAY,
        "invoice_code": "011002000211",
        "invoice_kind": "01",
        "amount": "100.00",
    }
    base.update(overrides)
    return InvoiceFields(**base)  # type: ignore[arg-type]


def make_result(
    status: VerificationStatus = VerificationStatus.VERIFIED_PASS,
    **overrides: object,
) -> VerificationResult:
    """构造一份默认查验结果（可用关键字覆盖任意字段）。"""
    kwargs: dict[str, object] = {"status": status}
    kwargs.update(overrides)
    return VerificationResult(**kwargs)  # type: ignore[arg-type]
