"""Market-data snapshot worker.

Polls ``POST /books`` in batches for the active token universe, validates every
book, and writes a snapshot only when something moved materially.

Two properties are load-bearing:

* **Batching.** N tokens cost ceil(N / book_batch_size) requests, not N. With
  the default batch of 50 a universe of 2,000 tokens is 40 requests per cycle,
  which is comfortably inside the documented ``/books`` budget of 500 per 10s.
* **Partial failure is survivable.** A batch that fails is logged and skipped;
  the remaining batches still run. A cycle that collects most of the universe
  is far better than one that collects none because a single request 500'd.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.enums import ComponentHealth, SystemComponent
from app.core.logging import get_logger
from app.db.models import MarketToken
from app.db.session import session_scope
from app.ingest.http import FetchError
from app.ingest.polymarket import PolymarketClient
from app.ingest.repository import record_book, record_system_event
from app.workers.discovery import active_token_ids

log = get_logger("workers.snapshot")


class SnapshotWorker:
    def __init__(self, client: PolymarketClient, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or get_settings()
        self.consecutive_batch_failures = 0
        self.last_clock_skew_s: float | None = None

    async def run_once(self, *, token_limit: int | None = None) -> dict:
        started = datetime.now(UTC)
        tokens = active_token_ids(limit=token_limit)

        stats = {
            "tokens_requested": len(tokens),
            "books_received": 0,
            "books_rejected": 0,
            "snapshots_written": 0,
            "snapshots_skipped_unchanged": 0,
            "batches_ok": 0,
            "batches_failed": 0,
            "stale_books": 0,
        }

        if not tokens:
            with session_scope() as session:
                record_system_event(
                    session,
                    component=SystemComponent.DATA_FEED.value,
                    event="snapshot_cycle",
                    severity="WARNING",
                    health=ComponentHealth.DEGRADED.value,
                    detail={**stats, "reason": "no active tokens; run discovery first"},
                )
            return stats

        await self._measure_clock_skew()

        token_to_market = self._token_market_map(tokens)
        batch_size = self.settings.book_batch_size

        for offset in range(0, len(tokens), batch_size):
            batch = tokens[offset : offset + batch_size]
            try:
                books, report = await self.client.get_books(batch)
            except FetchError as exc:
                # A failed batch is skipped, not retried inline — the fetcher has
                # already exhausted its own retry budget, and blocking the whole
                # cycle on one bad batch would starve every other market.
                self.consecutive_batch_failures += 1
                stats["batches_failed"] += 1
                log.warning(
                    "book batch failed",
                    extra={
                        "event": "book_batch_failed",
                        "error_code": exc.error_code,
                        "detail": {"batch_size": len(batch), "offset": offset},
                    },
                )
                continue

            self.consecutive_batch_failures = 0
            stats["batches_ok"] += 1
            stats["books_received"] += report.accepted
            stats["books_rejected"] += report.rejected

            known_at = datetime.now(UTC)
            with session_scope() as session:
                for book in books:
                    market_id = token_to_market.get(book.token_id)
                    if market_id is None:
                        # A book for a token we do not track: ignore rather than
                        # inventing a market row for it.
                        continue
                    snapshot_id, _ = record_book(
                        session,
                        market_id=market_id,
                        book=book,
                        known_at=known_at,
                        settings=self.settings,
                    )
                    if snapshot_id is None:
                        stats["snapshots_skipped_unchanged"] += 1
                    else:
                        stats["snapshots_written"] += 1
                        if book.observed_at and (
                            (known_at - book.observed_at).total_seconds()
                            > self.settings.data_staleness_s
                        ):
                            stats["stale_books"] += 1

        health = self._health(stats)
        stats["clock_skew_s"] = self.last_clock_skew_s

        with session_scope() as session:
            record_system_event(
                session,
                component=SystemComponent.DATA_FEED.value,
                event="snapshot_cycle",
                severity="INFO" if health is ComponentHealth.HEALTHY else "WARNING",
                health=health.value,
                detail=stats,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )

        log.info("snapshot cycle complete", extra={"event": "snapshot_cycle", "detail": stats})
        return stats

    # ------------------------------------------------------------------
    def _token_market_map(self, tokens: list[str]) -> dict[str, int]:
        with session_scope() as session:
            rows = session.execute(
                select(MarketToken.token_id, MarketToken.market_id).where(
                    MarketToken.token_id.in_(tokens)
                )
            ).all()
        return {token_id: market_id for token_id, market_id in rows}

    async def _measure_clock_skew(self) -> None:
        """Compare venue time to ours. Feeds CONNECTIVITY_KILL_SWITCH.

        A large skew means either our clock is wrong or we are talking to
        something unexpected; both are reasons to stop trading, not to guess.
        """
        server_time = await self.client.get_server_time()
        if server_time is None:
            self.last_clock_skew_s = None
            return
        self.last_clock_skew_s = datetime.now(UTC).timestamp() - float(server_time)

    def _health(self, stats: dict) -> ComponentHealth:
        if stats["batches_ok"] == 0:
            return ComponentHealth.FAILED
        if stats["batches_failed"] > stats["batches_ok"]:
            return ComponentHealth.DEGRADED
        if stats["stale_books"] > stats["books_received"] * 0.25:
            return ComponentHealth.STALE
        if stats["books_rejected"] > stats["books_received"] * 0.05:
            return ComponentHealth.DEGRADED
        return ComponentHealth.HEALTHY
