"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.claims import router as claims_router
from app.api.errors import register_exception_handlers
from app.db.session import engine


def create_app() -> FastAPI:
    """Build and return the business backend ASGI application."""
    app = FastAPI(title="ajaimgr backend", version="0.1.0")

    # Unified error-code mapping for every domain/API exception (see
    # ``app.api.errors``): authorization 403, invalid transition 409, business
    # validation 422, not-found 404, file-input 400, auth 401/503. Responses
    # never leak stack traces, DSNs or SQL.
    register_exception_handlers(app)

    # Expense claim endpoints (create/list/read/transitions/payments/export).
    app.include_router(claims_router)

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
