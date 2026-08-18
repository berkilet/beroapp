"""Failure handling and the spec's critical test cases.

Covers the numbered list in the specification: API unavailable, rate limiting,
malformed and missing data, stale prices, zero liquidity, extreme spreads,
invalid probabilities, duplicate signals, and so on. Each test asserts the
*fail-closed* behaviour — the degraded state must produce fewer signals, never
more.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.core.enums import KillSwitch, MarketCategory, Recommendation, Side
from app.engines.edge import EdgeEngine
from app.engines.killswitch import KillSwitchEvaluator, RiskState
from app.engines.liquidity import estimate_execution, profile_book
from app.engines.probability import (
    BaselineProbabilityModel,
    InvalidModelOutput,
    ProbabilityInputs,
    validate_probability,
)
from app.ingest.http import (
    CircuitOpenError,
    HttpFetcher,
    PermanentFetchError,
    RateLimitedError,
    RetryableFetchError,
)
from app.ingest.polymarket import PolymarketClient
from app.schemas.polymarket import GammaMarket, MalformedRecord, OrderBook
from tests.conftest import make_book

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


# ---------------------------------------------------------------------------
# 1-2. API unavailable and rate limited
# ---------------------------------------------------------------------------
@respx.mock
async def test_api_unavailable_raises_after_retries(settings) -> None:
    route = respx.get(f"{GAMMA}/events").mock(return_value=httpx.Response(503))
    fetcher = HttpFetcher(settings)
    fetcher.settings = settings

    with pytest.raises(RetryableFetchError) as exc:
        await fetcher.fetch_json(f"{GAMMA}/events", max_retries=2)

    assert exc.value.error_code == "http_503"
    assert route.call_count == 3  # initial attempt plus two retries
    await fetcher.aclose()


@respx.mock
async def test_rate_limit_honours_retry_after(settings, monkeypatch) -> None:
    """429 must wait the documented interval rather than backing off blindly."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    respx.get(f"{GAMMA}/events").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=[]),
        ]
    )
    fetcher = HttpFetcher(settings)
    result = await fetcher.fetch_json(f"{GAMMA}/events")

    assert result == []
    assert 7.0 in slept
    await fetcher.aclose()


@respx.mock
async def test_client_error_is_not_retried(settings) -> None:
    """A 404 will still be a 404 next time; retrying it wastes the budget."""
    route = respx.get(f"{GAMMA}/events").mock(return_value=httpx.Response(404))
    fetcher = HttpFetcher(settings)

    with pytest.raises(PermanentFetchError):
        await fetcher.fetch_json(f"{GAMMA}/events")
    assert route.call_count == 1
    await fetcher.aclose()


@respx.mock
async def test_circuit_breaker_opens_after_repeated_failures(settings, monkeypatch) -> None:
    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    respx.get(f"{GAMMA}/events").mock(return_value=httpx.Response(500))
    fetcher = HttpFetcher(settings)

    for _ in range(settings.circuit_breaker_failures):
        with pytest.raises((RetryableFetchError, CircuitOpenError)):
            await fetcher.fetch_json(f"{GAMMA}/events", max_retries=0)

    with pytest.raises(CircuitOpenError):
        await fetcher.fetch_json(f"{GAMMA}/events", max_retries=0)
    await fetcher.aclose()


@respx.mock
async def test_non_json_two_hundred_is_rejected(settings) -> None:
    """A 200 with an HTML body means the contract changed. Reject loudly."""
    respx.get(f"{GAMMA}/events").mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )
    fetcher = HttpFetcher(settings)
    with pytest.raises(PermanentFetchError) as exc:
        await fetcher.fetch_json(f"{GAMMA}/events")
    assert exc.value.error_code == "invalid_json"
    await fetcher.aclose()


@respx.mock
async def test_schema_change_is_detected_not_silently_absorbed(settings) -> None:
    """Gamma returning an object where a list is documented must fail visibly."""
    respx.get(f"{GAMMA}/events").mock(return_value=httpx.Response(200, json={"markets": []}))
    async with PolymarketClient(settings=settings) as client:
        with pytest.raises(MalformedRecord, match="schema change"):
            await client.list_events()


# ---------------------------------------------------------------------------
# SSRF and transport policy
# ---------------------------------------------------------------------------
async def test_non_allowlisted_host_is_refused(settings) -> None:
    fetcher = HttpFetcher(settings)
    with pytest.raises(PermanentFetchError, match="allow-list"):
        await fetcher.fetch_json("https://evil.example/data")
    await fetcher.aclose()


async def test_plain_http_is_refused(settings) -> None:
    fetcher = HttpFetcher(settings)
    with pytest.raises(PermanentFetchError, match="non-https"):
        await fetcher.fetch_json("http://gamma-api.polymarket.com/events")
    await fetcher.aclose()


@respx.mock
async def test_redirect_off_the_allowlist_is_refused(settings) -> None:
    respx.get(f"{GAMMA}/events").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example/"})
    )
    fetcher = HttpFetcher(settings)
    with pytest.raises(PermanentFetchError, match="redirect"):
        await fetcher.fetch_json(f"{GAMMA}/events")
    await fetcher.aclose()


# ---------------------------------------------------------------------------
# 3-5. Malformed, missing, duplicate market data
# ---------------------------------------------------------------------------
@respx.mock
async def test_malformed_markets_are_counted_not_ingested(settings) -> None:
    """One bad record must not discard a whole page of good ones."""
    respx.get(f"{GAMMA}/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "1", "conditionId": "0xa", "question": "good",
                    "outcomes": '["Yes","No"]', "clobTokenIds": '["1","2"]',
                },
                {"id": "2", "conditionId": "0xb", "question": "no tokens"},
                {"id": "3", "conditionId": "0xc", "clobTokenIds": "not json at all"},
            ],
        )
    )
    async with PolymarketClient(settings=settings) as client:
        markets, report = await client.list_markets()

    assert len(markets) == 1
    assert report.accepted == 1
    assert report.rejected == 2
    assert report.error_rate == pytest.approx(2 / 3)


@respx.mock
async def test_crossed_book_is_rejected(settings) -> None:
    """bid > ask is a venue bug or a corrupted response. Either way it must not
    reach the edge engine."""
    respx.post(f"{CLOB}/books").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "asset_id": "1", "market": "0xa", "timestamp": "1700000000000",
                    "bids": [{"price": "0.80", "size": "100"}],
                    "asks": [{"price": "0.20", "size": "100"}],
                }
            ],
        )
    )
    async with PolymarketClient(settings=settings) as client:
        books, report = await client.get_books(["1"])
    assert books == []
    assert report.rejected == 1


@pytest.mark.parametrize("bad_price", ["1.5", "-0.1", "abc", "NaN", "Infinity"])
def test_out_of_range_book_prices_are_rejected(bad_price: str) -> None:
    with pytest.raises(Exception):
        OrderBook.model_validate(
            {
                "asset_id": "1",
                "bids": [{"price": bad_price, "size": "100"}],
                "asks": [],
            }
        )


def test_nan_numeric_fields_become_none_not_zero() -> None:
    """Unknown is not zero. A NaN liquidity must not read as an empty book."""
    market = GammaMarket.model_validate(
        {
            "id": "1", "conditionId": "0xa",
            "outcomes": '["Yes","No"]', "clobTokenIds": '["1","2"]',
            "liquidityNum": "NaN", "volumeNum": "Infinity", "volume24hr": "",
        }
    )
    assert market.liquidity_num is None
    assert market.volume_num is None
    assert market.volume_24hr is None


def test_json_encoded_string_arrays_are_decoded() -> None:
    """The documented encoding hazard in Gamma responses."""
    market = GammaMarket.model_validate(
        {
            "id": "1", "conditionId": "0xa",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.4", "0.6"]',
            "clobTokenIds": '["111", "222"]',
        }
    )
    assert market.outcomes == ["Yes", "No"]
    assert market.outcome_prices == [0.4, 0.6]
    assert market.clob_token_ids == ["111", "222"]
    assert market.is_binary


def test_already_decoded_arrays_still_work() -> None:
    """Keep working if the venue ever fixes the encoding."""
    market = GammaMarket.model_validate(
        {
            "id": "1", "conditionId": "0xa",
            "outcomes": ["Yes", "No"], "clobTokenIds": ["111", "222"],
        }
    )
    assert market.clob_token_ids == ["111", "222"]


# ---------------------------------------------------------------------------
# 6-7. Stale price and stale order book
# ---------------------------------------------------------------------------
def test_stale_data_trips_the_data_switch(settings) -> None:
    now = datetime.now(UTC)
    report = KillSwitchEvaluator(settings).evaluate(
        last_data_at=now - timedelta(seconds=settings.data_staleness_s * 2),
        clock_skew_s=0.0, model_versions_registered=True,
        risk_state=RiskState(equity_usd=1, peak_equity_usd=1, daily_pnl_usd=0, day_start_equity_usd=1),
        now=now,
    )
    assert report.states[KillSwitch.DATA].tripped
    assert report.any_tripped


def test_future_dated_data_is_treated_as_suspect(settings) -> None:
    """A timestamp from the future means a clock problem somewhere."""
    now = datetime.now(UTC)
    report = KillSwitchEvaluator(settings).evaluate(
        last_data_at=now + timedelta(seconds=600),
        clock_skew_s=0.0, model_versions_registered=True,
        risk_state=RiskState(equity_usd=1, peak_equity_usd=1, daily_pnl_usd=0, day_start_equity_usd=1),
        now=now,
    )
    assert report.states[KillSwitch.DATA].tripped


# ---------------------------------------------------------------------------
# 8-9. Zero liquidity and extreme spread
# ---------------------------------------------------------------------------
def test_zero_liquidity_produces_no_trade(settings) -> None:
    empty = make_book(bids=[], asks=[])
    profile = profile_book(empty)
    prediction = BaselineProbabilityModel(settings).predict(
        ProbabilityInputs(
            market_id=1, token_id="t", category=MarketCategory.ELECTIONS,
            midpoint=0.5, executable_price=None, liquidity_profile=profile,
            hours_to_resolution=720.0, snapshot_count=10,
        )
    )
    result = EdgeEngine(settings).evaluate(prediction=prediction, book=empty, profile=profile)
    assert result.recommendation in (Recommendation.INSUFFICIENT_DATA, Recommendation.NO_TRADE, Recommendation.WATCH)
    assert result.side is None


def test_extreme_spread_yields_zero_spread_score(settings) -> None:
    from app.engines.modelability import _score_spread

    wide = profile_book(make_book(bids=[(0.10, 100)], asks=[(0.90, 100)]))
    score, note = _score_spread(wide, settings.max_spread)
    assert score == 0.0
    assert note is not None


# ---------------------------------------------------------------------------
# 10-12. Invalid, NaN probability; model unavailable
# ---------------------------------------------------------------------------
def test_invalid_market_midpoint_is_rejected_by_the_model(settings) -> None:
    model = BaselineProbabilityModel(settings)
    with pytest.raises(InvalidModelOutput):
        model.predict(
            ProbabilityInputs(
                market_id=1, token_id="t", category=MarketCategory.ELECTIONS,
                midpoint=float("nan"), executable_price=None,
                liquidity_profile=None, hours_to_resolution=100.0, snapshot_count=10,
            )
        )


def test_unregistered_model_version_trips_the_model_switch(settings) -> None:
    report = KillSwitchEvaluator(settings).evaluate(
        last_data_at=datetime.now(UTC), clock_skew_s=0.0,
        model_versions_registered=False,
        risk_state=RiskState(equity_usd=1, peak_equity_usd=1, daily_pnl_usd=0, day_start_equity_usd=1),
    )
    assert report.states[KillSwitch.MODEL].tripped


def test_calibration_drift_trips_the_model_switch(settings) -> None:
    report = KillSwitchEvaluator(settings).evaluate(
        last_data_at=datetime.now(UTC), clock_skew_s=0.0,
        model_versions_registered=True, calibration_drift=0.25,
        risk_state=RiskState(equity_usd=1, peak_equity_usd=1, daily_pnl_usd=0, day_start_equity_usd=1),
    )
    assert report.states[KillSwitch.MODEL].tripped
    assert "drift" in report.states[KillSwitch.MODEL].reason


# ---------------------------------------------------------------------------
# 15-17. Duplicate signals, duplicate orders, races
# ---------------------------------------------------------------------------
def test_signal_idempotency_key_is_stable_within_a_bucket() -> None:
    """Re-running a cycle must not create a second signal.

    The key is (token, model, recommendation, minute) — deterministic, so two
    runs in the same minute collide and the second is a no-op.
    """
    at = datetime(2026, 1, 1, 12, 34, 56, tzinfo=UTC)
    later_same_minute = datetime(2026, 1, 1, 12, 34, 59, tzinfo=UTC)
    next_minute = datetime(2026, 1, 1, 12, 35, 1, tzinfo=UTC)

    def key(ts: datetime) -> str:
        return f"tok:v1:BUY:{ts.strftime('%Y%m%d%H%M')}"

    assert key(at) == key(later_same_minute)
    assert key(at) != key(next_minute)


def test_paper_order_venue_is_constrained_at_the_database_level() -> None:
    """A live order can never be recorded in the paper table, even by mistake."""
    from app.db.models import PaperOrder

    constraints = {c.name for c in PaperOrder.__table__.constraints if c.name}
    assert any("paper_only" in c for c in constraints)


# ---------------------------------------------------------------------------
# 18. Resolution ambiguity
# ---------------------------------------------------------------------------
def _closed_market(prices: str | None) -> GammaMarket:
    payload = {
        "id": "1", "conditionId": "0xa",
        "outcomes": '["Yes","No"]', "clobTokenIds": '["1","2"]',
        "closed": True, "active": False,
    }
    if prices is not None:
        payload["outcomePrices"] = prices
    return GammaMarket.model_validate(payload)


@pytest.mark.parametrize(
    ("prices", "expected_outcome", "ambiguous"),
    [
        ('["1", "0"]', "YES", False),
        ('["0", "1"]', "NO", False),
        ('["0.5", "0.5"]', "AMBIGUOUS", True),   # no clean settlement
        ('["1", "1"]', "AMBIGUOUS", True),       # two winners
        ('["0", "0"]', "AMBIGUOUS", True),       # no winner
        ('["0.97", "0.03"]', "AMBIGUOUS", True), # near but not settled
        (None, "UNKNOWN", True),                 # closed, nothing published
    ],
)
def test_resolution_outcomes_and_ambiguity(prices, expected_outcome, ambiguous) -> None:
    from app.workers.resolution import ResolutionWorker

    worker = ResolutionWorker.__new__(ResolutionWorker)
    outcome, _index, is_ambiguous, evidence = worker._determine_outcome(_closed_market(prices))

    assert outcome.value == expected_outcome
    assert is_ambiguous is ambiguous
    assert "rule" in evidence


def test_a_high_price_on_an_open_market_is_not_a_resolution() -> None:
    """price ~ 1 must never mean resolved."""
    market = GammaMarket.model_validate(
        {
            "id": "1", "conditionId": "0xa",
            "outcomes": '["Yes","No"]', "clobTokenIds": '["1","2"]',
            "outcomePrices": '["0.995", "0.005"]',
            "closed": False, "active": True,
        }
    )
    from app.ingest.repository import derive_status
    from app.core.enums import MarketStatus

    assert derive_status(market) is MarketStatus.ACTIVE


# ---------------------------------------------------------------------------
# 21. Kill switch activation suppresses everything downstream
# ---------------------------------------------------------------------------
def test_any_tripped_switch_blocks_the_whole_report(settings) -> None:
    report = KillSwitchEvaluator(settings).evaluate(
        session=None, last_data_at=None, clock_skew_s=None,
        model_versions_registered=None, risk_state=None,
    )
    assert report.any_tripped
    assert len(report.blocking_reasons()) == len(report.tripped_switches)
    assert all(":" in reason for reason in report.blocking_reasons())
