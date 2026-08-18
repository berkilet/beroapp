"""Database engine and session management.

The engine is configured for a long-running process that must survive the
database going away and coming back: ``pool_pre_ping`` validates a connection
before handing it out, and every statement carries a server-side timeout so a
pathological query cannot wedge a worker forever.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def build_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
        connect_args={"application_name": settings.app_name},
    )

    timeout_ms = settings.db_statement_timeout_ms

    @event.listens_for(engine, "connect")
    def _set_statement_timeout(dbapi_conn, _record) -> None:  # noqa: ANN001
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(timeout_ms)}")

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def database_reachable() -> bool:
    """Cheap liveness probe used by /readiness and by the worker supervisor."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def reset_engine() -> None:
    """Drop the cached engine. Used by tests and after a fatal pool error."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
