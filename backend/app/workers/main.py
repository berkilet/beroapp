"""Worker process entrypoint.

Runs every ingestion and computation job on its own schedule under the
supervisor. This process — not the API — is the only thing that writes market
data, predictions and signals.

Run with:  python -m app.workers.main
Under launchd:  see ops/launchd/ and docs/OPERATIONS.md
"""

from __future__ import annotations

import asyncio
import os

from app.core.config import get_settings
from app.core.enums import SystemComponent
from app.core.logging import configure_logging, get_logger
from app.db.session import database_reachable, reset_engine, session_scope
from app.ingest.polymarket import PolymarketClient
from app.ingest.repository import record_system_event
from app.workers.discovery import DiscoveryWorker
from app.workers.metrics import MetricsWorker
from app.workers.prediction import PredictionWorker
from app.workers.resolution import ResolutionWorker
from app.workers.snapshot import SnapshotWorker
from app.workers.supervisor import Supervisor

log = get_logger("workers.main")


async def _wait_for_database(max_attempts: int = 30) -> None:
    """Block until PostgreSQL answers.

    On a macOS reboot the worker may start before PostgreSQL is accepting
    connections. Waiting is correct; crash-looping until launchd gives up is not.
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        if database_reachable():
            return
        reset_engine()
        log.warning(
            "database not reachable, waiting",
            extra={"event": "db_wait", "detail": {"attempt": attempt, "delay_s": delay}},
        )
        await asyncio.sleep(delay)
        delay = min(30.0, delay * 1.7)
    raise RuntimeError("database did not become reachable")


async def run() -> None:
    settings = get_settings()
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))

    log.info(
        "worker starting",
        extra={
            "event": "worker_starting",
            "detail": {
                "phase": settings.current_phase,
                "live_trading_enabled": settings.live_trading_enabled,
                "paper_trading_active": settings.paper_trading_active,
            },
        },
    )

    # A worker that somehow started with live trading armed is a worker that
    # should not start. Belt and braces on top of the config validator.
    if settings.live_trading_enabled and settings.current_phase != "PHASE_3":
        raise RuntimeError("refusing to start: live trading armed outside PHASE_3")

    await _wait_for_database()

    supervisor = Supervisor()

    async with PolymarketClient(settings=settings) as client:
        discovery = DiscoveryWorker(client, settings)
        snapshot = SnapshotWorker(client, settings)
        prediction = PredictionWorker(settings)
        resolution = ResolutionWorker(client, settings)
        metrics = MetricsWorker(settings)

        async def discovery_job() -> None:
            await discovery.run_once()

        async def snapshot_job() -> None:
            await snapshot.run_once()

        async def prediction_job() -> None:
            await prediction.run_once(
                clock_skew_s=snapshot.last_clock_skew_s,
                consecutive_api_failures=snapshot.consecutive_batch_failures,
            )

        async def resolution_job() -> None:
            await resolution.run_once()

        async def metrics_job() -> None:
            await metrics.run_once()

        async def heartbeat_job() -> None:
            with session_scope() as session:
                record_system_event(
                    session,
                    component=SystemComponent.WORKERS.value,
                    event="heartbeat",
                    detail=supervisor.status(),
                )

        # Discovery must land before snapshotting has anything to poll, so the
        # snapshot job starts one interval later on a cold boot.
        supervisor.add_job(
            "discovery", discovery_job,
            interval_s=settings.discovery_interval_s,
            component=SystemComponent.MARKET_DISCOVERY.value,
        )
        supervisor.add_job(
            "snapshot", snapshot_job,
            interval_s=settings.snapshot_interval_s,
            component=SystemComponent.DATA_FEED.value,
            initial_delay_s=20,
        )
        supervisor.add_job(
            "prediction", prediction_job,
            interval_s=settings.prediction_interval_s,
            component=SystemComponent.PROBABILITY_ENGINE.value,
            initial_delay_s=60,
        )
        supervisor.add_job(
            "resolution", resolution_job,
            interval_s=settings.resolution_interval_s,
            component=SystemComponent.RESOLUTION_ENGINE.value,
            initial_delay_s=120,
        )
        supervisor.add_job(
            "metrics", metrics_job,
            interval_s=settings.metrics_interval_s,
            component=SystemComponent.METRICS_ENGINE.value,
            initial_delay_s=180,
        )
        supervisor.add_job(
            "heartbeat", heartbeat_job,
            interval_s=settings.heartbeat_interval_s,
            component=SystemComponent.WORKERS.value,
        )

        await supervisor.run()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("interrupted", extra={"event": "worker_interrupted"})


if __name__ == "__main__":
    main()
