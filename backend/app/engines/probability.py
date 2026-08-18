"""Independent probability engine.

Design stance, stated plainly because it drives every choice below: **a
prediction market is a strong forecast**, and a model that disagrees loudly
with a liquid market on no evidence is not insightful, it is broken. The
baseline shipped here therefore agrees with the market *by default* and departs
from it only where it has a specific, nameable reason.

There are exactly three such reasons in the baseline, and each is a real,
measurable phenomenon rather than a fitted parameter:

1. **Neg-risk coherence.** Polymarket groups mutually-exclusive outcomes
   (`negRisk`) into an event. Their YES prices must sum to 1. When the observed
   sum drifts, at least one leg is mispriced, and the direction is known — if
   the group sums to 1.06, every leg is on average 6% too expensive. This
   requires no external data at all and is the only genuinely model-free edge
   the baseline claims.
2. **Microstructure imbalance.** Resting depth is a weak, short-horizon
   predictor of near-term drift. Weighted low, and decayed to nothing over
   longer horizons where it means nothing.
3. **Extreme-price regularisation.** Very cheap legs in large multi-outcome
   groups are systematically overpriced relative to their realised frequency —
   the favourite–longshot bias, one of the most replicated findings in the
   prediction-market literature. The baseline nudges toward zero on tails,
   and does so *symmetrically* so it cannot become a directional bet.

Everything else is shrinkage toward the market, scaled by uncertainty.

**Layer 2 (trained models) is deliberately inactive on a fresh install.** Until
`min_training_observations` markets have resolved there is nothing honest to
train on, and the engine reports INSUFFICIENT_DATA rather than shipping an
untrained estimator. This is the normal state of a new deployment and the
dashboard says so.

This module MUST NOT import anything from `app.execution`. That property is
asserted by tests/security/test_execution_boundary.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.core.enums import MarketCategory, ResolutionRisk
from app.engines.liquidity import LiquidityProfile

BASELINE_FEATURE_SET = [
    "market_midpoint",
    "executable_price",
    "spread_pct",
    "book_imbalance",
    "hours_to_resolution",
    "negrisk_group_sum",
    "negrisk_group_size",
    "snapshot_count",
]


class InvalidModelOutput(ValueError):
    """Raised when a model produces something that must not become a prediction.

    The spec is unambiguous here: a rejected prediction is dropped, never
    replaced with a random or default probability.
    """


@dataclass
class ProbabilityInputs:
    """Everything the baseline needs. Assembled by the caller from data whose
    ``known_at`` is at or before the prediction timestamp."""

    market_id: int
    token_id: str
    category: MarketCategory
    midpoint: float
    executable_price: float | None
    liquidity_profile: LiquidityProfile | None
    hours_to_resolution: float | None
    snapshot_count: int
    # Neg-risk context: the sum of midpoints across sibling YES legs, and how
    # many legs there are. None when the market is not part of such a group.
    negrisk_group_sum: float | None = None
    negrisk_group_size: int | None = None
    data_received_at: datetime | None = None


@dataclass
class ProbabilityResult:
    model_probability: float
    model_uncertainty: float
    confidence: float
    model_version: str
    features: dict[str, float | None]
    adjustments: dict[str, float] = field(default_factory=dict)
    rationale: dict = field(default_factory=dict)
    resolution_risk: ResolutionRisk = ResolutionRisk.UNKNOWN


def validate_probability(value: object, *, model_version: str, field_name: str = "probability") -> float:
    """Gate every number that claims to be a probability.

    Rejects non-numeric, NaN, infinity, and out-of-range values. Deliberately
    strict: a NaN that reaches the edge engine silently poisons a sizing
    decision, and a probability of 1.4 is not a probability.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidModelOutput(
            f"{field_name} from {model_version} is {type(value).__name__}, not numeric"
        )
    numeric = float(value)
    if math.isnan(numeric):
        raise InvalidModelOutput(f"{field_name} from {model_version} is NaN")
    if math.isinf(numeric):
        raise InvalidModelOutput(f"{field_name} from {model_version} is infinite")
    if not 0.0 <= numeric <= 1.0:
        raise InvalidModelOutput(
            f"{field_name} from {model_version} is {numeric}, outside [0,1]"
        )
    return numeric


def _logit(p: float, eps: float = 1e-6) -> float:
    p = min(1.0 - eps, max(eps, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class BaselineProbabilityModel:
    """Interpretable, deterministic, no training data required.

    Works in log-odds space so that adjustments compose sensibly and cannot
    push the output outside [0,1]. Every adjustment is recorded by name so the
    dashboard can show exactly why the model differs from the market.
    """

    # Adjustment magnitudes, in log-odds. Small on purpose: these are nudges
    # against a strong prior, not opinions.
    NEGRISK_MAX_ADJUSTMENT = 0.60
    IMBALANCE_MAX_ADJUSTMENT = 0.15
    LONGSHOT_MAX_ADJUSTMENT = 0.25

    MAX_TRUSTED_NEGRISK_ERROR = 0.20
    """Above this, an apparent coherence violation is far more likely to be our
    own missing data than a real arbitrage, so the adjustment is dropped."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.version = self.settings.baseline_model_version

    def predict(self, inputs: ProbabilityInputs) -> ProbabilityResult:
        market_p = validate_probability(
            inputs.midpoint, model_version=self.version, field_name="market midpoint"
        )

        base_logit = _logit(market_p)
        adjustments: dict[str, float] = {}

        adjustments["negrisk_coherence"] = self._negrisk_adjustment(inputs, market_p)
        adjustments["book_imbalance"] = self._imbalance_adjustment(inputs)
        adjustments["favourite_longshot"] = self._longshot_adjustment(inputs, market_p)

        raw_logit = base_logit + sum(adjustments.values())
        raw_probability = _sigmoid(raw_logit)

        uncertainty = self._uncertainty(inputs)

        # Shrink toward the market in proportion to how unsure we are. At
        # uncertainty 1.0 the model *is* the market, which is the correct
        # behaviour for a model that knows nothing.
        shrunk = market_p + (raw_probability - market_p) * (1.0 - uncertainty)
        model_probability = validate_probability(
            shrunk, model_version=self.version, field_name="model probability"
        )

        confidence = self._confidence(inputs, uncertainty)

        return ProbabilityResult(
            model_probability=model_probability,
            model_uncertainty=uncertainty,
            confidence=confidence,
            model_version=self.version,
            features=self._features(inputs),
            adjustments={k: round(v, 6) for k, v in adjustments.items()},
            rationale={
                "approach": "baseline log-odds adjustment against the market prior",
                "market_probability": round(market_p, 6),
                "pre_shrinkage_probability": round(raw_probability, 6),
                "shrinkage_weight": round(uncertainty, 4),
                "key_assumptions": [
                    "the market midpoint is a strong prior and is departed from only for a named reason",
                    "neg-risk sibling YES prices should sum to 1",
                    "resting depth carries weak short-horizon information only",
                ],
                "risk_factors": self._risk_factors(inputs),
            },
            resolution_risk=self._resolution_risk(inputs),
        )

    # ------------------------------------------------------------------
    # Adjustments
    # ------------------------------------------------------------------
    def _negrisk_adjustment(self, inputs: ProbabilityInputs, market_p: float) -> float:
        """Exploit incoherence across mutually-exclusive sibling outcomes.

        If sibling YES midpoints sum to S, coherence requires S == 1. When
        S > 1 every leg is on average too expensive, so each leg's probability
        should be revised down, and vice versa. The correction is applied in
        proportion to the leg's own share of the group, so a 0.02 leg is not
        moved as much as a 0.60 leg by the same group-level error.
        """
        group_sum = inputs.negrisk_group_sum
        group_size = inputs.negrisk_group_size
        if group_sum is None or group_size is None or group_size < 2 or group_sum <= 0:
            return 0.0

        # Ignore trivial incoherence: within a tick or two it is spread noise,
        # not information.
        error = group_sum - 1.0
        if abs(error) < 0.01:
            return 0.0

        # Treat an enormous deviation as a data problem rather than a windfall.
        # A genuine 30% arbitrage across a liquid neg-risk group would not sit
        # there waiting for us; far more likely we are missing legs, or pricing
        # a group mid-reshuffle. Fail closed and defer to the market.
        if abs(error) > self.MAX_TRUSTED_NEGRISK_ERROR:
            return 0.0

        # Coherent probability if the whole group were rescaled to sum to 1.
        coherent_p = market_p / group_sum
        if not 0.0 < coherent_p < 1.0:
            return 0.0

        implied = _logit(coherent_p) - _logit(market_p)
        return max(-self.NEGRISK_MAX_ADJUSTMENT, min(self.NEGRISK_MAX_ADJUSTMENT, implied))

    def _imbalance_adjustment(self, inputs: ProbabilityInputs) -> float:
        """Weak short-horizon drift term from resting depth.

        Decayed to zero beyond roughly a week, where book composition says
        nothing about an event months away.
        """
        profile = inputs.liquidity_profile
        if profile is None or profile.imbalance is None:
            return 0.0

        hours = inputs.hours_to_resolution
        if hours is None or hours <= 0:
            return 0.0
        horizon_weight = math.exp(-hours / 168.0)  # 1 week e-folding

        # Very lopsided books are usually one large resting order rather than
        # information, so the signal is compressed rather than linear.
        compressed = math.tanh(profile.imbalance * 1.5)
        return self.IMBALANCE_MAX_ADJUSTMENT * compressed * horizon_weight

    def _longshot_adjustment(self, inputs: ProbabilityInputs, market_p: float) -> float:
        """Favourite–longshot bias correction.

        Longshots are systematically overpriced and heavy favourites slightly
        underpriced. Applied symmetrically about 0.5 so this can never become a
        one-directional bet, and only in multi-outcome groups where the effect
        is best documented.
        """
        group_size = inputs.negrisk_group_size
        if group_size is None or group_size < 4:
            return 0.0
        if 0.10 <= market_p <= 0.90:
            return 0.0

        # Distance from the nearest boundary, normalised.
        if market_p < 0.10:
            severity = (0.10 - market_p) / 0.10
            direction = -1.0  # revise longshot down
        else:
            severity = (market_p - 0.90) / 0.10
            direction = 1.0  # revise heavy favourite up

        return direction * self.LONGSHOT_MAX_ADJUSTMENT * severity

    # ------------------------------------------------------------------
    # Uncertainty and confidence
    # ------------------------------------------------------------------
    def _uncertainty(self, inputs: ProbabilityInputs) -> float:
        """In [0,1]. 1.0 means "we know nothing beyond the price".

        The baseline starts near-total-uncertainty and earns its way down only
        through observable data quality. This is the mechanism that stops a
        model with no evidence from producing a large edge.
        """
        uncertainty = 0.90

        if inputs.negrisk_group_sum is not None and (inputs.negrisk_group_size or 0) >= 2:
            # Coherence is a genuinely independent constraint, so this is the
            # single biggest reduction the baseline can earn.
            uncertainty -= 0.35

        profile = inputs.liquidity_profile
        if profile is not None and profile.spread_pct is not None:
            if profile.spread_pct < 0.02:
                uncertainty -= 0.10
            elif profile.spread_pct > 0.15:
                uncertainty += 0.05

        if inputs.snapshot_count >= 30:
            uncertainty -= 0.05

        if inputs.hours_to_resolution is not None and inputs.hours_to_resolution < 24:
            # Near expiry, the market has usually already absorbed everything
            # knowable and our latency disadvantage is at its worst.
            uncertainty += 0.10

        return max(0.05, min(1.0, uncertainty))

    def _confidence(self, inputs: ProbabilityInputs, uncertainty: float) -> float:
        """Confidence in the *estimate*, not in the outcome.

        Note this is not `1 - uncertainty`: a model can be confident that it
        has correctly identified a small coherence error while remaining very
        uncertain about the event itself.
        """
        confidence = 1.0 - uncertainty

        profile = inputs.liquidity_profile
        if profile is None or not profile.has_two_sided_market:
            return 0.0
        if inputs.executable_price is None:
            confidence *= 0.5
        if inputs.snapshot_count < 5:
            confidence *= 0.6
        if profile.total_depth_usd < self.settings.min_liquidity:
            confidence *= 0.7

        return max(0.0, min(1.0, confidence))

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def _features(self, inputs: ProbabilityInputs) -> dict[str, float | None]:
        profile = inputs.liquidity_profile
        return {
            "market_midpoint": inputs.midpoint,
            "executable_price": inputs.executable_price,
            "spread_pct": profile.spread_pct if profile else None,
            "book_imbalance": profile.imbalance if profile else None,
            "hours_to_resolution": inputs.hours_to_resolution,
            "negrisk_group_sum": inputs.negrisk_group_sum,
            "negrisk_group_size": float(inputs.negrisk_group_size) if inputs.negrisk_group_size else None,
            "snapshot_count": float(inputs.snapshot_count),
        }

    def _risk_factors(self, inputs: ProbabilityInputs) -> list[str]:
        factors: list[str] = []
        if inputs.negrisk_group_sum is None:
            factors.append(
                "no neg-risk coherence constraint available; the estimate is "
                "essentially the market price and carries no independent information"
            )
        if inputs.snapshot_count < 5:
            factors.append("thin observation history for this market")
        if inputs.hours_to_resolution is not None and inputs.hours_to_resolution < 24:
            factors.append("close to resolution; adverse selection risk is elevated")
        profile = inputs.liquidity_profile
        if profile and profile.spread_pct is not None and profile.spread_pct > 0.10:
            factors.append("wide spread relative to price")
        return factors

    def _resolution_risk(self, inputs: ProbabilityInputs) -> ResolutionRisk:
        if inputs.hours_to_resolution is None:
            return ResolutionRisk.UNKNOWN
        if inputs.category in (MarketCategory.OTHER, MarketCategory.ENTERTAINMENT):
            return ResolutionRisk.HIGH
        if inputs.category in (MarketCategory.GEOPOLITICS, MarketCategory.POLITICS):
            return ResolutionRisk.MEDIUM
        return ResolutionRisk.LOW


class ProbabilityEngine:
    """Front door for probability estimation.

    Today it delegates to the baseline. When enough resolved markets exist, the
    trained ensemble registers here alongside it and the engine combines them —
    but only if walk-forward validation shows the combination actually improves
    Brier score, which is checked in `engines/training.py`, not assumed.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.baseline = BaselineProbabilityModel(self.settings)

    def predict(self, inputs: ProbabilityInputs) -> ProbabilityResult:
        started = datetime.now(UTC)
        result = self.baseline.predict(inputs)
        result.rationale["model_latency_ms"] = int(
            (datetime.now(UTC) - started).total_seconds() * 1000
        )
        return result

    @property
    def active_versions(self) -> list[str]:
        return [self.baseline.version]
