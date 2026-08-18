"""Market discovery worker.

Walks Polymarket's active universe via Gamma ``/events``, which gives us the
markets *and* the tags that drive classification in one request family. Events
are paged until the venue stops returning new ones or we hit the configured
page ceiling.

Nothing is hard-coded: no market ids, no event ids, no fixed universe. The
platform discovers what exists and records everything it finds, including
markets it will never trade — that record is what makes later performance
analysis free of survivorship bias.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.core.enums import ComponentHealth, SystemComponent
from app.core.logging import get_logger
from app.db.models import Market, MarketToken
from app.db.session import session_scope
from app.ingest.polymarket import PolymarketClient
from app.ingest.repository import (
    prune_stale_market_flags,
    record_system_event,
    upsert_event,
    upsert_market,
)

log = get_logger("workers.discovery")


class DiscoveryWorker:
    def __init__(self, client: PolymarketClient, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or get_settings()

    async def run_once(self) -> dict:
        started = datetime.now(UTC)
        stats = {
            "events_seen": 0,
            "markets_upserted": 0,
            "markets_rejected": 0,
            "pages": 0,
            "parse_error_rate": 0.0,
        }

        page_size = self.settings.discovery_page_size
        total_accepted = 0
        total_rejected = 0

        for page in range(self.settings.discovery_max_pages):
            events, report = await self.client.list_events(
                closed=False, limit=page_size, offset=page * page_size
            )
            total_accepted += report.accepted
            total_rejected += report.rejected
            stats["pages"] += 1

            if not events:
                break

            with session_scope() as session:
                for event in events:
                    stats["events_seen"] += 1
                    event_row_id = upsert_event(session, event)

                    tag_slugs = [t.slug for t in event.tags if t.slug]
                    tag_labels = [t.label for t in event.tags if t.label]

                    for market in event.markets:
                        try:
                            upsert_market(
                                session,
                                market,
                                event_id=event_row_id,
                                tag_slugs=tag_slugs,
                                tag_labels=tag_labels,
                            )
                            stats["markets_upserted"] += 1
                        except Exception as exc:  # noqa: BLE001
                            # One bad market must not abort the page.
                            stats["markets_rejected"] += 1
                            log.warning(
                                "market upsert failed",
                                extra={
                                    "event": "market_upsert_failed",
                                    "error_code": type(exc).__name__,
                                    "detail": {"gamma_market_id": market.gamma_market_id},
                                },
                            )

            if len(events) < page_size:
                break

        total = total_accepted + total_rejected
        stats["parse_error_rate"] = (total_rejected / total) if total else 0.0
        stats["markets_rejected"] += total_rejected

        with session_scope() as session:
            stale = prune_stale_market_flags(session)
            stats["markets_flagged_unseen"] = stale

            health = (
                ComponentHealth.HEALTHY
                if stats["markets_upserted"] > 0
                and stats["parse_error_rate"] <= self.settings.gate1_max_parse_error_rate
                else ComponentHealth.DEGRADED
            )
            record_system_event(
                session,
                component=SystemComponent.MARKET_DISCOVERY.value,
                event="discovery_cycle",
                health=health.value,
                detail=stats,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )

        log.info("discovery cycle complete", extra={"event": "discovery_cycle", "detail": stats})
        return stats


def _active_token_query():
    """Tokens worth polling. Excludes closed, archived and book-less markets."""
    return (
        select(MarketToken.token_id)
        .join(Market, Market.id == MarketToken.market_id)
        .where(
            Market.closed.is_(False),
            Market.archived.is_(False),
            Market.enable_order_book.is_(True),
            Market.accepting_orders.is_(True),
        )
    )


def active_token_ids(limit: int | None = None) -> list[str]:
    """Tokens to poll this cycle, most liquid first.

    The ordering is what makes a capped budget defensible: when we cannot poll
    everything within the cadence, we spend the budget where an executable edge
    could plausibly exist. Markets below the cut are still discovered, stored and
    retained — they are sampled less often, not dropped.
    """
    with session_scope() as session:
        stmt = _active_token_query().order_by(func.coalesce(Market.liquidity_num, 0).desc())
        if limit:
            stmt = stmt.limit(limit)
        return list(session.execute(stmt).scalars())


def count_active_tokens() -> int:
    """Size of the pollable universe, for coverage reporting."""
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count()).select_from(_active_token_query().subquery())
            ).scalar_one()
        )
