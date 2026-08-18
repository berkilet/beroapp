"""Database schema.

Conventions applied throughout:

* Every fact-bearing row carries both an *event time* (when the thing happened
  in the world) and ``known_at`` (the earliest moment this platform could
  legitimately use it). The backtester filters on ``known_at`` only. This is
  what makes look-ahead bias structurally impossible rather than merely
  discouraged.
* Universe tables retain closed, resolved, cancelled and invalid markets
  forever. Nothing is deleted for being uninteresting — that is what prevents
  survivorship bias.
* ``audit_logs`` and ``system_events`` are append-only. The application database
  role is granted INSERT/SELECT but not UPDATE/DELETE on them (ops/grants.sql),
  so a full application-layer compromise still cannot rewrite history.
* Nullable numeric columns mean "not known", never "zero". Ingestion never
  substitutes a default for a missing measurement.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    ComponentHealth,
    ExecutionVenue,
    MarketCategory,
    MarketStatus,
    ModelabilityStatus,
    OrderState,
    Recommendation,
    ResolutionOutcome,
    ResolutionRisk,
    RiskStatus,
    Side,
    SourceType,
    VerificationStatus,
)
from app.db.base import Base, ts_column, utcnow


def _enum_col(enum_cls: type, **kwargs: object) -> Mapped[str]:
    """Store enums as short strings.

    A CHECK constraint would be tighter, but native PG enums make migrations
    painful and we validate at the Python boundary in every write path.
    """
    return mapped_column(String(48), **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------


class Event(Base):
    """A Polymarket event: a group of related markets (Gamma /events)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gamma_event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ticker: Mapped[str | None] = mapped_column(String(256))
    slug: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSONB)
    neg_risk: Mapped[bool | None] = mapped_column(Boolean)
    active: Mapped[bool | None] = mapped_column(Boolean)
    closed: Mapped[bool | None] = mapped_column(Boolean)
    archived: Mapped[bool | None] = mapped_column(Boolean)
    liquidity: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[float | None] = mapped_column(Float)
    start_date: Mapped[datetime | None] = ts_column()
    end_date: Mapped[datetime | None] = ts_column()
    source_updated_at: Mapped[datetime | None] = ts_column()
    first_seen_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False)

    markets: Mapped[list["Market"]] = relationship(back_populates="event")

    __table_args__ = (Index("ix_events_closed_end_date", "closed", "end_date"),)


class Market(Base):
    """A single binary market. Retained forever, whatever its final status."""

    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gamma_market_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    question_id: Mapped[str | None] = mapped_column(String(80))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)

    slug: Mapped[str | None] = mapped_column(String(512))
    question: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    group_item_title: Mapped[str | None] = mapped_column(Text)

    # Untrusted text straight from the venue. Stored verbatim as DATA; never
    # interpreted as instruction anywhere in this codebase.
    resolution_source: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(80))
    uma_resolution_statuses: Mapped[list | None] = mapped_column(JSONB)

    outcomes: Mapped[list | None] = mapped_column(JSONB)
    category: Mapped[str] = _enum_col(MarketCategory, default=MarketCategory.OTHER.value, nullable=False)
    category_confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = _enum_col(MarketStatus, default=MarketStatus.UNKNOWN.value, nullable=False, index=True)

    active: Mapped[bool | None] = mapped_column(Boolean)
    closed: Mapped[bool | None] = mapped_column(Boolean)
    archived: Mapped[bool | None] = mapped_column(Boolean)
    accepting_orders: Mapped[bool | None] = mapped_column(Boolean)
    enable_order_book: Mapped[bool | None] = mapped_column(Boolean)
    neg_risk: Mapped[bool | None] = mapped_column(Boolean)
    neg_risk_market_id: Mapped[str | None] = mapped_column(String(80), index=True)

    liquidity_num: Mapped[float | None] = mapped_column(Float)
    volume_num: Mapped[float | None] = mapped_column(Float)
    volume_24hr: Mapped[float | None] = mapped_column(Float)
    order_min_size: Mapped[float | None] = mapped_column(Float)
    tick_size: Mapped[float | None] = mapped_column(Float)

    start_date: Mapped[datetime | None] = ts_column()
    end_date: Mapped[datetime | None] = ts_column(index=True)
    source_created_at: Mapped[datetime | None] = ts_column()
    source_updated_at: Mapped[datetime | None] = ts_column()

    modelability_status: Mapped[str] = _enum_col(
        ModelabilityStatus, default=ModelabilityStatus.INSUFFICIENT_DATA.value, nullable=False, index=True
    )
    modelability_score: Mapped[float | None] = mapped_column(Float)
    modelability_detail: Mapped[dict | None] = mapped_column(JSONB)

    first_seen_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False, index=True)

    event: Mapped[Event | None] = relationship(back_populates="markets")
    tokens: Mapped[list["MarketToken"]] = relationship(back_populates="market")

    __table_args__ = (
        Index("ix_markets_status_category", "status", "category"),
        Index("ix_markets_modelability_liquidity", "modelability_status", "liquidity_num"),
    )


class MarketToken(Base):
    """One CLOB outcome token. Binary markets have exactly two."""

    __tablename__ = "market_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True, nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), unique=True, index=True, nullable=False)
    outcome: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_index: Mapped[int] = mapped_column(Integer, nullable=False)

    market: Mapped[Market] = relationship(back_populates="tokens")

    __table_args__ = (UniqueConstraint("market_id", "outcome_index"),)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class MarketSnapshot(Base):
    """Derived microstructure summary at a point in time.

    Written only when something moved materially (settings.snapshot_min_price_change),
    so a quiet market costs no rows.
    """

    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)

    observed_at: Mapped[datetime] = ts_column(nullable=False)
    """Venue-reported time of the underlying book."""
    known_at: Mapped[datetime] = ts_column(nullable=False, index=True)
    """When this platform could first use it."""

    best_bid: Mapped[float | None] = mapped_column(Float)
    best_ask: Mapped[float | None] = mapped_column(Float)
    midpoint: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[float | None] = mapped_column(Float)
    last_trade_price: Mapped[float | None] = mapped_column(Float)
    bid_depth_usd: Mapped[float | None] = mapped_column(Float)
    ask_depth_usd: Mapped[float | None] = mapped_column(Float)
    book_imbalance: Mapped[float | None] = mapped_column(Float)
    tick_size: Mapped[float | None] = mapped_column(Float)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_market_snapshots_market_known", "market_id", "known_at"),
        Index("ix_market_snapshots_token_known", "token_id", "known_at"),
        CheckConstraint(
            "(best_bid IS NULL OR (best_bid >= 0 AND best_bid <= 1))",
            name="best_bid_in_unit_interval",
        ),
        CheckConstraint(
            "(best_ask IS NULL OR (best_ask >= 0 AND best_ask <= 1))",
            name="best_ask_in_unit_interval",
        ),
    )


class OrderBookSnapshot(Base):
    """Raw order book levels, retained so a decision can be reconstructed exactly."""

    __tablename__ = "order_book_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)
    observed_at: Mapped[datetime] = ts_column(nullable=False)
    known_at: Mapped[datetime] = ts_column(nullable=False, index=True)
    book_hash: Mapped[str | None] = mapped_column(String(96))
    bids: Mapped[list] = mapped_column(JSONB, nullable=False)
    asks: Mapped[list] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_order_book_snapshots_token_known", "token_id", "known_at"),
        UniqueConstraint("token_id", "book_hash", "observed_at", name="token_hash_observed"),
    )


class Trade(Base):
    """Public trade prints observed via the Data API. Not our trades."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"), index=True)
    condition_id: Mapped[str] = mapped_column(String(80), nullable=False)
    token_id: Mapped[str | None] = mapped_column(String(96))
    side: Mapped[str | None] = _enum_col(Side)
    price: Mapped[float | None] = mapped_column(Float)
    size: Mapped[float | None] = mapped_column(Float)
    traded_at: Mapped[datetime | None] = ts_column(index=True)
    known_at: Mapped[datetime] = ts_column(nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class ExternalSource(Base):
    """Registry of permitted evidence sources with tier and reliability."""

    __tablename__ = "external_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    source_type: Mapped[str] = _enum_col(SourceType, nullable=False)
    source_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    reliability_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_api_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    usage_notes: Mapped[str | None] = mapped_column(Text)

    health: Mapped[str] = _enum_col(ComponentHealth, default=ComponentHealth.UNKNOWN.value, nullable=False)
    last_success_at: Mapped[datetime | None] = ts_column()
    last_error_at: Mapped[datetime | None] = ts_column()
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    success_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (CheckConstraint("source_tier BETWEEN 1 AND 4", name="tier_range"),)


class ExternalEvent(Base):
    """An individual evidence item. Append-only: a correction is a new row."""

    __tablename__ = "external_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("external_sources.id"), index=True, nullable=False)
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"), index=True)

    source_type: Mapped[str] = _enum_col(SourceType, nullable=False)
    source_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_url: Mapped[str | None] = mapped_column(String(1024))
    title: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)

    published_at: Mapped[datetime | None] = ts_column()
    ingested_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False)
    known_at: Mapped[datetime] = ts_column(nullable=False, index=True)

    verification_status: Mapped[str] = _enum_col(
        VerificationStatus, default=VerificationStatus.UNVERIFIED.value, nullable=False
    )
    reliability_score: Mapped[float | None] = mapped_column(Float)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "content_hash", name="source_content"),
        Index("ix_external_events_market_known", "market_id", "known_at"),
    )


# ---------------------------------------------------------------------------
# Models & decisions
# ---------------------------------------------------------------------------


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str | None] = _enum_col(MarketCategory)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_set: Mapped[list] = mapped_column(JSONB, nullable=False)
    hyperparameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    training_period_start: Mapped[datetime | None] = ts_column()
    training_period_end: Mapped[datetime | None] = ts_column()
    validation_period_start: Mapped[datetime | None] = ts_column()
    validation_period_end: Mapped[datetime | None] = ts_column()
    performance_summary: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False)
    retired_at: Mapped[datetime | None] = ts_column()

    __table_args__ = (UniqueConstraint("model_id", "version", name="model_id_version"),)


class Prediction(Base):
    """An independent probability estimate.

    ``feature_snapshot`` and ``input_refs`` together answer the two questions the
    spec demands: what did the model know at time T, and why did it say this.
    """

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True, nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    market_probability: Mapped[float] = mapped_column(Float, nullable=False)
    executable_market_probability: Mapped[float | None] = mapped_column(Float)
    model_probability: Mapped[float] = mapped_column(Float, nullable=False)
    model_uncertainty: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # Latency instrumentation (spec §19).
    data_received_at: Mapped[datetime | None] = ts_column()
    feature_computed_at: Mapped[datetime | None] = ts_column()
    predicted_at: Mapped[datetime] = ts_column(nullable=False, index=True)
    known_at: Mapped[datetime] = ts_column(nullable=False, index=True)
    data_latency_ms: Mapped[int | None] = mapped_column(Integer)
    model_latency_ms: Mapped[int | None] = mapped_column(Integer)

    feature_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    input_refs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    rationale: Mapped[dict | None] = mapped_column(JSONB)
    resolution_risk: Mapped[str] = _enum_col(ResolutionRisk, default=ResolutionRisk.UNKNOWN.value, nullable=False)

    __table_args__ = (
        Index("ix_predictions_market_predicted", "market_id", "predicted_at"),
        CheckConstraint("model_probability >= 0 AND model_probability <= 1", name="model_prob_unit"),
        CheckConstraint("market_probability >= 0 AND market_probability <= 1", name="market_prob_unit"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_unit"),
    )


class ModelPrediction(Base):
    """Per-model component output feeding an ensemble prediction."""

    __tablename__ = "model_predictions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), index=True, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    probability: Mapped[float] = mapped_column(Float, nullable=False)
    weight: Mapped[float | None] = mapped_column(Float)
    uncertainty: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("probability >= 0 AND probability <= 1", name="component_prob_unit"),
    )


class Signal(Base):
    """A structured, validated recommendation. NOT an order."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), index=True, nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True, nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)

    side: Mapped[str | None] = _enum_col(Side)
    recommendation: Mapped[str] = _enum_col(Recommendation, nullable=False, index=True)

    market_probability: Mapped[float] = mapped_column(Float, nullable=False)
    model_probability: Mapped[float] = mapped_column(Float, nullable=False)
    raw_edge: Mapped[float] = mapped_column(Float, nullable=False)
    executable_edge: Mapped[float | None] = mapped_column(Float, index=True)
    liquidity_adjusted_edge: Mapped[float | None] = mapped_column(Float)
    risk_adjusted_edge: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    executable_price: Mapped[float | None] = mapped_column(Float)
    liquidity: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[float | None] = mapped_column(Float)
    estimated_slippage: Mapped[float | None] = mapped_column(Float)
    execution_probability: Mapped[float | None] = mapped_column(Float)
    resolution_risk: Mapped[str] = _enum_col(ResolutionRisk, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)

    rank_score: Mapped[float | None] = mapped_column(Float)
    rank_explanation: Mapped[dict | None] = mapped_column(JSONB)

    signal_at: Mapped[datetime] = ts_column(nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    """Deduplicates signals: one per (market, token, model, decision bucket)."""

    # Opportunity-persistence measurement (phase-2 gate criterion 11).
    persistence_checked_at: Mapped[datetime | None] = ts_column()
    edge_persisted: Mapped[bool | None] = mapped_column(Boolean)

    __table_args__ = (
        Index("ix_signals_recommendation_edge", "recommendation", "executable_edge"),
    )


class RiskDecision(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True, nullable=False)
    status: Mapped[str] = _enum_col(RiskStatus, nullable=False, index=True)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    limits_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    kill_switches: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    approved_size_usd: Mapped[float | None] = mapped_column(Float)
    checked_at: Mapped[datetime] = ts_column(nullable=False)
    risk_latency_ms: Mapped[int | None] = mapped_column(Integer)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


class PaperOrder(Base):
    """A simulated order. Venue is constrained to PAPER by a CHECK constraint —
    a live order can never be recorded in this table even by mistake."""

    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True, nullable=False)
    risk_decision_id: Mapped[int] = mapped_column(ForeignKey("risk_decisions.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True, nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)

    venue: Mapped[str] = _enum_col(ExecutionVenue, nullable=False, default=ExecutionVenue.PAPER.value)
    side: Mapped[str] = _enum_col(Side, nullable=False)
    state: Mapped[str] = _enum_col(OrderState, nullable=False, default=OrderState.PENDING.value)

    requested_price: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_executable_price: Mapped[float | None] = mapped_column(Float)
    requested_size_usd: Mapped[float] = mapped_column(Float, nullable=False)

    signal_at: Mapped[datetime] = ts_column(nullable=False)
    submitted_at: Mapped[datetime] = ts_column(nullable=False)
    execution_latency_ms: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("venue = 'PAPER'", name="paper_orders_are_paper_only"),
    )


class PaperFill(Base):
    __tablename__ = "paper_fills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id"), index=True, nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True, nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)
    side: Mapped[str] = _enum_col(Side, nullable=False)

    simulated_fill_price: Mapped[float] = mapped_column(Float, nullable=False)
    filled_size_usd: Mapped[float] = mapped_column(Float, nullable=False)
    filled_shares: Mapped[float] = mapped_column(Float, nullable=False)
    slippage: Mapped[float] = mapped_column(Float, nullable=False)
    fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    book_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("order_book_snapshots.id"))
    """The exact book the fill was simulated against — reproducibility anchor."""

    filled_at: Mapped[datetime] = ts_column(nullable=False, index=True)

    __table_args__ = (
        CheckConstraint(
            "simulated_fill_price >= 0 AND simulated_fill_price <= 1", name="fill_price_unit"
        ),
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True, nullable=False)
    token_id: Mapped[str] = mapped_column(String(96), nullable=False)
    venue: Mapped[str] = _enum_col(ExecutionVenue, nullable=False, default=ExecutionVenue.PAPER.value)

    shares: Mapped[float] = mapped_column(Numeric(24, 8), nullable=False, default=0)
    average_entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_basis_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realised_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fees_paid_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage_paid_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    opened_at: Mapped[datetime] = ts_column(nullable=False)
    closed_at: Mapped[datetime | None] = ts_column()
    updated_at: Mapped[datetime] = ts_column(nullable=False)

    __table_args__ = (UniqueConstraint("token_id", "venue", "opened_at", name="token_venue_open"),)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue: Mapped[str] = _enum_col(ExecutionVenue, nullable=False, default=ExecutionVenue.PAPER.value)
    taken_at: Mapped[datetime] = ts_column(nullable=False, index=True)

    cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    positions_value_usd: Mapped[float] = mapped_column(Float, nullable=False)
    equity_usd: Mapped[float] = mapped_column(Float, nullable=False)
    unrealised_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    realised_pnl_usd: Mapped[float] = mapped_column(Float, nullable=False)
    open_position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_exposure_usd: Mapped[float] = mapped_column(Float, nullable=False)
    max_concentration_pct: Mapped[float | None] = mapped_column(Float)
    peak_equity_usd: Mapped[float] = mapped_column(Float, nullable=False)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class Resolution(Base):
    """Official market resolution.

    Never inferred from price. ``price ~ 1`` is not resolution; a market is only
    recorded as resolved when the venue's own status fields say so, and an
    unclear signal is recorded as AMBIGUOUS rather than guessed.
    """

    __tablename__ = "resolutions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), unique=True, nullable=False)
    outcome: Mapped[str] = _enum_col(ResolutionOutcome, nullable=False, index=True)
    winning_outcome_index: Mapped[int | None] = mapped_column(Integer)
    resolution_source_text: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(80))
    uma_status: Mapped[list | None] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    resolved_at: Mapped[datetime | None] = ts_column(index=True)
    known_at: Mapped[datetime] = ts_column(nullable=False, index=True)
    recorded_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False)


class PerformanceMetric(Base):
    """Computed performance/calibration figures, sliced by bucket."""

    __tablename__ = "performance_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_value: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(64), index=True)

    window_start: Mapped[datetime | None] = ts_column()
    window_end: Mapped[datetime | None] = ts_column()
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_performance_metrics_kind_scope", "kind", "scope", "scope_value"),
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class SystemEvent(Base):
    """Append-only operational event log. Health is derived from these rows,
    not from an in-memory flag, so it survives a restart."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    health: Mapped[str | None] = _enum_col(ComponentHealth)
    market_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False, index=True)

    __table_args__ = (Index("ix_system_events_component_occurred", "component", "occurred_at"),)


class AuditLog(Base):
    """Append-only record of every material decision and operator action."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    component: Mapped[str] = mapped_column(String(48), nullable=False)
    market_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    model_version: Mapped[str | None] = mapped_column(String(64))
    input_refs: Mapped[dict | None] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    before_state: Mapped[dict | None] = mapped_column(JSONB)
    after_state: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    edge: Mapped[float | None] = mapped_column(Float)
    risk_status: Mapped[str | None] = _enum_col(RiskStatus)
    execution_status: Mapped[str | None] = mapped_column(String(32))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False, index=True)


class SystemConfig(Base):
    """Small mutable operational state: current phase, kill-switch overrides,
    phase-gate acknowledgements. Every change writes an AuditLog row."""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = ts_column(default=utcnow, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
