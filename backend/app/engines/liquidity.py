"""Liquidity and execution-cost engine.

This module answers one question the displayed price cannot: *if we actually
tried to trade this, what would we get?*

The core operation is walking the book. To buy `N` USD of a token you consume
ask levels from the best price upward until the notional is filled; your
average fill price is the size-weighted mean of the levels you consumed, and
your slippage is that average minus the best ask. If the book runs out before
`N` is filled, the order is **partial** — and a partial fill is reported as
such, never quietly rounded up to a full one.

Everything here is arithmetic on observed data. Nothing is estimated from a
model, and no figure is invented when the book is empty: an empty book yields
`None`, which downstream code must handle as "cannot execute".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import Side
from app.schemas.polymarket import BookLevel, OrderBook


@dataclass(frozen=True)
class ExecutionEstimate:
    """What we expect to get if we send this order right now."""

    side: Side
    requested_size_usd: float
    fillable_size_usd: float
    average_price: float | None
    reference_price: float
    """Best price on the side we cross — the price a naive system would assume."""
    slippage: float
    """average_price - reference_price for a buy; reference - average for a sell.
    Always signed so that positive means 'worse than the touch'."""
    shares: float
    levels_consumed: int
    is_partial: bool
    fill_ratio: float

    @property
    def is_executable(self) -> bool:
        return self.average_price is not None and self.fillable_size_usd > 0


@dataclass(frozen=True)
class LiquidityProfile:
    """Static description of a book's depth, independent of any order."""

    best_bid: float | None
    best_ask: float | None
    midpoint: float | None
    spread: float | None
    spread_pct: float | None
    bid_depth_usd: float
    ask_depth_usd: float
    total_depth_usd: float
    imbalance: float | None
    bid_levels: int
    ask_levels: int

    @property
    def has_two_sided_market(self) -> bool:
        return self.best_bid is not None and self.best_ask is not None


def profile_book(book: OrderBook) -> LiquidityProfile:
    bid, ask, mid = book.best_bid, book.best_ask, book.midpoint
    spread = book.spread
    # Spread as a fraction of midpoint. A 1-cent spread means something very
    # different at 0.50 than at 0.02, and the modelability filter needs to know.
    spread_pct = (spread / mid) if (spread is not None and mid and mid > 0) else None

    bid_usd = book.depth_usd("bid")
    ask_usd = book.depth_usd("ask")
    return LiquidityProfile(
        best_bid=bid,
        best_ask=ask,
        midpoint=mid,
        spread=spread,
        spread_pct=spread_pct,
        bid_depth_usd=bid_usd,
        ask_depth_usd=ask_usd,
        total_depth_usd=bid_usd + ask_usd,
        imbalance=book.imbalance,
        bid_levels=len(book.bids),
        ask_levels=len(book.asks),
    )


def _sorted_levels(levels: list[BookLevel], side: Side) -> list[BookLevel]:
    """Order levels best-first.

    Buying crosses asks, so cheapest first. Selling crosses bids, so highest
    first. Venue ordering is not trusted (see docs/DATA_SOURCES.md).
    """
    return sorted(levels, key=lambda lvl: lvl.price, reverse=(side is Side.SELL))


def estimate_execution(
    book: OrderBook, *, side: Side, size_usd: float
) -> ExecutionEstimate | None:
    """Walk the book for `size_usd` of notional.

    Returns None when the relevant side is empty — there is no price at which
    this trade could happen, and inventing one would be fabrication.
    """
    if size_usd <= 0:
        return None

    levels = _sorted_levels(book.asks if side is Side.BUY else book.bids, side)
    if not levels:
        return None

    reference_price = levels[0].price
    remaining = size_usd
    notional_filled = 0.0
    shares_filled = 0.0
    consumed = 0

    for level in levels:
        if remaining <= 1e-12:
            break
        if level.price <= 0 or level.size <= 0:
            continue
        level_notional = level.price * level.size
        take = min(level_notional, remaining)
        shares_filled += take / level.price
        notional_filled += take
        remaining -= take
        consumed += 1

    if notional_filled <= 0 or shares_filled <= 0:
        return None

    average_price = notional_filled / shares_filled
    slippage = (
        average_price - reference_price if side is Side.BUY else reference_price - average_price
    )

    return ExecutionEstimate(
        side=side,
        requested_size_usd=size_usd,
        fillable_size_usd=notional_filled,
        average_price=average_price,
        reference_price=reference_price,
        slippage=slippage,
        shares=shares_filled,
        levels_consumed=consumed,
        is_partial=notional_filled < size_usd - 1e-9,
        fill_ratio=notional_filled / size_usd,
    )


def executable_probability(book: OrderBook, *, side: Side, size_usd: float) -> float | None:
    """The market-implied probability we could *actually* transact at.

    For a YES token, buying at average price p implies paying p for a claim
    worth 1 if the event happens — so p is the probability the market charges
    us, not the probability it advertises. This is the number the edge engine
    must compare against, and it is strictly worse than the midpoint.
    """
    estimate = estimate_execution(book, side=side, size_usd=size_usd)
    if estimate is None or estimate.average_price is None:
        return None
    return estimate.average_price


def execution_probability(estimate: ExecutionEstimate | None) -> float:
    """Likelihood the intended order completes as modelled, in [0,1].

    A partial fill is the dominant failure mode in a thin prediction market, so
    fill ratio is the main term; consuming many levels is penalised because a
    deep walk is more exposed to the book moving underneath us.
    """
    if estimate is None or not estimate.is_executable:
        return 0.0
    depth_penalty = min(0.25, 0.03 * max(0, estimate.levels_consumed - 1))
    return max(0.0, min(1.0, estimate.fill_ratio - depth_penalty))
