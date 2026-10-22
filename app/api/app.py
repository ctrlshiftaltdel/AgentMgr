"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.admin import register_admin_exception_handlers
from app.api.admin import router as admin_router
from app.api.claims import router as claims_router
from app.api.documents import router as documents_router
from app.api.errors import register_exception_handlers
from app.api.invoices import router as invoices_router
from app.api.mcp.server import build_mcp_server, mount_mcp
from app.api.rest.errors import register_governance_exception_handlers
from app.api.rest.router import router as governance_router
from app.db.session import engine


def create_app() -> FastAPI:
    """Build and return the business backend ASGI application."""
    # The MCP server is built per application instance.  The SDK's Streamable
    # HTTP session manager may only be run once per instance, so a fresh server
    # is required for every ``create_app`` (tests build several apps per process).
    mcp_server = build_mcp_server()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Mounted Starlette apps do not automatically run their own lifespan;
        # the MCP SDK exposes the session manager for exactly this use case.
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="ajaimgr backend", version="0.1.0", lifespan=lifespan)

    # Unified error-code mapping for every domain/API exception (see
    # ``app.api.errors``): authorization 403, invalid transition 409, business
    # validation 422, not-found 404, file-input 400, auth 401/503. Responses
    # never leak stack traces, DSNs or SQL.
    register_exception_handlers(app)
    # Client-governance domain errors (task/governance reason codes) map to the
    # same stable envelope without duplicating the generic handlers.
    register_governance_exception_handlers(app)

    # Expense claim endpoints (create/list/read/transitions/payments/export,
    # plus invoice attach/list under ``/claims/{claim_id}/invoices``).  Service
    # semantics for attaching live in ``app.expense.invoices``.
    app.include_router(claims_router)

    # Document endpoints: original-file upload (``POST /documents``).  Type,
    # size, path-safety and hash gates live in ``app.documents.storage``.
    app.include_router(documents_router)

    # Invoice verification endpoints: manual-assisted result recording
    # (``POST /invoice-verifications/manual``, task W1-22).  Authorization,
    # state rules and evidence retention live in
    # ``app.integrations.invoice_service.record_manual_verification``.
    app.include_router(invoices_router)

    # Minimal server-rendered admin console (login/session, claims review,
    # role & channel-binding management, audit log).  Every page and action
    # re-checks permissions via ``app.identity.authorization``; the admin layer
    # only maps those decisions to HTML pages.
    app.include_router(admin_router)
    register_admin_exception_handlers(app)

    # Client governance surface (B1).  REST and MCP share
    # ``app.api.rest.operations`` and therefore the same application services,
    # PDP/PEP and authorization rules.  The MCP server uses the official SDK's
    # Streamable HTTP transport and is reachable at ``/mcp/``.
    app.include_router(governance_router)
    mount_mcp(app, mcp_server)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe: never touches the database."""
        return {"status": "ok"}

    @app.get("/health/db")
    def health_db() -> JSONResponse:
        """Readiness probe: runs ``SELECT 1`` against the configured database."""
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            # Never leak the DSN or driver error details to the caller.
            return JSONResponse(status_code=503, content={"database": "unavailable"})
        return JSONResponse(status_code=200, content={"database": "ok"})

    return app
