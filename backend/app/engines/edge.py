"""Edge engine.

Four numbers, computed in order, each strictly less optimistic than the last:

* **raw_edge** — `model_probability - market_midpoint`. The number a naive
  system would report and act on. Kept only so we can measure how misleading it
  is.
* **executable_edge** — recomputed against the price we would *actually* pay
  after crossing the spread and walking the book. This is the first number that
  corresponds to anything real.
* **liquidity_adjusted_edge** — executable edge scaled by the probability the
  order fills as modelled. A large edge on an order that fills 30% of the time
  is a small edge.
* **risk_adjusted_edge** — further scaled by model confidence and resolution
  risk. This is the only one used for ranking or sizing.

The spec's worked example is the design brief: model 64%, market 56%, raw edge
8 points — but if the realistic execution price is 59%, the edge is 5 points,
and the opportunity must be classified on the 5, not the 8.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.enums import Recommendation, ResolutionRisk, Side
from app.engines.liquidity import (
    ExecutionEstimate,
    LiquidityProfile,
    estimate_execution,
    execution_probability,
)
from app.engines.probability import ProbabilityResult
from app.schemas.polymarket import OrderBook

_PRICE_EPSILON = 1e-9
"""Tolerance for comparing prices against configured limits, absorbing binary-
float representation error without admitting a materially worse fill."""

# Multiplier applied to risk-adjusted edge by resolution risk. A market that
# might resolve against us on a technicality is worth materially less than its
# arithmetic suggests.
_RESOLUTION_RISK_FACTOR = {
    ResolutionRisk.LOW: 1.0,
    ResolutionRisk.MEDIUM: 0.75,
    ResolutionRisk.HIGH: 0.40,
    ResolutionRisk.UNKNOWN: 0.50,
}


@dataclass
class EdgeResult:
    side: Side | None
    recommendation: Recommendation

    market_probability: float
    model_probability: float
    raw_edge: float
    executable_edge: float | None
    liquidity_adjusted_edge: float | None
    risk_adjusted_edge: float | None

    executable_price: float | None
    estimated_slippage: float | None
    execution_probability: float | None
    liquidity: float | None
    spread: float | None
    fees: float

    confidence: float
    resolution_risk: ResolutionRisk
    model_version: str

    reasons: list[str] = field(default_factory=list)
    execution_estimate: ExecutionEstimate | None = None

    rank_score: float | None = None
    rank_explanation: dict = field(default_factory=dict)


class EdgeEngine:
    MIN_FILL_RATIO = 0.50
    """Fraction of the reference order size the book must be able to absorb
    before a signal counts as executable. Below this the edge is arithmetic on
    a position we could not actually take."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def evaluate(
        self,
        *,
        prediction: ProbabilityResult,
        book: OrderBook,
        profile: LiquidityProfile,
        size_usd: float | None = None,
    ) -> EdgeResult:
        size_usd = size_usd or self.settings.reference_order_size_usd
        market_p = profile.midpoint
        model_p = prediction.model_probability
        reasons: list[str] = []

        if market_p is None:
            return self._no_trade(
                prediction, profile, Recommendation.INSUFFICIENT_DATA,
                ["no two-sided market; midpoint is undefined"],
            )

        raw_edge = model_p - market_p

        # Direction. Below the minimum threshold there is nothing to evaluate,
        # and we say WATCH rather than manufacturing a marginal signal.
        if abs(raw_edge) < self.settings.min_executable_edge:
            return self._no_trade(
                prediction, profile, Recommendation.WATCH,
                [f"raw edge {raw_edge:+.4f} is inside the {self.settings.min_executable_edge} threshold"],
                raw_edge=raw_edge,
            )

        side = Side.BUY if raw_edge > 0 else Side.SELL

        estimate = estimate_execution(book, side=side, size_usd=size_usd)
        if estimate is None or estimate.average_price is None:
            return self._no_trade(
                prediction, profile, Recommendation.NO_TRADE,
                [f"no depth on the {side.value} side; the trade cannot be entered"],
                raw_edge=raw_edge,
            )

        exec_price = estimate.average_price

        # The executable edge. For a BUY we pay exec_price for a claim we value
        # at model_p; for a SELL we receive exec_price for a claim we value at
        # model_p. Both are expressed as profit per unit of notional.
        if side is Side.BUY:
            executable_edge = model_p - exec_price
        else:
            executable_edge = exec_price - model_p

        fees = self._fees(size_usd)
        executable_edge -= fees / size_usd if size_usd > 0 else 0.0

        if executable_edge < self.settings.min_executable_edge:
            reasons.append(
                f"raw edge {raw_edge:+.4f} collapses to executable {executable_edge:+.4f} "
                f"at the real fill price {exec_price:.4f} (touch {estimate.reference_price:.4f}, "
                f"slippage {estimate.slippage:.4f})"
            )
            return self._no_trade(
                prediction, profile, Recommendation.NO_TRADE, reasons,
                raw_edge=raw_edge, executable_edge=executable_edge, side=side,
                estimate=estimate, fees=fees,
            )

        # Spec §18: an opportunity that cannot realistically be entered is not
        # an executable opportunity. A one-level book shows zero slippage
        # against its own touch no matter how thin it is, so fill ratio — not
        # slippage — is what catches this case.
        if estimate.fill_ratio < self.MIN_FILL_RATIO:
            reasons.append(
                f"book fills only {estimate.fill_ratio:.0%} of the ${size_usd:,.0f} reference "
                f"size (${estimate.fillable_size_usd:,.0f} available); this is not an "
                "executable opportunity regardless of the arithmetic edge"
            )
            return self._no_trade(
                prediction, profile, Recommendation.NO_TRADE, reasons,
                raw_edge=raw_edge, executable_edge=executable_edge, side=side,
                estimate=estimate, fees=fees,
            )

        if estimate.slippage > self.settings.max_allowed_slippage + _PRICE_EPSILON:
            reasons.append(
                f"estimated slippage {estimate.slippage:.4f} exceeds the "
                f"{self.settings.max_allowed_slippage} limit"
            )
            return self._no_trade(
                prediction, profile, Recommendation.NO_TRADE, reasons,
                raw_edge=raw_edge, executable_edge=executable_edge, side=side,
                estimate=estimate, fees=fees,
            )

        exec_prob = execution_probability(estimate)
        liquidity_adjusted = executable_edge * exec_prob

        risk_factor = _RESOLUTION_RISK_FACTOR.get(prediction.resolution_risk, 0.5)
        risk_adjusted = liquidity_adjusted * prediction.confidence * risk_factor

        if estimate.is_partial:
            reasons.append(
                f"book fills only {estimate.fill_ratio:.0%} of the reference size"
            )

        recommendation = self._recommend(
            side=side,
            executable_edge=executable_edge,
            risk_adjusted_edge=risk_adjusted,
            confidence=prediction.confidence,
            reasons=reasons,
        )

        result = EdgeResult(
            side=side if recommendation in (Recommendation.BUY, Recommendation.SELL) else None,
            recommendation=recommendation,
            market_probability=market_p,
            model_probability=model_p,
            raw_edge=raw_edge,
            executable_edge=executable_edge,
            liquidity_adjusted_edge=liquidity_adjusted,
            risk_adjusted_edge=risk_adjusted,
            executable_price=exec_price,
            estimated_slippage=estimate.slippage,
            execution_probability=exec_prob,
            liquidity=profile.total_depth_usd,
            spread=profile.spread,
            fees=fees,
            confidence=prediction.confidence,
            resolution_risk=prediction.resolution_risk,
            model_version=prediction.model_version,
            reasons=reasons,
            execution_estimate=estimate,
        )
        self.rank(result, profile)
        return result

    # ------------------------------------------------------------------
    def _fees(self, size_usd: float) -> float:
        return size_usd * (self.settings.paper_fee_bps / 10_000.0)

    def _recommend(
        self,
        *,
        side: Side,
        executable_edge: float,
        risk_adjusted_edge: float,
        confidence: float,
        reasons: list[str],
    ) -> Recommendation:
        if confidence < self.settings.min_confidence:
            reasons.append(
                f"confidence {confidence:.3f} is below the {self.settings.min_confidence} floor"
            )
            return Recommendation.WATCH
        if risk_adjusted_edge < self.settings.min_executable_edge / 2:
            reasons.append(
                f"risk-adjusted edge {risk_adjusted_edge:+.4f} does not justify the position"
            )
            return Recommendation.HOLD
        reasons.append(
            f"executable edge {executable_edge:+.4f}, risk-adjusted {risk_adjusted_edge:+.4f}, "
            f"confidence {confidence:.3f}"
        )
        return Recommendation.BUY if side is Side.BUY else Recommendation.SELL

    def _no_trade(
        self,
        prediction: ProbabilityResult,
        profile: LiquidityProfile,
        recommendation: Recommendation,
        reasons: list[str],
        *,
        raw_edge: float = 0.0,
        executable_edge: float | None = None,
        side: Side | None = None,
        estimate: ExecutionEstimate | None = None,
        fees: float = 0.0,
    ) -> EdgeResult:
        return EdgeResult(
            side=None,
            recommendation=recommendation,
            market_probability=profile.midpoint if profile.midpoint is not None else 0.0,
            model_probability=prediction.model_probability,
            raw_edge=raw_edge,
            executable_edge=executable_edge,
            liquidity_adjusted_edge=None,
            risk_adjusted_edge=None,
            executable_price=estimate.average_price if estimate else None,
            estimated_slippage=estimate.slippage if estimate else None,
            execution_probability=execution_probability(estimate) if estimate else None,
            liquidity=profile.total_depth_usd,
            spread=profile.spread,
            fees=fees,
            confidence=prediction.confidence,
            resolution_risk=prediction.resolution_risk,
            model_version=prediction.model_version,
            reasons=reasons,
            execution_estimate=estimate,
        )

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------
    def rank(self, result: EdgeResult, profile: LiquidityProfile) -> None:
        """Transparent weighted score, with every contribution recorded.

        Stored on the signal so the dashboard can show *why* an opportunity
        ranked where it did. An opaque score would be worse than no score.
        """
        if result.risk_adjusted_edge is None:
            result.rank_score = None
            result.rank_explanation = {"reason": "no risk-adjusted edge to rank"}
            return

        components = {
            # Edge dominates, but is capped so one huge number cannot carry an
            # otherwise poor opportunity to the top of the table.
            "risk_adjusted_edge": min(1.0, result.risk_adjusted_edge / 0.10),
            "confidence": result.confidence,
            "execution_probability": result.execution_probability or 0.0,
            "liquidity": min(1.0, (profile.total_depth_usd or 0.0) / (self.settings.min_liquidity * 4)),
            "spread_quality": (
                max(0.0, 1.0 - (profile.spread or 1.0) / self.settings.max_spread)
            ),
            "resolution_quality": _RESOLUTION_RISK_FACTOR.get(result.resolution_risk, 0.5),
        }
        weights = {
            "risk_adjusted_edge": 0.40,
            "confidence": 0.15,
            "execution_probability": 0.15,
            "liquidity": 0.12,
            "spread_quality": 0.08,
            "resolution_quality": 0.10,
        }

        score = sum(components[name] * weight for name, weight in weights.items())
        result.rank_score = score
        result.rank_explanation = {
            "score": round(score, 4),
            "components": {k: round(v, 4) for k, v in components.items()},
            "weights": weights,
            "contributions": {
                k: round(components[k] * weights[k], 4) for k in components
            },
        }
