"""Metrics worker.

Computes calibration and performance from stored outcomes and writes them to
``performance_metrics``. Every figure is derived from data in the database; none
is asserted, and where the sample is too small the row records
``insufficient_data: true`` rather than a misleading number.

Deliberately absent: annualised returns. With a few weeks of observations an
annualised figure is arithmetic dressed up as a claim, and the spec forbids it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import ComponentHealth, ResolutionOutcome, SystemComponent
from app.core.logging import get_logger
from app.db.models import Market, PerformanceMetric, Prediction, Resolution
from app.db.session import session_scope
from app.engines.calibration import build_report, skill_versus_baseline
from app.ingest.repository import record_system_event

log = get_logger("workers.metrics")


class MetricsWorker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def run_once(self) -> dict:
        started = datetime.now(UTC)
        stats: dict = {"metric_rows_written": 0, "resolved_observations": 0}

        with session_scope() as session:
            observations = self._resolved_observations(session)
            stats["resolved_observations"] = len(observations)

            if observations:
                stats["metric_rows_written"] += self._write_overall(session, observations)
                stats["metric_rows_written"] += self._write_by_scope(session, observations)

            record_system_event(
                session,
                component=SystemComponent.METRICS_ENGINE.value,
                event="metrics_cycle",
                health=ComponentHealth.HEALTHY.value,
                detail=stats,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )

        log.info("metrics cycle complete", extra={"event": "metrics_cycle", "detail": stats})
        return stats

    # ------------------------------------------------------------------
    def _resolved_observations(self, session: Session) -> list[dict]:
        """Predictions whose market has since resolved unambiguously.

        Two exclusions matter:

        * Ambiguous resolutions are dropped. Scoring against a guessed outcome
          would corrupt every metric that depends on it.
        * Predictions made *after* the resolution became known are dropped.
          Without this, a prediction generated during the settlement window
          would be scored as a brilliant call, which is look-ahead bias
          arriving through the back door.
        """
        rows = session.execute(
            select(
                Prediction.model_probability,
                Prediction.market_probability,
                Prediction.predicted_at,
                Prediction.model_version,
                Prediction.confidence,
                Resolution.outcome,
                Resolution.known_at,
                Market.category,
                Market.liquidity_num,
            )
            .join(Resolution, Resolution.market_id == Prediction.market_id)
            .join(Market, Market.id == Prediction.market_id)
            .where(
                Resolution.is_ambiguous.is_(False),
                Resolution.outcome.in_([ResolutionOutcome.YES.value, ResolutionOutcome.NO.value]),
                Prediction.predicted_at < Resolution.known_at,
            )
        ).all()

        observations: list[dict] = []
        for (
            model_p, market_p, predicted_at, model_version, confidence,
            outcome, resolution_known_at, category, liquidity,
        ) in rows:
            observations.append(
                {
                    "model_probability": float(model_p),
                    "market_probability": float(market_p),
                    "outcome": 1 if outcome == ResolutionOutcome.YES.value else 0,
                    "predicted_at": predicted_at,
                    "resolution_known_at": resolution_known_at,
                    "model_version": model_version,
                    "confidence": float(confidence),
                    "category": category,
                    "liquidity": float(liquidity) if liquidity is not None else None,
                    "horizon_hours": (
                        (resolution_known_at - predicted_at).total_seconds() / 3600.0
                    ),
                }
            )
        return observations

    def _write_overall(self, session: Session, observations: list[dict]) -> int:
        model_p = [o["model_probability"] for o in observations]
        market_p = [o["market_probability"] for o in observations]
        outcomes = [o["outcome"] for o in observations]

        report = build_report(model_p, outcomes)
        market_report = build_report(market_p, outcomes)
        skill = skill_versus_baseline(model_p, market_p, outcomes)

        window_start = min(o["predicted_at"] for o in observations)
        window_end = max(o["resolution_known_at"] for o in observations)

        session.add(
            PerformanceMetric(
                kind="calibration",
                scope="overall",
                scope_value=None,
                model_version=None,
                window_start=window_start,
                window_end=window_end,
                sample_size=len(observations),
                metrics={
                    "model": report.as_dict(),
                    "market_baseline": market_report.as_dict(),
                    "skill_vs_market": skill,
                },
                computed_at=datetime.now(UTC),
            )
        )
        return 1

    def _write_by_scope(self, session: Session, observations: list[dict]) -> int:
        """Slice calibration by the dimensions the spec asks for."""
        written = 0
        slices: dict[str, dict[str, list[dict]]] = {
            "category": defaultdict(list),
            "model_version": defaultdict(list),
            "confidence_bucket": defaultdict(list),
            "liquidity_bucket": defaultdict(list),
            "horizon_bucket": defaultdict(list),
        }

        for obs in observations:
            slices["category"][obs["category"]].append(obs)
            slices["model_version"][obs["model_version"]].append(obs)
            slices["confidence_bucket"][_bucket_label(obs["confidence"], (0.5, 0.6, 0.7, 0.8))].append(obs)
            slices["liquidity_bucket"][_liquidity_label(obs["liquidity"])].append(obs)
            slices["horizon_bucket"][_horizon_label(obs["horizon_hours"])].append(obs)

        now = datetime.now(UTC)
        for scope, groups in slices.items():
            for scope_value, group in groups.items():
                model_p = [o["model_probability"] for o in group]
                market_p = [o["market_probability"] for o in group]
                outcomes = [o["outcome"] for o in group]
                report = build_report(model_p, outcomes)
                session.add(
                    PerformanceMetric(
                        kind="calibration",
                        scope=scope,
                        scope_value=str(scope_value),
                        model_version=scope_value if scope == "model_version" else None,
                        window_start=min(o["predicted_at"] for o in group),
                        window_end=max(o["resolution_known_at"] for o in group),
                        sample_size=len(group),
                        metrics={
                            "model": report.as_dict(),
                            "skill_vs_market": skill_versus_baseline(model_p, market_p, outcomes),
                        },
                        computed_at=now,
                    )
                )
                written += 1
        return written


def _bucket_label(value: float, edges: tuple[float, ...]) -> str:
    for edge in edges:
        if value < edge:
            return f"<{edge}"
    return f">={edges[-1]}"


def _liquidity_label(value: float | None) -> str:
    if value is None:
        return "unknown"
    for edge in (1_000, 10_000, 100_000, 1_000_000):
        if value < edge:
            return f"<{edge}"
    return ">=1000000"


def _horizon_label(hours: float) -> str:
    for edge, label in ((24, "<1d"), (168, "1-7d"), (720, "7-30d")):
        if hours < edge:
            return label
    return ">=30d"
