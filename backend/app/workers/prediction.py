"""Prediction worker.

Runs the decision pipeline for every eligible market:

    modelability -> probability -> edge -> risk -> (paper execution)

Two rules govern feature assembly and they are not negotiable:

* **known_at filtering.** Every input is selected with ``known_at <= as_of``.
  In live operation ``as_of`` is now, so this is a no-op; in a backtest it is a
  historical timestamp, and the *same code path* runs. Look-ahead bias is
  prevented by construction rather than by remembering to avoid it.
* **No fabrication.** A market missing a book, a midpoint, or a snapshot
  history does not get a default — it gets skipped, with the reason recorded.

The worker writes a Prediction for every market it can model, and a Signal for
every prediction, including NO_TRADE and WATCH ones. Storing the negatives is
what makes the eventual performance analysis honest: a system that only records
its BUY calls cannot tell you whether its filters were doing anything.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import (
    ComponentHealth,
    MarketCategory,
    MarketSubcategory,
    ModelabilityStatus,
    Recommendation,
    RiskStatus,
    SystemComponent,
)
from app.core.logging import get_correlation_id, get_logger
from app.db.models import (
    AuditLog,
    Market,
    MarketFeatures,
    MarketSnapshot,
    MarketToken,
    OrderBookSnapshot,
    Prediction,
    RiskDecision,
    Signal,
)
from app.db.session import session_scope
from app.engines.edge import EdgeEngine, EdgeResult
from app.engines.killswitch import KillSwitchEvaluator, KillSwitchReport, RiskState
from app.engines.liquidity import LiquidityProfile, executable_probability, profile_book
from app.engines.modelability import MarketFacts, assess
from app.engines.probability import (
    InvalidModelOutput,
    ProbabilityEngine,
    ProbabilityInputs,
)
from app.engines.category_models import CategoryModelRouter
from app.engines.features import FeatureBuilder
from app.engines.risk import PortfolioState, RiskEngine
from app.evidence.classify import classify_deep, modelability_tier
from app.evidence.question_shape import detect_shape
from app.evidence.signal_strength import assess_signal_strength
from app.ingest.repository import record_system_event
from app.schemas.polymarket import BookLevel, OrderBook
from app.core.enums import Side

log = get_logger("workers.prediction")

NEGRISK_MIN_COVERAGE = 0.98
"""Fraction of a neg-risk group's legs that must be priced before the group's
coherence sum is usable. Set high on purpose: the whole value of the constraint
is that it is exact, and a partially-observed sum is not a weaker signal, it is
a wrong one."""


@dataclass
class TokenContext:
    """A single YES-side token and everything known about it at ``as_of``."""

    market: Market
    token: MarketToken
    snapshot: MarketSnapshot
    book: OrderBook
    book_snapshot_id: int
    profile: LiquidityProfile
    snapshot_count: int
    negrisk_group_sum: float | None
    negrisk_group_size: int | None


class PredictionWorker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.probability = ProbabilityEngine(self.settings)
        self.features = FeatureBuilder(self.settings)
        self.category_models = CategoryModelRouter(self.settings)
        self.edge = EdgeEngine(self.settings)
        self.risk = RiskEngine(self.settings)
        self.kill_switches = KillSwitchEvaluator(self.settings)

    async def run_once(
        self,
        *,
        as_of: datetime | None = None,
        clock_skew_s: float | None = None,
        consecutive_api_failures: int = 0,
    ) -> dict:
        started = datetime.now(UTC)
        as_of = as_of or datetime.now(UTC)

        stats: dict = {
            "markets_considered": 0,
            "markets_modelable": 0,
            "predictions_written": 0,
            "signals_written": 0,
            "model_rejections": 0,
            "skipped_no_data": 0,
            "recommendations": defaultdict(int),
            "risk_outcomes": defaultdict(int),
            "signal_strengths": defaultdict(int),
            "independent_estimates": 0,
        }

        with session_scope() as session:
            switches = self._evaluate_kill_switches(
                session, as_of=as_of, clock_skew_s=clock_skew_s,
                consecutive_api_failures=consecutive_api_failures,
            )
            contexts = self._load_contexts(session, as_of=as_of)
            stats["markets_considered"] = len(contexts)

            portfolio = self._portfolio_state(session)

            for ctx in contexts:
                assessment = self._assess(ctx, as_of)
                self._persist_modelability(ctx.market, assessment)

                if assessment.status in (
                    ModelabilityStatus.UNMODELABLE,
                    ModelabilityStatus.RESOLUTION_RISK,
                ):
                    continue

                stats["markets_modelable"] += 1

                try:
                    outcome = self._predict_and_evaluate(session, ctx, as_of, portfolio, switches)
                except InvalidModelOutput as exc:
                    # Rejected, never replaced with a guess.
                    stats["model_rejections"] += 1
                    record_system_event(
                        session,
                        component=SystemComponent.PROBABILITY_ENGINE.value,
                        event="model_rejection",
                        severity="ERROR",
                        market_id=ctx.market.id,
                        error_code="invalid_model_output",
                        detail={"reason": str(exc)[:400]},
                        correlation_id=get_correlation_id(),
                    )
                    continue

                if outcome is None:
                    stats["skipped_no_data"] += 1
                    continue

                stats["predictions_written"] += 1
                stats["signals_written"] += 1
                stats["recommendations"][outcome[0].value] += 1
                stats["risk_outcomes"][outcome[1].value] += 1
                stats["signal_strengths"][outcome[2]] += 1
                if outcome[3]:
                    stats["independent_estimates"] += 1

            stats["recommendations"] = dict(stats["recommendations"])
            stats["risk_outcomes"] = dict(stats["risk_outcomes"])
            stats["signal_strengths"] = dict(stats["signal_strengths"])
            stats["kill_switches_tripped"] = [s.value for s in switches.tripped_switches]

            record_system_event(
                session,
                component=SystemComponent.PROBABILITY_ENGINE.value,
                event="prediction_cycle",
                health=(
                    ComponentHealth.HEALTHY
                    if stats["predictions_written"] > 0
                    else ComponentHealth.DEGRADED
                ).value,
                detail=stats,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                correlation_id=get_correlation_id(),
            )

        log.info("prediction cycle complete", extra={"event": "prediction_cycle", "detail": stats})
        return stats

    # ------------------------------------------------------------------
    # Kill switches
    # ------------------------------------------------------------------
    def _evaluate_kill_switches(
        self,
        session: Session,
        *,
        as_of: datetime,
        clock_skew_s: float | None,
        consecutive_api_failures: int,
    ) -> KillSwitchReport:
        last_data = session.execute(select(func.max(MarketSnapshot.known_at))).scalar_one_or_none()
        return self.kill_switches.evaluate(
            session=session,
            last_data_at=last_data,
            clock_skew_s=clock_skew_s,
            consecutive_api_failures=consecutive_api_failures,
            # The baseline is always registered; a trained model would be checked
            # against model_versions here.
            model_versions_registered=True,
            risk_state=self._risk_state(session),
            now=as_of,
        )

    def _risk_state(self, session: Session) -> RiskState:
        from app.db.models import PortfolioSnapshot

        latest = session.execute(
            select(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.taken_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if latest is None:
            # Phase 1 has no portfolio at all. Report the configured virtual
            # capital as a flat, undrawn state so the RISK switch does not sit
            # permanently tripped for a system that holds no positions.
            capital = self.settings.virtual_initial_capital
            return RiskState(
                equity_usd=capital,
                peak_equity_usd=capital,
                daily_pnl_usd=0.0,
                day_start_equity_usd=capital,
            )

        return RiskState(
            equity_usd=latest.equity_usd,
            peak_equity_usd=latest.peak_equity_usd,
            daily_pnl_usd=latest.realised_pnl_usd + latest.unrealised_pnl_usd,
            day_start_equity_usd=latest.peak_equity_usd or latest.equity_usd,
        )

    def _portfolio_state(self, session: Session) -> PortfolioState:
        from app.db.models import Position, PortfolioSnapshot

        latest = session.execute(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.taken_at.desc()).limit(1)
        ).scalar_one_or_none()

        equity = latest.equity_usd if latest else self.settings.virtual_initial_capital
        cash = latest.cash_usd if latest else self.settings.virtual_initial_capital
        gross = latest.gross_exposure_usd if latest else 0.0

        market_exposure: dict[int, float] = defaultdict(float)
        correlated: dict[str, float] = defaultdict(float)
        rows = session.execute(
            select(Position, Market.event_id, Market.neg_risk_market_id)
            .join(Market, Market.id == Position.market_id)
            .where(Position.is_open.is_(True))
        ).all()
        for position, event_id, negrisk_id in rows:
            market_exposure[position.market_id] += position.cost_basis_usd
            group = negrisk_id or (f"event:{event_id}" if event_id else None)
            if group:
                correlated[group] += position.cost_basis_usd

        return PortfolioState(
            equity_usd=equity,
            cash_usd=cash,
            gross_exposure_usd=gross,
            market_exposure_usd=dict(market_exposure),
            correlated_exposure_usd=dict(correlated),
        )

    # ------------------------------------------------------------------
    # Feature assembly — all known_at filtered
    # ------------------------------------------------------------------
    def _load_contexts(self, session: Session, *, as_of: datetime) -> list[TokenContext]:
        """Build a context per YES token from data known at ``as_of``.

        Only outcome index 0 (the YES leg) is modelled. The NO leg is the exact
        complement and modelling both would double-count every signal.
        """
        candidates = session.execute(
            select(Market, MarketToken)
            .join(MarketToken, MarketToken.market_id == Market.id)
            .where(
                Market.closed.is_(False),
                Market.archived.is_(False),
                Market.enable_order_book.is_(True),
                MarketToken.outcome_index == 0,
            )
        ).all()

        if not candidates:
            return []

        token_ids = [token.token_id for _, token in candidates]
        snapshots = self._latest_snapshots(session, token_ids, as_of)
        books = self._latest_books(session, token_ids, as_of)
        counts = self._snapshot_counts(session, token_ids, as_of)
        group_sums = self._negrisk_group_sums(session, as_of)

        contexts: list[TokenContext] = []
        for market, token in candidates:
            snapshot = snapshots.get(token.token_id)
            book_row = books.get(token.token_id)
            if snapshot is None or book_row is None:
                continue

            book = self._rehydrate_book(book_row, token.token_id)
            if book is None:
                continue

            profile = profile_book(book)
            group_key = market.neg_risk_market_id
            group = group_sums.get(group_key) if group_key else None

            contexts.append(
                TokenContext(
                    market=market,
                    token=token,
                    snapshot=snapshot,
                    book=book,
                    book_snapshot_id=book_row.id,
                    profile=profile,
                    snapshot_count=counts.get(token.token_id, 0),
                    negrisk_group_sum=group[0] if group else None,
                    negrisk_group_size=group[1] if group else None,
                )
            )
        return contexts

    def _latest_snapshots(
        self, session: Session, token_ids: list[str], as_of: datetime
    ) -> dict[str, MarketSnapshot]:
        newest = (
            select(
                MarketSnapshot.token_id.label("token_id"),
                func.max(MarketSnapshot.known_at).label("known_at"),
            )
            .where(MarketSnapshot.token_id.in_(token_ids), MarketSnapshot.known_at <= as_of)
            .group_by(MarketSnapshot.token_id)
            .subquery()
        )
        rows = session.execute(
            select(MarketSnapshot).join(
                newest,
                (MarketSnapshot.token_id == newest.c.token_id)
                & (MarketSnapshot.known_at == newest.c.known_at),
            )
        ).scalars()
        return {row.token_id: row for row in rows}

    def _latest_books(
        self, session: Session, token_ids: list[str], as_of: datetime
    ) -> dict[str, OrderBookSnapshot]:
        newest = (
            select(
                OrderBookSnapshot.token_id.label("token_id"),
                func.max(OrderBookSnapshot.known_at).label("known_at"),
            )
            .where(OrderBookSnapshot.token_id.in_(token_ids), OrderBookSnapshot.known_at <= as_of)
            .group_by(OrderBookSnapshot.token_id)
            .subquery()
        )
        rows = session.execute(
            select(OrderBookSnapshot).join(
                newest,
                (OrderBookSnapshot.token_id == newest.c.token_id)
                & (OrderBookSnapshot.known_at == newest.c.known_at),
            )
        ).scalars()
        return {row.token_id: row for row in rows}

    def _snapshot_counts(
        self, session: Session, token_ids: list[str], as_of: datetime
    ) -> dict[str, int]:
        rows = session.execute(
            select(MarketSnapshot.token_id, func.count())
            .where(MarketSnapshot.token_id.in_(token_ids), MarketSnapshot.known_at <= as_of)
            .group_by(MarketSnapshot.token_id)
        ).all()
        return {token_id: int(count) for token_id, count in rows}

    def _negrisk_group_sums(
        self, session: Session, as_of: datetime
    ) -> dict[str, tuple[float, int]]:
        """Sum of YES midpoints per neg-risk group, for fully-observed groups only.

        Coherence requires the sum to equal 1, and the deviation is the only
        genuinely model-free signal the baseline has. But it is only a signal
        when we have priced *every* leg: a 128-leg group of which we have
        snapshotted 6 sums to something near zero, and treating that as a 94%
        coherence error would manufacture an enormous edge out of our own
        incomplete sampling.

        So the group is reported only when observed legs cover at least
        NEGRISK_MIN_COVERAGE of the legs that exist. Otherwise it is omitted and
        the model falls back to agreeing with the market — which is the correct
        behaviour for a model that does not have the data.
        """
        expected = dict(
            session.execute(
                select(Market.neg_risk_market_id, func.count())
                .where(
                    Market.neg_risk_market_id.isnot(None),
                    Market.neg_risk_market_id != "",
                    Market.closed.is_(False),
                    Market.archived.is_(False),
                )
                .group_by(Market.neg_risk_market_id)
            ).all()
        )

        newest = (
            select(
                MarketSnapshot.token_id.label("token_id"),
                func.max(MarketSnapshot.known_at).label("known_at"),
            )
            .where(MarketSnapshot.known_at <= as_of)
            .group_by(MarketSnapshot.token_id)
            .subquery()
        )
        rows = session.execute(
            select(Market.neg_risk_market_id, MarketSnapshot.midpoint)
            .join(MarketToken, MarketToken.token_id == MarketSnapshot.token_id)
            .join(Market, Market.id == MarketToken.market_id)
            .join(
                newest,
                (MarketSnapshot.token_id == newest.c.token_id)
                & (MarketSnapshot.known_at == newest.c.known_at),
            )
            .where(
                Market.neg_risk_market_id.isnot(None),
                Market.neg_risk_market_id != "",
                Market.closed.is_(False),
                Market.archived.is_(False),
                MarketToken.outcome_index == 0,
                MarketSnapshot.midpoint.isnot(None),
            )
        ).all()

        observed: dict[str, tuple[float, int]] = {}
        for group_id, midpoint in rows:
            total, count = observed.get(group_id, (0.0, 0))
            observed[group_id] = (total + float(midpoint), count + 1)

        complete: dict[str, tuple[float, int]] = {}
        for group_id, (total, count) in observed.items():
            expected_count = expected.get(group_id, 0)
            if expected_count < 2:
                continue
            if count / expected_count < NEGRISK_MIN_COVERAGE:
                continue
            complete[group_id] = (total, expected_count)
        return complete

    @staticmethod
    def _rehydrate_book(row: OrderBookSnapshot, token_id: str) -> OrderBook | None:
        """Rebuild an OrderBook from a stored snapshot.

        Constructed directly rather than re-validated, because these levels
        already passed validation on ingest and re-running the crossed-book
        check on stored history would discard rows we deliberately kept.
        """
        try:
            return OrderBook.model_construct(
                token_id=token_id,
                condition_id=None,
                observed_at=row.observed_at,
                book_hash=row.book_hash,
                bids=[BookLevel.model_construct(price=lvl["price"], size=lvl["size"]) for lvl in row.bids],
                asks=[BookLevel.model_construct(price=lvl["price"], size=lvl["size"]) for lvl in row.asks],
                tick_size=None,
                min_order_size=None,
                neg_risk=None,
                last_trade_price=None,
            )
        except (KeyError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Per-market pipeline
    # ------------------------------------------------------------------
    def _assess(self, ctx: TokenContext, as_of: datetime):
        return assess(
            MarketFacts(
                category=MarketCategory(ctx.market.category),
                liquidity_num=ctx.market.liquidity_num,
                volume_num=ctx.market.volume_num,
                end_date=ctx.market.end_date,
                first_seen_at=ctx.market.first_seen_at,
                source_created_at=ctx.market.source_created_at,
                accepting_orders=ctx.market.accepting_orders,
                enable_order_book=ctx.market.enable_order_book,
                closed=ctx.market.closed,
                archived=ctx.market.archived,
                active=ctx.market.active,
                resolution_source=ctx.market.resolution_source,
                description=ctx.market.description,
                is_binary=len(ctx.market.outcomes or []) == 2,
                liquidity_profile=ctx.profile,
                snapshot_count=ctx.snapshot_count,
            ),
            now=as_of,
            settings=self.settings,
        )

    def _persist_modelability(self, market: Market, assessment) -> None:
        market.modelability_status = assessment.status.value
        market.modelability_score = assessment.score
        market.modelability_detail = assessment.as_detail()

    def _predict_and_evaluate(
        self,
        session: Session,
        ctx: TokenContext,
        as_of: datetime,
        portfolio: PortfolioState,
        switches: KillSwitchReport,
    ) -> tuple[Recommendation, RiskStatus, str, bool] | None:
        if ctx.profile.midpoint is None:
            return None

        feature_time = datetime.now(UTC)
        hours_to_resolution = (
            (ctx.market.end_date - as_of).total_seconds() / 3600.0
            if ctx.market.end_date
            else None
        )
        exec_price = executable_probability(
            ctx.book, side=Side.BUY, size_usd=self.settings.reference_order_size_usd
        )

        # -- Phase 1.5: deep classification, features, independent estimate ---
        classification = classify_deep(
            question=ctx.market.question,
            description=ctx.market.description,
            category=MarketCategory(ctx.market.category),
        )
        subcategory = (
            classification.subcategory
            if classification.subcategory is not MarketSubcategory.UNCLASSIFIED
            else None
        )

        shape_for_features = (
            detect_shape(ctx.market.question)
            if subcategory is MarketSubcategory.CRYPTO_PRICE
            else None
        )

        feature_vector = self.features.build(
            session,
            market=ctx.market,
            token_id=ctx.token.token_id,
            profile=ctx.profile,
            executable_price=exec_price,
            snapshot_count=ctx.snapshot_count,
            as_of=as_of,
            subcategory=subcategory,
            asset=classification.asset,
            threshold=(
                shape_for_features.threshold
                if shape_for_features is not None
                else classification.threshold_value
            ),
            threshold_direction=classification.threshold_direction,
        )

        # Price questions need their *shape* as well as their level: a barrier
        # question and a terminal question with the same number have materially
        # different answers.
        shape = shape_for_features

        category_estimate = self.category_models.estimate(
            feature_vector,
            subcategory=subcategory,
            threshold=classification.threshold_value,
            direction=classification.threshold_direction,
            shape=shape,
        )

        prediction = self.probability.predict(
            ProbabilityInputs(
                market_id=ctx.market.id,
                token_id=ctx.token.token_id,
                category=MarketCategory(ctx.market.category),
                midpoint=ctx.profile.midpoint,
                executable_price=exec_price,
                liquidity_profile=ctx.profile,
                hours_to_resolution=hours_to_resolution,
                snapshot_count=ctx.snapshot_count,
                negrisk_group_sum=ctx.negrisk_group_sum,
                negrisk_group_size=ctx.negrisk_group_size,
                data_received_at=ctx.snapshot.known_at,
            ),
            category_estimate=category_estimate,
        )

        self._persist_features(session, feature_vector)

        predicted_at = datetime.now(UTC)
        data_latency_ms = int((predicted_at - ctx.snapshot.known_at).total_seconds() * 1000)

        prediction_row = Prediction(
            market_id=ctx.market.id,
            token_id=ctx.token.token_id,
            model_version=prediction.model_version,
            market_probability=ctx.profile.midpoint,
            executable_market_probability=exec_price,
            model_probability=prediction.model_probability,
            model_uncertainty=prediction.model_uncertainty,
            confidence=prediction.confidence,
            data_received_at=ctx.snapshot.known_at,
            feature_computed_at=feature_time,
            predicted_at=predicted_at,
            known_at=predicted_at,
            data_latency_ms=max(0, data_latency_ms),
            model_latency_ms=prediction.rationale.get("model_latency_ms"),
            feature_snapshot={**prediction.features, **feature_vector.features},
            input_refs={
                "market_snapshot_id": ctx.snapshot.id,
                "order_book_snapshot_id": ctx.book_snapshot_id,
                "snapshot_known_at": ctx.snapshot.known_at.isoformat(),
                "as_of": as_of.isoformat(),
                "feature_set_version": feature_vector.version,
                "evidence_ids": feature_vector.evidence_ids,
                "missing_features": feature_vector.missing,
                "oldest_feature_age_s": feature_vector.oldest_feature_age_s(as_of),
            },
            rationale={**prediction.rationale, "adjustments": prediction.adjustments},
            resolution_risk=prediction.resolution_risk.value,
        )
        session.add(prediction_row)
        session.flush()

        edge_result = self.edge.evaluate(
            prediction=prediction, book=ctx.book, profile=ctx.profile
        )

        strength = assess_signal_strength(
            edge_result=edge_result,
            category_estimate=category_estimate,
            feature_vector=feature_vector,
            settings=self.settings,
        )

        self._persist_classification(
            ctx.market,
            classification=classification,
            feature_vector=feature_vector,
            has_independent_estimate=strength.has_independent_estimate,
        )

        signal_row = self._persist_signal(
            session, ctx, prediction_row, edge_result, predicted_at, strength
        )

        correlation_group = ctx.market.neg_risk_market_id or (
            f"event:{ctx.market.event_id}" if ctx.market.event_id else None
        )
        risk_result = self.risk.evaluate(
            signal=edge_result,
            market_id=ctx.market.id,
            correlation_group=correlation_group,
            portfolio=portfolio,
            kill_switches=switches,
        )

        session.add(
            RiskDecision(
                signal_id=signal_row.id,
                status=risk_result.status.value,
                reasons=risk_result.reasons,
                limits_snapshot=risk_result.limits_snapshot,
                kill_switches=risk_result.kill_switches,
                approved_size_usd=risk_result.approved_size_usd,
                checked_at=risk_result.checked_at,
                risk_latency_ms=risk_result.risk_latency_ms,
            )
        )

        session.add(
            AuditLog(
                actor="worker:prediction",
                action="prediction_evaluated",
                component=SystemComponent.PROBABILITY_ENGINE.value,
                market_id=ctx.market.id,
                model_version=prediction.model_version,
                input_refs=prediction_row.input_refs,
                output={
                    "model_probability": prediction.model_probability,
                    "recommendation": edge_result.recommendation.value,
                    "executable_edge": edge_result.executable_edge,
                    "signal_strength": strength.strength.value,
                    "independent_estimate": strength.has_independent_estimate,
                    "evidence_source_count": strength.evidence_source_count,
                },
                confidence=prediction.confidence,
                edge=edge_result.executable_edge,
                risk_status=risk_result.status.value,
                execution_status="NOT_EXECUTED_PHASE_1"
                if not self.settings.paper_trading_active
                else "PENDING_PAPER_EXECUTION",
                correlation_id=get_correlation_id(),
                occurred_at=predicted_at,
            )
        )

        return (
            edge_result.recommendation,
            risk_result.status,
            strength.strength.value,
            strength.has_independent_estimate,
        )

    def _persist_classification(
        self,
        market: Market,
        *,
        classification,
        feature_vector,
        has_independent_estimate: bool,
    ) -> None:
        """Cache the classification and the modelability tier on the market row.

        Ownership is split by column, not by worker, because the two workers
        cover different market sets and know different things:

        * subcategory / event_type / resolution_mechanism / detail — written by
          both this worker and the evidence worker. They cannot disagree:
          `classify_deep` is a pure function of the same three inputs. Writing
          it here matters because the evidence worker only reaches the most
          liquid few hundred markets per cycle, while this one evaluates every
          modelable market.
        * `evidence_available` — the evidence worker only, since it is the one
          that creates the links.
        * `modelability_tier` — this worker only, since HIGH depends on whether
          a category model actually produced an estimate, which is not known
          until the model has run.

        `evidence_feature_count` counts features sourced from outside
        Polymarket. A market whose features all come from its own order book
        has no outside evidence, whatever is linked to it.
        """
        market.subcategory = (
            classification.subcategory.value
            if classification.subcategory is not MarketSubcategory.UNCLASSIFIED
            else None
        )
        market.event_type = classification.event_type.value
        market.resolution_mechanism = classification.resolution_mechanism.value
        market.classification_detail = classification.as_detail()
        market.modelability_tier = modelability_tier(
            classification=classification,
            modelability_status=market.modelability_status,
            has_independent_estimate=has_independent_estimate,
            evidence_feature_count=feature_vector.evidence_feature_count(),
        ).value

    def _persist_features(self, session: Session, vector) -> None:
        """Materialise the feature vector so training and replay share one matrix.

        Idempotent on (token, version, known_at); a re-run of the same cycle
        writes nothing new.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        session.execute(
            pg_insert(MarketFeatures)
            .values(
                market_id=vector.market_id,
                token_id=vector.token_id,
                category=vector.category.value,
                subcategory=vector.subcategory.value if vector.subcategory else None,
                feature_set_version=vector.version,
                known_at=vector.known_at,
                features=vector.features,
                feature_timestamps=vector.timestamps,
                evidence_ids=vector.evidence_ids,
                missing_features=vector.missing,
            )
            .on_conflict_do_nothing(
                index_elements=["token_id", "feature_set_version", "known_at"]
            )
        )

    def _persist_signal(
        self,
        session: Session,
        ctx: TokenContext,
        prediction_row: Prediction,
        edge_result: EdgeResult,
        signal_at: datetime,
        strength=None,
    ) -> Signal:
        # Idempotency: one signal per (token, model, recommendation, minute).
        # Re-running a cycle cannot create a duplicate, which matters because
        # the worker may restart mid-cycle.
        #
        # Hashed rather than concatenated: a token id is 77 characters and a
        # combined model version can be arbitrarily long, so the readable form
        # overflowed the column as soon as category models started contributing
        # to the version string. A digest is bounded whatever the inputs become.
        bucket = signal_at.strftime("%Y%m%d%H%M")
        key = hashlib.sha256(
            f"{ctx.token.token_id}:{edge_result.model_version}:"
            f"{edge_result.recommendation.value}:{bucket}".encode()
        ).hexdigest()

        existing = session.execute(
            select(Signal).where(Signal.idempotency_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        signal = Signal(
            prediction_id=prediction_row.id,
            market_id=ctx.market.id,
            token_id=ctx.token.token_id,
            side=edge_result.side.value if edge_result.side else None,
            recommendation=edge_result.recommendation.value,
            market_probability=edge_result.market_probability,
            model_probability=edge_result.model_probability,
            raw_edge=edge_result.raw_edge,
            executable_edge=edge_result.executable_edge,
            liquidity_adjusted_edge=edge_result.liquidity_adjusted_edge,
            risk_adjusted_edge=edge_result.risk_adjusted_edge,
            confidence=edge_result.confidence,
            executable_price=edge_result.executable_price,
            liquidity=edge_result.liquidity,
            spread=edge_result.spread,
            estimated_slippage=edge_result.estimated_slippage,
            execution_probability=edge_result.execution_probability,
            resolution_risk=edge_result.resolution_risk.value,
            model_version=edge_result.model_version,
            rank_score=edge_result.rank_score,
            rank_explanation={
                **edge_result.rank_explanation,
                "reasons": edge_result.reasons,
                "signal_strength": strength.as_dict() if strength is not None else None,
            },
            signal_at=signal_at,
            idempotency_key=key,
        )
        session.add(signal)
        session.flush()
        return signal
