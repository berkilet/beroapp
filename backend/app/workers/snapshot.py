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
from app.workers.discovery import active_token_ids, count_active_tokens

log = get_logger("workers.snapshot")


class SnapshotWorker:
    """Polls order books for the most liquid slice of the universe that fits
    inside one interval.

    The budget is **measured, not assumed**. A first attempt sized it from the
    configured rate limit (5 req/s x 50 tokens x 60 s x safety factor = 9,000
    tokens, nominally 36 s) and the real cycle took 80 s: the limiter permits 5
    req/s, but round-trip latency and database writes mean observed throughput
    is closer to 2.2 batches/s. Rather than hand-tune a constant that will be
    wrong on a different machine or a slower day, the worker measures its own
    throughput and sizes the next cycle from it.
    """

    def __init__(self, client: PolymarketClient, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or get_settings()
        self.consecutive_batch_failures = 0
        self.last_clock_skew_s: float | None = None
        self.observed_tokens_per_second: float | None = None
        """Exponentially smoothed measurement of real throughput."""

    def _budget(self) -> int:
        """Tokens to poll this cycle.

        Before any measurement exists, fall back to the configured estimate.
        Afterwards, size from what this deployment actually achieves.
        """
        configured = self.settings.snapshot_token_budget
        if self.settings.snapshot_max_tokens > 0 or self.observed_tokens_per_second is None:
            return configured

        target_seconds = self.settings.snapshot_interval_s * self.settings.snapshot_budget_safety_factor
        measured = int(self.observed_tokens_per_second * target_seconds)
        # Never collapse to nothing on one slow cycle, and never exceed the
        # configured ceiling — the rate limit is still the hard constraint.
        return max(self.settings.book_batch_size, min(configured, measured))

    def _record_throughput(self, tokens: int, elapsed_s: float) -> None:
        if elapsed_s <= 0 or tokens <= 0:
            return
        rate = tokens / elapsed_s
        if self.observed_tokens_per_second is None:
            self.observed_tokens_per_second = rate
        else:
            # Smoothed so one slow cycle does not halve the universe coverage.
            self.observed_tokens_per_second = 0.7 * self.observed_tokens_per_second + 0.3 * rate

    async def run_once(self, *, token_limit: int | None = None) -> dict:
        started = datetime.now(UTC)

        # The universe is larger than the cadence: ~39,000 tokens cannot be
        # polled inside 60 s. Poll the most liquid slice that fits rather than
        # the whole universe slowly — a stale price on a liquid market is
        # misleading, while an illiquid market sampled less often is not
        # tradeable regardless. Nothing is dropped from the database; it is
        # sampled at a lower frequency.
        budget = token_limit or self._budget()
        universe_size = count_active_tokens()
        tokens = active_token_ids(limit=budget)

        stats = {
            "tokens_requested": len(tokens),
            "universe_size": universe_size,
            "universe_coverage": round(len(tokens) / universe_size, 4) if universe_size else 0.0,
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

        elapsed_s = (datetime.now(UTC) - started).total_seconds()
        self._record_throughput(len(tokens), elapsed_s)

        health = self._health(stats)
        stats["clock_skew_s"] = self.last_clock_skew_s
        stats["elapsed_s"] = round(elapsed_s, 1)
        stats["observed_tokens_per_second"] = (
            round(self.observed_tokens_per_second, 1) if self.observed_tokens_per_second else None
        )
        stats["next_cycle_budget"] = self._budget()

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
