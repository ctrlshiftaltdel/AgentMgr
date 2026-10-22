"""Strict request/response shapes shared by the REST mirror and MCP tools.

The models mirror the frozen task-envelope goal/resource-scope definitions so
that an unknown or authority-bearing field is rejected before it reaches the
governance services.  They are transport validation only: the domain services
still validate every value and re-resolve the trusted principal.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CancelTaskRequest",
    "CompletionCriterion",
    "ConfirmationRequest",
    "EvaluateTaskRequest",
    "Goal",
    "RESERVED_PAYLOAD_KEYS",
    "ResourceScope",
    "SubmitTaskRequest",
    "Verifier",
]

_IDEMPOTENCY_KEY = r"^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_TASK_ID = r"^task_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CORRELATION_ID = r"^corr_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CAPABILITY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*@[0-9]+\.[0-9]+\.[0-9]+$"
_CRITERION_ID = r"^crit_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_VERIFIER_ID = r"^ver_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_SEMVER = r"^[0-9]+\.[0-9]+\.[0-9]+$"
_RESOURCE_ID = r"^res_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$"
_CONNECTOR_ID = r"^conn_[A-Za-z0-9][A-Za-z0-9_-]{2,63}$"
_EVIDENCE_REF = r"^evidence:[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$"
_SHA256 = r"^sha256:[a-f0-9]{64}$"

#: Payload keys that name authority-bearing concepts.  The B1 ``payload`` is an
#: opaque execution blob, so the server never interprets these keys; rejecting
#: them is defence in depth against a future handler treating the payload as
#: identity/role/priority input.  Matching is case-insensitive and recursive.
RESERVED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "approver",
        "approver_employee_id",
        "approver_principal_id",
        "assignee",
        "assignee_principal_id",
        "auth",
        "authorization",
        "employee_id",
        "execution_mode",
        "executor",
        "executor_employee_id",
        "executor_principal_id",
        "owner",
        "owner_employee_id",
        "owner_principal_id",
        "permissions",
        "principal_id",
        "principal_type",
        "priority",
        "priority_authorized_by",
        "role",
        "role_ids",
        "roles",
        "service_identity_id",
        "token",
    }
)

_RESERVED_PAYLOAD_MESSAGE = "payload contains a reserved authority field"


def _check_reserved_payload_keys(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in RESERVED_PAYLOAD_KEYS:
                raise ValueError(f"{path}.{key}: {_RESERVED_PAYLOAD_MESSAGE}")
            _check_reserved_payload_keys(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_reserved_payload_keys(child, f"{path}[{index}]")


class Verifier(BaseModel):
    """A deterministic verifier identity from the frozen goal contract."""

    model_config = ConfigDict(extra="forbid")

    verifier_id: str = Field(pattern=_VERIFIER_ID)
    version: str = Field(pattern=_SEMVER)


class CompletionCriterion(BaseModel):
    """One machine-judgeable completion criterion."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(pattern=_CRITERION_ID)
    type: Literal[
        "business_state_equals",
        "resource_exists",
        "artifact_hash",
        "verifier_result",
    ]
    verifier: Verifier
    resource_ref: str | None = Field(default=None, pattern=_RESOURCE_ID)
    expected: str | None = Field(default=None, max_length=200)
    evidence_ref: str | None = Field(default=None, pattern=_EVIDENCE_REF)
    evidence_hash: str | None = Field(default=None, pattern=_SHA256)


class Goal(BaseModel):
    """Structured task goal (free text is never a completion definition)."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    completion_criteria: list[CompletionCriterion] = Field(
        min_length=1, max_length=20
    )
    required_inputs: list[str] | None = Field(default=None, max_length=50)
    out_of_scope: list[str] = Field(min_length=0, max_length=50)


class ResourceScope(BaseModel):
    """Frozen ``resourceScope`` shape; an empty set means no resources."""

    model_config = ConfigDict(extra="forbid")

    scope_type: Literal[
        "undetermined",
        "none",
        "self",
        "department",
        "company",
        "approval_chain",
        "project",
        "record_set",
        "connector",
    ]
    resource_ids: list[Annotated[str, Field(pattern=_RESOURCE_ID)]] = Field(
        default_factory=list, max_length=1000
    )
    connector_ids: list[Annotated[str, Field(pattern=_CONNECTOR_ID)]] = Field(
        default_factory=list, max_length=32
    )
    data_classifications: list[
        Literal["public", "internal", "confidential", "restricted"]
    ] = Field(default_factory=list)
    exclude_resource_ids: list[
        Annotated[str, Field(pattern=_RESOURCE_ID)]
    ] = Field(default_factory=list, max_length=1000)


class EvaluateTaskRequest(BaseModel):
    """Read-only evaluation input.

    There is deliberately no identity, owner, approver, role or priority field:
    those values are resolved server-side from the authenticated principal.
    """

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(pattern=_CAPABILITY)
    task_id: str | None = Field(default=None, pattern=_TASK_ID)
    resource_scope: ResourceScope | None = None


class SubmitTaskRequest(BaseModel):
    """Controlled task submission input.

    ``execution_mode`` is absent on purpose: a B1 client can never choose a
    stronger mode, and the adapter always submits advisory work.  ``priority``
    is absent for the same reason (priority never grants authority).

    Exactly one requested capability is accepted.  The core evaluates only the
    first capability while a grant must cover every requested capability, so a
    multi-capability request can never be honoured by this surface yet; it is
    rejected here, before any task row or decision is created.

    ``payload`` is opaque execution data.  It is rejected if it contains any
    authority-bearing key (owner/approver/role/priority/execution mode/...),
    even nested, so no transport can smuggle identity through the payload.
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY)
    goal: Goal
    requested_capabilities: list[
        Annotated[str, Field(pattern=_CAPABILITY)]
    ] = Field(min_length=1, max_length=1)
    resource_scope: ResourceScope
    payload: dict[str, Any] | None = None
    summary: str | None = Field(default=None, max_length=500)
    client_task_ref: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, pattern=_CORRELATION_ID)
    max_calls: int = Field(default=1, ge=1, le=1000)
    max_runtime_seconds: int = Field(default=300, ge=1, le=86400)
    max_cost_units: float | None = Field(default=None, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=100)
    retry_budget: int | None = Field(default=None, ge=0, le=100)
    retry_safe: bool = True

    @field_validator("payload")
    @classmethod
    def _reject_reserved_payload_keys(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        _check_reserved_payload_keys(value)
        return value


class ConfirmationRequest(BaseModel):
    """Request a task-bound confirmation gate."""

    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(default=900, ge=1, le=86400)


class CancelTaskRequest(BaseModel):
    """Register a cancellation request."""

    model_config = ConfigDict(extra="forbid")

    cancel_id: str | None = Field(
        default=None, pattern=r"^cancel_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
    )
