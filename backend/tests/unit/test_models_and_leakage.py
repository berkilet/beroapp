"""Category models, feature integrity, blending, signal gating and leakage.

The leakage tests are the ones that matter most. Look-ahead bias is invisible
when it happens and catastrophic when it does, so these assert it empirically
rather than trusting the design note that says it cannot occur.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.enums import (
    MarketCategory,
    MarketSubcategory,
    Recommendation,
    ResolutionRisk,
    SignalStrength,
)
from app.engines.category_models import (
    CategoryModelRouter,
    CryptoThresholdModel,
    MacroThresholdModel,
)
from app.engines.features import FEATURE_SET_VERSION, FeatureVector
from app.engines.probability import combine_estimates
from app.evidence.question_shape import QuestionShape, ShapeResult, detect_shape
from app.evidence.signal_strength import assess_signal_strength


@pytest.fixture
def model_settings() -> Settings:
    return Settings(allow_insecure_local=True, api_key="", min_evidence_items_for_model=2)


def vector(**features) -> FeatureVector:
    v = FeatureVector(
        market_id=1, token_id="t", category=MarketCategory.CRYPTO,
        subcategory=MarketSubcategory.CRYPTO_PRICE, known_at=datetime.now(UTC),
    )
    for name, value in features.items():
        v.set(name, value)
    return v


# ---------------------------------------------------------------------------
# Feature vector integrity
# ---------------------------------------------------------------------------
def test_missing_feature_is_named_not_defaulted() -> None:
    """Substituting zero for an unavailable CPI reading would produce a
    confident number from no information."""
    v = vector(spot_price=100.0)
    v.set("cpi_yoy", None)

    assert "cpi_yoy" in v.missing
    assert "cpi_yoy" not in v.features
    assert v.features.get("cpi_yoy", "absent") == "absent"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_feature_is_treated_as_missing(bad: float) -> None:
    v = vector()
    v.set("spot_price", bad)
    assert "spot_price" in v.missing
    assert "spot_price" not in v.features


def test_every_present_feature_carries_a_timestamp() -> None:
    v = vector(spot_price=100.0, realised_volatility=0.4)
    assert set(v.timestamps) == set(v.features)


def test_vector_age_is_that_of_its_stalest_input() -> None:
    """A vector is only as fresh as its oldest part."""
    now = datetime.now(UTC)
    v = FeatureVector(
        market_id=1, token_id="t", category=MarketCategory.MACROECONOMICS,
        subcategory=None, known_at=now,
    )
    v.set("spread", 0.01, known_at=now)
    v.set("cpi_yoy", 3.0, known_at=now - timedelta(days=40))

    age = v.oldest_feature_age_s(now)
    assert age == pytest.approx(40 * 86_400, rel=0.01)


def test_evidence_feature_count_excludes_market_features() -> None:
    """The honest measure of independence: zero means we are looking only at
    the price we claim to beat."""
    v = vector(market_midpoint=0.5, spread=0.01, liquidity_num=1000.0)
    assert v.evidence_feature_count() == 0

    v.set("cpi_yoy", 3.0)
    assert v.evidence_feature_count() == 1


# ---------------------------------------------------------------------------
# Crypto model: the four shapes
# ---------------------------------------------------------------------------
def _crypto_vector(hours: float = 24 * 7) -> FeatureVector:
    return vector(
        spot_price=64_000.0,
        realised_volatility=0.35,
        hours_to_resolution=hours,
        evidence_source_count=2.0,
        distance_to_threshold=-2_000.0,
        normalised_distance=-0.031,
        threshold_z_score=-0.6,
    )


def test_barrier_probability_exceeds_terminal_for_the_same_level(model_settings) -> None:
    """The core property that makes shape detection necessary: a path can touch
    a level and fall back, so the barrier probability is strictly larger."""
    model = CryptoThresholdModel(model_settings)
    v = _crypto_vector()

    terminal = model.estimate(v, shape=ShapeResult(QuestionShape.TERMINAL, lower=66_000.0))
    barrier = model.estimate(v, shape=ShapeResult(QuestionShape.BARRIER_ABOVE, lower=66_000.0))

    assert terminal.is_usable and barrier.is_usable
    assert barrier.probability > terminal.probability
    # Near the money the barrier probability is roughly double the terminal one.
    assert barrier.probability == pytest.approx(2 * terminal.probability, rel=0.15)


def test_range_probability_is_bounded_by_its_parts(model_settings) -> None:
    model = CryptoThresholdModel(model_settings)
    v = _crypto_vector()

    band = model.estimate(
        v, shape=ShapeResult(QuestionShape.RANGE, lower=62_000.0, upper=66_000.0)
    )
    narrow = model.estimate(
        v, shape=ShapeResult(QuestionShape.RANGE, lower=63_500.0, upper=64_500.0)
    )

    assert 0.0 < narrow.probability < band.probability < 1.0


def test_terminal_below_is_the_complement_of_terminal_above(model_settings) -> None:
    model = CryptoThresholdModel(model_settings)
    v = _crypto_vector()

    above = model.estimate(v, shape=ShapeResult(QuestionShape.TERMINAL, lower=66_000.0))
    below = model.estimate(v, shape=ShapeResult(QuestionShape.TERMINAL, upper=66_000.0))

    assert above.probability + below.probability == pytest.approx(1.0, abs=0.01)


def test_model_refuses_an_undeterminable_shape(model_settings) -> None:
    model = CryptoThresholdModel(model_settings)
    result = model.estimate(
        _crypto_vector(), shape=detect_shape("Bitcoin Up or Down - August 5")
    )
    assert result.is_usable is False
    assert "not modelable" in result.reason


def test_model_refuses_an_already_breached_barrier(model_settings) -> None:
    """Spot already past the level makes the outcome an observation, not a
    forecast. Producing a probability would be fabricating one."""
    model = CryptoThresholdModel(model_settings)
    result = model.estimate(
        _crypto_vector(), shape=ShapeResult(QuestionShape.BARRIER_ABOVE, lower=60_000.0)
    )
    assert result.is_usable is False
    assert "already" in result.reason


@pytest.mark.parametrize("hours", [0.5, 200 * 24])
def test_model_refuses_horizons_outside_its_validity(model_settings, hours: float) -> None:
    model = CryptoThresholdModel(model_settings)
    result = model.estimate(
        _crypto_vector(hours), shape=ShapeResult(QuestionShape.TERMINAL, lower=66_000.0)
    )
    assert result.is_usable is False


def test_model_refuses_without_volatility(model_settings) -> None:
    v = vector(spot_price=64_000.0, hours_to_resolution=24 * 7)
    result = CryptoThresholdModel(model_settings).estimate(
        v, shape=ShapeResult(QuestionShape.TERMINAL, lower=66_000.0)
    )
    assert result.is_usable is False
    assert "realised_volatility" in result.missing_features


def test_barrier_estimates_carry_extra_uncertainty(model_settings) -> None:
    """Barrier probabilities depend on the whole path, so they are more
    sensitive to the volatility estimate than terminal ones."""
    model = CryptoThresholdModel(model_settings)
    v = _crypto_vector()

    terminal = model.estimate(v, shape=ShapeResult(QuestionShape.TERMINAL, lower=66_000.0))
    barrier = model.estimate(v, shape=ShapeResult(QuestionShape.BARRIER_ABOVE, lower=66_000.0))
    assert barrier.uncertainty > terminal.uncertainty


def test_higher_volatility_widens_the_distribution(model_settings) -> None:
    model = CryptoThresholdModel(model_settings)
    shape = ShapeResult(QuestionShape.TERMINAL, lower=70_000.0)

    calm = model.estimate(_crypto_vector() , shape=shape)
    wild_vector = _crypto_vector()
    wild_vector.set("realised_volatility", 1.2)
    wild = model.estimate(wild_vector, shape=shape)

    # A far-out-of-the-money level becomes more reachable as volatility rises.
    assert wild.probability > calm.probability


def test_every_estimate_records_its_assumptions(model_settings) -> None:
    result = CryptoThresholdModel(model_settings).estimate(
        _crypto_vector(), shape=ShapeResult(QuestionShape.TERMINAL, lower=66_000.0)
    )
    assert result.assumptions
    assert any("driftless" in a for a in result.assumptions)


# ---------------------------------------------------------------------------
# Macro model
# ---------------------------------------------------------------------------
def test_macro_model_needs_its_series(model_settings) -> None:
    result = MacroThresholdModel(model_settings).estimate(
        vector(hours_to_resolution=24 * 30),
        subcategory=MarketSubcategory.INFLATION, threshold=3.0, direction=None,
    )
    assert result.is_usable is False
    assert "cpi_yoy" in result.missing_features


def test_macro_model_needs_a_threshold(model_settings) -> None:
    result = MacroThresholdModel(model_settings).estimate(
        vector(cpi_yoy=3.2, hours_to_resolution=24 * 30),
        subcategory=MarketSubcategory.INFLATION, threshold=None, direction=None,
    )
    assert result.is_usable is False
    assert "threshold" in result.reason


def test_macro_probability_moves_with_distance_to_threshold(model_settings) -> None:
    model = MacroThresholdModel(model_settings)
    v = vector(cpi_yoy=3.0, hours_to_resolution=24 * 30)

    near = model.estimate(v, subcategory=MarketSubcategory.INFLATION, threshold=3.1, direction=None)
    far = model.estimate(v, subcategory=MarketSubcategory.INFLATION, threshold=4.5, direction=None)

    assert near.probability > far.probability
    assert far.probability < 0.05


# ---------------------------------------------------------------------------
# Router gating
# ---------------------------------------------------------------------------
def test_router_refuses_without_enough_evidence_features(model_settings) -> None:
    """A model must rest on outside information, not a rearrangement of price."""
    v = vector(market_midpoint=0.5, spread=0.01)  # market features only
    result = CategoryModelRouter(model_settings).estimate(
        v, subcategory=MarketSubcategory.CRYPTO_PRICE, threshold=60_000.0,
        direction=None, shape=ShapeResult(QuestionShape.TERMINAL, lower=60_000.0),
    )
    assert result.is_usable is False
    assert "evidence-derived features" in result.reason


def test_router_has_no_model_for_unsupported_subcategories(model_settings) -> None:
    v = vector(ust_3m=3.9, ust_10y=4.7, days_to_next_fomc=20.0)
    result = CategoryModelRouter(model_settings).estimate(
        v, subcategory=MarketSubcategory.FED_RATES, threshold=None, direction=None
    )
    assert result.is_usable is False
    assert "no category model is implemented" in result.reason


def test_router_declines_an_unclassified_market(model_settings) -> None:
    result = CategoryModelRouter(model_settings).estimate(
        vector(), subcategory=None, threshold=None, direction=None
    )
    assert result.is_usable is False


# ---------------------------------------------------------------------------
# Blending
# ---------------------------------------------------------------------------
def test_confident_category_estimate_dominates_an_unsure_baseline() -> None:
    blended, weights = combine_estimates(
        market_probability=0.50, baseline_probability=0.50, baseline_uncertainty=0.9,
        category_probability=0.70, category_uncertainty=0.3,
    )
    assert 0.60 < blended < 0.70
    assert weights["category_weight"] > weights["baseline_weight"]


def test_unsure_category_estimate_barely_moves_the_result() -> None:
    blended, weights = combine_estimates(
        market_probability=0.50, baseline_probability=0.50, baseline_uncertainty=0.3,
        category_probability=0.90, category_uncertainty=0.95,
    )
    assert blended == pytest.approx(0.50, abs=0.06)
    assert weights["baseline_weight"] > weights["category_weight"]


def test_implausible_departure_from_the_market_is_capped() -> None:
    """A model claiming a 40-point edge is likelier to have a parsing bug than
    an insight, so the departure is bounded."""
    blended, weights = combine_estimates(
        market_probability=0.50, baseline_probability=0.50, baseline_uncertainty=0.9,
        category_probability=0.999, category_uncertainty=0.05,
    )
    assert weights["departure_capped"] is True
    assert blended < 0.80


def test_blend_stays_in_the_unit_interval_at_extremes() -> None:
    for market in (0.001, 0.05, 0.5, 0.95, 0.999):
        for category in (0.001, 0.5, 0.999):
            blended, _ = combine_estimates(
                market_probability=market, baseline_probability=market,
                baseline_uncertainty=0.5, category_probability=category,
                category_uncertainty=0.3,
            )
            assert 0.0 <= blended <= 1.0
            assert math.isfinite(blended)


# ---------------------------------------------------------------------------
# Signal strength gating
# ---------------------------------------------------------------------------
class _Edge:
    def __init__(self, **kw):
        self.recommendation = kw.get("recommendation", Recommendation.BUY)
        self.executable_edge = kw.get("executable_edge", 0.08)
        self.confidence = kw.get("confidence", 0.80)
        self.liquidity = kw.get("liquidity", 50_000.0)
        self.resolution_risk = kw.get("resolution_risk", ResolutionRisk.LOW)


class _Estimate:
    def __init__(self, usable=True):
        self.is_usable = usable


def _assess(settings, *, edge=None, estimate=None, sources=2.0):
    return assess_signal_strength(
        edge_result=edge or _Edge(),
        category_estimate=estimate if estimate is not None else _Estimate(),
        feature_vector=vector(evidence_source_count=sources),
        settings=settings,
    )


def test_full_signal_requires_every_gate(model_settings) -> None:
    result = _assess(model_settings)
    assert result.strength is SignalStrength.SIGNAL
    assert not result.gates_failed


def test_no_independent_estimate_blocks_a_signal(model_settings) -> None:
    """The gate that matters: a signal must rest on outside information."""
    result = _assess(model_settings, estimate=_Estimate(usable=False))
    assert result.strength is SignalStrength.CANDIDATE
    assert "has_independent_estimate" in result.gates_failed


def test_single_source_blocks_a_signal(model_settings) -> None:
    result = _assess(model_settings, sources=1.0)
    assert result.strength is SignalStrength.CANDIDATE
    assert "corroborated_by_multiple_sources" in result.gates_failed


def test_thin_liquidity_blocks_a_signal(model_settings) -> None:
    result = _assess(model_settings, edge=_Edge(liquidity=500.0))
    assert result.strength is SignalStrength.CANDIDATE
    assert "liquidity_sufficient" in result.gates_failed


def test_high_resolution_risk_blocks_a_signal(model_settings) -> None:
    result = _assess(model_settings, edge=_Edge(resolution_risk=ResolutionRisk.HIGH))
    assert result.strength is SignalStrength.CANDIDATE


def test_small_edge_is_only_a_watch(model_settings) -> None:
    result = _assess(model_settings, edge=_Edge(executable_edge=0.005, confidence=0.4))
    assert result.strength is SignalStrength.WATCH


def test_non_trade_recommendation_is_a_watch(model_settings) -> None:
    result = _assess(model_settings, edge=_Edge(recommendation=Recommendation.NO_TRADE))
    assert result.strength is SignalStrength.WATCH


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------
def test_observation_publication_and_knowledge_times_are_distinct() -> None:
    """Conflating any two of the three is how a replay 'knows' July's CPI in
    July, or knows a figure before it was published."""
    from app.evidence.base import EvidenceItem
    from app.core.enums import EvidenceType, SourceType

    item = EvidenceItem(
        source_key="bls", source_type=SourceType.OFFICIAL_GOVERNMENT, source_tier=1,
        evidence_type=EvidenceType.TIME_SERIES_OBSERVATION, series_key="CPI_URBAN_ALL",
        title="CPI July", known_at=datetime(2026, 8, 12, tzinfo=UTC),
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
        observation_date=datetime(2026, 7, 1, tzinfo=UTC),
        parser_version="v1", numeric_value=333.9,
    )
    assert item.observation_date < item.published_at <= item.known_at


def test_feature_set_version_is_recorded_on_every_vector() -> None:
    """Without it, a stored vector cannot be interpreted after the schema moves."""
    assert vector(spot_price=1.0).version == FEATURE_SET_VERSION


def test_walk_forward_refuses_to_split_too_little_data() -> None:
    from app.backtest.walkforward import build_folds

    result = build_folds([], n_folds=4)
    assert result.folds == []
    assert "too few" in result.note


def test_walk_forward_purges_outcomes_known_during_validation() -> None:
    """Training on a market whose answer arrives during the validation window
    is training on the answer to the test."""
    from app.backtest.walkforward import Observation, build_folds

    start = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        Observation(
            market_id=i,
            event_group=f"g{i}",
            predicted_at=start + timedelta(days=i),
            # Every outcome lands far in the future, so training must be purged.
            resolution_known_at=start + timedelta(days=200),
            model_probability=0.5, market_probability=0.5, outcome=i % 2,
            category="CRYPTO", model_version="v1",
        )
        for i in range(120)
    ]
    result = build_folds(observations, n_folds=3)
    assert result.folds
    assert sum(f.purged for f in result.folds) > 0


def test_walk_forward_keeps_correlated_legs_in_one_fold() -> None:
    """Neg-risk siblings sum to one; splitting them leaks the answer."""
    from app.backtest.walkforward import Observation, build_folds

    start = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        Observation(
            market_id=i, event_group="shared-group",
            predicted_at=start + timedelta(days=i),
            resolution_known_at=start + timedelta(days=i + 1),
            model_probability=0.5, market_probability=0.5, outcome=i % 2,
            category="ELECTIONS", model_version="v1",
        )
        for i in range(120)
    ]
    result = build_folds(observations, n_folds=3)

    for fold in result.folds:
        later = {o.event_group for o in fold.validation + fold.test}
        assert not ({o.event_group for o in fold.train} & later)


def test_category_sample_sizes_are_reported_separately() -> None:
    """Pooling categories to reach a threshold would make an inflation model
    partly trained on sports."""
    from app.backtest.walkforward import Observation, category_sample_sizes

    now = datetime.now(UTC)
    observations = [
        Observation(
            market_id=i, event_group=f"g{i}", predicted_at=now,
            resolution_known_at=now + timedelta(days=1),
            model_probability=0.5, market_probability=0.5, outcome=0,
            category="CRYPTO" if i < 7 else "ELECTIONS", model_version="v1",
        )
        for i in range(10)
    ]
    counts = category_sample_sizes(observations)
    assert counts == {"CRYPTO": 7, "ELECTIONS": 3}
