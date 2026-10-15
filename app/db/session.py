"""Engine, session factory and the FastAPI session dependency."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# ``pool_pre_ping`` guards against stale connections being handed to requests.
engine: Engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session that is always closed afterwards."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
