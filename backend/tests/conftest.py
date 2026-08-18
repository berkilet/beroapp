"""Shared test fixtures.

Every test runs against a settings object built explicitly here rather than
against the ambient environment, so a test can never accidentally pass because
of a value in someone's `.env`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("ALLOW_INSECURE_LOCAL", "true")

from app.core.config import Settings  # noqa: E402
from app.schemas.polymarket import BookLevel, OrderBook  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings. Note the safety defaults are NOT overridden."""
    return Settings(
        environment="test",
        allow_insecure_local=True,
        api_key="",
        database_url="postgresql+psycopg://beroapp:beroapp@127.0.0.1:5432/beroapp_test",
        min_liquidity=1_000.0,
        max_spread=0.05,
        min_executable_edge=0.02,
        min_confidence=0.55,
        reference_order_size_usd=500.0,
        virtual_initial_capital=10_000.0,
        min_market_age_hours=24.0,
    )


def make_book(
    *,
    token_id: str = "token-1",
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    observed_at: datetime | None = None,
) -> OrderBook:
    """Build an OrderBook directly, bypassing venue-shaped validation.

    Levels are given as (price, size) tuples in any order — the code under test
    must not depend on ordering.
    """
    return OrderBook.model_construct(
        token_id=token_id,
        condition_id="0xcondition",
        observed_at=observed_at or datetime.now(UTC),
        book_hash="hash",
        bids=[BookLevel.model_construct(price=p, size=s) for p, s in (bids or [])],
        asks=[BookLevel.model_construct(price=p, size=s) for p, s in (asks or [])],
        tick_size=0.001,
        min_order_size=5.0,
        neg_risk=False,
        last_trade_price=None,
    )


@pytest.fixture
def liquid_book() -> OrderBook:
    """A realistically shaped book around a 0.56 midpoint."""
    return make_book(
        bids=[(0.555, 20_000), (0.550, 40_000), (0.540, 60_000)],
        asks=[(0.565, 20_000), (0.570, 40_000), (0.580, 60_000)],
    )


@pytest.fixture
def thin_book() -> OrderBook:
    """Wide and shallow: the kind of book that destroys a paper edge."""
    return make_book(bids=[(0.40, 100)], asks=[(0.70, 100)])


@pytest.fixture
def future_date() -> datetime:
    return datetime.now(UTC) + timedelta(days=30)
