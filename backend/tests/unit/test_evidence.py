"""Evidence layer: registry, providers, store, matching, conflicts, shapes.

The connector tests use recorded response shapes rather than live calls, so they
are deterministic and run offline. The shapes themselves were captured from the
real endpoints on 2026-08-18 and are reproduced faithfully — including the
awkward parts, like BLS reporting failure in-band with HTTP 200.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.enums import (
    ComponentHealth,
    ConflictResolution,
    EvidenceType,
    MarketCategory,
    MarketSubcategory,
    SourceType,
    VerificationStatus,
)
from app.evidence.base import EvidenceError, EvidenceItem
from app.evidence.classify import classify_deep
from app.evidence.question_shape import QuestionShape, detect_shape
from app.evidence.registry import (
    BY_KEY,
    SOURCES,
    allowed_evidence_hosts,
    definitions_for,
    is_enabled,
)
from app.ingest.http import HttpFetcher


@pytest.fixture
def evidence_settings() -> Settings:
    return Settings(
        allow_insecure_local=True,
        api_key="",
        sec_user_agent="beroapp-test test@example.invalid",
        database_url="postgresql+psycopg://beroapp:beroapp@127.0.0.1:5432/beroapp_test",
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_every_source_key_is_unique() -> None:
    keys = [s.source_key for s in SOURCES]
    assert len(keys) == len(set(keys))


def test_allow_list_contains_only_implemented_sources() -> None:
    """A declared-but-unimplemented source must not be reachable at all."""
    hosts = allowed_evidence_hosts()
    for definition in SOURCES:
        if definition.implemented:
            assert definition.host in hosts
        elif not any(d.implemented and d.host == definition.host for d in SOURCES):
            assert definition.host not in hosts


def test_settings_allow_list_includes_registry_hosts(evidence_settings: Settings) -> None:
    allowed = evidence_settings.allowed_outbound_hosts
    assert "api.bls.gov" in allowed
    assert "home.treasury.gov" in allowed
    assert "gamma-api.polymarket.com" in allowed
    assert "evil.example" not in allowed


def test_source_requiring_a_missing_key_is_disabled(evidence_settings: Settings) -> None:
    enabled, reason = is_enabled(BY_KEY["fec"], evidence_settings)
    assert enabled is False
    assert "FEC_API_KEY" in reason


def test_sec_is_disabled_without_a_declared_user_agent() -> None:
    """SEC policy requires a contactable identity; we refuse rather than breach it."""
    settings = Settings(allow_insecure_local=True, api_key="", sec_user_agent="")
    enabled, reason = is_enabled(BY_KEY["sec_edgar"], settings)
    assert enabled is False
    assert "SEC_USER_AGENT" in reason


def test_routing_sends_only_relevant_sources_to_a_market() -> None:
    """A Fed market must not be handed a crypto feed."""
    fed = {d.source_key for d in definitions_for(MarketCategory.FEDERAL_RESERVE, MarketSubcategory.FED_RATES)}
    assert "treasury_yield_curve" in fed
    assert "fomc_calendar" in fed
    assert "coinbase_exchange" not in fed

    crypto = {d.source_key for d in definitions_for(MarketCategory.CRYPTO, MarketSubcategory.CRYPTO_PRICE)}
    assert "coinbase_exchange" in crypto
    assert "bls" not in crypto


def test_bls_declares_its_documented_daily_budget() -> None:
    """The 25/day keyless limit is the binding constraint in the system."""
    assert BY_KEY["bls"].daily_request_budget == 25


# ---------------------------------------------------------------------------
# EvidenceItem invariants
# ---------------------------------------------------------------------------
def _item(**overrides) -> EvidenceItem:
    base = dict(
        source_key="bls",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        source_tier=1,
        evidence_type=EvidenceType.TIME_SERIES_OBSERVATION,
        series_key="CPI_URBAN_ALL",
        title="CPI July 2026",
        known_at=datetime.now(UTC),
        parser_version="v1",
        numeric_value=333.9,
        observation_date=datetime(2026, 7, 1, tzinfo=UTC),
    )
    base.update(overrides)
    return EvidenceItem(**base)


def test_evidence_rejects_publication_after_known_at() -> None:
    """We cannot know a figure before it was published; that is look-ahead at
    the very source, and it must not be constructible."""
    with pytest.raises(ValueError, match="published_at"):
        _item(
            published_at=datetime.now(UTC) + timedelta(hours=1),
            known_at=datetime.now(UTC),
        )


def test_evidence_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _item(known_at=datetime(2026, 8, 1))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_evidence_rejects_non_finite_values(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        _item(numeric_value=bad)


def test_content_hash_identifies_the_fact_not_the_fetch() -> None:
    """Re-fetching an unchanged observation must deduplicate; a revision must not."""
    first = _item(known_at=datetime(2026, 8, 1, tzinfo=UTC))
    same_fact_later_fetch = _item(known_at=datetime(2026, 8, 2, tzinfo=UTC))
    revision = _item(numeric_value=334.1, known_at=datetime(2026, 8, 2, tzinfo=UTC))

    assert first.content_hash == same_fact_later_fetch.content_hash
    assert first.content_hash != revision.content_hash


# ---------------------------------------------------------------------------
# Question shape — the module that exists because of a real fabrication bug
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "expected", "lower", "upper"),
    [
        ("Will Bitcoin dip to $62,000 August 17-23?", QuestionShape.BARRIER_BELOW, 62_000, None),
        ("Will Bitcoin reach $65,000 in August?", QuestionShape.BARRIER_ABOVE, 65_000, None),
        ("Will BTC hit $70,000 by September?", QuestionShape.BARRIER_ABOVE, 70_000, None),
        ("Will Ethereum be above $2,000 on August 31?", QuestionShape.TERMINAL, 2_000, None),
        ("Will Bitcoin close below $60,000 on August 30?", QuestionShape.TERMINAL, None, 60_000),
        (
            "Will the price of Bitcoin be between $62,000 and $64,000 on August 22?",
            QuestionShape.RANGE, 62_000, 64_000,
        ),
    ],
)
def test_shape_detection(question: str, expected: QuestionShape, lower, upper) -> None:
    result = detect_shape(question)
    assert result.shape is expected
    assert result.lower == lower
    assert result.upper == upper


@pytest.mark.parametrize(
    "question",
    [
        "Bitcoin Up or Down - August 5, 10:55AM-11:00AM ET",
        "Will Bitcoin be higher or lower than yesterday?",
        "Will Bitcoin hit a new all-time high in 2026?",
        "Will Bitcoin outperform Ethereum?",
        "Will BTC rise 10% this week?",
        "",
    ],
)
def test_unmodelable_questions_are_refused(question: str) -> None:
    """A question comparing against an unstated reference must be declined, not
    guessed at. This is the class of question that produced fabricated edges."""
    result = detect_shape(question)
    assert result.shape is QuestionShape.UNKNOWN
    assert result.is_modelable is False
    assert result.reason


def test_ambiguous_multi_amount_question_is_refused() -> None:
    result = detect_shape("Will Bitcoin be above $60,000 having fallen from $70,000?")
    assert result.shape is QuestionShape.UNKNOWN
    assert "ambiguous" in result.reason


# ---------------------------------------------------------------------------
# Deep classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "subcategory"),
    [
        ("Will the Fed cut rates by 25 bps in September?", MarketSubcategory.FED_RATES),
        ("Will CPI inflation exceed 3% in August?", MarketSubcategory.INFLATION),
        ("Will unemployment be above 4.5%?", MarketSubcategory.EMPLOYMENT),
        ("Will Bitcoin reach $100,000?", MarketSubcategory.CRYPTO_PRICE),
        ("Will there be a recession in 2026?", MarketSubcategory.RECESSION),
        ("Who will win the 2028 Democratic nomination?", MarketSubcategory.US_PRIMARY),
    ],
)
def test_deep_classification(question: str, subcategory: MarketSubcategory) -> None:
    assert classify_deep(question=question).subcategory is subcategory


def test_unclassified_question_reports_low_confidence() -> None:
    result = classify_deep(question="Will it rain on Tuesday somewhere?")
    assert result.subcategory is MarketSubcategory.UNCLASSIFIED
    assert result.confidence < 0.3


def test_classification_extracts_crypto_asset_and_threshold() -> None:
    result = classify_deep(question="Will Ethereum reach $2,200 in August?")
    assert result.asset == "ETH"
    assert result.threshold_value == 2_200.0


# ---------------------------------------------------------------------------
# Connectors, against recorded response shapes
# ---------------------------------------------------------------------------
TREASURY_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
 <entry><content type="application/xml"><m:properties>
   <d:NEW_DATE m:type="Edm.DateTime">2026-08-17T00:00:00</d:NEW_DATE>
   <d:BC_3MONTH m:type="Edm.Double">3.87</d:BC_3MONTH>
   <d:BC_2YEAR m:type="Edm.Double">4.19</d:BC_2YEAR>
   <d:BC_10YEAR m:type="Edm.Double">4.72</d:BC_10YEAR>
 </m:properties></content></entry>
 <entry><content type="application/xml"><m:properties>
   <d:NEW_DATE m:type="Edm.DateTime">2026-08-16T00:00:00</d:NEW_DATE>
   <d:BC_3MONTH m:type="Edm.Double">3.85</d:BC_3MONTH>
 </m:properties></content></entry>
</feed>"""

BLS_OK = {
    "status": "REQUEST_SUCCEEDED",
    "message": [],
    "Results": {
        "series": [
            {
                "seriesID": "CUUR0000SA0",
                "data": [
                    {"year": "2026", "period": "M07", "periodName": "July",
                     "latest": "true", "value": "333.918", "footnotes": [{}]},
                    {"year": "2026", "period": "M13", "periodName": "Annual",
                     "value": "330.0", "footnotes": [{}]},
                ],
            }
        ]
    },
}


async def _build(provider_key: str, settings: Settings):
    from app.evidence.providers import build_provider

    fetcher = HttpFetcher(settings)
    return build_provider(provider_key, fetcher, settings), fetcher


@respx.mock
async def test_treasury_parses_only_the_latest_business_day(evidence_settings) -> None:
    respx.get(BY_KEY["treasury_yield_curve"].base_url).mock(
        return_value=httpx.Response(200, text=TREASURY_XML)
    )
    provider, fetcher = await _build("treasury_yield_curve", evidence_settings)
    items = await provider.collect()
    await fetcher.aclose()

    assert {i.series_key for i in items} == {"UST_YIELD_3M", "UST_YIELD_2Y", "UST_YIELD_10Y"}
    assert all(i.observation_date == datetime(2026, 8, 17, tzinfo=UTC) for i in items)
    assert next(i for i in items if i.series_key == "UST_YIELD_10Y").numeric_value == 4.72


@respx.mock
async def test_treasury_rejects_malformed_xml(evidence_settings) -> None:
    respx.get(BY_KEY["treasury_yield_curve"].base_url).mock(
        return_value=httpx.Response(200, text="<feed><unclosed>")
    )
    provider, fetcher = await _build("treasury_yield_curve", evidence_settings)
    with pytest.raises(EvidenceError) as exc:
        await provider.collect()
    await fetcher.aclose()
    assert exc.value.error_code == "parse_error"
    assert provider.get_health().health is ComponentHealth.FAILED


@respx.mock
async def test_bls_parses_monthly_periods_and_skips_annual(evidence_settings) -> None:
    respx.post(BY_KEY["bls"].base_url).mock(return_value=httpx.Response(200, json=BLS_OK))
    provider, fetcher = await _build("bls", evidence_settings)
    items = await provider.collect()
    await fetcher.aclose()

    # M13 is an annual average, not a monthly observation.
    assert len(items) == 1
    assert items[0].observation_date == datetime(2026, 7, 1, tzinfo=UTC)
    assert items[0].numeric_value == 333.918


@respx.mock
async def test_bls_treats_in_band_failure_as_a_failure(evidence_settings) -> None:
    """BLS reports quota exhaustion with HTTP 200, so the transport layer cannot
    catch it. The connector must."""
    respx.post(BY_KEY["bls"].base_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "REQUEST_NOT_PROCESSED",
                "message": ["daily threshold of 25 queries reached"],
                "Results": {},
            },
        )
    )
    provider, fetcher = await _build("bls", evidence_settings)
    with pytest.raises(EvidenceError) as exc:
        await provider.collect()
    await fetcher.aclose()
    assert exc.value.error_code == "bls_request_failed"


@respx.mock
async def test_bls_batches_every_series_into_one_request(evidence_settings) -> None:
    """The whole point of the design: one query, not one per series."""
    route = respx.post(BY_KEY["bls"].base_url).mock(
        return_value=httpx.Response(200, json=BLS_OK)
    )
    provider, fetcher = await _build("bls", evidence_settings)
    await provider.collect()
    await fetcher.aclose()

    assert route.call_count == 1
    assert provider.request_cost == 1


@respx.mock
async def test_kraken_treats_in_band_error_as_a_failure(evidence_settings) -> None:
    respx.get(url__startswith=f"{BY_KEY['kraken'].base_url}/0/public/Ticker").mock(
        return_value=httpx.Response(200, json={"error": ["EGeneral:Invalid arguments"]})
    )
    provider, fetcher = await _build("kraken", evidence_settings)
    with pytest.raises(EvidenceError) as exc:
        await provider.collect()
    await fetcher.aclose()
    assert exc.value.error_code == "kraken_error"


@respx.mock
async def test_kraken_matches_normalised_pair_names(evidence_settings) -> None:
    """XBTUSD comes back as XXBTZUSD; the mapping must not be guessed."""
    respx.get(url__startswith=f"{BY_KEY['kraken'].base_url}/0/public/Ticker").mock(
        return_value=httpx.Response(
            200,
            json={"error": [], "result": {"XXBTZUSD": {"c": ["64288.20", "0.001"]}}},
        )
    )
    provider, fetcher = await _build("kraken", evidence_settings)
    items = await provider.collect()
    await fetcher.aclose()

    assert len(items) == 1
    assert items[0].series_key == "CRYPTO_SPOT_BTC_USD"
    assert items[0].numeric_value == pytest.approx(64288.20)


@respx.mock
async def test_sec_refuses_without_a_declared_user_agent() -> None:
    settings = Settings(allow_insecure_local=True, api_key="", sec_user_agent="")
    provider, fetcher = await _build("sec_edgar", settings)
    with pytest.raises(EvidenceError) as exc:
        await provider.collect()
    await fetcher.aclose()
    assert exc.value.error_code == "missing_user_agent"


@respx.mock
async def test_provider_timeout_surfaces_as_evidence_error(evidence_settings) -> None:
    respx.get(BY_KEY["treasury_yield_curve"].base_url).mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )
    provider, fetcher = await _build("treasury_yield_curve", evidence_settings)
    with pytest.raises(EvidenceError):
        await provider.collect(now=datetime.now(UTC))
    await fetcher.aclose()


@respx.mock
async def test_empty_response_is_degraded_not_an_error(evidence_settings) -> None:
    """An empty feed means 'nothing new', which is different from a failure."""
    respx.get(BY_KEY["treasury_yield_curve"].base_url).mock(
        return_value=httpx.Response(
            200,
            text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
        )
    )
    provider, fetcher = await _build("treasury_yield_curve", evidence_settings)
    items = await provider.collect()
    await fetcher.aclose()

    assert items == []
    assert provider.get_health().health is ComponentHealth.DEGRADED


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------
class _FakeEvent:
    """Minimal stand-in with the fields the resolver reads."""

    def __init__(
        self, *, id, source_id, tier, value, verification, reliability, known_at,
        series_key="X", observation_date=None, unit=None,
    ):
        self.id = id
        self.source_id = source_id
        self.source_tier = tier
        self.numeric_value = value
        self.verification_status = verification
        self.reliability_score = reliability
        self.known_at = known_at
        self.series_key = series_key
        self.observation_date = observation_date or datetime(2026, 8, 1, tzinfo=UTC)
        self.unit = unit


def _event(**kwargs) -> _FakeEvent:
    base = dict(
        id=1, source_id=1, tier=1, value=100.0,
        verification=VerificationStatus.CONFIRMED_FACT.value,
        reliability=0.9, known_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    base.update(kwargs)
    return _FakeEvent(**base)


def test_authority_beats_everything() -> None:
    """An official statistic beats a more recent, more reliable news report."""
    from app.evidence.conflicts import resolve

    official = _event(id=1, tier=1, value=3.0, known_at=datetime(2026, 8, 1, tzinfo=UTC), reliability=0.5)
    news = _event(
        id=2, source_id=2, tier=2, value=3.4,
        known_at=datetime(2026, 8, 20, tzinfo=UTC), reliability=0.99,
    )
    outcome = resolve([news, official])
    assert outcome.winner.id == 1
    assert outcome.resolution is ConflictResolution.HIGHER_TIER


def test_recency_breaks_a_tie_within_a_tier() -> None:
    """A revision is the issuer's own better estimate."""
    from app.evidence.conflicts import resolve

    old = _event(id=1, value=3.0, known_at=datetime(2026, 8, 1, tzinfo=UTC))
    revised = _event(id=2, source_id=2, value=3.2, known_at=datetime(2026, 8, 15, tzinfo=UTC))
    outcome = resolve([old, revised])
    assert outcome.winner.id == 2
    assert outcome.resolution is ConflictResolution.MORE_RECENT


def test_verification_beats_recency() -> None:
    from app.evidence.conflicts import resolve

    confirmed = _event(id=1, value=3.0, known_at=datetime(2026, 8, 1, tzinfo=UTC))
    rumour = _event(
        id=2, source_id=2, value=9.0,
        verification=VerificationStatus.UNCONFIRMED_CLAIM.value,
        known_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    outcome = resolve([confirmed, rumour])
    assert outcome.winner.id == 1
    assert outcome.resolution is ConflictResolution.BETTER_VERIFIED


def test_indistinguishable_candidates_are_unresolved_not_guessed() -> None:
    """Picking arbitrarily and calling it a decision would be dishonest."""
    from app.evidence.conflicts import resolve

    a = _event(id=1, source_id=1, value=100.0)
    b = _event(id=2, source_id=2, value=110.0)
    outcome = resolve([a, b])
    assert outcome.winner is None
    assert outcome.resolution is ConflictResolution.UNRESOLVED


def test_conflicts_are_never_averaged() -> None:
    """The winner is one of the candidates, never a blend of them."""
    from app.evidence.conflicts import resolve

    a = _event(id=1, tier=1, value=100.0, known_at=datetime(2026, 8, 15, tzinfo=UTC))
    b = _event(id=2, source_id=2, tier=2, value=200.0)
    outcome = resolve([a, b])
    assert outcome.winner.numeric_value in (100.0, 200.0)
    assert outcome.winner.numeric_value != 150.0


def test_relative_spread_is_scale_aware() -> None:
    """The same absolute gap means different things at 3.9% and at $64,000."""
    from app.evidence.conflicts import _relative_spread

    assert _relative_spread([3.90, 3.95]) > _relative_spread([64000.0, 64050.0])


def test_immaterial_disagreement_is_not_a_conflict() -> None:
    from app.evidence.conflicts import MATERIAL_DISAGREEMENT, _relative_spread

    # Two exchanges 0.01% apart are agreeing.
    assert _relative_spread([64295.11, 64288.20]) < MATERIAL_DISAGREEMENT


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def test_series_routing_is_subcategory_specific() -> None:
    from app.evidence.matching import relevant_series

    fed = relevant_series(MarketSubcategory.FED_RATES)
    assert "FOMC_MEETING" in fed
    assert "UST_YIELD_3M" in fed
    assert not any(s.startswith("CRYPTO_") for s in fed)

    crypto = relevant_series(MarketSubcategory.CRYPTO_PRICE, asset="BTC")
    assert "CRYPTO_SPOT_BTC_USD" in crypto
    assert "CPI_URBAN_ALL" not in crypto


def test_unmapped_subcategory_yields_no_series() -> None:
    """No series means no independent estimate, which is the honest outcome."""
    from app.evidence.matching import relevant_series

    assert relevant_series(MarketSubcategory.UNCLASSIFIED) == ()
    assert relevant_series(None) == ()


def test_asset_match_scores_highest() -> None:
    from app.evidence.matching import score_match

    asset = score_match(
        series_key="CRYPTO_SPOT_BTC_USD", subcategory=MarketSubcategory.CRYPTO_PRICE,
        asset="BTC", subject_tags=frozenset(), evidence_tags=frozenset(),
    )
    primary = score_match(
        series_key="CPI_URBAN_ALL", subcategory=MarketSubcategory.INFLATION,
        asset=None, subject_tags=frozenset(), evidence_tags=frozenset(),
    )
    assert asset[0] > primary[0]


def test_unrelated_evidence_scores_none_rather_than_a_small_number() -> None:
    """Returning a token score for everything would make 'unrelated' and
    'weakly related' indistinguishable."""
    from app.evidence.matching import score_match

    assert score_match(
        series_key="CRYPTO_SPOT_BTC_USD", subcategory=MarketSubcategory.INFLATION,
        asset=None, subject_tags=frozenset({"cpi"}), evidence_tags=frozenset({"btc"}),
    ) is None
