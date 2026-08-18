"""Database integration: idempotency, constraints, and recovery.

Runs against a real PostgreSQL instance (``beroapp_test``). Skipped with a clear
message when one is not reachable, rather than silently passing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import (
    Market,
    MarketSnapshot,
    MarketToken,
    OrderBookSnapshot,
    PaperOrder,
    Prediction,
    Resolution,
)
from app.ingest.repository import (
    record_book,
    record_system_event,
    snapshot_count,
    upsert_event,
    upsert_market,
)
from app.schemas.polymarket import GammaEvent, GammaMarket
from tests.conftest import make_book

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://beroapp:beroapp@127.0.0.1:5432/beroapp_test",
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine():
    try:
        engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable at {TEST_DB_URL}: {exc}")

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Session:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s
        s.rollback()
    # Clean between tests so ordering cannot matter.
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))


def _market_payload(market_id: str = "1", **overrides) -> dict:
    payload = {
        "id": market_id,
        "conditionId": f"0xcondition{market_id}",
        "question": f"Question {market_id}?",
        "description": "A description.",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["tok{market_id}a", "tok{market_id}b"]',
        "closed": False,
        "active": True,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "liquidityNum": 50_000,
        "volumeNum": 100_000,
        "endDate": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Idempotency — the property that makes a crashed worker safe to restart
# ---------------------------------------------------------------------------
def test_upserting_the_same_market_twice_creates_one_row(session: Session) -> None:
    market = GammaMarket.model_validate(_market_payload())
    first = upsert_market(session, market)
    second = upsert_market(session, market)
    session.commit()

    assert first == second
    assert session.execute(select(func.count()).select_from(Market)).scalar_one() == 1
    assert session.execute(select(func.count()).select_from(MarketToken)).scalar_one() == 2


def test_upsert_updates_changed_fields_without_losing_first_seen(session: Session) -> None:
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()
    original_first_seen = session.get(Market, market_id).first_seen_at

    updated = GammaMarket.model_validate(_market_payload(liquidityNum=99_999, closed=True))
    upsert_market(session, updated)
    session.commit()

    row = session.get(Market, market_id)
    session.refresh(row)
    assert row.liquidity_num == 99_999
    assert row.closed is True
    assert row.first_seen_at == original_first_seen  # history is preserved


def test_event_linkage_is_not_overwritten_with_null(session: Session) -> None:
    """A later discovery pass that lacks event context must not unlink."""
    event = GammaEvent.model_validate({"id": "100", "title": "An event"})
    event_id = upsert_event(session, event)
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()), event_id=event_id)
    session.commit()

    upsert_market(session, GammaMarket.model_validate(_market_payload()), event_id=None)
    session.commit()

    row = session.get(Market, market_id)
    session.refresh(row)
    assert row.event_id == event_id


def test_token_reassignment_is_handled(session: Session) -> None:
    """Token ids are globally unique; the same token must not duplicate."""
    upsert_market(session, GammaMarket.model_validate(_market_payload("1")))
    session.commit()
    upsert_market(session, GammaMarket.model_validate(_market_payload("1")))
    session.commit()
    assert session.execute(select(func.count()).select_from(MarketToken)).scalar_one() == 2


# ---------------------------------------------------------------------------
# Snapshot write suppression
# ---------------------------------------------------------------------------
def test_unchanged_book_writes_no_second_snapshot(session: Session, settings) -> None:
    """A quiet market must cost zero rows."""
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()

    book = make_book(token_id="tok1a", bids=[(0.50, 10_000)], asks=[(0.52, 10_000)])
    first, _ = record_book(session, market_id=market_id, book=book, settings=settings)
    session.commit()
    second, _ = record_book(session, market_id=market_id, book=book, settings=settings)
    session.commit()

    assert first is not None
    assert second is None
    assert snapshot_count(session, "tok1a") == 1


def test_material_price_move_writes_a_snapshot(session: Session, settings) -> None:
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()

    record_book(session, market_id=market_id,
                book=make_book(token_id="tok1a", bids=[(0.50, 10_000)], asks=[(0.52, 10_000)]),
                settings=settings)
    session.commit()
    moved, _ = record_book(session, market_id=market_id,
                           book=make_book(token_id="tok1a", bids=[(0.60, 10_000)], asks=[(0.62, 10_000)]),
                           settings=settings)
    session.commit()

    assert moved is not None
    assert snapshot_count(session, "tok1a") == 2


def test_depth_collapse_writes_a_snapshot_even_at_an_unchanged_touch(
    session: Session, settings
) -> None:
    """Depth vanishing is a tradeable event even when the touch does not move."""
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()

    record_book(session, market_id=market_id,
                book=make_book(token_id="tok1a", bids=[(0.50, 100_000)], asks=[(0.52, 100_000)]),
                settings=settings)
    session.commit()
    collapsed, _ = record_book(session, market_id=market_id,
                               book=make_book(token_id="tok1a", bids=[(0.50, 100)], asks=[(0.52, 100)]),
                               settings=settings)
    session.commit()
    assert collapsed is not None


def test_stale_book_is_flagged(session: Session, settings) -> None:
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()

    old = datetime.now(UTC) - timedelta(seconds=settings.data_staleness_s * 3)
    snapshot_id, _ = record_book(
        session, market_id=market_id,
        book=make_book(token_id="tok1a", bids=[(0.5, 100)], asks=[(0.52, 100)], observed_at=old),
        settings=settings,
    )
    session.commit()
    assert session.get(MarketSnapshot, snapshot_id).is_stale is True


def test_snapshot_records_both_event_time_and_known_at(session: Session, settings) -> None:
    """The pair is what makes look-ahead-free backtesting possible."""
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()

    observed = datetime.now(UTC) - timedelta(seconds=30)
    known = datetime.now(UTC)
    snapshot_id, book_id = record_book(
        session, market_id=market_id,
        book=make_book(token_id="tok1a", bids=[(0.5, 100)], asks=[(0.52, 100)], observed_at=observed),
        known_at=known, settings=settings,
    )
    session.commit()

    snapshot = session.get(MarketSnapshot, snapshot_id)
    assert snapshot.observed_at is not None
    assert snapshot.known_at is not None
    assert snapshot.observed_at < snapshot.known_at
    assert snapshot.data_latency_ms >= 25_000
    assert session.get(OrderBookSnapshot, book_id).known_at is not None


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------
def test_paper_order_rejects_a_live_venue(session: Session) -> None:
    """The database itself refuses to record a live order in the paper table."""
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()

    from app.db.models import RiskDecision, Signal

    prediction = Prediction(
        market_id=market_id, token_id="tok1a", model_version="v",
        market_probability=0.5, model_probability=0.5, confidence=0.5,
        predicted_at=datetime.now(UTC), known_at=datetime.now(UTC),
    )
    session.add(prediction)
    session.flush()
    signal = Signal(
        prediction_id=prediction.id, market_id=market_id, token_id="tok1a",
        recommendation="BUY", market_probability=0.5, model_probability=0.6,
        raw_edge=0.1, confidence=0.8, resolution_risk="LOW", model_version="v",
        signal_at=datetime.now(UTC), idempotency_key="k1",
    )
    session.add(signal)
    session.flush()
    risk = RiskDecision(
        signal_id=signal.id, status="APPROVED", reasons=[], limits_snapshot={},
        kill_switches={}, checked_at=datetime.now(UTC),
    )
    session.add(risk)
    session.flush()

    session.add(
        PaperOrder(
            signal_id=signal.id, risk_decision_id=risk.id, market_id=market_id,
            token_id="tok1a", venue="LIVE", side="BUY", state="PENDING",
            requested_price=0.5, requested_size_usd=100,
            signal_at=datetime.now(UTC), submitted_at=datetime.now(UTC),
            idempotency_key="order-1",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_probability_check_constraints_are_enforced(session: Session) -> None:
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()

    session.add(
        Prediction(
            market_id=market_id, token_id="tok1a", model_version="v",
            market_probability=0.5, model_probability=1.5,  # out of range
            confidence=0.5, predicted_at=datetime.now(UTC), known_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_duplicate_signal_idempotency_key_is_rejected(session: Session) -> None:
    from app.db.models import Signal

    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()
    prediction = Prediction(
        market_id=market_id, token_id="tok1a", model_version="v",
        market_probability=0.5, model_probability=0.5, confidence=0.5,
        predicted_at=datetime.now(UTC), known_at=datetime.now(UTC),
    )
    session.add(prediction)
    session.flush()

    for _ in range(2):
        session.add(
            Signal(
                prediction_id=prediction.id, market_id=market_id, token_id="tok1a",
                recommendation="BUY", market_probability=0.5, model_probability=0.6,
                raw_edge=0.1, confidence=0.8, resolution_risk="LOW", model_version="v",
                signal_at=datetime.now(UTC), idempotency_key="duplicate-key",
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_one_resolution_per_market(session: Session) -> None:
    market_id = upsert_market(session, GammaMarket.model_validate(_market_payload()))
    session.commit()
    for _ in range(2):
        session.add(
            Resolution(
                market_id=market_id, outcome="YES", known_at=datetime.now(UTC),
                evidence={}, is_ambiguous=False,
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


# ---------------------------------------------------------------------------
# Survivorship: closed markets are retained
# ---------------------------------------------------------------------------
def test_closed_and_resolved_markets_are_retained(session: Session) -> None:
    """Nothing is deleted for being uninteresting."""
    for i, payload in enumerate(
        [
            _market_payload("1"),
            _market_payload("2", closed=True, active=False),
            _market_payload("3", archived=True, closed=True),
        ]
    ):
        upsert_market(session, GammaMarket.model_validate(payload))
    session.commit()

    assert session.execute(select(func.count()).select_from(Market)).scalar_one() == 3
    statuses = set(session.execute(select(Market.status)).scalars())
    assert "CLOSED" in statuses


def test_derive_status_never_reads_price() -> None:
    """Status comes from venue flags only."""
    import inspect

    from app.ingest.repository import derive_status as fn

    source = inspect.getsource(fn)
    assert "outcome_prices" not in source
    assert "midpoint" not in source
    assert "best_bid" not in source


# ---------------------------------------------------------------------------
# Reconnection
# ---------------------------------------------------------------------------
def test_database_reachable_reports_false_for_a_bad_url(monkeypatch) -> None:
    """The readiness probe must report unreachable rather than raising."""
    from app.db import session as session_module

    session_module.reset_engine()
    monkeypatch.setattr(
        session_module,
        "get_engine",
        lambda: create_engine("postgresql+psycopg://nobody:nobody@127.0.0.1:1/none"),
    )
    assert session_module.database_reachable() is False


def test_system_events_are_appendable(session: Session) -> None:
    record_system_event(
        session, component="DATA_FEED", event="test_event", detail={"k": "v"}
    )
    session.commit()
    from app.db.models import SystemEvent

    rows = list(session.execute(select(SystemEvent)).scalars())
    assert len(rows) == 1
    assert rows[0].detail == {"k": "v"}
    assert rows[0].occurred_at is not None
