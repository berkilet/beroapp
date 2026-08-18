"""Engine behaviour: liquidity, probability, edge, risk, calibration.

These test the claims the system makes about itself. Where a claim is
load-bearing — "executable edge is always worse than raw edge", "a partially
observed neg-risk group produces no signal" — there is a test that would fail if
it stopped being true.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.core.enums import (
    KillSwitch,
    MarketCategory,
    ModelabilityStatus,
    Recommendation,
    ResolutionRisk,
    RiskStatus,
    Side,
)
from app.engines.calibration import (
    brier_score,
    build_report,
    log_loss,
    skill_versus_baseline,
)
from app.engines.classification import classify
from app.engines.edge import EdgeEngine
from app.engines.killswitch import KillSwitchEvaluator, KillSwitchReport, RiskState, SwitchState
from app.engines.liquidity import (
    estimate_execution,
    execution_probability,
    executable_probability,
    profile_book,
)
from app.engines.modelability import MarketFacts, assess
from app.engines.probability import (
    BaselineProbabilityModel,
    InvalidModelOutput,
    ProbabilityInputs,
    validate_probability,
)
from app.engines.risk import PortfolioState, RiskEngine
from tests.conftest import make_book


# ---------------------------------------------------------------------------
# Liquidity: order-book walking
# ---------------------------------------------------------------------------
def test_best_bid_ask_do_not_depend_on_level_order() -> None:
    """The venue returned bids ascending and asks descending. Shuffling the
    levels must not change the answer."""
    ascending = make_book(bids=[(0.50, 100), (0.55, 100)], asks=[(0.60, 100), (0.57, 100)])
    descending = make_book(bids=[(0.55, 100), (0.50, 100)], asks=[(0.57, 100), (0.60, 100)])
    assert ascending.best_bid == descending.best_bid == 0.55
    assert ascending.best_ask == descending.best_ask == 0.57


def test_walking_the_book_costs_more_than_the_touch(liquid_book) -> None:
    """The whole point of the liquidity engine."""
    small = estimate_execution(liquid_book, side=Side.BUY, size_usd=100)
    large = estimate_execution(liquid_book, side=Side.BUY, size_usd=40_000)
    assert small is not None and large is not None
    assert small.average_price == pytest.approx(0.565)
    assert large.average_price > small.average_price
    assert large.slippage > small.slippage
    assert large.levels_consumed > small.levels_consumed


def test_partial_fill_is_reported_not_rounded_up() -> None:
    book = make_book(asks=[(0.50, 100)], bids=[(0.49, 100)])  # $50 of depth
    estimate = estimate_execution(book, side=Side.BUY, size_usd=500)
    assert estimate is not None
    assert estimate.is_partial is True
    assert estimate.fillable_size_usd == pytest.approx(50.0)
    assert estimate.fill_ratio == pytest.approx(0.1)


def test_empty_side_returns_none_rather_than_a_price() -> None:
    """No depth means no price. Inventing one would be fabrication."""
    book = make_book(bids=[(0.50, 100)], asks=[])
    assert estimate_execution(book, side=Side.BUY, size_usd=100) is None
    assert executable_probability(book, side=Side.BUY, size_usd=100) is None


def test_selling_walks_bids_from_the_top() -> None:
    book = make_book(bids=[(0.50, 1000), (0.45, 1000)], asks=[(0.55, 1000)])
    estimate = estimate_execution(book, side=Side.SELL, size_usd=100)
    assert estimate is not None
    assert estimate.average_price == pytest.approx(0.50)
    assert estimate.slippage == pytest.approx(0.0)


def test_execution_probability_falls_with_partial_fills_and_depth() -> None:
    full = estimate_execution(make_book(asks=[(0.5, 10_000)], bids=[(0.49, 10_000)]), side=Side.BUY, size_usd=100)
    partial = estimate_execution(make_book(asks=[(0.5, 100)], bids=[(0.49, 100)]), side=Side.BUY, size_usd=500)
    assert execution_probability(full) > execution_probability(partial)
    assert execution_probability(None) == 0.0


def test_depth_is_measured_in_notional_not_shares() -> None:
    book = make_book(bids=[(0.10, 1000)], asks=[(0.90, 1000)])
    profile = profile_book(book)
    assert profile.bid_depth_usd == pytest.approx(100.0)
    assert profile.ask_depth_usd == pytest.approx(900.0)


def test_spread_pct_is_relative_to_midpoint() -> None:
    """A 1-cent spread means something different at 0.50 than at 0.02."""
    wide = profile_book(make_book(bids=[(0.015, 100)], asks=[(0.025, 100)]))
    tight = profile_book(make_book(bids=[(0.495, 100)], asks=[(0.505, 100)]))
    assert wide.spread == pytest.approx(tight.spread, abs=1e-9)
    assert wide.spread_pct > tight.spread_pct * 10


# ---------------------------------------------------------------------------
# Probability: validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf"), -0.01, 1.01, "0.5", None, True, [0.5]]
)
def test_invalid_probability_is_rejected(bad: object) -> None:
    """Rejected, never replaced with a guess."""
    with pytest.raises(InvalidModelOutput):
        validate_probability(bad, model_version="test")


@pytest.mark.parametrize("good", [0.0, 0.5, 1.0, 0.999999])
def test_valid_probability_is_accepted(good: float) -> None:
    assert validate_probability(good, model_version="test") == good


# ---------------------------------------------------------------------------
# Probability: baseline behaviour
# ---------------------------------------------------------------------------
def _inputs(**overrides) -> ProbabilityInputs:
    base = dict(
        market_id=1,
        token_id="t",
        category=MarketCategory.ELECTIONS,
        midpoint=0.50,
        executable_price=0.51,
        liquidity_profile=profile_book(make_book(bids=[(0.495, 20_000)], asks=[(0.505, 20_000)])),
        hours_to_resolution=720.0,
        snapshot_count=50,
    )
    base.update(overrides)
    return ProbabilityInputs(**base)


def test_baseline_defers_to_the_market_without_evidence(settings) -> None:
    """A model with nothing to say must say what the market says.

    This is the property that stops a fresh install from manufacturing edge.
    """
    result = BaselineProbabilityModel(settings).predict(_inputs())
    assert result.model_probability == pytest.approx(0.50, abs=0.02)
    assert result.model_uncertainty > 0.5


def test_negrisk_incoherence_moves_the_estimate(settings) -> None:
    """A group summing to 1.10 means every leg is on average too expensive."""
    model = BaselineProbabilityModel(settings)
    neutral = model.predict(_inputs(negrisk_group_sum=1.0, negrisk_group_size=5))
    expensive = model.predict(_inputs(negrisk_group_sum=1.10, negrisk_group_size=5))
    cheap = model.predict(_inputs(negrisk_group_sum=0.90, negrisk_group_size=5))

    assert expensive.model_probability < neutral.model_probability
    assert cheap.model_probability > neutral.model_probability
    # And having the constraint at all reduces uncertainty.
    assert expensive.model_uncertainty < model.predict(_inputs()).model_uncertainty


def test_absurd_negrisk_error_is_ignored_as_a_data_problem(settings) -> None:
    """A 60% apparent arbitrage is missing data, not a windfall."""
    model = BaselineProbabilityModel(settings)
    result = model.predict(_inputs(negrisk_group_sum=0.40, negrisk_group_size=50))
    assert result.adjustments["negrisk_coherence"] == 0.0
    assert result.model_probability == pytest.approx(0.50, abs=0.03)


def test_longshot_correction_is_symmetric(settings) -> None:
    """It must not be a one-directional bet."""
    model = BaselineProbabilityModel(settings)
    low = model.predict(_inputs(midpoint=0.03, negrisk_group_size=10, negrisk_group_sum=1.0))
    high = model.predict(_inputs(midpoint=0.97, negrisk_group_size=10, negrisk_group_sum=1.0))
    assert low.adjustments["favourite_longshot"] < 0  # longshot revised down
    assert high.adjustments["favourite_longshot"] > 0  # favourite revised up
    assert low.adjustments["favourite_longshot"] == pytest.approx(
        -high.adjustments["favourite_longshot"], abs=1e-9
    )


def test_output_always_stays_in_the_unit_interval(settings) -> None:
    """Log-odds arithmetic cannot escape [0,1], but assert it at the extremes."""
    model = BaselineProbabilityModel(settings)
    for midpoint in (0.001, 0.01, 0.5, 0.99, 0.999):
        for group_sum in (0.85, 1.0, 1.15):
            result = model.predict(
                _inputs(midpoint=midpoint, negrisk_group_sum=group_sum, negrisk_group_size=8)
            )
            assert 0.0 <= result.model_probability <= 1.0
            assert not math.isnan(result.model_probability)


def test_confidence_is_zero_without_a_two_sided_market(settings) -> None:
    one_sided = profile_book(make_book(bids=[(0.5, 100)], asks=[]))
    result = BaselineProbabilityModel(settings).predict(_inputs(liquidity_profile=one_sided))
    assert result.confidence == 0.0


def test_uncertainty_rises_close_to_expiry(settings) -> None:
    model = BaselineProbabilityModel(settings)
    far = model.predict(_inputs(hours_to_resolution=720.0))
    near = model.predict(_inputs(hours_to_resolution=6.0))
    assert near.model_uncertainty > far.model_uncertainty


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------
def test_executable_edge_is_never_more_optimistic_than_raw(settings, liquid_book) -> None:
    """The central discipline of the whole system."""
    profile = profile_book(liquid_book)
    prediction = BaselineProbabilityModel(settings).predict(
        _inputs(midpoint=profile.midpoint, liquidity_profile=profile,
                negrisk_group_sum=0.90, negrisk_group_size=5)
    )
    result = EdgeEngine(settings).evaluate(
        prediction=prediction, book=liquid_book, profile=profile
    )
    if result.executable_edge is not None:
        assert result.executable_edge <= abs(result.raw_edge) + 1e-9


def test_the_specs_worked_example(settings) -> None:
    """Model 64%, market 56%, raw edge 8pp — but a 59% fill makes it 5pp.

    Built as a synthetic book so the arithmetic is exact.
    """
    book = make_book(bids=[(0.55, 100_000)], asks=[(0.59, 100_000)])
    profile = profile_book(book)

    class FixedModel:
        model_probability = 0.64
        model_uncertainty = 0.2
        confidence = 0.8
        model_version = "test"
        resolution_risk = ResolutionRisk.LOW
        features: dict = {}
        adjustments: dict = {}
        rationale: dict = {}

    result = EdgeEngine(settings).evaluate(
        prediction=FixedModel(), book=book, profile=profile, size_usd=1_000
    )
    assert result.market_probability == pytest.approx(0.57)
    assert result.executable_price == pytest.approx(0.59)
    assert result.executable_edge == pytest.approx(0.05, abs=1e-6)
    # Classified on the executable 5pp, not on the raw 7pp.
    assert result.raw_edge > result.executable_edge


def test_thin_book_collapses_the_edge_to_no_trade(settings, thin_book) -> None:
    profile = profile_book(thin_book)

    class FixedModel:
        model_probability = 0.90
        model_uncertainty = 0.1
        confidence = 0.9
        model_version = "test"
        resolution_risk = ResolutionRisk.LOW
        features: dict = {}
        adjustments: dict = {}
        rationale: dict = {}

    result = EdgeEngine(settings).evaluate(prediction=FixedModel(), book=thin_book, profile=profile)
    # $70 of ask depth against a $500 reference size. The arithmetic edge is
    # large (0.90 model vs a 0.70 fill) and the slippage against the single
    # level is zero, so only the fill-ratio gate catches this.
    assert result.recommendation is Recommendation.NO_TRADE
    assert any("fills only" in r for r in result.reasons)


def test_small_raw_edge_produces_watch_not_a_trade(settings, liquid_book) -> None:
    profile = profile_book(liquid_book)

    class FixedModel:
        model_probability = profile.midpoint + 0.005
        model_uncertainty = 0.1
        confidence = 0.9
        model_version = "test"
        resolution_risk = ResolutionRisk.LOW
        features: dict = {}
        adjustments: dict = {}
        rationale: dict = {}

    result = EdgeEngine(settings).evaluate(prediction=FixedModel(), book=liquid_book, profile=profile)
    assert result.recommendation is Recommendation.WATCH


def test_rank_explanation_accounts_for_the_whole_score(settings) -> None:
    """A score nobody can explain is worse than no score."""
    book = make_book(bids=[(0.55, 200_000)], asks=[(0.57, 200_000)])
    profile = profile_book(book)

    class FixedModel:
        model_probability = 0.70
        model_uncertainty = 0.15
        confidence = 0.85
        model_version = "test"
        resolution_risk = ResolutionRisk.LOW
        features: dict = {}
        adjustments: dict = {}
        rationale: dict = {}

    result = EdgeEngine(settings).evaluate(prediction=FixedModel(), book=book, profile=profile)
    assert result.rank_score is not None
    contributions = result.rank_explanation["contributions"]
    assert sum(contributions.values()) == pytest.approx(result.rank_score, abs=1e-6)
    assert set(result.rank_explanation["weights"]) == set(contributions)


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
def _clear() -> KillSwitchReport:
    return KillSwitchReport(states={s: SwitchState(s, False, "clear") for s in KillSwitch})


def _tradeable_signal(settings, *, liquidity=100_000.0, spread=0.01, slippage=0.001, confidence=0.9):
    book = make_book(bids=[(0.55, 200_000)], asks=[(0.56, 200_000)])
    profile = profile_book(book)

    class FixedModel:
        model_probability = 0.70
        model_uncertainty = 0.1
        confidence_ = confidence
        model_version = "test"
        resolution_risk = ResolutionRisk.LOW
        features: dict = {}
        adjustments: dict = {}
        rationale: dict = {}

    FixedModel.confidence = confidence
    result = EdgeEngine(settings).evaluate(prediction=FixedModel(), book=book, profile=profile)
    return result


def test_risk_blocks_everything_when_a_kill_switch_is_tripped(settings) -> None:
    states = {s: SwitchState(s, False, "clear") for s in KillSwitch}
    states[KillSwitch.GLOBAL] = SwitchState(KillSwitch.GLOBAL, True, "operator halt")

    decision = RiskEngine(settings).evaluate(
        signal=_tradeable_signal(settings),
        market_id=1,
        correlation_group=None,
        portfolio=PortfolioState(equity_usd=10_000, cash_usd=10_000, gross_exposure_usd=0),
        kill_switches=KillSwitchReport(states=states),
    )
    assert decision.status is RiskStatus.BLOCKED_BY_KILL_SWITCH
    assert decision.approved_size_usd is None


def test_unknown_portfolio_state_fails_closed(settings) -> None:
    decision = RiskEngine(settings).evaluate(
        signal=_tradeable_signal(settings),
        market_id=1, correlation_group=None,
        portfolio=None,
        kill_switches=_clear(),
    )
    assert decision.status is RiskStatus.REJECTED


def test_position_size_respects_the_tightest_binding_limit(settings) -> None:
    """2% of 10,000 is 200, but only 50 of market room remains."""
    decision = RiskEngine(settings).evaluate(
        signal=_tradeable_signal(settings),
        market_id=1,
        correlation_group=None,
        portfolio=PortfolioState(
            equity_usd=10_000, cash_usd=10_000, gross_exposure_usd=0,
            market_exposure_usd={1: 450.0},  # cap is 5% = 500
        ),
        kill_switches=_clear(),
    )
    assert decision.status is RiskStatus.APPROVED
    assert decision.approved_size_usd == pytest.approx(50.0)
    assert "MAX_MARKET_EXPOSURE_PERCENT" in decision.reasons[0]


def test_correlated_exposure_is_shared_across_a_group(settings) -> None:
    """Markets in one neg-risk group share a single limit.

    The position cap alone would allow $200; the group already holds $1,480 of
    its $1,500 allowance, so only $20 may be added.
    """
    decision = RiskEngine(settings).evaluate(
        signal=_tradeable_signal(settings),
        market_id=99,
        correlation_group="0xnegrisk-group",
        portfolio=PortfolioState(
            equity_usd=10_000, cash_usd=10_000, gross_exposure_usd=0,
            correlated_exposure_usd={"0xnegrisk-group": 1_480.0},
        ),
        kill_switches=_clear(),
    )
    assert decision.status is RiskStatus.APPROVED
    assert decision.approved_size_usd == pytest.approx(20.0)
    assert "MAX_CORRELATED_EXPOSURE_PERCENT" in decision.reasons[0]


def test_exhausted_correlated_group_rejects_entirely(settings) -> None:
    """When the group has no meaningful room left, nothing is approved."""
    decision = RiskEngine(settings).evaluate(
        signal=_tradeable_signal(settings),
        market_id=99,
        correlation_group="0xnegrisk-group",
        portfolio=PortfolioState(
            equity_usd=10_000, cash_usd=10_000, gross_exposure_usd=0,
            correlated_exposure_usd={"0xnegrisk-group": 1_499.0},
        ),
        kill_switches=_clear(),
    )
    assert decision.status is RiskStatus.REJECTED
    assert "minimum" in decision.reasons[0]


def test_cash_constrains_size(settings) -> None:
    decision = RiskEngine(settings).evaluate(
        signal=_tradeable_signal(settings),
        market_id=1, correlation_group=None,
        portfolio=PortfolioState(equity_usd=10_000, cash_usd=75.0, gross_exposure_usd=0),
        kill_switches=_clear(),
    )
    assert decision.status is RiskStatus.APPROVED
    assert decision.approved_size_usd == pytest.approx(75.0)
    assert "available cash" in decision.reasons[0]


def test_limits_snapshot_is_recorded_with_every_decision(settings) -> None:
    decision = RiskEngine(settings).evaluate(
        signal=_tradeable_signal(settings),
        market_id=1, correlation_group=None,
        portfolio=PortfolioState(equity_usd=10_000, cash_usd=10_000, gross_exposure_usd=0),
        kill_switches=_clear(),
    )
    assert decision.limits_snapshot["MAX_POSITION_SIZE_PERCENT"] == settings.max_position_size_percent
    assert "MIN_LIQUIDITY" in decision.limits_snapshot


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------
def test_switches_fail_closed_on_unknown_state(settings) -> None:
    """Every automatic switch must trip when its input is unknown."""
    report = KillSwitchEvaluator(settings).evaluate(
        session=None,
        last_data_at=None,
        clock_skew_s=None,
        model_versions_registered=None,
        risk_state=None,
    )
    assert report.states[KillSwitch.DATA].tripped
    assert report.states[KillSwitch.MODEL].tripped
    assert report.states[KillSwitch.RISK].tripped
    assert report.states[KillSwitch.CONNECTIVITY].tripped
    assert report.states[KillSwitch.GLOBAL].tripped


def test_data_switch_trips_on_stale_data(settings) -> None:
    now = datetime.now(UTC)
    report = KillSwitchEvaluator(settings).evaluate(
        last_data_at=now - timedelta(seconds=settings.data_staleness_s + 60),
        clock_skew_s=0.0, model_versions_registered=True,
        risk_state=RiskState(equity_usd=1, peak_equity_usd=1, daily_pnl_usd=0, day_start_equity_usd=1),
        now=now,
    )
    assert report.states[KillSwitch.DATA].tripped
    assert "old" in report.states[KillSwitch.DATA].reason


def test_connectivity_switch_trips_on_clock_skew(settings) -> None:
    report = KillSwitchEvaluator(settings).evaluate(
        last_data_at=datetime.now(UTC), clock_skew_s=600.0,
        model_versions_registered=True,
        risk_state=RiskState(equity_usd=1, peak_equity_usd=1, daily_pnl_usd=0, day_start_equity_usd=1),
    )
    assert report.states[KillSwitch.CONNECTIVITY].tripped
    assert "skew" in report.states[KillSwitch.CONNECTIVITY].reason


def test_risk_switch_trips_on_drawdown(settings) -> None:
    report = KillSwitchEvaluator(settings).evaluate(
        last_data_at=datetime.now(UTC), clock_skew_s=0.0, model_versions_registered=True,
        risk_state=RiskState(
            equity_usd=7_000, peak_equity_usd=10_000,  # 30% drawdown, limit is 20%
            daily_pnl_usd=0.0, day_start_equity_usd=10_000,
        ),
    )
    assert report.states[KillSwitch.RISK].tripped
    assert "drawdown" in report.states[KillSwitch.RISK].reason


# ---------------------------------------------------------------------------
# Modelability
# ---------------------------------------------------------------------------
def _facts(**overrides) -> MarketFacts:
    base = dict(
        category=MarketCategory.ELECTIONS,
        liquidity_num=50_000.0,
        volume_num=200_000.0,
        end_date=datetime.now(UTC) + timedelta(days=30),
        first_seen_at=datetime.now(UTC) - timedelta(days=30),
        source_created_at=datetime.now(UTC) - timedelta(days=30),
        accepting_orders=True,
        enable_order_book=True,
        closed=False,
        archived=False,
        active=True,
        resolution_source="Resolves according to the official certified results published by the state election authority.",
        description="A detailed description of the resolution criteria for this market, referencing an official announcement." * 4,
        is_binary=True,
        liquidity_profile=profile_book(make_book(bids=[(0.495, 50_000)], asks=[(0.505, 50_000)])),
        snapshot_count=50,
    )
    base.update(overrides)
    return MarketFacts(**base)


def test_a_good_market_is_tradeable(settings) -> None:
    assert assess(_facts(), settings=settings).status is ModelabilityStatus.TRADEABLE


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"closed": True}, ModelabilityStatus.UNMODELABLE),
        ({"archived": True}, ModelabilityStatus.UNMODELABLE),
        ({"accepting_orders": False}, ModelabilityStatus.UNMODELABLE),
        ({"enable_order_book": False}, ModelabilityStatus.UNMODELABLE),
        ({"is_binary": False}, ModelabilityStatus.UNMODELABLE),
        ({"category": MarketCategory.SPORTS}, ModelabilityStatus.UNMODELABLE),
        ({"resolution_source": None, "description": None}, ModelabilityStatus.RESOLUTION_RISK),
        ({"snapshot_count": 1}, ModelabilityStatus.INSUFFICIENT_DATA),
        ({"category": MarketCategory.OTHER}, ModelabilityStatus.WATCHLIST),
    ],
)
def test_disqualifying_conditions(settings, override: dict, expected) -> None:
    assert assess(_facts(**override), settings=settings).status is expected


def test_one_sided_book_is_unmodelable(settings) -> None:
    one_sided = profile_book(make_book(bids=[(0.5, 1000)], asks=[]))
    assessment = assess(_facts(liquidity_profile=one_sided), settings=settings)
    assert assessment.status is ModelabilityStatus.UNMODELABLE
    assert any("one-sided" in d for d in assessment.disqualifiers)


def test_modelability_detail_explains_the_score(settings) -> None:
    detail = assess(_facts(), settings=settings).as_detail()
    assert set(detail["components"]) == set(detail["weights"])
    recomputed = sum(detail["components"][k] * detail["weights"][k] for k in detail["weights"])
    assert recomputed == pytest.approx(detail["score"], abs=1e-3)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def test_brier_and_log_loss_on_known_values() -> None:
    assert brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    assert brier_score([1.0, 0.0], [1, 0]) == pytest.approx(0.0)
    assert log_loss([0.5, 0.5], [1, 0]) == pytest.approx(math.log(2))


def test_small_sample_reports_insufficient_data_not_a_number() -> None:
    report = build_report([0.5] * 5, [1, 0, 1, 0, 1])
    assert report.insufficient_data is True
    assert report.brier_score is None
    assert "below the" in report.note


def test_a_well_calibrated_forecaster_scores_low_ece() -> None:
    """70% claimed, 70% realised, over a large enough sample."""
    predictions = [0.7] * 200 + [0.3] * 200
    outcomes = [1] * 140 + [0] * 60 + [1] * 60 + [0] * 140
    report = build_report(predictions, outcomes)
    assert report.insufficient_data is False
    assert report.expected_calibration_error == pytest.approx(0.0, abs=0.02)


def test_an_overconfident_forecaster_scores_high_ece() -> None:
    predictions = [0.95] * 100
    outcomes = [1] * 50 + [0] * 50  # claimed 95%, realised 50%
    report = build_report(predictions, outcomes)
    assert report.expected_calibration_error > 0.4


def test_skill_score_detects_beating_and_losing_to_the_market() -> None:
    outcomes = [1] * 50 + [0] * 50
    market = [0.5] * 100
    better = [0.9] * 50 + [0.1] * 50
    worse = [0.1] * 50 + [0.9] * 50

    assert skill_versus_baseline(better, market, outcomes)["beats_baseline"] is True
    assert skill_versus_baseline(better, market, outcomes)["brier_skill_score"] > 0
    assert skill_versus_baseline(worse, market, outcomes)["beats_baseline"] is False


def test_skill_score_reports_none_when_it_cannot_be_computed() -> None:
    result = skill_versus_baseline([], [], [])
    assert result["brier_skill_score"] is None
    assert result["beats_baseline"] is None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def test_tags_beat_keywords_and_earn_higher_confidence() -> None:
    tagged = classify(question="Will the Lakers win?", tag_slugs=["fed"])
    assert tagged.category is MarketCategory.FEDERAL_RESERVE
    assert tagged.confidence == 0.90

    keyword_only = classify(question="Will the Fed cut rates in March?")
    assert keyword_only.category is MarketCategory.FEDERAL_RESERVE
    assert keyword_only.confidence == 0.55


def test_more_specific_category_wins_when_several_tags_match() -> None:
    result = classify(question="q", tag_slugs=["economy", "fed", "politics"])
    assert result.category is MarketCategory.FEDERAL_RESERVE


def test_unmatched_market_is_other_with_low_confidence() -> None:
    result = classify(question="Will it be sunny tomorrow in an unnamed place?")
    assert result.category is MarketCategory.OTHER
    assert result.confidence == 0.20


def test_classification_records_its_evidence() -> None:
    result = classify(question="q", tag_slugs=["crypto", "bitcoin"])
    assert result.matched_on == "tags"
    assert any("crypto" in e or "bitcoin" in e for e in result.evidence)


# ---------------------------------------------------------------------------
# Snapshot budget
# ---------------------------------------------------------------------------
def test_snapshot_budget_fits_inside_the_interval() -> None:
    """The universe is larger than the cadence, so the budget must be derived
    from what one interval can actually poll — otherwise every cycle overruns
    and data age drifts toward the staleness limit."""
    from app.core.config import Settings

    s = Settings(
        allow_insecure_local=True, api_key="",
        snapshot_interval_s=60, clob_rps=5.0, book_batch_size=50,
        snapshot_budget_safety_factor=0.6,
    )
    budget = s.snapshot_token_budget
    seconds_needed = budget / s.book_batch_size / s.clob_rps

    assert seconds_needed <= s.snapshot_interval_s * s.snapshot_budget_safety_factor + 1
    assert budget == 9_000


def test_explicit_snapshot_cap_overrides_the_derived_budget() -> None:
    from app.core.config import Settings

    s = Settings(allow_insecure_local=True, api_key="", snapshot_max_tokens=500)
    assert s.snapshot_token_budget == 500


def test_budget_never_falls_below_one_batch() -> None:
    """A pathological configuration must still poll something."""
    from app.core.config import Settings

    s = Settings(
        allow_insecure_local=True, api_key="",
        snapshot_interval_s=1, data_staleness_s=60, clob_rps=0.1, book_batch_size=50,
    )
    assert s.snapshot_token_budget >= s.book_batch_size


def test_snapshot_budget_adapts_to_measured_throughput() -> None:
    """The configured budget is an estimate; the measured one is the truth.

    Sizing from the rate limit alone gave a 9,000-token budget that took 80s
    against a 60s interval, because round-trip latency and database writes
    dominate the limiter. The worker must shrink to what it actually achieves.
    """
    from app.core.config import Settings
    from app.workers.snapshot import SnapshotWorker

    s = Settings(
        allow_insecure_local=True, api_key="",
        snapshot_interval_s=60, clob_rps=5.0, book_batch_size=50,
        snapshot_budget_safety_factor=0.6,
    )
    worker = SnapshotWorker.__new__(SnapshotWorker)
    worker.settings = s
    worker.observed_tokens_per_second = None

    # No measurement yet: use the configured estimate.
    assert worker._budget() == 9_000

    # After observing the real rate (9,000 tokens in 80s = 112.5/s), the budget
    # shrinks to what fits 36s.
    worker._record_throughput(9_000, 80.0)
    assert worker.observed_tokens_per_second == pytest.approx(112.5)
    assert worker._budget() == pytest.approx(112.5 * 36, rel=0.01)
    assert worker._budget() < 9_000


def test_throughput_measurement_is_smoothed() -> None:
    """One slow cycle must not halve coverage."""
    from app.core.config import Settings
    from app.workers.snapshot import SnapshotWorker

    worker = SnapshotWorker.__new__(SnapshotWorker)
    worker.settings = Settings(allow_insecure_local=True, api_key="")
    worker.observed_tokens_per_second = None

    worker._record_throughput(1_000, 10.0)   # 100/s
    assert worker.observed_tokens_per_second == pytest.approx(100.0)

    worker._record_throughput(1_000, 100.0)  # a 10/s outlier
    assert worker.observed_tokens_per_second == pytest.approx(0.7 * 100 + 0.3 * 10)
    assert worker.observed_tokens_per_second > 50


def test_budget_never_collapses_below_one_batch() -> None:
    from app.core.config import Settings
    from app.workers.snapshot import SnapshotWorker

    s = Settings(allow_insecure_local=True, api_key="", book_batch_size=50)
    worker = SnapshotWorker.__new__(SnapshotWorker)
    worker.settings = s
    worker.observed_tokens_per_second = 0.001  # pathologically slow
    assert worker._budget() == s.book_batch_size


def test_explicit_cap_disables_adaptation() -> None:
    """An operator who sets an explicit cap means it."""
    from app.core.config import Settings
    from app.workers.snapshot import SnapshotWorker

    s = Settings(allow_insecure_local=True, api_key="", snapshot_max_tokens=500)
    worker = SnapshotWorker.__new__(SnapshotWorker)
    worker.settings = s
    worker.observed_tokens_per_second = 10_000.0
    assert worker._budget() == 500
