"""System health assessment.

Health is derived from ``system_events`` rows written by the worker, not from
an in-memory flag. That matters for two reasons: the API process has no
visibility into the worker's memory, and a health signal that resets to
"healthy" every time a process restarts is worse than no signal at all.

The rule applied throughout: a component whose state cannot be determined is
UNKNOWN, and a component that should have reported by now and has not is STALE.
Neither is quietly upgraded to HEALTHY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import ComponentHealth, SystemComponent
from app.db.models import Market, MarketSnapshot, Prediction, Signal, SystemEvent
from app.db.session import database_reachable

# How long after its expected cadence a component is considered stale.
_STALENESS_MULTIPLIER = 3.0


@dataclass
class ComponentStatus:
    component: str
    health: ComponentHealth
    last_event_at: datetime | None = None
    age_s: float | None = None
    detail: dict = field(default_factory=dict)
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "health": self.health.value,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "age_seconds": round(self.age_s, 1) if self.age_s is not None else None,
            "message": self.message,
            "detail": self.detail,
        }


def _expected_interval(component: str, settings: Settings) -> float:
    return {
        SystemComponent.MARKET_DISCOVERY.value: settings.discovery_interval_s,
        SystemComponent.DATA_FEED.value: settings.snapshot_interval_s,
        SystemComponent.PROBABILITY_ENGINE.value: settings.prediction_interval_s,
        SystemComponent.RESOLUTION_ENGINE.value: settings.resolution_interval_s,
        SystemComponent.METRICS_ENGINE.value: settings.metrics_interval_s,
        SystemComponent.WORKERS.value: settings.heartbeat_interval_s,
    }.get(component, settings.discovery_interval_s)


def component_statuses(
    session: Session, *, settings: Settings | None = None, now: datetime | None = None
) -> list[ComponentStatus]:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    tracked = [
        SystemComponent.MARKET_DISCOVERY,
        SystemComponent.DATA_FEED,
        SystemComponent.PROBABILITY_ENGINE,
        SystemComponent.RESOLUTION_ENGINE,
        SystemComponent.METRICS_ENGINE,
        SystemComponent.WORKERS,
    ]

    statuses: list[ComponentStatus] = []
    for component in tracked:
        row = session.execute(
            select(SystemEvent)
            .where(SystemEvent.component == component.value)
            .order_by(SystemEvent.occurred_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if row is None:
            statuses.append(
                ComponentStatus(
                    component=component.value,
                    health=ComponentHealth.UNKNOWN,
                    message="no events recorded; this component has never run",
                )
            )
            continue

        occurred = row.occurred_at if row.occurred_at.tzinfo else row.occurred_at.replace(tzinfo=UTC)
        age = (now - occurred).total_seconds()
        expected = _expected_interval(component.value, settings)

        if age > expected * _STALENESS_MULTIPLIER:
            health = ComponentHealth.STALE
            message = (
                f"last reported {age:.0f}s ago; expected roughly every {expected:.0f}s"
            )
        else:
            health = ComponentHealth(row.health) if row.health else ComponentHealth.UNKNOWN
            message = f"last {row.event} {age:.0f}s ago"

        statuses.append(
            ComponentStatus(
                component=component.value,
                health=health,
                last_event_at=occurred,
                age_s=age,
                detail=row.detail or {},
                message=message,
            )
        )

    # Database and engines that have no cadence of their own.
    statuses.append(
        ComponentStatus(
            component=SystemComponent.DATABASE.value,
            health=ComponentHealth.HEALTHY if database_reachable() else ComponentHealth.FAILED,
            message="connection probe",
        )
    )
    return statuses


def overall_health(statuses: list[ComponentStatus]) -> ComponentHealth:
    """Worst-of. A system is only as healthy as its unhealthiest component."""
    order = [
        ComponentHealth.FAILED,
        ComponentHealth.STALE,
        ComponentHealth.DEGRADED,
        ComponentHealth.UNKNOWN,
        ComponentHealth.HEALTHY,
    ]
    present = {s.health for s in statuses}
    for level in order:
        if level in present:
            return level
    return ComponentHealth.UNKNOWN


def data_freshness(session: Session, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    latest = session.execute(select(func.max(MarketSnapshot.known_at))).scalar_one_or_none()
    if latest is None:
        return {"last_update_at": None, "age_seconds": None, "is_stale": True}
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    age = (now - latest).total_seconds()
    return {
        "last_update_at": latest.isoformat(),
        "age_seconds": round(age, 1),
        "is_stale": age > get_settings().data_staleness_s,
    }


def counters(session: Session, *, now: datetime | None = None) -> dict:
    """Headline counts for the dashboard. All derived, none asserted."""
    now = now or datetime.now(UTC)
    day_ago = now - timedelta(days=1)

    markets_total = session.execute(select(func.count()).select_from(Market)).scalar_one()
    markets_with_data = session.execute(
        select(func.count(func.distinct(MarketSnapshot.market_id)))
    ).scalar_one()
    markets_tradeable = session.execute(
        select(func.count()).select_from(Market).where(Market.modelability_status == "TRADEABLE")
    ).scalar_one()
    markets_watchlist = session.execute(
        select(func.count()).select_from(Market).where(Market.modelability_status == "WATCHLIST")
    ).scalar_one()
    predictions_total = session.execute(select(func.count()).select_from(Prediction)).scalar_one()
    predictions_24h = session.execute(
        select(func.count()).select_from(Prediction).where(Prediction.predicted_at >= day_ago)
    ).scalar_one()
    opportunities = session.execute(
        select(func.count())
        .select_from(Signal)
        .where(Signal.recommendation.in_(["BUY", "SELL"]), Signal.signal_at >= day_ago)
    ).scalar_one()
    high_confidence = session.execute(
        select(func.count())
        .select_from(Signal)
        .where(
            Signal.recommendation.in_(["BUY", "SELL"]),
            Signal.signal_at >= day_ago,
            Signal.confidence >= 0.7,
        )
    ).scalar_one()

    return {
        "markets_discovered": int(markets_total),
        "markets_with_market_data": int(markets_with_data),
        "markets_tradeable": int(markets_tradeable),
        "markets_watchlist": int(markets_watchlist),
        "predictions_total": int(predictions_total),
        "predictions_24h": int(predictions_24h),
        "opportunities_24h": int(opportunities),
        "high_confidence_opportunities_24h": int(high_confidence),
    }
