"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.session import engine


def create_app() -> FastAPI:
    """Build and return the business backend ASGI application."""
    app = FastAPI(title="ajaimgr backend", version="0.1.0")

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
