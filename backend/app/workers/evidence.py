"""Evidence collection worker.

Runs every enabled connector, stores what they produce, links it to markets, and
records conflicts.

**Source isolation is the defining property.** Each provider runs inside its own
try/except and its own database transaction. Treasury being down does not stop
crypto ingestion; a malformed BLS payload does not lose the FOMC calendar. A
failing source is recorded against that source and the cycle continues, because
the alternative — one bad feed silently blanking the evidence layer — is exactly
the failure mode that would make the models quietly wrong rather than loudly
degraded.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import ComponentHealth, MarketCategory, MarketSubcategory, SystemComponent
from app.core.logging import get_correlation_id, get_logger
from app.db.models import ExternalSource, Market, MarketEvidenceLink
from app.db.session import session_scope
from app.evidence import conflicts as conflict_engine
from app.evidence.base import EvidenceError, EvidenceProvider
from app.evidence.classify import classify_deep
from app.evidence.matching import link_evidence_for_market, relevant_series
from app.evidence.providers import build_enabled_providers
from app.evidence.registry import (
    consume_budget,
    evaluate_source_health,
    record_source_result,
    sync_registry,
)
from app.evidence.store import source_row_ids, store_items
from app.ingest.repository import record_system_event

log = get_logger("workers.evidence")

# Markets to (re)link per cycle. Linking is cheap but not free, and the markets
# worth linking are the ones a model could actually run on.
MAX_MARKETS_PER_CYCLE = 400


def _has_existing_links(session: Session, market_id: int) -> bool:
    """Whether any evidence is already linked to this market.

    A cycle that creates no new links has not established that a market is
    without evidence — the links may simply have been made on an earlier pass.
    """
    return session.execute(
        select(MarketEvidenceLink.id).where(MarketEvidenceLink.market_id == market_id).limit(1)
    ).scalar_one_or_none() is not None


class EvidenceWorker:
    def __init__(self, fetcher, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.fetcher = fetcher
        self.providers: list[EvidenceProvider] = build_enabled_providers(fetcher, self.settings)

    async def run_once(self, *, as_of: datetime | None = None) -> dict:
        started = datetime.now(UTC)
        as_of = as_of or datetime.now(UTC)

        stats: dict = {
            "providers_run": 0,
            "providers_failed": 0,
            "providers_skipped_budget": 0,
            "items_collected": 0,
            "items_inserted": 0,
            "items_duplicate": 0,
            "items_revised": 0,
            "items_rejected": 0,
            "markets_linked": 0,
            "links_created": 0,
            "conflicts_recorded": 0,
            "per_source": {},
        }

        with session_scope() as session:
            sync_registry(session, self.settings)

        await self._collect_all(as_of, stats)
        self._link_markets(as_of, stats)
        self._record_conflicts(as_of, stats)
        self._refresh_health(stats)

        with session_scope() as session:
            record_system_event(
                session,
                component=SystemComponent.DATA_FEED.value,
                event="evidence_cycle",
                health=self._cycle_health(stats).value,
                detail=stats,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                correlation_id=get_correlation_id(),
            )

        log.info("evidence cycle complete", extra={"event": "evidence_cycle", "detail": stats})
        return stats

    # ------------------------------------------------------------------
    async def _collect_all(self, as_of: datetime, stats: dict) -> None:
        """Run every provider in isolation."""
        for provider in self.providers:
            source_key = provider.source_key

            # Respect a documented daily cap before spending a request on it.
            with session_scope() as session:
                if not consume_budget(session, source_key, provider.request_cost):
                    stats["providers_skipped_budget"] += 1
                    stats["per_source"][source_key] = {
                        "status": "skipped",
                        "reason": "daily request budget exhausted",
                    }
                    log.warning(
                        "source budget exhausted",
                        extra={
                            "event": "evidence_budget_exhausted",
                            "detail": {"source": source_key},
                        },
                    )
                    continue

            try:
                items = await provider.collect(now=as_of)
            except EvidenceError as exc:
                # Isolated: this source failed, the cycle continues.
                stats["providers_failed"] += 1
                stats["per_source"][source_key] = {
                    "status": "failed",
                    "error_code": exc.error_code,
                    "error": str(exc)[:200],
                }
                with session_scope() as session:
                    record_source_result(
                        session, source_key, success=False, error_code=exc.error_code
                    )
                log.warning(
                    "evidence provider failed",
                    extra={
                        "event": "evidence_provider_failed",
                        "error_code": exc.error_code,
                        "detail": {"source": source_key},
                    },
                )
                continue
            except Exception as exc:  # noqa: BLE001 - a provider must never kill the cycle
                stats["providers_failed"] += 1
                stats["per_source"][source_key] = {
                    "status": "failed",
                    "error_code": type(exc).__name__,
                    "error": str(exc)[:200],
                }
                with session_scope() as session:
                    record_source_result(
                        session, source_key, success=False, error_code=type(exc).__name__
                    )
                log.exception(
                    "evidence provider raised",
                    extra={
                        "event": "evidence_provider_error",
                        "detail": {"source": source_key},
                    },
                )
                continue

            stats["providers_run"] += 1
            stats["items_collected"] += len(items)

            health = provider.get_health()
            with session_scope() as session:
                report, _ = store_items(
                    session,
                    items,
                    source_row_ids=source_row_ids(session),
                    max_age_days=self.settings.evidence_max_age_days,
                    now=as_of,
                )
                record_source_result(
                    session,
                    source_key,
                    success=True,
                    latency_ms=health.latency_ms,
                    health=health.health,
                )

            stats["items_inserted"] += report.inserted
            stats["items_duplicate"] += report.duplicates
            stats["items_revised"] += report.revisions
            stats["items_rejected"] += report.rejected
            stats["per_source"][source_key] = {
                "status": "ok",
                "health": health.health.value,
                "collected": len(items),
                "latency_ms": health.latency_ms,
                **report.as_dict(),
            }

    # ------------------------------------------------------------------
    def _link_markets(self, as_of: datetime, stats: dict) -> None:
        """Associate evidence with the markets it bears on."""
        with session_scope() as session:
            markets = session.execute(
                select(Market)
                .where(
                    Market.closed.is_(False),
                    Market.archived.is_(False),
                    Market.enable_order_book.is_(True),
                )
                .order_by(Market.liquidity_num.desc().nullslast())
                .limit(MAX_MARKETS_PER_CYCLE)
            ).scalars().all()

            for market in markets:
                classification = classify_deep(
                    question=market.question,
                    description=market.description,
                    category=MarketCategory(market.category),
                )

                subcategory = (
                    classification.subcategory
                    if classification.subcategory is not MarketSubcategory.UNCLASSIFIED
                    else None
                )

                # This worker owns the classification columns on `markets`. The
                # prediction worker recomputes the same classification for its
                # own use but writes only `modelability_tier`, so the two cannot
                # disagree about what a market is.
                market.subcategory = subcategory.value if subcategory else None
                market.event_type = classification.event_type.value
                market.resolution_mechanism = classification.resolution_mechanism.value
                market.classification_detail = classification.as_detail()

                series = relevant_series(subcategory, asset=classification.asset)
                if not series:
                    # No source is declared for this kind of question, so no
                    # evidence can exist for it — which is different from a
                    # question we could source but have not collected yet.
                    market.evidence_available = False
                    continue

                matches = link_evidence_for_market(
                    session,
                    market,
                    subcategory=subcategory,
                    asset=classification.asset,
                    ticker=None,
                    subject_tags=classification.subject_tags,
                    as_of=as_of,
                )
                # `evidence_available` means evidence is actually linked, not
                # that a source exists in principle. The dashboard renders it as
                # a badge, and the optimistic reading would put an EVIDENCE
                # badge on a market with nothing behind it.
                market.evidence_available = bool(matches) or _has_existing_links(
                    session, market.id
                )
                if matches:
                    stats["markets_linked"] += 1
                    stats["links_created"] += len(matches)

    # ------------------------------------------------------------------
    def _record_conflicts(self, as_of: datetime, stats: dict) -> None:
        """Check series that more than one source reports.

        Crypto spot is the case that actually occurs today — Coinbase and
        Kraken both quote it — and a material divergence between two venues is
        a data-quality signal worth storing.
        """
        from app.evidence.providers.crypto import TRACKED_ASSETS

        series_keys = [f"CRYPTO_SPOT_{asset}_USD" for asset, _, _ in TRACKED_ASSETS]

        with session_scope() as session:
            for series_key in series_keys:
                try:
                    outcome = conflict_engine.detect_and_record(
                        session, series_key=series_key, as_of=as_of
                    )
                except Exception:  # noqa: BLE001
                    log.warning(
                        "conflict detection failed",
                        extra={
                            "event": "conflict_detection_failed",
                            "detail": {"series": series_key},
                        },
                    )
                    continue
                if (
                    outcome is not None
                    and outcome.spread is not None
                    and outcome.spread >= conflict_engine.MATERIAL_DISAGREEMENT
                ):
                    stats["conflicts_recorded"] += 1

    # ------------------------------------------------------------------
    def _refresh_health(self, stats: dict) -> None:
        """Recompute per-source health from observed behaviour."""
        with session_scope() as session:
            rows = session.execute(select(ExternalSource)).scalars().all()
            summary: dict[str, str] = {}
            for row in rows:
                health = evaluate_source_health(row)
                row.health = health.value
                if row.source_key:
                    summary[row.source_key] = health.value
            stats["source_health"] = summary

    def _cycle_health(self, stats: dict) -> ComponentHealth:
        if stats["providers_run"] == 0:
            return ComponentHealth.FAILED
        if stats["providers_failed"] > 0 or stats["providers_skipped_budget"] > 0:
            return ComponentHealth.DEGRADED
        return ComponentHealth.HEALTHY
