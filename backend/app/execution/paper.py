"""Paper (shadow) execution adapter.

Simulates a fill against the **recorded order book**, not against the signal
price. The spec is explicit and it matters: a signal at $0.43 with a $500
allocation must not be booked at $0.43. It is booked at what walking the real
book would have produced, after the real spread, after modelled latency, and
with partial fills allowed.

Latency is modelled as a delay between the signal and the execution attempt.
Where a later book snapshot exists within that window, the fill is simulated
against *that* book rather than the one the signal saw — which is precisely how
a real order loses the edge it was chasing. Where no later snapshot exists, the
signal's own book is used and the fill is flagged so the optimism is visible in
the record rather than hidden in it.

Nothing here touches money. `paper_orders.venue` carries a CHECK constraint
pinning it to PAPER at the database level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, get_settings
from app.core.enums import ExecutionVenue, OrderState, Side
from app.engines.authorization import ExecutionToken, verify_token
from app.engines.liquidity import estimate_execution
from app.schemas.polymarket import OrderBook

_PRICE_EPSILON = 1e-9
"""Tolerance for comparing prices against configured limits.

Polymarket ticks are 0.001 at the finest, so a nanoprice of slack cannot admit
a materially worse fill; it only absorbs binary-float representation error.
"""


@dataclass
class SimulatedFill:
    state: OrderState
    side: Side
    simulated_fill_price: float | None
    filled_size_usd: float
    filled_shares: float
    slippage: float
    fees: float
    is_partial: bool
    levels_consumed: int
    execution_latency_ms: int
    book_used: str
    """Which book the fill was simulated against: 'post_latency' or 'signal_time'."""
    reject_reason: str | None = None


class PaperExecutionAdapter:
    """Only acts on a token minted by the authorization service."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def submit(
        self,
        token: ExecutionToken,
        *,
        side: Side,
        signal_price: float,
        signal_at: datetime,
        signal_book: OrderBook,
        post_latency_book: OrderBook | None = None,
        now: datetime | None = None,
    ) -> SimulatedFill:
        verify_token(token, expected_venue=ExecutionVenue.PAPER)

        now = now or datetime.now(UTC)
        latency_ms = self.settings.paper_latency_ms
        execution_time = signal_at + timedelta(milliseconds=latency_ms)

        # Prefer the book as it stood *after* our latency. That is the honest
        # simulation: it is the book our order would actually have met.
        if post_latency_book is not None:
            book = post_latency_book
            book_used = "post_latency"
        else:
            book = signal_book
            book_used = "signal_time"

        estimate = estimate_execution(book, side=side, size_usd=token.size_usd)

        if estimate is None or estimate.average_price is None:
            return SimulatedFill(
                state=OrderState.REJECTED,
                side=side,
                simulated_fill_price=None,
                filled_size_usd=0.0,
                filled_shares=0.0,
                slippage=0.0,
                fees=0.0,
                is_partial=False,
                levels_consumed=0,
                execution_latency_ms=latency_ms,
                book_used=book_used,
                reject_reason="no depth available on the required side at execution time",
            )

        fill_price = estimate.average_price

        # Slippage measured against the price the signal was based on, which is
        # what actually erodes the edge — not against the touch at fill time.
        realised_slippage = (
            fill_price - signal_price if side is Side.BUY else signal_price - fill_price
        )

        # Compared with a tolerance because these are binary-float prices:
        # 0.45 - 0.43 evaluates to 0.020000000000000018, which would otherwise
        # reject a fill that is exactly at the configured limit.
        if realised_slippage > self.settings.max_allowed_slippage + _PRICE_EPSILON:
            return SimulatedFill(
                state=OrderState.REJECTED,
                side=side,
                simulated_fill_price=fill_price,
                filled_size_usd=0.0,
                filled_shares=0.0,
                slippage=realised_slippage,
                fees=0.0,
                is_partial=False,
                levels_consumed=estimate.levels_consumed,
                execution_latency_ms=latency_ms,
                book_used=book_used,
                reject_reason=(
                    f"realised slippage {realised_slippage:.4f} exceeds "
                    f"MAX_ALLOWED_SLIPPAGE {self.settings.max_allowed_slippage}"
                ),
            )

        fees = estimate.fillable_size_usd * (self.settings.paper_fee_bps / 10_000.0)

        return SimulatedFill(
            state=OrderState.PARTIALLY_FILLED if estimate.is_partial else OrderState.FILLED,
            side=side,
            simulated_fill_price=fill_price,
            filled_size_usd=estimate.fillable_size_usd,
            filled_shares=estimate.shares,
            slippage=realised_slippage,
            fees=fees,
            is_partial=estimate.is_partial,
            levels_consumed=estimate.levels_consumed,
            execution_latency_ms=int((execution_time - signal_at).total_seconds() * 1000),
            book_used=book_used,
        )
