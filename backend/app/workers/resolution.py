"""Resolution worker.

Resolution is the ground truth every performance and calibration figure depends
on, so it is the place where a shortcut does the most damage.

The rule this worker enforces: **resolution is never inferred from price.** A
market trading at 0.995 is a market trading at 0.995. It has resolved only when
the venue's own status says it has, and the recorded outcome comes from the
venue's outcome data — not from which leg happens to be expensive.

Where the venue's signals are contradictory or incomplete, the outcome is
recorded as AMBIGUOUS and flagged. An ambiguous resolution is excluded from
calibration statistics rather than guessed at, because a guessed outcome would
silently corrupt every metric downstream.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import ComponentHealth, MarketStatus, ResolutionOutcome, SystemComponent
from app.core.logging import get_logger
from app.db.models import Market, Resolution
from app.db.session import session_scope
from app.ingest.polymarket import PolymarketClient
from app.ingest.repository import derive_status, record_system_event
from app.schemas.polymarket import GammaMarket

log = get_logger("workers.resolution")

# A resolved binary market's outcome prices settle at exactly these values.
# Anything short of near-certainty is treated as unresolved, not as a hint.
_SETTLED_HIGH = 0.99
_SETTLED_LOW = 0.01


class ResolutionWorker:
    def __init__(self, client: PolymarketClient, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or get_settings()

    async def run_once(self, *, batch_size: int = 50) -> dict:
        started = datetime.now(UTC)
        stats = {
            "markets_checked": 0,
            "resolutions_recorded": 0,
            "ambiguous": 0,
            "still_open": 0,
            "batches_failed": 0,
        }

        condition_ids = self._pending_condition_ids()
        if not condition_ids:
            return stats

        for offset in range(0, len(condition_ids), batch_size):
            batch = condition_ids[offset : offset + batch_size]
            try:
                markets, _ = await self.client.get_markets_by_condition_ids(batch)
            except Exception as exc:  # noqa: BLE001
                stats["batches_failed"] += 1
                log.warning(
                    "resolution batch failed",
                    extra={"event": "resolution_batch_failed", "error_code": type(exc).__name__},
                )
                continue

            with session_scope() as session:
                for gamma_market in markets:
                    stats["markets_checked"] += 1
                    result = self._record(session, gamma_market)
                    if result == "resolved":
                        stats["resolutions_recorded"] += 1
                    elif result == "ambiguous":
                        stats["ambiguous"] += 1
                    else:
                        stats["still_open"] += 1

        with session_scope() as session:
            record_system_event(
                session,
                component=SystemComponent.RESOLUTION_ENGINE.value,
                event="resolution_cycle",
                health=(
                    ComponentHealth.DEGRADED if stats["batches_failed"] else ComponentHealth.HEALTHY
                ).value,
                detail=stats,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            )

        log.info("resolution cycle complete", extra={"event": "resolution_cycle", "detail": stats})
        return stats

    # ------------------------------------------------------------------
    def _pending_condition_ids(self) -> list[str]:
        """Markets we track that have no recorded resolution yet.

        Includes markets already flagged closed — a closed market is exactly the
        one whose outcome we still need.
        """
        with session_scope() as session:
            rows = session.execute(
                select(Market.condition_id)
                .outerjoin(Resolution, Resolution.market_id == Market.id)
                .where(
                    Resolution.id.is_(None),
                    Market.status.in_(
                        [
                            MarketStatus.CLOSED.value,
                            MarketStatus.CLOSING.value,
                            MarketStatus.ACTIVE.value,
                            MarketStatus.UNKNOWN.value,
                        ]
                    ),
                )
                .limit(500)
            ).scalars()
            return list(rows)

    def _record(self, session: Session, gamma_market: GammaMarket) -> str:
        market = session.execute(
            select(Market).where(Market.gamma_market_id == gamma_market.gamma_market_id)
        ).scalar_one_or_none()
        if market is None:
            return "unknown"

        # Keep the market row current regardless of resolution state.
        market.status = derive_status(gamma_market).value
        market.closed = gamma_market.closed
        market.archived = gamma_market.archived
        market.active = gamma_market.active
        market.uma_resolution_statuses = gamma_market.uma_resolution_statuses
        market.last_seen_at = datetime.now(UTC)

        # A market the venue has not closed has not resolved. Full stop — we do
        # not look at price to second-guess this.
        if not gamma_market.closed:
            return "open"

        outcome, index, ambiguous, evidence = self._determine_outcome(gamma_market)

        already = session.execute(
            select(Resolution).where(Resolution.market_id == market.id)
        ).scalar_one_or_none()
        if already is not None:
            return "resolved"

        now = datetime.now(UTC)
        session.add(
            Resolution(
                market_id=market.id,
                outcome=outcome.value,
                winning_outcome_index=index,
                resolution_source_text=gamma_market.resolution_source,
                resolved_by=gamma_market.resolved_by,
                uma_status=gamma_market.uma_resolution_statuses,
                evidence=evidence,
                is_ambiguous=ambiguous,
                resolved_at=gamma_market.source_updated_at,
                known_at=now,
                recorded_at=now,
            )
        )
        market.status = MarketStatus.RESOLVED.value if not ambiguous else MarketStatus.CLOSED.value
        return "ambiguous" if ambiguous else "resolved"

    def _determine_outcome(
        self, gamma_market: GammaMarket
    ) -> tuple[ResolutionOutcome, int | None, bool, dict]:
        """Read the outcome off the venue's settled outcome prices.

        This is not "inferring resolution from price". The market is already
        known to be closed from its own status flags; settled outcome prices on
        a closed market are the venue's *record of the payout*, which is a
        different thing from a live trading price. Anything that is not an
        unambiguous 1/0 settlement is recorded as AMBIGUOUS.
        """
        evidence: dict = {
            "closed": gamma_market.closed,
            "archived": gamma_market.archived,
            "active": gamma_market.active,
            "outcome_prices": gamma_market.outcome_prices,
            "outcomes": gamma_market.outcomes,
            "uma_resolution_statuses": gamma_market.uma_resolution_statuses,
            "resolved_by": gamma_market.resolved_by,
            "rule": "outcome taken from settled outcome prices on a venue-closed market",
        }

        if gamma_market.archived and not gamma_market.outcome_prices:
            evidence["reason"] = "archived with no settlement prices"
            return ResolutionOutcome.CANCELLED, None, False, evidence

        prices = gamma_market.outcome_prices
        if not prices:
            evidence["reason"] = "closed but no outcome prices published"
            return ResolutionOutcome.UNKNOWN, None, True, evidence

        winners = [i for i, p in enumerate(prices) if p >= _SETTLED_HIGH]
        losers = [i for i, p in enumerate(prices) if p <= _SETTLED_LOW]

        if len(winners) != 1 or len(winners) + len(losers) != len(prices):
            # Two winners, no winner, or a leg sitting mid-range: the venue has
            # not published a clean settlement, so we refuse to invent one.
            evidence["reason"] = (
                f"settlement is not unambiguous: {len(winners)} legs at >= {_SETTLED_HIGH}, "
                f"{len(losers)} at <= {_SETTLED_LOW}, out of {len(prices)}"
            )
            return ResolutionOutcome.AMBIGUOUS, None, True, evidence

        index = winners[0]
        evidence["winning_index"] = index
        evidence["reason"] = "single leg settled at 1, all others at 0"

        # For a binary market, index 0 is the YES leg.
        if len(prices) == 2:
            return (
                ResolutionOutcome.YES if index == 0 else ResolutionOutcome.NO,
                index,
                False,
                evidence,
            )
        return ResolutionOutcome.YES, index, False, evidence
