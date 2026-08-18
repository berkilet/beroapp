"""Job supervisor.

Each job runs on its own schedule and fails independently. A job that raises is
logged, recorded as a system event, backed off, and retried — it never takes
the process down and never stops its siblings. This is what lets the platform
survive an API outage in one feed while continuing to collect another.

Shutdown is graceful: SIGTERM/SIGINT set an event, in-flight jobs finish their
current iteration, and the process exits cleanly so launchd's KeepAlive does not
fight a half-dead process.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.logging import get_logger, new_correlation_id
from app.db.session import database_reachable, reset_engine, session_scope
from app.ingest.repository import record_system_event

log = get_logger("workers.supervisor")

JobFn = Callable[[], Awaitable[None]]


@dataclass
class JobStatus:
    name: str
    interval_s: float
    last_started_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    run_count: int = 0
    success_count: int = 0
    missed_cycles: int = 0
    last_duration_ms: int | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "interval_s": self.interval_s,
            "last_started_at": self.last_started_at.isoformat() if self.last_started_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "run_count": self.run_count,
            "success_count": self.success_count,
            "missed_cycles": self.missed_cycles,
            "last_duration_ms": self.last_duration_ms,
        }


@dataclass
class Job:
    name: str
    fn: JobFn
    interval_s: float
    component: str
    initial_delay_s: float = 0.0
    max_backoff_s: float = 300.0
    status: JobStatus = field(init=False)

    def __post_init__(self) -> None:
        self.status = JobStatus(name=self.name, interval_s=self.interval_s)


class Supervisor:
    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.started_at: datetime | None = None

    def add_job(
        self,
        name: str,
        fn: JobFn,
        *,
        interval_s: float,
        component: str,
        initial_delay_s: float = 0.0,
    ) -> None:
        self.jobs.append(
            Job(name=name, fn=fn, interval_s=interval_s, component=component, initial_delay_s=initial_delay_s)
        )

    def request_stop(self) -> None:
        log.info("shutdown requested", extra={"event": "shutdown_requested"})
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def status(self) -> dict:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopping": self.stopping,
            "jobs": {job.name: job.status.as_dict() for job in self.jobs},
        }

    async def run(self) -> None:
        self.started_at = datetime.now(UTC)
        self._install_signal_handlers()

        self._tasks = [asyncio.create_task(self._run_job(job), name=job.name) for job in self.jobs]
        log.info(
            "supervisor started",
            extra={"event": "supervisor_started", "detail": {"jobs": [j.name for j in self.jobs]}},
        )
        await self._stop.wait()

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        log.info("supervisor stopped", extra={"event": "supervisor_stopped"})

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self.request_stop)

    async def _run_job(self, job: Job) -> None:
        if job.initial_delay_s:
            await self._sleep(job.initial_delay_s)

        while not self.stopping:
            cycle_started = time.monotonic()
            new_correlation_id()
            job.status.last_started_at = datetime.now(UTC)
            job.status.run_count += 1

            try:
                await job.fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a job must never kill the process
                job.status.consecutive_failures += 1
                job.status.last_error_at = datetime.now(UTC)
                job.status.last_error = f"{type(exc).__name__}: {exc}"[:500]
                log.exception(
                    "job failed",
                    extra={
                        "event": "job_failed",
                        "component": job.component,
                        "error_code": type(exc).__name__,
                        "detail": {"job": job.name, "consecutive_failures": job.status.consecutive_failures},
                    },
                )
                self._record_event(job, "job_failed", "ERROR", str(exc))
                await self._sleep(self._backoff(job))
                continue
            else:
                job.status.consecutive_failures = 0
                job.status.success_count += 1
                job.status.last_success_at = datetime.now(UTC)

            elapsed = time.monotonic() - cycle_started
            job.status.last_duration_ms = int(elapsed * 1000)

            if elapsed > job.interval_s:
                # The job took longer than its own period. That is not fatal,
                # but it means the cadence is not being met and the phase gate
                # that measures snapshot gaps needs to know.
                job.status.missed_cycles += 1
                log.warning(
                    "job overran its interval",
                    extra={
                        "event": "job_overran",
                        "component": job.component,
                        "detail": {
                            "job": job.name,
                            "elapsed_s": round(elapsed, 2),
                            "interval_s": job.interval_s,
                        },
                    },
                )

            await self._sleep(max(0.0, job.interval_s - elapsed))

    def _backoff(self, job: Job) -> float:
        base = min(job.max_backoff_s, 2 ** min(job.status.consecutive_failures, 8))
        return random.uniform(base / 2, base)

    def _record_event(self, job: Job, event: str, severity: str, detail: str) -> None:
        """Best-effort. If the database itself is the failure, do not compound it."""
        if not database_reachable():
            reset_engine()
            return
        try:
            with session_scope() as session:
                record_system_event(
                    session,
                    component=job.component,
                    event=event,
                    severity=severity,
                    error_code=event,
                    detail={"job": job.name, "error": detail[:500]},
                )
        except Exception:  # noqa: BLE001
            log.warning(
                "could not record system event",
                extra={"event": "system_event_write_failed", "detail": {"job": job.name}},
            )

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep so shutdown is prompt rather than eventual."""
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
