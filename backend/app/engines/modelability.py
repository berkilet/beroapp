"""Market modelability filter.

Most markets should not be traded, and the reason is usually not that the model
disagrees — it is that the market cannot be modelled, cannot be entered, or
cannot be trusted to resolve cleanly. Deciding that *before* spending model
effort is both cheaper and safer.

The score is a transparent weighted sum of named components, each in [0,1], and
the full component breakdown is stored so the dashboard can explain exactly why
a market was excluded. There is no opaque number here.

Status is not derived from the score alone. Several conditions are
disqualifying regardless of how good everything else looks — a market with no
two-sided book cannot be entered at any score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.core.enums import MarketCategory, ModelabilityStatus
from app.engines.liquidity import LiquidityProfile

# Categories for which this platform has, or can plausibly obtain, genuine
# external evidence. A market outside this set is not *unmodelable* in
# principle, but we would be modelling it on price alone, which is not an
# independent estimate — so it goes to WATCHLIST, never TRADEABLE.
_EVIDENCE_SUPPORTED = {
    MarketCategory.FEDERAL_RESERVE,
    MarketCategory.MACROECONOMICS,
    MarketCategory.ELECTIONS,
    MarketCategory.POLITICS,
    MarketCategory.CRYPTO,
    MarketCategory.BUSINESS,
}

# Sports resolve on a scoreline within hours and are dominated by specialist
# models with data we do not have. Excluded deliberately, not by oversight.
_EXCLUDED_CATEGORIES = {MarketCategory.SPORTS}

WEIGHTS = {
    "liquidity": 0.25,
    "spread": 0.20,
    "resolution_quality": 0.20,
    "evidence_availability": 0.15,
    "time_horizon": 0.10,
    "maturity": 0.10,
}


@dataclass
class ModelabilityAssessment:
    status: ModelabilityStatus
    score: float
    components: dict[str, float] = field(default_factory=dict)
    disqualifiers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_detail(self) -> dict:
        return {
            "score": round(self.score, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "weights": WEIGHTS,
            "disqualifiers": self.disqualifiers,
            "notes": self.notes,
        }


@dataclass
class MarketFacts:
    """Everything the filter needs, gathered by the caller.

    Optional fields are genuinely optional: `None` means unknown and is scored
    as unknown, not as zero.
    """

    category: MarketCategory
    liquidity_num: float | None
    volume_num: float | None
    end_date: datetime | None
    first_seen_at: datetime | None
    source_created_at: datetime | None
    accepting_orders: bool | None
    enable_order_book: bool | None
    closed: bool | None
    archived: bool | None
    active: bool | None
    resolution_source: str | None
    description: str | None
    is_binary: bool
    liquidity_profile: LiquidityProfile | None
    snapshot_count: int = 0


def _score_liquidity(liquidity: float | None, min_liquidity: float) -> tuple[float, str | None]:
    if liquidity is None:
        return 0.0, "liquidity unknown"
    if liquidity <= 0:
        return 0.0, "zero liquidity"
    # Saturating at 10x the floor: beyond that, more depth stops mattering for
    # the order sizes this platform contemplates.
    ceiling = min_liquidity * 10.0
    return min(1.0, liquidity / ceiling), None


def _score_spread(profile: LiquidityProfile | None, max_spread: float) -> tuple[float, str | None]:
    if profile is None or profile.spread is None:
        return 0.0, "spread unknown"
    if profile.spread <= 0:
        # A zero or negative spread is not a wonderfully tight market; it is a
        # malformed or crossed book.
        return 0.0, "non-positive spread"
    if profile.spread >= max_spread:
        return 0.0, f"spread {profile.spread:.4f} at or beyond limit {max_spread}"
    return 1.0 - (profile.spread / max_spread), None


def _score_resolution_quality(
    resolution_source: str | None, description: str | None
) -> tuple[float, str | None]:
    """How confidently can we tell what resolves this market?

    Judged on the presence and specificity of the venue's own resolution text.
    Deliberately crude — a genuinely careful reading of resolution criteria is
    exactly the kind of judgement this system should not pretend to automate.
    """
    text = " ".join(filter(None, [resolution_source, description])).strip()
    if not text:
        return 0.0, "no resolution text"

    score = 0.35
    lowered = text.lower()
    if resolution_source:
        score += 0.25
    if any(marker in lowered for marker in ("official", "announce", "report", "publish", "certif")):
        score += 0.20
    if len(text) > 300:
        score += 0.10
    # Words that signal genuine ambiguity in the resolution criteria.
    if any(marker in lowered for marker in ("at the discretion", "consensus of", "credible report", "may resolve")):
        score -= 0.30
    return max(0.0, min(1.0, score)), None


def _score_evidence(category: MarketCategory) -> tuple[float, str | None]:
    if category in _EXCLUDED_CATEGORIES:
        return 0.0, f"category {category.value} excluded by policy"
    if category in _EVIDENCE_SUPPORTED:
        return 1.0, None
    if category is MarketCategory.OTHER:
        return 0.1, "market is unclassified"
    return 0.4, f"no evidence connector for {category.value}"


def _score_time_horizon(
    end_date: datetime | None, now: datetime, settings: Settings
) -> tuple[float, str | None]:
    if end_date is None:
        return 0.0, "no end date"
    hours = (end_date - now).total_seconds() / 3600.0
    if hours <= 0:
        return 0.0, "end date in the past"
    if hours < settings.min_hours_to_resolution:
        return 0.0, f"resolves in {hours:.1f}h, under the {settings.min_hours_to_resolution}h floor"
    days = hours / 24.0
    if days > settings.max_days_to_resolution:
        return 0.0, f"resolves in {days:.0f}d, beyond the {settings.max_days_to_resolution}d horizon"
    # Peak usefulness around a week to a couple of months out: long enough that
    # evidence can move the price, short enough that capital is not dead.
    if days <= 7:
        return 0.55 + 0.45 * (days / 7.0), None
    if days <= 60:
        return 1.0, None
    return max(0.2, 1.0 - (days - 60) / settings.max_days_to_resolution), None


def _score_maturity(
    created_at: datetime | None, first_seen_at: datetime | None, snapshot_count: int, now: datetime, settings: Settings
) -> tuple[float, str | None]:
    """A market we have barely observed is one we cannot yet reason about."""
    reference = created_at or first_seen_at
    if reference is None:
        return 0.0, "market age unknown"
    age_hours = (now - reference).total_seconds() / 3600.0
    if age_hours < settings.min_market_age_hours:
        return 0.0, f"market only {age_hours:.1f}h old"
    age_component = min(1.0, age_hours / (settings.min_market_age_hours * 7))
    history_component = min(1.0, snapshot_count / 30.0)
    return 0.5 * age_component + 0.5 * history_component, None


def assess(facts: MarketFacts, *, now: datetime | None = None, settings: Settings | None = None) -> ModelabilityAssessment:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    components: dict[str, float] = {}
    notes: list[str] = []
    disqualifiers: list[str] = []

    # ---- hard disqualifiers, checked before scoring -------------------
    if facts.closed:
        disqualifiers.append("market is closed")
    if facts.archived:
        disqualifiers.append("market is archived")
    if facts.active is False:
        disqualifiers.append("market is inactive")
    if facts.accepting_orders is False:
        disqualifiers.append("market is not accepting orders")
    if facts.enable_order_book is False:
        disqualifiers.append("market has no CLOB order book")
    if not facts.is_binary:
        disqualifiers.append("market is not binary; this platform models binary markets only")
    if facts.liquidity_profile is not None and not facts.liquidity_profile.has_two_sided_market:
        disqualifiers.append("book is one-sided; no executable entry exists")

    # ---- component scores --------------------------------------------
    for name, (value, note) in {
        "liquidity": _score_liquidity(facts.liquidity_num, settings.min_liquidity),
        "spread": _score_spread(facts.liquidity_profile, settings.max_spread),
        "resolution_quality": _score_resolution_quality(facts.resolution_source, facts.description),
        "evidence_availability": _score_evidence(facts.category),
        "time_horizon": _score_time_horizon(facts.end_date, now, settings),
        "maturity": _score_maturity(
            facts.source_created_at, facts.first_seen_at, facts.snapshot_count, now, settings
        ),
    }.items():
        components[name] = value
        if note:
            notes.append(f"{name}: {note}")

    score = sum(components[name] * weight for name, weight in WEIGHTS.items())

    # ---- status ------------------------------------------------------
    status = _decide_status(facts, components, score, disqualifiers, settings)

    return ModelabilityAssessment(
        status=status, score=score, components=components, disqualifiers=disqualifiers, notes=notes
    )


def _decide_status(
    facts: MarketFacts,
    components: dict[str, float],
    score: float,
    disqualifiers: list[str],
    settings: Settings,
) -> ModelabilityStatus:
    if disqualifiers:
        return ModelabilityStatus.UNMODELABLE

    # Resolution ambiguity outranks everything else. A market we might win on
    # the facts and still lose on the wording is not an opportunity.
    if components["resolution_quality"] < 0.35:
        return ModelabilityStatus.RESOLUTION_RISK

    if facts.category in _EXCLUDED_CATEGORIES:
        return ModelabilityStatus.UNMODELABLE

    # No genuine external evidence means any "independent" probability would be
    # a repackaging of the price. That is WATCHLIST at best.
    if components["evidence_availability"] < 0.5:
        return ModelabilityStatus.WATCHLIST

    if components["maturity"] <= 0.0 or facts.snapshot_count < 3:
        return ModelabilityStatus.INSUFFICIENT_DATA

    if components["liquidity"] <= 0.0 or components["spread"] <= 0.0:
        return ModelabilityStatus.WATCHLIST

    if components["time_horizon"] <= 0.0:
        return ModelabilityStatus.UNMODELABLE

    if score >= 0.55:
        return ModelabilityStatus.TRADEABLE
    if score >= 0.30:
        return ModelabilityStatus.WATCHLIST
    return ModelabilityStatus.INSUFFICIENT_DATA
