"""API routes.

Read-mostly by design. The only mutating endpoints are the kill switches, and
they can only move the system toward a safer state. There is deliberately no
endpoint that places an order, sizes a position, edits a risk limit, or changes
the phase — those are operator actions performed at the command line with an
audit trail, not things a browser session can do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.api.health import (
    component_statuses,
    counters,
    data_freshness,
    overall_health,
)
from app.api.security import Role, require_operator, require_viewer
from app.core.config import get_settings
from app.core.enums import KillSwitch, SystemComponent
from app.db.models import (
    AuditLog,
    ExternalSource,
    Market,
    MarketSnapshot,
    MarketToken,
    ModelVersion,
    OrderBookSnapshot,
    PerformanceMetric,
    PortfolioSnapshot,
    Position,
    Prediction,
    Resolution,
    RiskDecision,
    Signal,
    SystemEvent,
)
from app.db.session import session_scope
from app.engines.killswitch import KillSwitchEvaluator, RiskState, set_global_kill_switch

router = APIRouter()
Viewer = Annotated[Role, Depends(require_viewer)]
Operator = Annotated[Role, Depends(require_operator)]


def get_session():
    with session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(get_session)]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@router.get("/api/dashboard")
def dashboard(session: DbSession, _: Viewer) -> dict:
    settings = get_settings()
    statuses = component_statuses(session)
    freshness = data_freshness(session)

    evaluator = KillSwitchEvaluator(settings)
    switches = evaluator.evaluate(
        session=session,
        last_data_at=session.execute(select(func.max(MarketSnapshot.known_at))).scalar_one_or_none(),
        clock_skew_s=0.0,
        model_versions_registered=True,
        risk_state=RiskState(
            equity_usd=settings.virtual_initial_capital,
            peak_equity_usd=settings.virtual_initial_capital,
            daily_pnl_usd=0.0,
            day_start_equity_usd=settings.virtual_initial_capital,
        ),
    )

    portfolio = session.execute(
        select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.taken_at)).limit(1)
    ).scalar_one_or_none()

    calibration = session.execute(
        select(PerformanceMetric)
        .where(PerformanceMetric.kind == "calibration", PerformanceMetric.scope == "overall")
        .order_by(desc(PerformanceMetric.computed_at))
        .limit(1)
    ).scalar_one_or_none()

    return {
        "phase": {
            "current": settings.current_phase,
            "live_trading_enabled": settings.live_trading_enabled,
            "paper_trading_active": settings.paper_trading_active,
            "notice": (
                "Phase 1: prediction only. No orders of any kind are placed, "
                "simulated or otherwise."
                if not settings.paper_trading_active
                else "Phase 2: paper trading. All capital shown is VIRTUAL."
            ),
        },
        "system_health": overall_health(statuses).value,
        "components": [s.as_dict() for s in statuses],
        "data_freshness": freshness,
        "counters": counters(session),
        "kill_switches": switches.as_dict(),
        "portfolio": _portfolio_payload(portfolio),
        "calibration": calibration.metrics if calibration else None,
        "calibration_sample_size": calibration.sample_size if calibration else 0,
    }


def _portfolio_payload(snapshot: PortfolioSnapshot | None) -> dict:
    settings = get_settings()
    if snapshot is None:
        return {
            "is_virtual": True,
            "capital_label": "VIRTUAL / PAPER CAPITAL",
            "initial_capital_usd": settings.virtual_initial_capital,
            "equity_usd": None,
            "note": (
                "No portfolio exists. Paper trading begins in Phase 2; the system is "
                f"currently in {settings.current_phase}."
            ),
        }
    return {
        "is_virtual": True,
        "capital_label": "VIRTUAL / PAPER CAPITAL",
        "initial_capital_usd": settings.virtual_initial_capital,
        "equity_usd": snapshot.equity_usd,
        "cash_usd": snapshot.cash_usd,
        "positions_value_usd": snapshot.positions_value_usd,
        "unrealised_pnl_usd": snapshot.unrealised_pnl_usd,
        "realised_pnl_usd": snapshot.realised_pnl_usd,
        "roi_pct": (
            (snapshot.equity_usd - settings.virtual_initial_capital)
            / settings.virtual_initial_capital
            * 100
        ),
        "drawdown_pct": snapshot.drawdown_pct,
        "peak_equity_usd": snapshot.peak_equity_usd,
        "open_positions": snapshot.open_position_count,
        "taken_at": snapshot.taken_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------
@router.get("/api/markets")
def list_markets(
    session: DbSession,
    _: Viewer,
    category: str | None = None,
    modelability: str | None = None,
    status: str | None = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict:
    stmt = select(Market)
    if category:
        stmt = stmt.where(Market.category == category)
    if modelability:
        stmt = stmt.where(Market.modelability_status == modelability)
    if status:
        stmt = stmt.where(Market.status == status)
    if search:
        # Parameter-bound; ilike escapes nothing itself, so wildcards in user
        # input are neutralised before binding.
        cleaned = search.replace("%", r"\%").replace("_", r"\_")
        stmt = stmt.where(Market.question.ilike(f"%{cleaned}%"))

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    rows = session.execute(
        stmt.order_by(desc(func.coalesce(Market.liquidity_num, 0))).limit(limit).offset(offset)
    ).scalars()

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [_market_summary(m) for m in rows],
    }


def _market_summary(market: Market) -> dict:
    return {
        "id": market.id,
        "gamma_market_id": market.gamma_market_id,
        "condition_id": market.condition_id,
        "question": market.question,
        "slug": market.slug,
        "category": market.category,
        "category_confidence": market.category_confidence,
        "status": market.status,
        "modelability_status": market.modelability_status,
        "modelability_score": market.modelability_score,
        "liquidity_num": market.liquidity_num,
        "volume_num": market.volume_num,
        "volume_24hr": market.volume_24hr,
        "end_date": market.end_date.isoformat() if market.end_date else None,
        "neg_risk": market.neg_risk,
    }


@router.get("/api/markets/{market_id}")
def market_detail(market_id: int, session: DbSession, _: Viewer) -> dict:
    market = session.get(Market, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    tokens = list(
        session.execute(
            select(MarketToken).where(MarketToken.market_id == market_id).order_by(MarketToken.outcome_index)
        ).scalars()
    )

    yes_token = tokens[0].token_id if tokens else None

    snapshots = (
        list(
            session.execute(
                select(MarketSnapshot)
                .where(MarketSnapshot.token_id == yes_token)
                .order_by(desc(MarketSnapshot.known_at))
                .limit(300)
            ).scalars()
        )
        if yes_token
        else []
    )

    latest_book = (
        session.execute(
            select(OrderBookSnapshot)
            .where(OrderBookSnapshot.token_id == yes_token)
            .order_by(desc(OrderBookSnapshot.known_at))
            .limit(1)
        ).scalar_one_or_none()
        if yes_token
        else None
    )

    predictions = list(
        session.execute(
            select(Prediction)
            .where(Prediction.market_id == market_id)
            .order_by(desc(Prediction.predicted_at))
            .limit(100)
        ).scalars()
    )

    signals = list(
        session.execute(
            select(Signal)
            .where(Signal.market_id == market_id)
            .order_by(desc(Signal.signal_at))
            .limit(50)
        ).scalars()
    )

    resolution = session.execute(
        select(Resolution).where(Resolution.market_id == market_id)
    ).scalar_one_or_none()

    latest = snapshots[0] if snapshots else None

    return {
        "market": {
            **_market_summary(market),
            # Untrusted venue text. The frontend renders it as a text node.
            "description": market.description,
            "resolution_source": market.resolution_source,
            "resolved_by": market.resolved_by,
            "outcomes": market.outcomes,
            "uma_resolution_statuses": market.uma_resolution_statuses,
            "modelability_detail": market.modelability_detail,
            "tick_size": market.tick_size,
            "order_min_size": market.order_min_size,
            "start_date": market.start_date.isoformat() if market.start_date else None,
            "first_seen_at": market.first_seen_at.isoformat(),
            "last_seen_at": market.last_seen_at.isoformat(),
            "untrusted_text_notice": (
                "description and resolution_source are verbatim third-party text "
                "and are treated as data, never as instructions"
            ),
        },
        "tokens": [
            {"token_id": t.token_id, "outcome": t.outcome, "outcome_index": t.outcome_index}
            for t in tokens
        ],
        "current": _snapshot_payload(latest),
        "order_book": (
            {
                "observed_at": latest_book.observed_at.isoformat(),
                "known_at": latest_book.known_at.isoformat(),
                "bids": sorted(latest_book.bids, key=lambda x: -x["price"])[:25],
                "asks": sorted(latest_book.asks, key=lambda x: x["price"])[:25],
            }
            if latest_book
            else None
        ),
        "price_history": [
            {
                "known_at": s.known_at.isoformat(),
                "midpoint": s.midpoint,
                "best_bid": s.best_bid,
                "best_ask": s.best_ask,
                "spread": s.spread,
            }
            for s in reversed(snapshots)
        ],
        "prediction_history": [
            {
                "predicted_at": p.predicted_at.isoformat(),
                "market_probability": p.market_probability,
                "model_probability": p.model_probability,
                "confidence": p.confidence,
                "model_uncertainty": p.model_uncertainty,
                "model_version": p.model_version,
                "rationale": p.rationale,
                "input_refs": p.input_refs,
            }
            for p in reversed(predictions)
        ],
        "signals": [_signal_payload(s) for s in signals],
        "resolution": (
            {
                "outcome": resolution.outcome,
                "is_ambiguous": resolution.is_ambiguous,
                "winning_outcome_index": resolution.winning_outcome_index,
                "resolved_at": resolution.resolved_at.isoformat() if resolution.resolved_at else None,
                "known_at": resolution.known_at.isoformat(),
                "evidence": resolution.evidence,
            }
            if resolution
            else None
        ),
    }


def _snapshot_payload(s: MarketSnapshot | None) -> dict | None:
    if s is None:
        return None
    return {
        "observed_at": s.observed_at.isoformat(),
        "known_at": s.known_at.isoformat(),
        "best_bid": s.best_bid,
        "best_ask": s.best_ask,
        "midpoint": s.midpoint,
        "spread": s.spread,
        "bid_depth_usd": s.bid_depth_usd,
        "ask_depth_usd": s.ask_depth_usd,
        "book_imbalance": s.book_imbalance,
        "last_trade_price": s.last_trade_price,
        "is_stale": s.is_stale,
        "data_latency_ms": s.data_latency_ms,
    }


# ---------------------------------------------------------------------------
# Opportunities & predictions
# ---------------------------------------------------------------------------
def _signal_payload(s: Signal) -> dict:
    return {
        "id": s.id,
        "market_id": s.market_id,
        "token_id": s.token_id,
        "side": s.side,
        "recommendation": s.recommendation,
        "market_probability": s.market_probability,
        "model_probability": s.model_probability,
        "raw_edge": s.raw_edge,
        "executable_edge": s.executable_edge,
        "liquidity_adjusted_edge": s.liquidity_adjusted_edge,
        "risk_adjusted_edge": s.risk_adjusted_edge,
        "confidence": s.confidence,
        "executable_price": s.executable_price,
        "liquidity": s.liquidity,
        "spread": s.spread,
        "estimated_slippage": s.estimated_slippage,
        "execution_probability": s.execution_probability,
        "resolution_risk": s.resolution_risk,
        "model_version": s.model_version,
        "rank_score": s.rank_score,
        "rank_explanation": s.rank_explanation,
        "signal_at": s.signal_at.isoformat(),
        "edge_persisted": s.edge_persisted,
    }


@router.get("/api/opportunities")
def opportunities(
    session: DbSession,
    _: Viewer,
    min_edge: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
    min_confidence: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
    min_liquidity: Annotated[float, Query(ge=0.0)] = 0.0,
    category: str | None = None,
    recommendation: str | None = None,
    max_hours_to_resolution: Annotated[float | None, Query(ge=0.0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    cutoff = datetime.now(UTC) - timedelta(hours=24)

    stmt = (
        select(Signal, Market, RiskDecision)
        .join(Market, Market.id == Signal.market_id)
        .outerjoin(RiskDecision, RiskDecision.signal_id == Signal.id)
        .where(Signal.signal_at >= cutoff, Signal.confidence >= min_confidence)
    )
    if min_edge > 0:
        stmt = stmt.where(Signal.executable_edge >= min_edge)
    if min_liquidity > 0:
        stmt = stmt.where(Signal.liquidity >= min_liquidity)
    if category:
        stmt = stmt.where(Market.category == category)
    if recommendation:
        stmt = stmt.where(Signal.recommendation == recommendation)
    else:
        stmt = stmt.where(Signal.recommendation.in_(["BUY", "SELL"]))
    if max_hours_to_resolution is not None:
        horizon = datetime.now(UTC) + timedelta(hours=max_hours_to_resolution)
        stmt = stmt.where(Market.end_date <= horizon)

    rows = session.execute(
        stmt.order_by(desc(func.coalesce(Signal.rank_score, 0))).limit(limit)
    ).all()

    return {
        "window_hours": 24,
        "count": len(rows),
        "items": [
            {
                **_signal_payload(signal),
                "market": _market_summary(market),
                "hours_to_resolution": (
                    (market.end_date - datetime.now(UTC)).total_seconds() / 3600.0
                    if market.end_date
                    else None
                ),
                "risk_status": risk.status if risk else "NOT_EVALUATED",
                "risk_reasons": risk.reasons if risk else [],
                "approved_size_usd": risk.approved_size_usd if risk else None,
                "is_order": False,
                "notice": "This is a recommendation, not an order. Nothing is executed.",
            }
            for signal, market, risk in rows
        ],
    }


@router.get("/api/predictions")
def list_predictions(
    session: DbSession,
    _: Viewer,
    market_id: int | None = None,
    model_version: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict:
    stmt = select(Prediction, Market.question, Market.category).join(
        Market, Market.id == Prediction.market_id
    )
    if market_id:
        stmt = stmt.where(Prediction.market_id == market_id)
    if model_version:
        stmt = stmt.where(Prediction.model_version == model_version)

    rows = session.execute(
        stmt.order_by(desc(Prediction.predicted_at)).limit(limit).offset(offset)
    ).all()

    return {
        "count": len(rows),
        "items": [
            {
                "id": p.id,
                "market_id": p.market_id,
                "question": question,
                "category": category,
                "token_id": p.token_id,
                "model_version": p.model_version,
                "market_probability": p.market_probability,
                "executable_market_probability": p.executable_market_probability,
                "model_probability": p.model_probability,
                "model_uncertainty": p.model_uncertainty,
                "confidence": p.confidence,
                "resolution_risk": p.resolution_risk,
                "predicted_at": p.predicted_at.isoformat(),
                "data_latency_ms": p.data_latency_ms,
                "model_latency_ms": p.model_latency_ms,
                "feature_snapshot": p.feature_snapshot,
                "rationale": p.rationale,
                "input_refs": p.input_refs,
            }
            for p, question, category in rows
        ],
    }


# ---------------------------------------------------------------------------
# Portfolio, performance, calibration
# ---------------------------------------------------------------------------
@router.get("/api/portfolio")
def portfolio(session: DbSession, _: Viewer) -> dict:
    settings = get_settings()
    latest = session.execute(
        select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.taken_at)).limit(1)
    ).scalar_one_or_none()

    positions = list(
        session.execute(
            select(Position, Market.question)
            .join(Market, Market.id == Position.market_id)
            .where(Position.is_open.is_(True))
            .order_by(desc(Position.cost_basis_usd))
        ).all()
    )

    equity_curve = list(
        session.execute(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.taken_at).limit(2000)
        ).scalars()
    )

    return {
        "summary": _portfolio_payload(latest),
        "phase": settings.current_phase,
        "positions": [
            {
                "market_id": p.market_id,
                "question": question,
                "token_id": p.token_id,
                "shares": float(p.shares),
                "average_entry_price": p.average_entry_price,
                "cost_basis_usd": p.cost_basis_usd,
                "realised_pnl_usd": p.realised_pnl_usd,
                "fees_paid_usd": p.fees_paid_usd,
                "slippage_paid_usd": p.slippage_paid_usd,
                "opened_at": p.opened_at.isoformat(),
            }
            for p, question in positions
        ],
        "equity_curve": [
            {
                "taken_at": s.taken_at.isoformat(),
                "equity_usd": s.equity_usd,
                "drawdown_pct": s.drawdown_pct,
            }
            for s in equity_curve
        ],
    }


@router.get("/api/performance")
def performance(
    session: DbSession,
    _: Viewer,
    scope: str = "overall",
    kind: str = "calibration",
) -> dict:
    rows = list(
        session.execute(
            select(PerformanceMetric)
            .where(PerformanceMetric.kind == kind, PerformanceMetric.scope == scope)
            .order_by(desc(PerformanceMetric.computed_at))
            .limit(100)
        ).scalars()
    )
    return {
        "scope": scope,
        "kind": kind,
        "count": len(rows),
        "items": [
            {
                "scope_value": r.scope_value,
                "model_version": r.model_version,
                "sample_size": r.sample_size,
                "window_start": r.window_start.isoformat() if r.window_start else None,
                "window_end": r.window_end.isoformat() if r.window_end else None,
                "metrics": r.metrics,
                "computed_at": r.computed_at.isoformat(),
            }
            for r in rows
        ],
        "note": (
            "Figures are computed from resolved markets only. Where the sample is "
            "too small, metrics report insufficient_data rather than a number. "
            "Annualised returns are deliberately not reported."
        ),
    }


@router.get("/api/calibration")
def calibration(session: DbSession, _: Viewer) -> dict:
    scopes = ["overall", "category", "model_version", "confidence_bucket", "liquidity_bucket", "horizon_bucket"]
    out: dict = {}
    for scope in scopes:
        latest_time = session.execute(
            select(func.max(PerformanceMetric.computed_at)).where(
                PerformanceMetric.kind == "calibration", PerformanceMetric.scope == scope
            )
        ).scalar_one_or_none()
        if latest_time is None:
            out[scope] = []
            continue
        rows = session.execute(
            select(PerformanceMetric).where(
                PerformanceMetric.kind == "calibration",
                PerformanceMetric.scope == scope,
                PerformanceMetric.computed_at == latest_time,
            )
        ).scalars()
        out[scope] = [
            {"scope_value": r.scope_value, "sample_size": r.sample_size, "metrics": r.metrics}
            for r in rows
        ]
    return out


@router.get("/api/model-health")
def model_health(session: DbSession, _: Viewer) -> dict:
    settings = get_settings()
    versions = list(session.execute(select(ModelVersion)).scalars())
    resolved_count = session.execute(select(func.count()).select_from(Resolution)).scalar_one()

    return {
        "active_versions": [settings.baseline_model_version],
        "registered_versions": [
            {
                "model_id": v.model_id,
                "version": v.version,
                "algorithm": v.algorithm,
                "category": v.category,
                "is_active": v.is_active,
                "feature_set": v.feature_set,
                "hyperparameters": v.hyperparameters,
                "training_period": {
                    "start": v.training_period_start.isoformat() if v.training_period_start else None,
                    "end": v.training_period_end.isoformat() if v.training_period_end else None,
                },
                "performance_summary": v.performance_summary,
                "created_at": v.created_at.isoformat(),
            }
            for v in versions
        ],
        "training_readiness": {
            "resolved_markets": int(resolved_count),
            "required": settings.min_training_observations,
            "trained_models_active": False,
            "note": (
                f"Learned models activate at {settings.min_training_observations} resolved "
                f"markets; {resolved_count} are recorded. Until then the interpretable "
                "baseline is the only estimator, by design — an untrained model would be "
                "a fabricated one."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Data sources, system, audit
# ---------------------------------------------------------------------------
@router.get("/api/data-sources")
def data_sources(session: DbSession, _: Viewer) -> dict:
    settings = get_settings()
    rows = list(session.execute(select(ExternalSource).order_by(ExternalSource.source_tier)).scalars())

    polymarket = [
        {
            "name": "Polymarket Gamma API",
            "tier": 1,
            "source_type": "market_data",
            "base_url": settings.gamma_base_url,
            "purpose": "market and event discovery, metadata, tags",
            "status": "ENABLED",
            "documented_rate_limit": "/markets 300 req/10s; /events 500 req/10s",
            "configured_rps": settings.gamma_rps,
        },
        {
            "name": "Polymarket CLOB API",
            "tier": 1,
            "source_type": "market_data",
            "base_url": settings.clob_base_url,
            "purpose": "order books, executable prices, price history",
            "status": "ENABLED",
            "documented_rate_limit": "/books 500 req/10s; /book 1500 req/10s",
            "configured_rps": settings.clob_rps,
        },
        {
            "name": "Polymarket Data API",
            "tier": 1,
            "source_type": "market_data",
            "base_url": settings.data_base_url,
            "purpose": "open interest, public trade prints",
            "status": "ENABLED",
            "documented_rate_limit": "general 1000 req/10s; /trades 200 req/10s",
            "configured_rps": settings.data_rps,
        },
    ]

    feed_status = component_statuses(session)
    data_feed = next(
        (s for s in feed_status if s.component == SystemComponent.DATA_FEED.value), None
    )

    return {
        "polymarket": polymarket,
        "market_data_feed_health": data_feed.as_dict() if data_feed else None,
        "external_sources": [
            {
                "name": r.name,
                "tier": r.source_tier,
                "source_type": r.source_type,
                "base_url": r.base_url,
                "enabled": r.enabled,
                "requires_api_key": r.requires_api_key,
                "health": r.health,
                "reliability_score": r.reliability_score,
                "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
                "last_error_at": r.last_error_at.isoformat() if r.last_error_at else None,
                "last_error_code": r.last_error_code,
                "last_latency_ms": r.last_latency_ms,
                "success_count": r.success_count,
                "error_count": r.error_count,
                "error_rate": (
                    r.error_count / (r.success_count + r.error_count)
                    if (r.success_count + r.error_count)
                    else None
                ),
                "usage_notes": r.usage_notes,
            }
            for r in rows
        ],
    }


@router.get("/api/system")
def system(session: DbSession, _: Viewer) -> dict:
    settings = get_settings()
    statuses = component_statuses(session)

    recent = list(
        session.execute(
            select(SystemEvent).order_by(desc(SystemEvent.occurred_at)).limit(100)
        ).scalars()
    )

    return {
        "phase": settings.current_phase,
        "live_trading_enabled": settings.live_trading_enabled,
        "overall_health": overall_health(statuses).value,
        "components": [s.as_dict() for s in statuses],
        "data_freshness": data_freshness(session),
        "configuration": {
            # Intervals and limits only. No credential, no connection string.
            "discovery_interval_s": settings.discovery_interval_s,
            "snapshot_interval_s": settings.snapshot_interval_s,
            "prediction_interval_s": settings.prediction_interval_s,
            "data_staleness_s": settings.data_staleness_s,
            "min_executable_edge": settings.min_executable_edge,
            "min_confidence": settings.min_confidence,
            "min_liquidity": settings.min_liquidity,
            "max_spread": settings.max_spread,
            "max_allowed_slippage": settings.max_allowed_slippage,
            "virtual_initial_capital": settings.virtual_initial_capital,
            "risk_limits": {
                "MAX_POSITION_SIZE_PERCENT": settings.max_position_size_percent,
                "MAX_MARKET_EXPOSURE_PERCENT": settings.max_market_exposure_percent,
                "MAX_PORTFOLIO_EXPOSURE_PERCENT": settings.max_portfolio_exposure_percent,
                "MAX_DAILY_LOSS_PERCENT": settings.max_daily_loss_percent,
                "MAX_DRAWDOWN_PERCENT": settings.max_drawdown_percent,
                "MAX_CORRELATED_EXPOSURE_PERCENT": settings.max_correlated_exposure_percent,
            },
        },
        "recent_events": [
            {
                "component": e.component,
                "event": e.event,
                "severity": e.severity,
                "health": e.health,
                "error_code": e.error_code,
                "detail": e.detail,
                "duration_ms": e.duration_ms,
                "occurred_at": e.occurred_at.isoformat(),
            }
            for e in recent
        ],
    }


class KillSwitchRequest(BaseModel):
    tripped: bool = Field(description="True trips the switch (halts trading activity)")
    reason: str = Field(min_length=1, max_length=500)


@router.post("/api/system/kill-switch/global")
def set_kill_switch(
    payload: KillSwitchRequest,
    request: Request,
    session: DbSession,
    _: Operator,
) -> dict:
    """The only mutating endpoint in the API.

    Note the asymmetry: tripping is always allowed, clearing is an audited
    operator action. There is no endpoint that raises a limit, changes the
    phase, or enables live trading — those are command-line operations by
    design.
    """
    before = session.execute(
        select(func.count()).select_from(SystemEvent).where(SystemEvent.event == "kill_switch_change")
    ).scalar_one()

    set_global_kill_switch(
        session, tripped=payload.tripped, actor="operator", reason=payload.reason
    )

    session.add(
        AuditLog(
            actor="operator",
            action="kill_switch_change",
            component=SystemComponent.RISK_ENGINE.value,
            output={"switch": KillSwitch.GLOBAL.value, "tripped": payload.tripped},
            after_state={"tripped": payload.tripped, "reason": payload.reason},
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    )
    session.add(
        SystemEvent(
            component=SystemComponent.RISK_ENGINE.value,
            event="kill_switch_change",
            severity="WARNING",
            detail={"tripped": payload.tripped, "reason": payload.reason},
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    )

    return {
        "switch": KillSwitch.GLOBAL.value,
        "tripped": payload.tripped,
        "reason": payload.reason,
        "prior_change_count": int(before),
    }


@router.get("/api/audit")
def audit(
    session: DbSession,
    _: Viewer,
    action: str | None = None,
    market_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict:
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if market_id:
        stmt = stmt.where(AuditLog.market_id == market_id)

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(desc(AuditLog.occurred_at)).limit(limit).offset(offset)
    ).scalars()

    return {
        "total": int(total),
        "items": [
            {
                "id": r.id,
                "actor": r.actor,
                "action": r.action,
                "component": r.component,
                "market_id": r.market_id,
                "model_version": r.model_version,
                "input_refs": r.input_refs,
                "output": r.output,
                "confidence": r.confidence,
                "edge": r.edge,
                "risk_status": r.risk_status,
                "execution_status": r.execution_status,
                "correlation_id": r.correlation_id,
                "occurred_at": r.occurred_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/api/paper-trading")
def paper_trading(session: DbSession, _: Viewer) -> dict:
    from app.db.models import PaperFill, PaperOrder

    settings = get_settings()
    orders = list(
        session.execute(
            select(PaperOrder, Market.question)
            .join(Market, Market.id == PaperOrder.market_id)
            .order_by(desc(PaperOrder.submitted_at))
            .limit(200)
        ).all()
    )
    fills = {
        f.order_id: f
        for f in session.execute(
            select(PaperFill).order_by(desc(PaperFill.filled_at)).limit(500)
        ).scalars()
    }

    return {
        "phase": settings.current_phase,
        "active": settings.paper_trading_active,
        "capital_label": "VIRTUAL / PAPER CAPITAL",
        "notice": (
            "All trades shown are simulated against recorded order books. No real "
            "money is involved and no order is sent to any venue."
            if settings.paper_trading_active
            else f"Paper trading begins in Phase 2. The system is in {settings.current_phase}."
        ),
        "orders": [
            {
                "id": o.id,
                "market_id": o.market_id,
                "question": question,
                "venue": o.venue,
                "side": o.side,
                "state": o.state,
                "requested_price": o.requested_price,
                "estimated_executable_price": o.estimated_executable_price,
                "requested_size_usd": o.requested_size_usd,
                "signal_at": o.signal_at.isoformat(),
                "submitted_at": o.submitted_at.isoformat(),
                "execution_latency_ms": o.execution_latency_ms,
                "reject_reason": o.reject_reason,
                "fill": (
                    {
                        "simulated_fill_price": fills[o.id].simulated_fill_price,
                        "filled_size_usd": fills[o.id].filled_size_usd,
                        "filled_shares": fills[o.id].filled_shares,
                        "slippage": fills[o.id].slippage,
                        "fees": fills[o.id].fees,
                        "is_partial": fills[o.id].is_partial,
                        "filled_at": fills[o.id].filled_at.isoformat(),
                    }
                    if o.id in fills
                    else None
                ),
            }
            for o, question in orders
        ],
    }
