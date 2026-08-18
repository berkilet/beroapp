"""Paper (shadow) execution.

The single property that matters here: a paper fill must never be booked at the
signal price. The spec's example is the test — signal at $0.43 with a $500
allocation must fill at whatever walking the real book produces, after spread,
after latency, with partial fills allowed and a rejection when the depth is not
there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.enums import ExecutionVenue, KillSwitch, OrderState, RiskStatus, Side
from app.engines.authorization import AuthorizationDenied, ExecutionAuthorizationService
from app.engines.killswitch import KillSwitchReport, SwitchState
from app.engines.risk import RiskDecisionResult
from app.execution.paper import PaperExecutionAdapter
from tests.conftest import make_book


@pytest.fixture
def phase2_settings() -> Settings:
    return Settings(
        allow_insecure_local=True,
        api_key="",
        current_phase="PHASE_2",
        paper_latency_ms=1_500,
        max_allowed_slippage=0.02,
        virtual_initial_capital=10_000.0,
    )


def _clear() -> KillSwitchReport:
    return KillSwitchReport(states={s: SwitchState(s, False, "clear") for s in KillSwitch})


def _approved(size: float) -> RiskDecisionResult:
    return RiskDecisionResult(
        status=RiskStatus.APPROVED, reasons=["ok"], approved_size_usd=size,
        limits_snapshot={}, kill_switches={}, checked_at=datetime.now(UTC), risk_latency_ms=1,
    )


def _token(settings: Settings, size: float):
    return ExecutionAuthorizationService(settings).authorize(
        venue=ExecutionVenue.PAPER, signal_id=1, market_id=1, token_id="tok",
        risk_decision=_approved(size), kill_switches=_clear(),
    )


# ---------------------------------------------------------------------------
def test_the_specs_worked_example_is_not_booked_at_the_signal_price(phase2_settings) -> None:
    """Signal BUY YES at $0.43, allocation $500.

    The book's best ask is 0.45, so the fill is at 0.45 and the recorded
    slippage is the 2 cents the signal price failed to anticipate. Booking this
    at 0.43 would silently invent 2 cents of edge on every trade.
    """
    book = make_book(asks=[(0.45, 100_000)], bids=[(0.43, 100_000)])
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0),
        side=Side.BUY,
        signal_price=0.43,
        signal_at=datetime.now(UTC),
        signal_book=book,
    )

    assert fill.state is OrderState.FILLED
    assert fill.simulated_fill_price == pytest.approx(0.45)
    assert fill.simulated_fill_price != 0.43
    assert fill.slippage == pytest.approx(0.02)
    assert fill.filled_size_usd == pytest.approx(500.0)


def test_fill_walks_multiple_levels_when_the_touch_is_thin(phase2_settings) -> None:
    book = make_book(
        asks=[(0.50, 200), (0.52, 200), (0.55, 2_000)],  # $100, $104, $1100
        bids=[(0.48, 10_000)],
    )
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0),
        side=Side.BUY, signal_price=0.50,
        signal_at=datetime.now(UTC), signal_book=book,
    )
    assert fill.levels_consumed == 3
    assert 0.50 < fill.simulated_fill_price < 0.55
    assert fill.slippage > 0


def test_partial_fill_is_recorded_as_partial(phase2_settings) -> None:
    book = make_book(asks=[(0.50, 200)], bids=[(0.48, 10_000)])  # $100 available
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0),
        side=Side.BUY, signal_price=0.50,
        signal_at=datetime.now(UTC), signal_book=book,
    )
    assert fill.state is OrderState.PARTIALLY_FILLED
    assert fill.is_partial is True
    assert fill.filled_size_usd == pytest.approx(100.0)


def test_no_depth_is_a_rejection_not_a_zero_price_fill(phase2_settings) -> None:
    book = make_book(asks=[], bids=[(0.48, 1_000)])
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0),
        side=Side.BUY, signal_price=0.50,
        signal_at=datetime.now(UTC), signal_book=book,
    )
    assert fill.state is OrderState.REJECTED
    assert fill.simulated_fill_price is None
    assert fill.filled_size_usd == 0.0
    assert "no depth" in fill.reject_reason


def test_excessive_slippage_is_rejected_rather_than_absorbed(phase2_settings) -> None:
    """A fill far worse than the signal assumed is not a fill we would take."""
    book = make_book(asks=[(0.60, 100_000)], bids=[(0.40, 100_000)])
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0),
        side=Side.BUY, signal_price=0.45,  # 15 cents of slippage
        signal_at=datetime.now(UTC), signal_book=book,
    )
    assert fill.state is OrderState.REJECTED
    assert "slippage" in fill.reject_reason
    assert fill.filled_size_usd == 0.0


def test_latency_book_is_preferred_and_recorded(phase2_settings) -> None:
    """The honest simulation uses the book our order would actually have met.

    Here the price moves against us during the latency window, which is exactly
    how a real order loses the edge it was chasing.
    """
    signal_book = make_book(asks=[(0.45, 100_000)], bids=[(0.43, 100_000)])
    later_book = make_book(asks=[(0.50, 100_000)], bids=[(0.48, 100_000)])

    adapter = PaperExecutionAdapter(phase2_settings)
    optimistic = adapter.submit(
        _token(phase2_settings, 500.0), side=Side.BUY, signal_price=0.45,
        signal_at=datetime.now(UTC), signal_book=signal_book,
    )
    honest = adapter.submit(
        _token(phase2_settings, 500.0), side=Side.BUY, signal_price=0.45,
        signal_at=datetime.now(UTC), signal_book=signal_book, post_latency_book=later_book,
    )

    assert optimistic.book_used == "signal_time"
    assert honest.book_used == "post_latency"
    assert honest.simulated_fill_price > optimistic.simulated_fill_price


def test_optimistic_fill_is_flagged_so_the_bias_is_visible(phase2_settings) -> None:
    """When no post-latency book exists we say so rather than hiding it."""
    book = make_book(asks=[(0.45, 100_000)], bids=[(0.43, 100_000)])
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0), side=Side.BUY, signal_price=0.45,
        signal_at=datetime.now(UTC), signal_book=book,
    )
    assert fill.book_used == "signal_time"


def test_selling_walks_bids(phase2_settings) -> None:
    book = make_book(bids=[(0.55, 100_000)], asks=[(0.57, 100_000)])
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0), side=Side.SELL, signal_price=0.56,
        signal_at=datetime.now(UTC), signal_book=book,
    )
    assert fill.simulated_fill_price == pytest.approx(0.55)
    # Selling below the signal price is positive slippage: it cost us.
    assert fill.slippage == pytest.approx(0.01)


def test_latency_is_recorded_on_every_fill(phase2_settings) -> None:
    book = make_book(asks=[(0.45, 100_000)], bids=[(0.43, 100_000)])
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0), side=Side.BUY, signal_price=0.45,
        signal_at=datetime.now(UTC), signal_book=book,
    )
    assert fill.execution_latency_ms == phase2_settings.paper_latency_ms


def test_shares_and_notional_are_consistent(phase2_settings) -> None:
    """shares * average price must equal the notional filled."""
    book = make_book(asks=[(0.40, 500), (0.50, 5_000)], bids=[(0.38, 10_000)])
    fill = PaperExecutionAdapter(phase2_settings).submit(
        _token(phase2_settings, 500.0), side=Side.BUY, signal_price=0.40,
        signal_at=datetime.now(UTC), signal_book=book,
    )
    assert fill.filled_shares * fill.simulated_fill_price == pytest.approx(
        fill.filled_size_usd, rel=1e-9
    )


# ---------------------------------------------------------------------------
# The adapter will not act without a valid token
# ---------------------------------------------------------------------------
def test_adapter_refuses_a_forged_token(phase2_settings) -> None:
    from app.engines.authorization import ExecutionToken

    forged = ExecutionToken(
        venue=ExecutionVenue.PAPER, signal_id=1, market_id=1, token_id="t",
        size_usd=1_000_000.0, issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=1), signature="0" * 64,
    )
    with pytest.raises(AuthorizationDenied):
        PaperExecutionAdapter(phase2_settings).submit(
            forged, side=Side.BUY, signal_price=0.5,
            signal_at=datetime.now(UTC),
            signal_book=make_book(asks=[(0.5, 100_000)], bids=[(0.48, 100_000)]),
        )


def test_no_paper_execution_is_possible_in_phase_1(settings) -> None:
    """Phase 1 does not simulate execution at all."""
    with pytest.raises(AuthorizationDenied, match="PHASE_2"):
        _token(settings, 500.0)
