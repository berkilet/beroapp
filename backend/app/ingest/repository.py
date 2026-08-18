"""Persistence for ingested data.

All writes are idempotent: a worker that dies mid-cycle and restarts must be
able to re-run the same cycle without creating duplicates or corrupting state.
That is achieved with natural keys (gamma ids, token ids, book hashes) and
PostgreSQL ``ON CONFLICT`` rather than with read-then-write races.

Snapshot writes are conditional. A market whose book has not moved materially
since the last snapshot costs zero rows — the spec is explicit that we must not
write every second when nothing changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import MarketStatus
from app.core.logging import get_logger
from app.db.models import (
    Event,
    Market,
    MarketSnapshot,
    MarketToken,
    OrderBookSnapshot,
    SystemEvent,
)
from app.engines.classification import classify
from app.engines.liquidity import profile_book
from app.schemas.polymarket import GammaEvent, GammaMarket, OrderBook

log = get_logger("ingest.repository")


def derive_status(market: GammaMarket) -> MarketStatus:
    """Map venue flags onto our status vocabulary.

    Note what this does *not* do: it never infers resolution from price. A
    market trading at 0.99 is a market trading at 0.99, not a resolved one. Only
    the venue's own closed/archived flags move a market out of ACTIVE, and the
    actual outcome comes from the resolution worker.
    """
    if market.archived:
        return MarketStatus.CANCELLED if not market.closed else MarketStatus.CLOSED
    if market.closed:
        return MarketStatus.CLOSED
    if market.active is False:
        return MarketStatus.INVALID
    if market.accepting_orders is False:
        return MarketStatus.CLOSING
    if market.active:
        return MarketStatus.ACTIVE
    return MarketStatus.UNKNOWN


def upsert_event(session: Session, event: GammaEvent, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    values = {
        "gamma_event_id": event.gamma_event_id,
        "ticker": event.ticker,
        "slug": event.slug,
        "title": event.title,
        "description": event.description,
        "tags": [t.model_dump() for t in event.tags],
        "neg_risk": event.neg_risk,
        "active": event.active,
        "closed": event.closed,
        "archived": event.archived,
        "liquidity": event.liquidity,
        "volume": event.volume,
        "open_interest": event.open_interest,
        "start_date": event.start_date,
        "end_date": event.end_date,
        "source_updated_at": event.source_updated_at,
        "first_seen_at": now,
        "last_seen_at": now,
    }
    stmt = (
        pg_insert(Event)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Event.gamma_event_id],
            set_={k: v for k, v in values.items() if k != "first_seen_at"},
        )
        .returning(Event.id)
    )
    return session.execute(stmt).scalar_one()


def upsert_market(
    session: Session,
    market: GammaMarket,
    *,
    event_id: int | None = None,
    tag_slugs: list[str] | None = None,
    tag_labels: list[str] | None = None,
    now: datetime | None = None,
) -> int:
    """Insert or update a market, classify it, and register its tokens."""
    now = now or datetime.now(UTC)
    classification = classify(
        question=market.question, tag_slugs=tag_slugs, tag_labels=tag_labels
    )

    values = {
        "gamma_market_id": market.gamma_market_id,
        "condition_id": market.condition_id,
        "question_id": market.question_id,
        "event_id": event_id,
        "slug": market.slug,
        "question": market.question,
        "description": market.description,
        "group_item_title": market.group_item_title,
        "resolution_source": market.resolution_source,
        "resolved_by": market.resolved_by,
        "uma_resolution_statuses": market.uma_resolution_statuses,
        "outcomes": market.outcomes,
        "category": classification.category.value,
        "category_confidence": classification.confidence,
        "status": derive_status(market).value,
        "active": market.active,
        "closed": market.closed,
        "archived": market.archived,
        "accepting_orders": market.accepting_orders,
        "enable_order_book": market.enable_order_book,
        "neg_risk": market.neg_risk,
        "neg_risk_market_id": market.neg_risk_market_id,
        "liquidity_num": market.liquidity_num,
        "volume_num": market.volume_num,
        "volume_24hr": market.volume_24hr,
        "order_min_size": market.order_min_size,
        "tick_size": market.tick_size,
        "start_date": market.start_date,
        "end_date": market.end_date,
        "source_created_at": market.source_created_at,
        "source_updated_at": market.source_updated_at,
        "first_seen_at": now,
        "last_seen_at": now,
    }

    update_set = {k: v for k, v in values.items() if k != "first_seen_at"}
    # Never let a later discovery pass overwrite an event linkage with NULL.
    if event_id is None:
        update_set.pop("event_id", None)

    stmt = (
        pg_insert(Market)
        .values(**values)
        .on_conflict_do_update(index_elements=[Market.gamma_market_id], set_=update_set)
        .returning(Market.id)
    )
    market_id = session.execute(stmt).scalar_one()

    _upsert_tokens(session, market_id, market)
    return market_id


def _upsert_tokens(session: Session, market_id: int, market: GammaMarket) -> None:
    for index, token_id in enumerate(market.clob_token_ids):
        outcome = market.outcomes[index] if index < len(market.outcomes) else f"OUTCOME_{index}"
        stmt = (
            pg_insert(MarketToken)
            .values(
                market_id=market_id,
                token_id=token_id,
                outcome=outcome,
                outcome_index=index,
            )
            .on_conflict_do_update(
                index_elements=[MarketToken.token_id],
                set_={"outcome": outcome, "outcome_index": index, "market_id": market_id},
            )
        )
        session.execute(stmt)


def latest_snapshot(session: Session, token_id: str) -> MarketSnapshot | None:
    return session.execute(
        select(MarketSnapshot)
        .where(MarketSnapshot.token_id == token_id)
        .order_by(MarketSnapshot.known_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _materially_changed(
    previous: MarketSnapshot | None, book: OrderBook, min_change: float
) -> bool:
    """Decide whether this book is worth a row.

    Anything structural (a side appearing or disappearing) always counts. Price
    moves count when they exceed the configured tick threshold. Depth changes
    count when they are large in relative terms, because depth collapsing is a
    tradeable event even at an unchanged touch.
    """
    if previous is None:
        return True

    bid, ask = book.best_bid, book.best_ask
    if (previous.best_bid is None) != (bid is None):
        return True
    if (previous.best_ask is None) != (ask is None):
        return True

    if bid is not None and previous.best_bid is not None and abs(bid - previous.best_bid) >= min_change:
        return True
    if ask is not None and previous.best_ask is not None and abs(ask - previous.best_ask) >= min_change:
        return True

    prev_depth = (previous.bid_depth_usd or 0.0) + (previous.ask_depth_usd or 0.0)
    new_depth = book.depth_usd("bid") + book.depth_usd("ask")
    if prev_depth > 0 and abs(new_depth - prev_depth) / prev_depth > 0.25:
        return True

    return False


def record_book(
    session: Session,
    *,
    market_id: int,
    book: OrderBook,
    known_at: datetime | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> tuple[int | None, int | None]:
    """Persist a book if it moved. Returns (snapshot_id, order_book_snapshot_id).

    Both are None when the book was unchanged and the write was skipped.
    """
    settings = settings or get_settings()
    known_at = known_at or datetime.now(UTC)
    observed_at = book.observed_at or known_at

    previous = latest_snapshot(session, book.token_id)
    if not force and not _materially_changed(previous, book, settings.snapshot_min_price_change):
        return None, None

    profile = profile_book(book)
    age_s = (known_at - observed_at).total_seconds()
    is_stale = age_s > settings.data_staleness_s

    book_row = OrderBookSnapshot(
        market_id=market_id,
        token_id=book.token_id,
        observed_at=observed_at,
        known_at=known_at,
        book_hash=book.book_hash,
        bids=[{"price": lvl.price, "size": lvl.size} for lvl in book.bids],
        asks=[{"price": lvl.price, "size": lvl.size} for lvl in book.asks],
    )
    session.add(book_row)
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market_id,
        token_id=book.token_id,
        observed_at=observed_at,
        known_at=known_at,
        best_bid=profile.best_bid,
        best_ask=profile.best_ask,
        midpoint=profile.midpoint,
        spread=profile.spread,
        last_trade_price=book.last_trade_price,
        bid_depth_usd=profile.bid_depth_usd,
        ask_depth_usd=profile.ask_depth_usd,
        book_imbalance=profile.imbalance,
        tick_size=book.tick_size,
        is_stale=is_stale,
        data_latency_ms=int(max(0.0, age_s) * 1000),
    )
    session.add(snapshot)
    session.flush()
    return snapshot.id, book_row.id


def snapshot_count(session: Session, token_id: str) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(MarketSnapshot).where(MarketSnapshot.token_id == token_id)
        ).scalar_one()
    )


def latest_data_timestamp(session: Session) -> datetime | None:
    """Newest observation across the whole feed. Drives DATA_KILL_SWITCH."""
    return session.execute(select(func.max(MarketSnapshot.known_at))).scalar_one_or_none()


def record_system_event(
    session: Session,
    *,
    component: str,
    event: str,
    severity: str = "INFO",
    health: str | None = None,
    market_id: int | None = None,
    error_code: str | None = None,
    detail: dict | None = None,
    correlation_id: str | None = None,
    duration_ms: int | None = None,
) -> None:
    session.add(
        SystemEvent(
            component=component,
            event=event,
            severity=severity,
            health=health,
            market_id=market_id,
            error_code=error_code,
            detail=detail,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
            occurred_at=datetime.now(UTC),
        )
    )


def prune_stale_market_flags(session: Session, *, older_than_hours: int = 48) -> int:
    """Mark markets we have stopped seeing in discovery.

    They are never deleted — that would reintroduce survivorship bias. They are
    only flagged so the modelability filter stops treating them as live.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    result = session.execute(
        select(Market).where(
            Market.last_seen_at < cutoff,
            Market.status.in_([MarketStatus.ACTIVE.value, MarketStatus.CLOSING.value]),
        )
    )
    count = 0
    for market in result.scalars():
        market.status = MarketStatus.UNKNOWN.value
        count += 1
    return count
