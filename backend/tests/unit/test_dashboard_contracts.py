"""Regressions for defects the dashboard integration surfaced.

Every test here corresponds to something that was actually wrong and rendered
as a plausible-looking number or badge. That is the failure mode worth guarding:
not a crash, but a page that reads confidently and says the wrong thing.
"""

from __future__ import annotations


import pytest

from app.api.routes import _signal_strength_value
from app.core.config import Settings
from app.core.enums import (
    MarketCategory,
    MarketSubcategory,
    ModelabilityStatus,
    ModelabilityTier,
    ResolutionMechanism,
)
from app.db.models import Signal
from app.evidence.classify import classify_deep, modelability_tier
from app.evidence.registry import SOURCES, allowed_evidence_hosts


# ---------------------------------------------------------------------------
# One registry, one row per source
# ---------------------------------------------------------------------------
def test_source_keys_are_unique() -> None:
    """Two declarations of one source produced two rows on the dashboard.

    The Phase 1 seed list and the Phase 1.5 registry both described BLS, under
    names that differed by a "U.S." prefix, so the reconciler could not match
    them. The page then showed the same bureau twice: once enabled and
    collecting, once disabled and empty.
    """
    keys = [s.source_key for s in SOURCES]
    assert len(keys) == len(set(keys)), "duplicate source_key in the registry"


def test_source_names_are_unique() -> None:
    """Names are the fallback match key, so they must disambiguate too."""
    names = [s.name for s in SOURCES]
    assert len(names) == len(set(names)), "duplicate source name in the registry"


def test_unimplemented_sources_contribute_no_reachable_host() -> None:
    """A declared-but-unbuilt source must not widen the SSRF allow-list."""
    allowed = allowed_evidence_hosts()
    for source in SOURCES:
        if not source.implemented:
            assert source.host not in allowed, (
                f"{source.source_key} has no connector but its host is allow-listed"
            )


def test_placeholder_sources_point_at_an_unroutable_host() -> None:
    """The tier 3-4 placeholders name no real third party.

    A plausible-looking base_url on a source nobody has reviewed is an
    invitation to point a scraper at it later without checking terms first.
    """
    for source in SOURCES:
        if source.access_method == "NOT_IMPLEMENTED" and source.tier >= 3:
            assert source.host.endswith(".invalid"), (
                f"{source.source_key} names a real host for an unbuilt connector"
            )


# ---------------------------------------------------------------------------
# Signal strength shape
# ---------------------------------------------------------------------------
def _signal(rank_explanation) -> Signal:
    return Signal(rank_explanation=rank_explanation)


def test_signal_strength_lifts_the_scalar_out_of_the_assessment() -> None:
    """The stored value is the whole assessment; the API must expose a string.

    Returning the object made the opportunities page throw, because React
    cannot render a dict. The gates are worth storing, so the fix is to lift
    the scalar rather than to stop storing them.
    """
    stored = {
        "signal_strength": {
            "strength": "CANDIDATE",
            "gates_failed": ["has_independent_estimate"],
            "gates_passed": [],
            "reasons": [],
            "has_independent_estimate": False,
            "evidence_source_count": 0,
        }
    }
    assert _signal_strength_value(_signal(stored)) == "CANDIDATE"


def test_signal_strength_reads_back_a_bare_string() -> None:
    """An older build stored the bare enum value. It must still read back."""
    assert _signal_strength_value(_signal({"signal_strength": "SIGNAL"})) == "SIGNAL"


@pytest.mark.parametrize(
    "stored",
    [None, {}, {"signal_strength": None}, {"signal_strength": {}}, {"signal_strength": 3}],
)
def test_signal_strength_is_none_when_never_assessed(stored) -> None:
    """Phase 1 signals carry no assessment and must not raise."""
    assert _signal_strength_value(_signal(stored)) is None


# ---------------------------------------------------------------------------
# Modelability tier
# ---------------------------------------------------------------------------
def _crypto_classification():
    return classify_deep(
        question="Will Bitcoin reach $65,000 in August?",
        description=None,
        category=MarketCategory.CRYPTO,
    )


def test_tier_high_requires_both_an_estimate_and_evidence() -> None:
    """A model running on nothing but the venue's own price is not HIGH."""
    classification = _crypto_classification()

    assert (
        modelability_tier(
            classification=classification,
            modelability_status=ModelabilityStatus.TRADEABLE.value,
            has_independent_estimate=True,
            evidence_feature_count=4,
        )
        is ModelabilityTier.HIGH
    )
    assert (
        modelability_tier(
            classification=classification,
            modelability_status=ModelabilityStatus.TRADEABLE.value,
            has_independent_estimate=True,
            evidence_feature_count=0,
        )
        is ModelabilityTier.LOW
    )


def test_tier_is_unmodelable_when_resolution_is_subjective() -> None:
    """No series exists for "what the media broadly agreed"."""
    classification = classify_deep(
        question="Will the coverage be broadly positive?",
        description=None,
        category=MarketCategory.OTHER,
    )
    object.__setattr__(
        classification, "resolution_mechanism", ResolutionMechanism.MEDIA_CONSENSUS
    )
    object.__setattr__(classification, "subcategory", MarketSubcategory.INFLATION)

    assert (
        modelability_tier(
            classification=classification,
            modelability_status=ModelabilityStatus.TRADEABLE.value,
            has_independent_estimate=True,
            evidence_feature_count=9,
        )
        is ModelabilityTier.UNMODELABLE
    )


def test_tier_respects_phase_1_unmodelable_status() -> None:
    """Phase 1's verdict is not overridden by Phase 1.5 optimism."""
    assert (
        modelability_tier(
            classification=_crypto_classification(),
            modelability_status=ModelabilityStatus.UNMODELABLE.value,
            has_independent_estimate=True,
            evidence_feature_count=9,
        )
        is ModelabilityTier.UNMODELABLE
    )


def test_tier_never_reads_the_edge() -> None:
    """The tier answers "could we model this", decided before the model runs.

    Asserted structurally: the function takes no edge, probability or
    recommendation argument, so it cannot become a post-hoc justification for
    whatever number came out.
    """
    import inspect

    params = set(inspect.signature(modelability_tier).parameters)
    assert not (params & {"edge", "probability", "recommendation", "confidence"})


# ---------------------------------------------------------------------------
# Only the latest signal per token is a current opportunity
# ---------------------------------------------------------------------------
def test_opportunities_query_deduplicates_by_token() -> None:
    """A superseded BUY must not outlive the NO_TRADE that replaced it.

    This is checked against the compiled SQL rather than a live database
    because the bug was in the query shape: without the grouping, a token
    re-evaluated every cycle appears once per cycle in the 24-hour window, and
    the stale row is the one that looks like an opportunity.
    """
    from app.api import routes

    source = inspect_source(routes.opportunities)
    assert "group_by(Signal.token_id)" in source
    assert "func.max(Signal.signal_at)" in source


def inspect_source(func) -> str:
    import inspect

    return inspect.getsource(func)


# ---------------------------------------------------------------------------
# evidence_available is three-state
# ---------------------------------------------------------------------------
def test_evidence_available_starts_unassessed() -> None:
    """A fresh market has never been looked at, which is not "no evidence".

    The evidence worker walks the most liquid markets first and does not reach
    the long tail every cycle. Defaulting the column to False would put a
    confident NO_EVIDENCE badge on markets nobody had checked.
    """
    from app.db.models import Market

    column = Market.__table__.columns["evidence_available"]
    assert column.nullable is True
    assert column.default is None
    assert column.server_default is None


# ---------------------------------------------------------------------------
# Phase 1 safety defaults survive Phase 1.5
# ---------------------------------------------------------------------------
def test_live_trading_still_defaults_off() -> None:
    settings = Settings(allow_insecure_local=True, api_key="")
    assert settings.live_trading_enabled is False
    assert settings.current_phase == "PHASE_1"


def test_evidence_worker_interval_is_not_aggressive() -> None:
    """Polling a public agency endpoint every few seconds is not acceptable use."""
    settings = Settings(allow_insecure_local=True, api_key="")
    assert settings.evidence_interval_s >= 600


def test_budgeted_sources_declare_a_daily_cap() -> None:
    """BLS keyless allows 25 queries a day. A cap that is not declared is not enforced."""
    bls = next(s for s in SOURCES if s.source_key == "bls")
    assert bls.daily_request_budget is not None
    assert bls.daily_request_budget <= 25


def test_update_frequencies_stay_inside_the_declared_budget() -> None:
    """A source cannot be scheduled more often than its own daily cap allows."""
    day_s = 24 * 3600
    for source in SOURCES:
        if not source.implemented or source.daily_request_budget is None:
            continue
        max_calls_per_day = day_s / source.update_frequency_s
        assert max_calls_per_day <= source.daily_request_budget, (
            f"{source.source_key} would make {max_calls_per_day:.0f} calls/day "
            f"against a budget of {source.daily_request_budget}"
        )


def test_evidence_link_relevance_stays_a_probability() -> None:
    """Relevance is rendered as a 0-1 score; the constraint must actually hold."""
    from app.db.models import MarketEvidenceLink

    checks = [
        c.sqltext.text
        for c in MarketEvidenceLink.__table__.constraints
        if hasattr(c, "sqltext")
    ]
    assert any("relevance" in text for text in checks), "no CHECK on relevance"


def test_known_at_is_never_defaulted_to_now_in_the_database() -> None:
    """`known_at` is set by the code that knows when we learned something.

    A database-side `now()` default would silently stamp a backfilled row with
    the time of the backfill, which is exactly the look-ahead the three
    timestamps exist to prevent.
    """
    from app.db.models import ExternalEvent

    column = ExternalEvent.__table__.columns["known_at"]
    assert column.server_default is None


def test_recent_signal_window_is_bounded() -> None:
    """The opportunities window is a fixed 24 hours, not "everything ever"."""
    source = inspect_source(__import__("app.api.routes", fromlist=["opportunities"]).opportunities)
    assert "timedelta(hours=24)" in source
