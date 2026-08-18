"""Walk-forward validation.

Splits resolved observations into rolling train / validation / out-of-sample
windows, with the three protections that make the result mean anything:

* **Purging.** Training observations whose outcome became known inside the
  validation window are removed. Without this the model trains on the answer to
  a question it is about to be tested on.
* **Embargo.** A gap after each validation window before training resumes.
  Prediction-market observations are serially correlated — the same market
  predicted daily for a month is thirty near-copies — and without an embargo
  that correlation leaks across the boundary.
* **Event grouping.** All legs of one neg-risk event land in the same fold.
  Splitting them leaks the answer directly, because the legs sum to one.

The module deliberately does not fit anything. Model fitting requires resolved
history this deployment does not have, and building a trainer that has never
seen data would be building a fiction. What exists is the splitter and the
evaluator — the parts that determine whether a future fit can be trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Market, Prediction, Resolution
from app.engines.calibration import build_report, skill_versus_baseline


@dataclass
class Observation:
    """One resolved prediction, with everything the splitter needs."""

    market_id: int
    event_group: str
    predicted_at: datetime
    resolution_known_at: datetime
    model_probability: float
    market_probability: float
    outcome: int
    category: str
    model_version: str


@dataclass
class Fold:
    index: int
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime
    train: list[Observation] = field(default_factory=list)
    validation: list[Observation] = field(default_factory=list)
    test: list[Observation] = field(default_factory=list)
    purged: int = 0
    embargoed: int = 0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "train": {
                "start": self.train_start.isoformat(),
                "end": self.train_end.isoformat(),
                "n": len(self.train),
            },
            "validation": {
                "start": self.validation_start.isoformat(),
                "end": self.validation_end.isoformat(),
                "n": len(self.validation),
            },
            "test": {
                "start": self.test_start.isoformat(),
                "end": self.test_end.isoformat(),
                "n": len(self.test),
            },
            "purged": self.purged,
            "embargoed": self.embargoed,
        }


@dataclass
class WalkForwardResult:
    folds: list[Fold] = field(default_factory=list)
    total_observations: int = 0
    note: str = ""

    def evaluate(self) -> dict:
        """Out-of-sample metrics per fold, and pooled across folds.

        Pooled figures come from concatenating the test sets, never from
        averaging per-fold scores — averaging weights a fold of five
        observations equally with a fold of five hundred.
        """
        per_fold: list[dict] = []
        pooled_model: list[float] = []
        pooled_market: list[float] = []
        pooled_outcomes: list[int] = []

        for fold in self.folds:
            if not fold.test:
                per_fold.append({**fold.as_dict(), "metrics": {"insufficient_data": True}})
                continue

            model = [o.model_probability for o in fold.test]
            market = [o.market_probability for o in fold.test]
            outcomes = [o.outcome for o in fold.test]

            pooled_model.extend(model)
            pooled_market.extend(market)
            pooled_outcomes.extend(outcomes)

            per_fold.append(
                {
                    **fold.as_dict(),
                    "metrics": {
                        "model": build_report(model, outcomes).as_dict(),
                        "skill_vs_market": skill_versus_baseline(model, market, outcomes),
                    },
                }
            )

        pooled: dict
        if pooled_outcomes:
            pooled = {
                "sample_size": len(pooled_outcomes),
                "model": build_report(pooled_model, pooled_outcomes).as_dict(),
                "market_baseline": build_report(pooled_market, pooled_outcomes).as_dict(),
                "skill_vs_market": skill_versus_baseline(
                    pooled_model, pooled_market, pooled_outcomes
                ),
            }
        else:
            pooled = {
                "sample_size": 0,
                "insufficient_data": True,
                "note": "no out-of-sample observations across any fold",
            }

        return {
            "folds": per_fold,
            "pooled_out_of_sample": pooled,
            "total_observations": self.total_observations,
            "note": self.note,
        }


def load_observations(
    session: Session, *, min_lead_hours: float = 24.0
) -> list[Observation]:
    """Resolved predictions eligible for validation.

    Two exclusions, both load-bearing:
      * ambiguous resolutions, which would score the model against a guess;
      * predictions made within `min_lead_hours` of the resolution becoming
        known, which prove nothing about forecasting.
    """
    rows = session.execute(
        select(
            Prediction.market_id,
            Prediction.predicted_at,
            Prediction.model_probability,
            Prediction.market_probability,
            Prediction.model_version,
            Resolution.outcome,
            Resolution.known_at,
            Market.category,
            Market.neg_risk_market_id,
            Market.event_id,
        )
        .join(Resolution, Resolution.market_id == Prediction.market_id)
        .join(Market, Market.id == Prediction.market_id)
        .where(
            Resolution.is_ambiguous.is_(False),
            Resolution.outcome.in_(["YES", "NO"]),
        )
        .order_by(Prediction.predicted_at)
    ).all()

    observations: list[Observation] = []
    for (
        market_id, predicted_at, model_p, market_p, model_version,
        outcome, known_at, category, negrisk_id, event_id,
    ) in rows:
        lead = (known_at - predicted_at).total_seconds() / 3600.0
        if lead < min_lead_hours:
            continue

        # Correlated legs must share a group so the splitter keeps them together.
        group = negrisk_id or (f"event:{event_id}" if event_id else f"market:{market_id}")

        observations.append(
            Observation(
                market_id=market_id,
                event_group=group,
                predicted_at=predicted_at,
                resolution_known_at=known_at,
                model_probability=float(model_p),
                market_probability=float(market_p),
                outcome=1 if outcome == "YES" else 0,
                category=category,
                model_version=model_version,
            )
        )
    return observations


def build_folds(
    observations: list[Observation],
    *,
    n_folds: int = 4,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
    embargo: timedelta = timedelta(days=3),
    settings: Settings | None = None,
) -> WalkForwardResult:
    """Rolling train/validation/test folds with purging and embargo."""
    settings = settings or get_settings()
    result = WalkForwardResult(total_observations=len(observations))

    if len(observations) < n_folds * 10:
        result.note = (
            f"{len(observations)} resolved observations is too few for {n_folds} "
            "walk-forward folds; no split was produced. This is the expected state "
            "until markets predicted by this system have resolved."
        )
        return result

    ordered = sorted(observations, key=lambda o: o.predicted_at)
    start = ordered[0].predicted_at
    end = ordered[-1].predicted_at
    span = end - start
    if span <= timedelta(0):
        result.note = "all observations share one timestamp; no time split is possible"
        return result

    # Each fold advances its window by one step across the span.
    window = span / (n_folds + 1)

    for index in range(n_folds):
        fold_start = start + window * index
        train_end = fold_start + window * train_fraction
        validation_end = train_end + window * validation_fraction
        test_end = fold_start + window

        fold = Fold(
            index=index,
            train_start=fold_start,
            train_end=train_end,
            validation_start=train_end,
            validation_end=validation_end,
            test_start=validation_end,
            test_end=test_end,
        )

        _populate_fold(fold, ordered, embargo)
        result.folds.append(fold)

    return result


def _populate_fold(fold: Fold, ordered: list[Observation], embargo: timedelta) -> None:
    """Assign observations to a fold, purging and embargoing as required."""
    # Groups appearing in validation or test are barred from training entirely,
    # because a neg-risk sibling in training gives away its complement.
    later_groups = {
        o.event_group
        for o in ordered
        if fold.validation_start <= o.predicted_at < fold.test_end
    }

    for observation in ordered:
        predicted = observation.predicted_at

        if fold.train_start <= predicted < fold.train_end:
            # Purge: outcome known during validation or test.
            if observation.resolution_known_at >= fold.validation_start:
                fold.purged += 1
                continue
            # Embargo: too close to the validation boundary.
            if predicted > fold.train_end - embargo:
                fold.embargoed += 1
                continue
            # Group leakage.
            if observation.event_group in later_groups:
                fold.purged += 1
                continue
            fold.train.append(observation)

        elif fold.validation_start <= predicted < fold.validation_end:
            fold.validation.append(observation)

        elif fold.test_start <= predicted < fold.test_end:
            fold.test.append(observation)


def category_sample_sizes(observations: list[Observation]) -> dict[str, int]:
    """Resolved observations per category.

    Reported separately because the spec is explicit that categories must not be
    pooled to reach a round number — an inflation model trained partly on sports
    outcomes is not an inflation model.
    """
    counts: dict[str, int] = {}
    for observation in observations:
        counts[observation.category] = counts.get(observation.category, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def training_readiness(
    observations: list[Observation], *, settings: Settings | None = None
) -> dict:
    """Whether any category has enough resolved history to train on."""
    settings = settings or get_settings()
    counts = category_sample_sizes(observations)
    threshold = settings.min_category_training_observations

    return {
        "total_resolved_observations": len(observations),
        "per_category": counts,
        "per_category_threshold": threshold,
        "categories_ready": [c for c, n in counts.items() if n >= threshold],
        "global_threshold": settings.min_training_observations,
        "global_ready": len(observations) >= settings.min_training_observations,
        "note": (
            "categories are counted separately and never pooled to reach a "
            "threshold; a model is trained per category or not at all"
        ),
    }
