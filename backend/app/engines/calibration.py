"""Calibration and scoring.

Calibration is the first-class metric in this system, ahead of P&L. A model that
says 70% and is right 70% of the time is useful even if it never trades; a model
that is profitable but miscalibrated is lucky, and luck does not survive
scaling.

Everything here is computed from stored outcomes only. Functions return `None`
rather than a number when the sample is too small to support one — reporting a
Brier score over four observations would be worse than reporting nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_SAMPLE_FOR_SCORE = 20
MIN_SAMPLE_PER_BUCKET = 5
DEFAULT_BUCKETS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


@dataclass
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float | None
    observed_frequency: float | None

    def as_dict(self) -> dict:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "mean_predicted": self.mean_predicted,
            "observed_frequency": self.observed_frequency,
            # The gap is the whole point of a reliability diagram: how far the
            # claimed probability sat from what actually happened.
            "gap": (
                None
                if self.mean_predicted is None or self.observed_frequency is None
                else round(self.observed_frequency - self.mean_predicted, 4)
            ),
        }


@dataclass
class CalibrationReport:
    sample_size: int
    brier_score: float | None
    log_loss: float | None
    expected_calibration_error: float | None
    max_calibration_error: float | None
    base_rate: float | None
    mean_prediction: float | None
    bins: list[CalibrationBin] = field(default_factory=list)
    insufficient_data: bool = False
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "sample_size": self.sample_size,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "expected_calibration_error": self.expected_calibration_error,
            "max_calibration_error": self.max_calibration_error,
            "base_rate": self.base_rate,
            "mean_prediction": self.mean_prediction,
            "bins": [b.as_dict() for b in self.bins],
            "insufficient_data": self.insufficient_data,
            "note": self.note,
        }


def brier_score(predictions: list[float], outcomes: list[int]) -> float | None:
    """Mean squared error of probabilistic forecasts. Lower is better.

    0.25 is what you get by always saying 0.5. A model above that on balanced
    events is worse than useless.
    """
    if len(predictions) != len(outcomes) or not predictions:
        return None
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes, strict=True)) / len(predictions)


def log_loss(predictions: list[float], outcomes: list[int], eps: float = 1e-15) -> float | None:
    """Penalises confident errors far more harshly than Brier does."""
    if len(predictions) != len(outcomes) or not predictions:
        return None
    total = 0.0
    for p, o in zip(predictions, outcomes, strict=True):
        clipped = min(1 - eps, max(eps, p))
        total += -(o * math.log(clipped) + (1 - o) * math.log(1 - clipped))
    return total / len(predictions)


def calibration_bins(
    predictions: list[float],
    outcomes: list[int],
    buckets: tuple[float, ...] = DEFAULT_BUCKETS,
) -> list[CalibrationBin]:
    bins: list[CalibrationBin] = []
    for lower, upper in zip(buckets[:-1], buckets[1:], strict=True):
        # Upper-inclusive on the last bin so p == 1.0 is not discarded.
        is_last = upper == buckets[-1]
        members = [
            (p, o)
            for p, o in zip(predictions, outcomes, strict=True)
            if ((lower <= p <= upper) if is_last else (lower <= p < upper))
        ]
        if members:
            mean_p = sum(p for p, _ in members) / len(members)
            observed = sum(o for _, o in members) / len(members)
        else:
            mean_p = observed = None
        bins.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(members),
                mean_predicted=mean_p,
                observed_frequency=observed,
            )
        )
    return bins


def expected_calibration_error(bins: list[CalibrationBin], total: int) -> tuple[float | None, float | None]:
    """Sample-weighted mean gap, and the worst single gap.

    Bins below MIN_SAMPLE_PER_BUCKET are excluded: a bin with two observations
    reports a 0% or 100% frequency and would dominate the average with noise.
    """
    if total == 0:
        return None, None
    weighted = 0.0
    counted = 0
    worst: float | None = None
    for b in bins:
        if b.count < MIN_SAMPLE_PER_BUCKET or b.mean_predicted is None or b.observed_frequency is None:
            continue
        gap = abs(b.observed_frequency - b.mean_predicted)
        weighted += gap * b.count
        counted += b.count
        worst = gap if worst is None else max(worst, gap)
    if counted == 0:
        return None, None
    return weighted / counted, worst


def build_report(
    predictions: list[float],
    outcomes: list[int],
    *,
    min_sample: int = MIN_SAMPLE_FOR_SCORE,
) -> CalibrationReport:
    """Full calibration report, or an explicit statement that we cannot make one."""
    n = len(predictions)
    if n != len(outcomes):
        raise ValueError("predictions and outcomes must be the same length")

    if n < min_sample:
        return CalibrationReport(
            sample_size=n,
            brier_score=None,
            log_loss=None,
            expected_calibration_error=None,
            max_calibration_error=None,
            base_rate=None,
            mean_prediction=None,
            bins=[],
            insufficient_data=True,
            note=(
                f"{n} resolved observations is below the {min_sample} minimum; "
                "no calibration figure is reported rather than an unreliable one"
            ),
        )

    bins = calibration_bins(predictions, outcomes)
    ece, mce = expected_calibration_error(bins, n)
    return CalibrationReport(
        sample_size=n,
        brier_score=brier_score(predictions, outcomes),
        log_loss=log_loss(predictions, outcomes),
        expected_calibration_error=ece,
        max_calibration_error=mce,
        base_rate=sum(outcomes) / n,
        mean_prediction=sum(predictions) / n,
        bins=bins,
        insufficient_data=False,
    )


def skill_versus_baseline(
    model_predictions: list[float],
    baseline_predictions: list[float],
    outcomes: list[int],
) -> dict:
    """Does the model beat the market on the same resolved set?

    This is the comparison that matters and the one most systems avoid. Being
    well calibrated is not an achievement — the market is well calibrated too.
    The Brier skill score is positive only if we genuinely improved on it.
    """
    model_brier = brier_score(model_predictions, outcomes)
    baseline_brier = brier_score(baseline_predictions, outcomes)

    if model_brier is None or baseline_brier is None:
        return {
            "model_brier": model_brier,
            "baseline_brier": baseline_brier,
            "brier_skill_score": None,
            "beats_baseline": None,
            "note": "insufficient data to compare",
        }

    skill = 1.0 - (model_brier / baseline_brier) if baseline_brier > 0 else None
    return {
        "model_brier": model_brier,
        "baseline_brier": baseline_brier,
        "brier_skill_score": skill,
        "beats_baseline": model_brier < baseline_brier,
        "note": (
            "baseline is the market-implied probability at prediction time; "
            "a positive skill score means the model improved on the market"
        ),
    }
