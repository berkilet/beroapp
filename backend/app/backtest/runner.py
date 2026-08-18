"""Historical replay.

Re-runs the decision pipeline at a series of past timestamps, seeing only what
was knowable then.

The design decision that makes this trustworthy is that there is **no separate
backtest pipeline**. The replay calls the same `FeatureBuilder`, the same
`CategoryModelRouter`, the same `ProbabilityEngine` and the same `EdgeEngine`
that production calls, differing only in the `as_of` it passes. A backtest that
reimplements the production path is a backtest of the reimplementation, and it
drifts silently.

Look-ahead is prevented structurally rather than by vigilance: every query in
the feature layer filters `known_at <= as_of`, so an earlier `as_of` simply sees
less. `verify_no_lookahead` additionally asserts this empirically by checking
that no evidence row contributing to a replayed vector post-dates the replay
timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import MarketCategory, MarketSubcategory
from app.core.logging import get_logger
from app.db.models import (
    ExternalEvent,
    Market,
    MarketSnapshot,
    MarketToken,
    OrderBookSnapshot,
    Resolution,
)
from app.engines.calibration import build_report, skill_versus_baseline
from app.engines.category_models import CategoryModelRouter
from app.engines.edge import EdgeEngine
from app.engines.features import FeatureBuilder
from app.engines.liquidity import executable_probability, profile_book
from app.engines.probability import ProbabilityEngine, ProbabilityInputs
from app.evidence.classify import classify_deep
from app.evidence.question_shape import detect_shape
from app.schemas.polymarket import BookLevel, OrderBook

log = get_logger("backtest.runner")


@dataclass
class ReplayPoint:
    """One market evaluated at one historical timestamp."""

    as_of: datetime
    market_id: int
    token_id: str
    category: str
    subcategory: str | None
    market_probability: float
    model_probability: float
    confidence: float
    executable_edge: float | None
    recommendation: str
    had_independent_estimate: bool
    evidence_ids: list[int] = field(default_factory=list)
    outcome: int | None = None
    resolution_known_at: datetime | None = None

    @property
    def is_scoreable(self) -> bool:
        """Usable for calibration: resolved, and predicted before we knew."""
        return (
            self.outcome is not None
            and self.resolution_known_at is not None
            and self.as_of < self.resolution_known_at
        )


@dataclass
class ReplayResult:
    points: list[ReplayPoint] = field(default_factory=list)
    timestamps_evaluated: int = 0
    markets_evaluated: int = 0
    skipped_no_data: int = 0
    errors: int = 0

    def scoreable(self) -> list[ReplayPoint]:
        return [p for p in self.points if p.is_scoreable]

    def metrics(self) -> dict:
        """Calibration and skill over the replay, or an explicit refusal."""
        scoreable = self.scoreable()
        if not scoreable:
            return {
                "sample_size": 0,
                "insufficient_data": True,
                "note": (
                    "no replayed prediction has a resolved outcome yet, so no "
                    "calibration or skill figure can be computed"
                ),
            }

        model = [p.model_probability for p in scoreable]
        market = [p.market_probability for p in scoreable]
        outcomes = [p.outcome for p in scoreable]

        return {
            "sample_size": len(scoreable),
            "insufficient_data": False,
            "model": build_report(model, outcomes).as_dict(),
            "market_baseline": build_report(market, outcomes).as_dict(),
            "skill_vs_market": skill_versus_baseline(model, market, outcomes),
            "with_independent_estimate": sum(1 for p in scoreable if p.had_independent_estimate),
        }

    def as_dict(self) -> dict:
        return {
            "timestamps_evaluated": self.timestamps_evaluated,
            "markets_evaluated": self.markets_evaluated,
            "points": len(self.points),
            "scoreable_points": len(self.scoreable()),
            "skipped_no_data": self.skipped_no_data,
            "errors": self.errors,
            "metrics": self.metrics(),
        }


class BacktestRunner:
    """Replays the production decision path over historical timestamps."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.features = FeatureBuilder(self.settings)
        self.category_models = CategoryModelRouter(self.settings)
        self.probability = ProbabilityEngine(self.settings)
        self.edge = EdgeEngine(self.settings)

    # ------------------------------------------------------------------
    def replay(
        self,
        session: Session,
        *,
        start: datetime,
        end: datetime,
        step: timedelta,
        market_ids: list[int] | None = None,
        max_markets_per_step: int = 200,
    ) -> ReplayResult:
        result = ReplayResult()
        as_of = start

        while as_of <= end:
            result.timestamps_evaluated += 1
            evaluated = self._replay_one_timestamp(
                session, as_of=as_of, market_ids=market_ids, limit=max_markets_per_step,
                result=result,
            )
            result.markets_evaluated += evaluated
            as_of += step

        return result

    def _replay_one_timestamp(
        self,
        session: Session,
        *,
        as_of: datetime,
        market_ids: list[int] | None,
        limit: int,
        result: ReplayResult,
    ) -> int:
        """Evaluate every eligible market as it stood at ``as_of``."""
        # Markets that existed by then. A market discovered later must not
        # appear, or the replay silently gains hindsight about which markets
        # were worth watching.
        #
        # Restricted to markets with a book snapshot at or before `as_of`:
        # without that restriction the limit is spent on markets we never
        # sampled, and the replay evaluates almost nothing. This filter uses
        # only information available at `as_of`, so it introduces no hindsight —
        # it is the same "do we have data for this?" question the production
        # worker asks.
        having_data = (
            select(OrderBookSnapshot.market_id)
            .where(OrderBookSnapshot.known_at <= as_of)
            .distinct()
            .subquery()
        )
        query = (
            select(Market)
            .join(having_data, having_data.c.market_id == Market.id)
            .where(Market.first_seen_at <= as_of)
        )
        if market_ids:
            query = query.where(Market.id.in_(market_ids))
        markets = session.execute(
            query.order_by(Market.liquidity_num.desc().nullslast()).limit(limit)
        ).scalars().all()

        evaluated = 0
        for market in markets:
            try:
                point = self._evaluate(session, market=market, as_of=as_of)
            except Exception:  # noqa: BLE001 - one market must not stop the replay
                result.errors += 1
                continue

            if point is None:
                result.skipped_no_data += 1
                continue

            result.points.append(point)
            evaluated += 1

        return evaluated

    # ------------------------------------------------------------------
    def _evaluate(
        self, session: Session, *, market: Market, as_of: datetime
    ) -> ReplayPoint | None:
        """One market at one timestamp, through the production code path."""
        token = session.execute(
            select(MarketToken).where(
                MarketToken.market_id == market.id, MarketToken.outcome_index == 0
            )
        ).scalar_one_or_none()
        if token is None:
            return None

        book_row = session.execute(
            select(OrderBookSnapshot)
            .where(
                OrderBookSnapshot.token_id == token.token_id,
                OrderBookSnapshot.known_at <= as_of,
            )
            .order_by(OrderBookSnapshot.known_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if book_row is None:
            return None

        book = self._rehydrate(book_row, token.token_id)
        if book is None:
            return None

        profile = profile_book(book)
        if profile.midpoint is None:
            return None

        snapshot_count = int(
            session.execute(
                select(func.count())
                .select_from(MarketSnapshot)
                .where(
                    MarketSnapshot.token_id == token.token_id,
                    MarketSnapshot.known_at <= as_of,
                )
            ).scalar_one()
        )

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
        shape = (
            detect_shape(market.question)
            if subcategory is MarketSubcategory.CRYPTO_PRICE
            else None
        )

        exec_price = executable_probability(
            book, side=__import__("app.core.enums", fromlist=["Side"]).Side.BUY,
            size_usd=self.settings.reference_order_size_usd,
        )

        vector = self.features.build(
            session,
            market=market,
            token_id=token.token_id,
            profile=profile,
            executable_price=exec_price,
            snapshot_count=snapshot_count,
            as_of=as_of,
            subcategory=subcategory,
            asset=classification.asset,
            threshold=shape.threshold if shape else classification.threshold_value,
            threshold_direction=classification.threshold_direction,
        )

        estimate = self.category_models.estimate(
            vector,
            subcategory=subcategory,
            threshold=classification.threshold_value,
            direction=classification.threshold_direction,
            shape=shape,
        )

        prediction = self.probability.predict(
            ProbabilityInputs(
                market_id=market.id,
                token_id=token.token_id,
                category=MarketCategory(market.category),
                midpoint=profile.midpoint,
                executable_price=exec_price,
                liquidity_profile=profile,
                hours_to_resolution=(
                    (market.end_date - as_of).total_seconds() / 3600.0
                    if market.end_date
                    else None
                ),
                snapshot_count=snapshot_count,
            ),
            category_estimate=estimate,
        )

        edge_result = self.edge.evaluate(prediction=prediction, book=book, profile=profile)

        outcome, resolution_known_at = self._outcome(session, market.id)

        return ReplayPoint(
            as_of=as_of,
            market_id=market.id,
            token_id=token.token_id,
            category=market.category,
            subcategory=subcategory.value if subcategory else None,
            market_probability=profile.midpoint,
            model_probability=prediction.model_probability,
            confidence=prediction.confidence,
            executable_edge=edge_result.executable_edge,
            recommendation=edge_result.recommendation.value,
            had_independent_estimate=estimate.is_usable,
            evidence_ids=list(vector.evidence_ids),
            outcome=outcome,
            resolution_known_at=resolution_known_at,
        )

    @staticmethod
    def _outcome(session: Session, market_id: int) -> tuple[int | None, datetime | None]:
        """The eventual outcome, used only for scoring, never as an input.

        Read after the estimate is produced, and `is_scoreable` additionally
        requires the replay timestamp to precede the resolution becoming known.
        """
        resolution = session.execute(
            select(Resolution).where(
                Resolution.market_id == market_id, Resolution.is_ambiguous.is_(False)
            )
        ).scalar_one_or_none()
        if resolution is None or resolution.outcome not in ("YES", "NO"):
            return None, None
        return (1 if resolution.outcome == "YES" else 0), resolution.known_at

    @staticmethod
    def _rehydrate(row: OrderBookSnapshot, token_id: str) -> OrderBook | None:
        try:
            return OrderBook.model_construct(
                token_id=token_id,
                condition_id=None,
                observed_at=row.observed_at,
                book_hash=row.book_hash,
                bids=[
                    BookLevel.model_construct(price=lvl["price"], size=lvl["size"])
                    for lvl in row.bids
                ],
                asks=[
                    BookLevel.model_construct(price=lvl["price"], size=lvl["size"])
                    for lvl in row.asks
                ],
                tick_size=None,
                min_order_size=None,
                neg_risk=None,
                last_trade_price=None,
            )
        except (KeyError, TypeError):
            return None


def verify_no_lookahead(session: Session, result: ReplayResult) -> dict:
    """Empirically confirm no replayed point used future information.

    The structural guarantee is that every feature query filters on known_at.
    This checks it held, by asserting that no evidence row cited by a replayed
    vector became knowable after the timestamp it was replayed at. A test that
    only trusts the design is a test of the design's documentation.
    """
    violations: list[dict] = []
    checked = 0

    for point in result.points:
        if not point.evidence_ids:
            continue
        rows = session.execute(
            select(ExternalEvent.id, ExternalEvent.known_at).where(
                ExternalEvent.id.in_(point.evidence_ids)
            )
        ).all()
        for evidence_id, known_at in rows:
            checked += 1
            if known_at > point.as_of:
                violations.append(
                    {
                        "market_id": point.market_id,
                        "as_of": point.as_of.isoformat(),
                        "evidence_id": evidence_id,
                        "evidence_known_at": known_at.isoformat(),
                    }
                )

    return {
        "evidence_references_checked": checked,
        "violations": violations,
        "clean": not violations,
    }
