"""Evidence conflict resolution.

Sources disagree. The spec is explicit that they must not simply be averaged,
and the reasoning is worth stating: averaging an official statistic with a news
report *about* that statistic does not produce a better estimate. It produces a
worse one, and it destroys the information that the disagreement existed.

Resolution is a deterministic precedence ladder, applied in order:

1. **Authority** — a lower tier number wins. An official statistic beats a
   report of it, always.
2. **Verification** — CONFIRMED beats REPORTED beats UNCONFIRMED beats OPINION.
3. **Recency** — among equals, the later observation wins, because economic
   figures are revised and the revision is the better estimate.
4. **Source reliability** — the registry's score breaks remaining ties.

If two candidates are indistinguishable on all four, the conflict is recorded
UNRESOLVED and no winner is chosen. The feature layer then treats the series as
unreliable for that period rather than picking arbitrarily.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import ConflictResolution, VerificationStatus
from app.db.models import EvidenceConflict, ExternalEvent

# Higher is better. Ordering the spec's four verification states explicitly
# rather than relying on enum declaration order.
_VERIFICATION_RANK = {
    VerificationStatus.CONFIRMED_FACT.value: 4,
    VerificationStatus.REPORTED_INFORMATION.value: 3,
    VerificationStatus.UNCONFIRMED_CLAIM.value: 2,
    VerificationStatus.ANALYST_OPINION.value: 1,
    VerificationStatus.UNVERIFIED.value: 0,
}

# Relative disagreement below this is measurement noise, not a conflict.
# Two exchanges quoting BTC 0.01% apart are not in conflict.
MATERIAL_DISAGREEMENT = 0.005


@dataclass
class ConflictOutcome:
    series_key: str
    observation_date: datetime | None
    winner: ExternalEvent | None
    resolution: ConflictResolution
    candidates: list[dict] = field(default_factory=list)
    spread: float | None = None
    detail: dict = field(default_factory=dict)

    @property
    def is_conflict(self) -> bool:
        return len(self.candidates) > 1


def _candidate_dict(event: ExternalEvent) -> dict:
    return {
        "evidence_id": event.id,
        "source_id": event.source_id,
        "source_tier": event.source_tier,
        "value": event.numeric_value,
        "unit": event.unit,
        "verification_status": event.verification_status,
        "reliability_score": event.reliability_score,
        "known_at": event.known_at.isoformat() if event.known_at else None,
        "observation_date": (
            event.observation_date.isoformat() if event.observation_date else None
        ),
    }


def _relative_spread(values: list[float]) -> float | None:
    """Disagreement as a fraction of the mean magnitude.

    Relative rather than absolute because the same absolute gap means very
    different things for a 3.9% yield and a $64,000 price.
    """
    if len(values) < 2:
        return None
    low, high = min(values), max(values)
    scale = max(abs(low), abs(high))
    if scale == 0:
        return 0.0
    return (high - low) / scale


def resolve(candidates: list[ExternalEvent]) -> ConflictOutcome:
    """Pick the authoritative observation from competing claims."""
    if not candidates:
        return ConflictOutcome(
            series_key="", observation_date=None, winner=None,
            resolution=ConflictResolution.UNRESOLVED,
        )

    series_key = candidates[0].series_key or ""
    observation_date = candidates[0].observation_date

    if len(candidates) == 1:
        return ConflictOutcome(
            series_key=series_key,
            observation_date=observation_date,
            winner=candidates[0],
            resolution=ConflictResolution.HIGHER_TIER,
            candidates=[_candidate_dict(candidates[0])],
            detail={"note": "single candidate; no conflict"},
        )

    values = [c.numeric_value for c in candidates if c.numeric_value is not None]
    spread = _relative_spread(values) if len(values) > 1 else None

    # 1. Authority.
    best_tier = min(c.source_tier for c in candidates)
    tier_winners = [c for c in candidates if c.source_tier == best_tier]
    if len(tier_winners) == 1:
        return _outcome(
            series_key, observation_date, tier_winners[0], ConflictResolution.HIGHER_TIER,
            candidates, spread, {"winning_tier": best_tier},
        )

    # 2. Verification status.
    best_rank = max(_VERIFICATION_RANK.get(c.verification_status, 0) for c in tier_winners)
    verified = [
        c for c in tier_winners
        if _VERIFICATION_RANK.get(c.verification_status, 0) == best_rank
    ]
    if len(verified) == 1:
        return _outcome(
            series_key, observation_date, verified[0], ConflictResolution.BETTER_VERIFIED,
            candidates, spread, {"verification_rank": best_rank},
        )

    # 3. Recency. A later observation of the same period is a revision, and a
    # revision is the issuer's own better estimate.
    latest = max(c.known_at for c in verified)
    recent = [c for c in verified if c.known_at == latest]
    if len(recent) == 1:
        return _outcome(
            series_key, observation_date, recent[0], ConflictResolution.MORE_RECENT,
            candidates, spread, {"known_at": latest.isoformat()},
        )

    # 4. Registry reliability.
    best_reliability = max((c.reliability_score or 0.0) for c in recent)
    reliable = [c for c in recent if (c.reliability_score or 0.0) == best_reliability]
    if len(reliable) == 1:
        return _outcome(
            series_key, observation_date, reliable[0],
            ConflictResolution.MORE_RELIABLE_SOURCE, candidates, spread,
            {"reliability_score": best_reliability},
        )

    # Genuinely indistinguishable. Recording that honestly is better than
    # picking the first row and pretending it was a decision.
    return ConflictOutcome(
        series_key=series_key,
        observation_date=observation_date,
        winner=None,
        resolution=ConflictResolution.UNRESOLVED,
        candidates=[_candidate_dict(c) for c in candidates],
        spread=spread,
        detail={
            "note": (
                f"{len(reliable)} candidates are indistinguishable on tier, "
                "verification, recency and reliability; no winner chosen"
            )
        },
    )


def _outcome(
    series_key: str,
    observation_date: datetime | None,
    winner: ExternalEvent,
    resolution: ConflictResolution,
    candidates: list[ExternalEvent],
    spread: float | None,
    detail: dict,
) -> ConflictOutcome:
    return ConflictOutcome(
        series_key=series_key,
        observation_date=observation_date,
        winner=winner,
        resolution=resolution,
        candidates=[_candidate_dict(c) for c in candidates],
        spread=spread,
        detail=detail,
    )


def detect_and_record(
    session: Session,
    *,
    series_key: str,
    as_of: datetime,
    market_id: int | None = None,
) -> ConflictOutcome | None:
    """Find competing observations of a series and record any real conflict.

    Returns None when there is nothing to compare. Only *material* numeric
    disagreement is recorded — two exchanges a hundredth of a percent apart are
    agreeing, and logging that as a conflict would bury the real ones.
    """
    latest_period = session.execute(
        select(ExternalEvent.observation_date)
        .where(
            ExternalEvent.series_key == series_key,
            ExternalEvent.known_at <= as_of,
            ExternalEvent.observation_date.isnot(None),
        )
        .order_by(ExternalEvent.observation_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_period is None:
        return None

    candidates = session.execute(
        select(ExternalEvent).where(
            ExternalEvent.series_key == series_key,
            ExternalEvent.observation_date == latest_period,
            ExternalEvent.known_at <= as_of,
        )
    ).scalars().all()

    if len(candidates) < 2:
        return None

    outcome = resolve(list(candidates))

    if outcome.spread is not None and outcome.spread < MATERIAL_DISAGREEMENT:
        return outcome  # agreement, nothing to record

    session.add(
        EvidenceConflict(
            series_key=series_key,
            observation_date=latest_period,
            market_id=market_id,
            winning_evidence_id=outcome.winner.id if outcome.winner else None,
            resolution=outcome.resolution.value,
            resolution_detail=outcome.detail,
            candidates=outcome.candidates,
            spread=outcome.spread,
            detected_at=datetime.now(UTC),
            known_at=as_of,
        )
    )
    return outcome


def authoritative_value(
    session: Session, series_key: str, *, as_of: datetime
) -> tuple[float | None, ExternalEvent | None, ConflictResolution]:
    """The value to use for a series, after resolving any conflict.

    This is the accessor the feature layer calls. It never averages, and it
    returns None when the conflict is unresolved rather than guessing.
    """
    latest_period = session.execute(
        select(ExternalEvent.observation_date)
        .where(
            ExternalEvent.series_key == series_key,
            ExternalEvent.known_at <= as_of,
            ExternalEvent.numeric_value.isnot(None),
        )
        .order_by(ExternalEvent.observation_date.desc().nullslast())
        .limit(1)
    ).scalar_one_or_none()

    query = select(ExternalEvent).where(
        ExternalEvent.series_key == series_key,
        ExternalEvent.known_at <= as_of,
        ExternalEvent.numeric_value.isnot(None),
    )
    query = (
        query.where(ExternalEvent.observation_date == latest_period)
        if latest_period is not None
        else query.order_by(ExternalEvent.known_at.desc()).limit(1)
    )

    candidates = list(session.execute(query).scalars().all())
    if not candidates:
        return None, None, ConflictResolution.UNRESOLVED

    outcome = resolve(candidates)
    if outcome.winner is None:
        return None, None, outcome.resolution
    return outcome.winner.numeric_value, outcome.winner, outcome.resolution
