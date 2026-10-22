"""Official MCP server for client governance (Streamable HTTP).

Trust boundary
--------------
This is the B1 *client* surface (WorkBuddy/Codex/OpenCode/Inspector style).  It
is deliberately separate from the server-side Agent business-tool surface (B2).
The trusted principal is read **only** from request headers through the existing
internal-token + verified-employee adapter; no tool accepts an identity, owner,
approver, role or priority argument.

The endpoint is an internal/gateway-only surface.  Every MCP HTTP request must
already carry ``Authorization: Bearer <internal-token>``; a transport-level
middleware rejects ``initialize``/``tools/list``/session creation before the SDK
sees them, so the shared internal token is not a way to expose this endpoint to
untrusted remote clients.  A remote deployment must terminate TLS at a trusted
gateway that injects both the token and ``X-Employee-Id``; clients must never
receive the shared token.  Allowed ``Host`` values are configured through
``AJAIMGR_MCP_ALLOWED_HOSTS`` (comma-separated, ``host:port`` patterns) and
default to loopback only.

Transport
---------
``mcp==2.3.0`` (``MCPServer``, formerly ``FastMCP``) exposes
``streamable_http_app()`` and a ``session_manager`` for mounting in an existing
ASGI application.  The FastAPI factory mounts it at ``/mcp`` and drives the
session manager from the application lifespan.  This module never implements
the wire protocol by hand.

The surface is stateless (``stateless_http=True``): the tools only need the
per-request headers, so no session affinity or sticky worker routing is
required.

SDK upgrade note
----------------
Registration below intentionally uses the SDK's private ``_tool_manager`` /
``fn_metadata.arg_model`` to force ``additionalProperties: false`` on the
generated JSON Schema.  That is a pin-specific workaround: the project pins
``mcp==2.3.0`` and a dependency upgrade must be verified against this module
(and ``app/tests/mcp/test_tool_surface.py``) before changing the pin.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.deps import _configured_token, _extract_bearer
from app.api.errors import ApiError, error_body
from app.api.rest import operations
from app.api.rest.errors import to_api_error
from app.api.rest.models import (
    CancelTaskRequest,
    ConfirmationRequest,
    EvaluateTaskRequest,
    Goal,
    ResourceScope,
    SubmitTaskRequest,
)
from app.api.rest.principal import resolve_actor_from_headers
from app.db.session import SessionLocal
from app.governance.errors import GovernanceError
from app.tasks.errors import TaskError

__all__ = ["MCP_ALLOWED_HOSTS_ENV", "build_mcp_server", "mount_mcp"]

logger = logging.getLogger(__name__)

#: Comma-separated ``host:port`` patterns accepted in the HTTP ``Host`` header.
#: Unset or blank keeps the loopback-only default.
MCP_ALLOWED_HOSTS_ENV = "AJAIMGR_MCP_ALLOWED_HOSTS"
_DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
)

_CAPABILITY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*@[0-9]+\.[0-9]+\.[0-9]+$"
_TASK_ID = r"^task_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CANCEL_ID = r"^cancel_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_IDEMPOTENCY_KEY = r"^idem_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"
_CORRELATION_ID = r"^corr_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"

_EVALUATE_DESCRIPTION = """\
Evaluate one governance capability with the deterministic PDP before/without
executing anything.

Side effects: LOW. Writes one immutable governance decision + audit record by
contract; never creates a task, issues a grant, mutates task state, or consumes
a confirmation/approval. The response reports these effects explicitly in
`side_effects` rather than claiming to be read-only.

Risk: business-state evaluation. A returned `allow` is advisory only; it does
not authorise execution and no execution grant exists until a task is submitted
and queued by the server.

Not applicable: this is not a way to execute, approve, confirm or cancel work,
and it cannot raise privilege. A task id may be supplied only when the caller is
authorized to read that task.

Confirmation: not required. Approval: not required.

Errors: stable `error.code` values (for example `task_not_found`,
`task_unauthorized_actor`, `capability_not_granted`, `policy_denied`)."""

_SUBMIT_DESCRIPTION = """\
Submit an advisory task to the governance service.

Side effects: MEDIUM. Creates a durable task, a governance decision and audit
events; policy may queue it, require clarification/confirmation/approval, or
deny it. It never executes business actions itself. A domain rejection after
evaluation rolls the partial task back instead of leaving an unrecoverable row.

Risk: a submission starts a governed lifecycle. Owner, executor, approver,
priority and execution mode are resolved server-side; clients cannot name them.
This surface always submits `advisory` work and cannot select
`managed_execution`.

Capabilities: exactly one requested capability (`requested_capabilities` has
exactly one item). Multi-capability evaluation/granting is not supported by this
surface yet and a longer list is rejected before any task is created.

Payload: `payload` is opaque execution data. Authority-bearing keys such as
`execution_mode`, `owner`, `approver`, `principal_id`, `role`, `permissions` or
`priority` (at any nesting level) are rejected as invalid arguments.

Not applicable: do not use this tool for an action that is irreversible, a
payment, a deletion or a bulk external release without the policy-required
confirmation/approval gates. Do not use it to bypass an approval.

Confirmation: the server decides from the active policy; a task may enter
`awaiting_confirmation`, in which case use `governance.request_confirmation`.

Approval: the server decides from the active policy; an independent approver is
resolved from the directory. A task may enter `awaiting_approval`.

Errors: stable `error.code` values (for example `required_input_missing`,
`task_unauthorized_actor`, `self_approval_forbidden`, `policy_denied`,
`rate_limited`)."""

_GET_DESCRIPTION = """\
Read one task's safe status summary.

Side effects: NONE. Risk: low. Only a participant, an authorized service
identity or an identity manager may read the task; otherwise the call is denied.

Not applicable: this does not return the execution payload, employee ids or
credentials, and it is not a result endpoint (use `governance.get_result`).

Confirmation: not required. Approval: not required. Errors: `task_not_found`,
`task_unauthorized_actor`."""

_LIST_DESCRIPTION = """\
List tasks where the authenticated principal is a participant.

Side effects: NONE. Risk: low. The result is scoped to the caller and never
lists another principal's tasks.

Not applicable: not an administrative search; identity managers still only see
their own participant tasks here. It returns at most 100 summaries.

Confirmation: not required. Approval: not required. Errors: `rate_limited`."""

_CONFIRMATION_DESCRIPTION = """\
Request (or return) a single-use confirmation gate bound to a task's exact
execution payload.

Side effects: MEDIUM. May create one pending confirmation row and an audit
event; it does not satisfy, consume or approve the gate. Only the requester,
accountable owner or an identity manager may mint it.

Risk: a confirmation is consent for a specific payload hash and expires. If the
payload changes, the confirmation is invalid.

Not applicable: do not use it as approval, and do not use it to replay or
consume an existing confirmation.

Confirmation: this tool is the request side. A human must satisfy the
confirmation in the bound channel.

Approval: not required. Errors: `task_unauthorized_actor`,
`task_confirmation_actor_invalid`, `task_terminal_state_immutable`,
`rate_limited`."""

_APPROVAL_DESCRIPTION = """\
Return the existing independent approval request for a task in
`awaiting_approval`.

Side effects: NONE. Risk: low. It never records an approval decision and never
accepts an approver from the caller; the approver was resolved server-side and
must satisfy separation of duties. The response only reports that an approver is
assigned and that separation of duties is enforced; the approver's identity is
not exposed.

Not applicable: only valid while the task is `awaiting_approval`; do not use it
to self-approve or to choose a different approver.

Confirmation: not required. Approval: this is the request/read side; an actual
human approval is a separate workflow action.

Errors: `task_invalid_transition`, `task_approval_not_found`,
`rate_limited`."""

_CANCEL_DESCRIPTION = """\
Register a cancellation request for a task.

Side effects: MEDIUM. A task still in a pre-execution gate (`draft`,
`awaiting_clarification`, `awaiting_confirmation`, `awaiting_approval`) becomes
`cancelled` directly and records that execution never started. A `queued`,
`evaluated`, `dispatched`, `running` or `verifying` task becomes
`cancel_requested`; that is **not** a confirmed stop.

Risk: a request is not a stop. Only stop evidence/reconciliation can move a task
to `cancelled` once dispatch may have happened.

Not applicable: terminal tasks cannot be cancelled; a service/runtime principal
may act only through its normal boundary.

Confirmation: not required by this tool; policy gates already apply.
Approval: not required by this tool; policy gates already apply.

Errors: `task_not_found`, `task_unauthorized_actor`,
`task_terminal_state_immutable`, `task_invalid_transition`, `rate_limited`."""

_RESULT_DESCRIPTION = """\
Read the authorized result and evidence references for a task.

Side effects: NONE. Risk: low. Returns only whitelisted fields (`outcome`,
`result_ref`, `evidence_refs`, verification result/time, stop/reconciliation
flags) for a participant; raw execution payloads, employee ids and credentials
are never returned.

Long tasks: this endpoint returns the current status and `result_not_available`
until a result exists. No event streaming is claimed.

Not applicable: use `governance.get_task` for progress/state.
Confirmation: not required. Approval: not required. Errors: `task_not_found`,
`task_unauthorized_actor`, `rate_limited`."""


def _error_envelope(code: str, message: str) -> str:
    payload = {"error": {"code": code, "message": message}}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _tool_error(exc: Exception) -> ToolError:
    """Render a domain/API exception as the shared stable error envelope."""
    mapped = to_api_error(exc)
    return ToolError(_error_envelope(mapped.code, mapped.message))


class GovernanceMCPServer(MCPServer):
    """MCPServer that normalises protocol-level argument/tool errors.

    The SDK performs argument validation before the tool function runs, so its
    ``ValidationError`` never reaches our handler.  This override keeps the
    externally visible error shape stable for invalid arguments and unknown
    tools while still delegating all protocol work to the official SDK.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except ValidationError:
            raise ToolError(
                _error_envelope(
                    "invalid_arguments", "request failed schema validation"
                )
            ) from None
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                raise ToolError(
                    _error_envelope(
                        "invalid_arguments", "request failed schema validation"
                    )
                ) from None
            if str(exc).startswith("Unknown tool"):
                raise ToolError(
                    _error_envelope("unknown_tool", "unknown tool")
                ) from None
            raise


def _run(ctx: Context, operation: str, call):
    """Resolve the trusted header principal, run one operation, map errors.

    The HTTP request headers are client-supplied input for transport purposes;
    they are trusted only because the existing internal-token + employee-header
    adapter validates them against the directory.  Tool arguments are never a
    principal source.
    """
    headers = ctx.headers or {}
    with SessionLocal() as session:
        try:
            with operations.unit_of_work(session):
                actor = resolve_actor_from_headers(
                    session,
                    authorization=headers.get("authorization"),
                    employee_header=headers.get("x-employee-id"),
                )
                return call(session, actor)
        except (ApiError, GovernanceError, TaskError) as exc:
            raise _tool_error(exc) from None
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace
            logger.error(
                "governance MCP operation %s failed: %s",
                operation,
                type(exc).__name__,
            )
            raise _tool_error(
                ApiError(500, "internal_error", "internal server error")
            ) from None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def evaluate_task(
    ctx: Context,
    capability: Annotated[str, Field(pattern=_CAPABILITY)],
    task_id: Annotated[str | None, Field(default=None, pattern=_TASK_ID)] = None,
    resource_scope: ResourceScope | None = None,
) -> dict[str, Any]:
    """Evaluate a capability (see the registered description)."""
    request = EvaluateTaskRequest(
        capability=capability,
        task_id=task_id,
        resource_scope=resource_scope,
    )
    return _run(
        ctx,
        operations.OPERATION_EVALUATE,
        lambda session, actor: operations.evaluate_task(session, actor, request),
    )


def submit_task(
    ctx: Context,
    idempotency_key: Annotated[str, Field(pattern=_IDEMPOTENCY_KEY)],
    goal: Goal,
    requested_capabilities: Annotated[
        list[Annotated[str, Field(pattern=_CAPABILITY)]],
        Field(min_length=1, max_length=1),
    ],
    resource_scope: ResourceScope,
    payload: dict[str, Any] | None = None,
    summary: str | None = None,
    client_task_ref: str | None = None,
    correlation_id: Annotated[
        str | None, Field(default=None, pattern=_CORRELATION_ID)
    ] = None,
    max_calls: Annotated[int, Field(default=1, ge=1, le=1000)] = 1,
    max_runtime_seconds: Annotated[
        int, Field(default=300, ge=1, le=86400)
    ] = 300,
    max_cost_units: Annotated[float | None, Field(default=None, ge=0)] = None,
    max_attempts: Annotated[int, Field(default=3, ge=1, le=100)] = 3,
    retry_budget: Annotated[int | None, Field(default=None, ge=0, le=100)] = None,
    retry_safe: bool = True,
) -> dict[str, Any]:
    """Submit a task (see the registered description)."""
    request = SubmitTaskRequest(
        idempotency_key=idempotency_key,
        goal=goal,
        requested_capabilities=requested_capabilities,
        resource_scope=resource_scope,
        payload=payload,
        summary=summary,
        client_task_ref=client_task_ref,
        correlation_id=correlation_id,
        max_calls=max_calls,
        max_runtime_seconds=max_runtime_seconds,
        max_cost_units=max_cost_units,
        max_attempts=max_attempts,
        retry_budget=retry_budget,
        retry_safe=retry_safe,
    )
    return _run(
        ctx,
        operations.OPERATION_SUBMIT,
        lambda session, actor: operations.submit_task(session, actor, request),
    )


def get_task(
    ctx: Context,
    task_id: Annotated[str, Field(pattern=_TASK_ID)],
) -> dict[str, Any]:
    """Read a task summary (see the registered description)."""
    return _run(
        ctx,
        operations.OPERATION_GET,
        lambda session, actor: operations.get_task(session, actor, task_id),
    )


def list_my_tasks(
    ctx: Context,
    limit: Annotated[int, Field(default=50, ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """List the caller's tasks (see the registered description)."""
    return _run(
        ctx,
        operations.OPERATION_LIST,
        lambda session, actor: operations.list_my_tasks(
            session, actor, limit=limit
        ),
    )


def request_confirmation(
    ctx: Context,
    task_id: Annotated[str, Field(pattern=_TASK_ID)],
    ttl_seconds: Annotated[int, Field(default=900, ge=1, le=86400)] = 900,
) -> dict[str, Any]:
    """Request a confirmation gate (see the registered description)."""
    request = ConfirmationRequest(ttl_seconds=ttl_seconds)
    return _run(
        ctx,
        operations.OPERATION_REQUEST_CONFIRMATION,
        lambda session, actor: operations.request_confirmation(
            session, actor, task_id, request
        ),
    )


def request_approval(
    ctx: Context,
    task_id: Annotated[str, Field(pattern=_TASK_ID)],
) -> dict[str, Any]:
    """Read the existing approval request (see the registered description)."""
    return _run(
        ctx,
        operations.OPERATION_REQUEST_APPROVAL,
        lambda session, actor: operations.request_approval(
            session, actor, task_id
        ),
    )


def cancel_task(
    ctx: Context,
    task_id: Annotated[str, Field(pattern=_TASK_ID)],
    cancel_id: Annotated[
        str | None, Field(default=None, pattern=_CANCEL_ID)
    ] = None,
) -> dict[str, Any]:
    """Register a cancellation request (see the registered description)."""
    request = CancelTaskRequest(cancel_id=cancel_id)
    return _run(
        ctx,
        operations.OPERATION_CANCEL,
        lambda session, actor: operations.cancel_task(
            session, actor, task_id, request
        ),
    )


def get_result(
    ctx: Context,
    task_id: Annotated[str, Field(pattern=_TASK_ID)],
) -> dict[str, Any]:
    """Read authorized result references (see the registered description)."""
    return _run(
        ctx,
        operations.OPERATION_RESULT,
        lambda session, actor: operations.get_result(session, actor, task_id),
    )


# ---------------------------------------------------------------------------
# Registration / mounting
# ---------------------------------------------------------------------------


def _register(server: MCPServer, fn, *, name: str, description: str) -> None:
    # ``_tool_manager``/``arg_model`` are private mcp==2.3.0 APIs (see the
    # module-level SDK upgrade note); the surface tests are the upgrade gate.
    server.add_tool(fn, name=name, description=description)
    tool = server._tool_manager.get_tool(name)
    if tool is None:  # pragma: no cover - registration just succeeded
        raise RuntimeError(f"tool {name!r} was not registered")
    # The SDK builds a per-function Pydantic argument model.  Attach the
    # frozen strictness: unknown tool arguments are rejected instead of being
    # silently ignored, which is what stops a client from smuggling an
    # identity/owner/approver field into a call.
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    parameters = tool.parameters
    if isinstance(parameters, dict):
        parameters["additionalProperties"] = False


def build_mcp_server() -> MCPServer:
    """Build a fresh MCP server (one per application instance)."""
    server = GovernanceMCPServer(
        name="AgentMgr Governance (client)",
        version="0.1.0",
        instructions=(
            "Client governance tools. The trusted principal comes from the "
            "Authorization and X-Employee-Id request headers; never from tool "
            "arguments. Tasks are advisory unless the server's policy says "
            "otherwise. Use get_task/get_result to poll long-running work; no "
            "event streaming is promised."
        ),
    )
    _register(
        server,
        evaluate_task,
        name="governance.evaluate_task",
        description=_EVALUATE_DESCRIPTION,
    )
    _register(
        server,
        submit_task,
        name="governance.submit_task",
        description=_SUBMIT_DESCRIPTION,
    )
    _register(
        server,
        get_task,
        name="governance.get_task",
        description=_GET_DESCRIPTION,
    )
    _register(
        server,
        list_my_tasks,
        name="governance.list_my_tasks",
        description=_LIST_DESCRIPTION,
    )
    _register(
        server,
        request_confirmation,
        name="governance.request_confirmation",
        description=_CONFIRMATION_DESCRIPTION,
    )
    _register(
        server,
        request_approval,
        name="governance.request_approval",
        description=_APPROVAL_DESCRIPTION,
    )
    _register(
        server,
        cancel_task,
        name="governance.cancel_task",
        description=_CANCEL_DESCRIPTION,
    )
    _register(
        server,
        get_result,
        name="governance.get_result",
        description=_RESULT_DESCRIPTION,
    )
    return server


def _transport_security_settings() -> TransportSecuritySettings:
    """DNS-rebinding/Origin policy for the mounted MCP transport.

    Loopback-only by default.  Operators that front the endpoint with a trusted
    gateway can extend the allowed ``Host`` values without disabling the
    protection.  Origins are derived for both ``http`` and ``https`` so a
    browser-based client behind TLS is still validated.
    """
    raw = os.environ.get(MCP_ALLOWED_HOSTS_ENV)
    if raw is None or not raw.strip():
        hosts = list(_DEFAULT_ALLOWED_HOSTS)
    else:
        hosts = [part.strip() for part in raw.split(",") if part.strip()]
        if not hosts:
            hosts = list(_DEFAULT_ALLOWED_HOSTS)
    origins = [
        f"{scheme}://{host}"
        for host in hosts
        for scheme in ("http", "https")
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


class _MCPBearerAuth:
    """Transport-level bearer check for the mounted MCP ASGI app.

    Tool calls already re-verify the internal token, but ``initialize``,
    ``tools/list`` and session creation happen before any tool runs.  This
    wrapper keeps the endpoint from being reachable with a shared credential
    that untrusted remote clients could simply be given.  It reuses the exact
    token extraction/comparison from ``app.api.deps`` so there is one auth
    definition.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = _configured_token()
        if token is None:
            response = JSONResponse(
                status_code=503,
                content=error_body("service_unavailable", "service unavailable"),
            )
            await response(scope, receive, send)
            return

        provided = _extract_bearer(Headers(scope=scope).get("authorization"))
        if provided is None or not hmac.compare_digest(
            provided.encode("utf-8"), token.encode("utf-8")
        ):
            response = JSONResponse(
                status_code=401,
                content=error_body("unauthorized", "authentication required"),
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def mount_mcp(app, server: MCPServer) -> None:
    """Mount the stateless Streamable HTTP transport at ``/mcp/`` on ``app``."""
    mcp_app = server.streamable_http_app(
        streamable_http_path="/",
        # Tool-only surface: every call carries its own trusted headers, so no
        # session affinity or sticky worker routing is required.
        stateless_http=True,
        transport_security=_transport_security_settings(),
    )
    app.mount("/mcp", _MCPBearerAuth(mcp_app))
