"""FastAPI application.

Read-mostly. This process performs no ingestion and no model inference — it
reads what the worker has already written. A slow or hostile HTTP request
therefore cannot delay data collection, and the worker's cadence does not depend
on anyone looking at the dashboard.

Run with:  uvicorn app.api.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.api.health import component_statuses, data_freshness, overall_health
from app.api.routes import router
from app.api.security import (
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.config import get_settings
from app.core.enums import ComponentHealth
from app.core.logging import configure_logging, get_correlation_id, get_logger
from app.db.session import database_reachable, get_engine, session_scope

log = get_logger("api.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    settings = get_settings()
    log.info(
        "api starting",
        extra={
            "event": "api_starting",
            "detail": {
                "phase": settings.current_phase,
                "live_trading_enabled": settings.live_trading_enabled,
            },
        },
    )
    yield
    log.info("api stopping", extra={"event": "api_stopping"})


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="beroapp — prediction-market research platform",
        version="0.1.0",
        lifespan=lifespan,
        # No interactive docs in production: they enumerate the surface for free.
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )

    # Order matters: headers outermost so every response gets them, including
    # rate-limit rejections and validation errors.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(RequestSizeLimitMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type", "X-Correlation-ID"],
        max_age=600,
    )

    app.include_router(router)
    _install_exception_handlers(app)
    _install_health_routes(app)
    return app


def _install_exception_handlers(app: FastAPI) -> None:
    """One shape for every error. Stack traces go to the log, never the client."""

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request failed validation",
                    "correlation_id": get_correlation_id(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = get_correlation_id()
        log.exception(
            "unhandled error",
            extra={
                "event": "unhandled_error",
                "error_code": type(exc).__name__,
                "correlation_id": correlation_id,
                "detail": {"path": request.url.path},
            },
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    # Deliberately uninformative. The correlation id is how an
                    # operator finds the real detail in the log.
                    "message": "an internal error occurred",
                    "correlation_id": correlation_id,
                }
            },
        )


def _install_health_routes(app: FastAPI) -> None:
    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        """Liveness. Cheap, unauthenticated, and reveals nothing."""
        return {"status": "ok"}

    @app.get("/readiness", include_in_schema=False)
    async def readiness() -> JSONResponse:
        """Readiness. Reports whether dependencies are actually usable."""
        db_ok = database_reachable()
        if not db_ok:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "database": "unreachable"},
            )

        with session_scope() as session:
            statuses = component_statuses(session)
            freshness = data_freshness(session)
            health = overall_health(statuses)

        ready = health not in (ComponentHealth.FAILED,)
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "ready" if ready else "not_ready",
                "database": "ok",
                "overall_health": health.value,
                "data_freshness": freshness,
            },
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> JSONResponse:
        """Prometheus-style operational metrics.

        Counts and health only — no credentials, no connection strings, no
        market-level detail.
        """
        if not database_reachable():
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": {"code": "database_unavailable"}},
            )

        lines: list[str] = []
        with get_engine().connect() as conn:
            for table in (
                "markets", "market_snapshots", "order_book_snapshots",
                "predictions", "signals", "risk_decisions", "resolutions",
                "paper_orders", "paper_fills", "system_events", "audit_logs",
            ):
                # Table names come from this fixed tuple, never from a request.
                count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                lines.append(f'beroapp_rows_total{{table="{table}"}} {count}')

        with session_scope() as session:
            for status_row in component_statuses(session):
                healthy = 1 if status_row.health is ComponentHealth.HEALTHY else 0
                lines.append(
                    f'beroapp_component_healthy{{component="{status_row.component}"}} {healthy}'
                )
                if status_row.age_s is not None:
                    lines.append(
                        f'beroapp_component_last_event_age_seconds'
                        f'{{component="{status_row.component}"}} {status_row.age_s:.1f}'
                    )
            freshness = data_freshness(session)

        if freshness["age_seconds"] is not None:
            lines.append(f'beroapp_market_data_age_seconds {freshness["age_seconds"]}')

        settings = get_settings()
        lines.append(f"beroapp_live_trading_enabled {int(settings.live_trading_enabled)}")

        # PlainTextResponse, not JSONResponse: a JSON-encoded string is not
        # parseable by any Prometheus scraper.
        return PlainTextResponse(
            content="\n".join(lines) + "\n",
            media_type="text/plain; version=0.0.4",
        )


app = create_app()
