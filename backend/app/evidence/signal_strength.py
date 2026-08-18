"""Graduated signal strength.

The spec's ladder: WATCH for a low edge, CANDIDATE for a medium edge with high
confidence, SIGNAL only when the edge, confidence, liquidity *and* corroborating
evidence all clear their thresholds.

Every threshold is configurable, and the defaults are starting points chosen in
advance rather than fitted to anything — nothing here has been calibrated
against outcomes, because no market this system predicted has resolved yet.

The rule the spec states five separate ways, implemented once here: if the
evidence is thin, the model uncertain, the market illiquid, the source
unreliable or the resolution ambiguous, the answer is WATCH. A system whose
correct output is usually "no trade" is working.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.enums import Recommendation, ResolutionRisk, SignalStrength


@dataclass
class SignalAssessment:
    strength: SignalStrength
    reasons: list[str] = field(default_factory=list)
    gates_passed: list[str] = field(default_factory=list)
    gates_failed: list[str] = field(default_factory=list)
    has_independent_estimate: bool = False
    evidence_source_count: int = 0

    def as_dict(self) -> dict:
        return {
            "strength": self.strength.value,
            "reasons": self.reasons,
            "gates_passed": self.gates_passed,
            "gates_failed": self.gates_failed,
            "has_independent_estimate": self.has_independent_estimate,
            "evidence_source_count": self.evidence_source_count,
        }


def assess_signal_strength(
    *,
    edge_result,
    category_estimate,
    feature_vector,
    settings: Settings | None = None,
) -> SignalAssessment:
    """Grade an opportunity WATCH / CANDIDATE / SIGNAL, with every gate named."""
    settings = settings or get_settings()

    has_independent = bool(
        category_estimate is not None and getattr(category_estimate, "is_usable", False)
    )
    source_count = int(feature_vector.features.get("evidence_source_count", 0))

    assessment = SignalAssessment(
        strength=SignalStrength.NONE,
        has_independent_estimate=has_independent,
        evidence_source_count=source_count,
    )

    # Nothing that is not a trade recommendation can be a signal.
    if edge_result.recommendation not in (Recommendation.BUY, Recommendation.SELL):
        assessment.strength = SignalStrength.WATCH
        assessment.reasons.append(
            f"recommendation is {edge_result.recommendation.value}, not a trade"
        )
        return assessment

    edge = edge_result.executable_edge or 0.0
    confidence = edge_result.confidence
    liquidity = edge_result.liquidity or 0.0

    gates = {
        "edge_meets_signal_threshold": edge >= settings.signal_edge_threshold,
        "confidence_meets_signal_threshold": confidence >= settings.signal_confidence_threshold,
        "liquidity_sufficient": liquidity >= settings.signal_min_liquidity,
        # The gate that matters most: a signal must rest on outside information,
        # not on a rearrangement of the price it claims to beat.
        "has_independent_estimate": has_independent,
        "corroborated_by_multiple_sources": source_count >= settings.signal_min_evidence_sources,
        "resolution_risk_acceptable": edge_result.resolution_risk
        in (ResolutionRisk.LOW, ResolutionRisk.MEDIUM),
    }

    assessment.gates_passed = [name for name, passed in gates.items() if passed]
    assessment.gates_failed = [name for name, passed in gates.items() if not passed]

    if all(gates.values()):
        assessment.strength = SignalStrength.SIGNAL
        assessment.reasons.append(
            f"executable edge {edge:+.4f} with confidence {confidence:.2f}, "
            f"{source_count} corroborating sources, liquidity ${liquidity:,.0f}"
        )
        return assessment

    candidate_gates = {
        "edge_meets_candidate_threshold": edge >= settings.candidate_edge_threshold,
        "confidence_meets_candidate_threshold": confidence
        >= settings.candidate_confidence_threshold,
    }
    if all(candidate_gates.values()):
        assessment.strength = SignalStrength.CANDIDATE
        assessment.reasons.append(
            f"executable edge {edge:+.4f} clears the candidate threshold but "
            f"{len(assessment.gates_failed)} signal gate(s) failed: "
            f"{', '.join(assessment.gates_failed)}"
        )
        return assessment

    assessment.strength = SignalStrength.WATCH
    assessment.reasons.append(
        f"executable edge {edge:+.4f} and confidence {confidence:.2f} are below the "
        f"candidate thresholds ({settings.candidate_edge_threshold}, "
        f"{settings.candidate_confidence_threshold})"
    )
    return assessment
