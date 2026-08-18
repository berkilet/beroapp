"""Risk engine.

Deterministic. Every check is arithmetic against a configured limit, and every
rejection names the limit it hit. No model, no heuristic, and nothing an LLM can
reach — the limits live on the frozen settings object and there is no setter.

The engine's contract is narrow on purpose: given a signal and the current
portfolio state, return APPROVED with a size, or REJECTED with reasons. It does
not place anything. Approval is a *precondition* for execution, never a trigger
for it — the authorization service is the only thing that can turn an approval
into an execution token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.core.enums import Recommendation, RiskStatus
from app.engines.edge import EdgeResult
from app.engines.killswitch import KillSwitchReport


@dataclass
class PortfolioState:
    """Current exposure. Every field must be known; the caller computes these
    from the database, and a missing value is treated as an unknown risk state
    which fails closed."""

    equity_usd: float
    cash_usd: float
    gross_exposure_usd: float
    market_exposure_usd: dict[int, float] = field(default_factory=dict)
    correlated_exposure_usd: dict[str, float] = field(default_factory=dict)
    """Keyed by correlation group — event id or neg-risk group id."""


@dataclass
class RiskDecisionResult:
    status: RiskStatus
    reasons: list[str]
    approved_size_usd: float | None
    limits_snapshot: dict
    kill_switches: dict
    checked_at: datetime
    risk_latency_ms: int

    @property
    def approved(self) -> bool:
        return self.status is RiskStatus.APPROVED


class RiskEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def evaluate(
        self,
        *,
        signal: EdgeResult,
        market_id: int,
        correlation_group: str | None,
        portfolio: PortfolioState | None,
        kill_switches: KillSwitchReport,
        requested_size_usd: float | None = None,
    ) -> RiskDecisionResult:
        started = datetime.now(UTC)
        s = self.settings
        limits = self._limits_snapshot()
        reasons: list[str] = []

        # 1. Kill switches first. Nothing else matters if one is tripped.
        if kill_switches.any_tripped:
            return self._decision(
                RiskStatus.BLOCKED_BY_KILL_SWITCH,
                kill_switches.blocking_reasons(),
                None, limits, kill_switches, started,
            )

        # 2. The signal must actually be a trade recommendation.
        if signal.recommendation not in (Recommendation.BUY, Recommendation.SELL):
            return self._decision(
                RiskStatus.REJECTED,
                [f"recommendation is {signal.recommendation.value}, not a trade"],
                None, limits, kill_switches, started,
            )

        # 3. Unknown portfolio state fails closed.
        if portfolio is None or portfolio.equity_usd <= 0:
            return self._decision(
                RiskStatus.REJECTED,
                ["portfolio state is unknown or equity is non-positive"],
                None, limits, kill_switches, started,
            )

        # 4. Market-quality gates.
        if signal.liquidity is None or signal.liquidity < s.min_liquidity:
            reasons.append(
                f"liquidity {signal.liquidity} is below the MIN_LIQUIDITY floor {s.min_liquidity}"
            )
        if signal.spread is None or signal.spread > s.max_spread:
            reasons.append(
                f"spread {signal.spread} exceeds MAX_SPREAD {s.max_spread}"
            )
        if signal.estimated_slippage is None or signal.estimated_slippage > s.max_allowed_slippage:
            reasons.append(
                f"estimated slippage {signal.estimated_slippage} exceeds "
                f"MAX_ALLOWED_SLIPPAGE {s.max_allowed_slippage}"
            )
        if signal.confidence < s.min_confidence:
            reasons.append(
                f"confidence {signal.confidence:.3f} is below the {s.min_confidence} floor"
            )
        if signal.executable_edge is None or signal.executable_edge < s.min_executable_edge:
            reasons.append(
                f"executable edge {signal.executable_edge} is below the "
                f"{s.min_executable_edge} threshold"
            )

        if reasons:
            return self._decision(
                RiskStatus.REJECTED, reasons, None, limits, kill_switches, started
            )

        # 5. Sizing. Start from the position cap and shrink to fit every other
        # limit. The result is the largest size that violates nothing.
        equity = portfolio.equity_usd
        size = equity * (s.max_position_size_percent / 100.0)
        binding: list[str] = ["MAX_POSITION_SIZE_PERCENT"]

        if requested_size_usd is not None and requested_size_usd < size:
            size = requested_size_usd
            binding = ["requested size"]

        # Per-market exposure.
        market_cap = equity * (s.max_market_exposure_percent / 100.0)
        existing_market = portfolio.market_exposure_usd.get(market_id, 0.0)
        room = market_cap - existing_market
        if room < size:
            size = room
            binding.append("MAX_MARKET_EXPOSURE_PERCENT")

        # Portfolio-wide exposure.
        portfolio_cap = equity * (s.max_portfolio_exposure_percent / 100.0)
        portfolio_room = portfolio_cap - portfolio.gross_exposure_usd
        if portfolio_room < size:
            size = portfolio_room
            binding.append("MAX_PORTFOLIO_EXPOSURE_PERCENT")

        # Correlated exposure — markets in the same event or neg-risk group move
        # together, so their limit is shared.
        if correlation_group is not None:
            correlated_cap = equity * (s.max_correlated_exposure_percent / 100.0)
            existing_correlated = portfolio.correlated_exposure_usd.get(correlation_group, 0.0)
            correlated_room = correlated_cap - existing_correlated
            if correlated_room < size:
                size = correlated_room
                binding.append("MAX_CORRELATED_EXPOSURE_PERCENT")

        # Cash constraint. Cannot spend money that is not there.
        if portfolio.cash_usd < size:
            size = portfolio.cash_usd
            binding.append("available cash")

        # Cannot size beyond what the book will actually fill.
        estimate = signal.execution_estimate
        if estimate is not None and estimate.fillable_size_usd < size:
            size = estimate.fillable_size_usd
            binding.append("order-book depth")

        if size <= 0:
            return self._decision(
                RiskStatus.REJECTED,
                [f"no room to size this position; binding constraints: {', '.join(binding)}"],
                None, limits, kill_switches, started,
            )

        # A position too small to be meaningful is noise that still consumes
        # capital and attention.
        min_size = max(1.0, equity * 0.0005)
        if size < min_size:
            return self._decision(
                RiskStatus.REJECTED,
                [f"permitted size ${size:.2f} is below the ${min_size:.2f} minimum"],
                None, limits, kill_switches, started,
            )

        return self._decision(
            RiskStatus.APPROVED,
            [f"approved ${size:.2f}; binding constraints: {', '.join(binding)}"],
            size, limits, kill_switches, started,
        )

    # ------------------------------------------------------------------
    def _limits_snapshot(self) -> dict:
        s = self.settings
        return {
            "MAX_POSITION_SIZE_PERCENT": s.max_position_size_percent,
            "MAX_MARKET_EXPOSURE_PERCENT": s.max_market_exposure_percent,
            "MAX_PORTFOLIO_EXPOSURE_PERCENT": s.max_portfolio_exposure_percent,
            "MAX_DAILY_LOSS_PERCENT": s.max_daily_loss_percent,
            "MAX_DRAWDOWN_PERCENT": s.max_drawdown_percent,
            "MAX_CORRELATED_EXPOSURE_PERCENT": s.max_correlated_exposure_percent,
            "MIN_LIQUIDITY": s.min_liquidity,
            "MAX_SPREAD": s.max_spread,
            "MAX_ALLOWED_SLIPPAGE": s.max_allowed_slippage,
            "MIN_EXECUTABLE_EDGE": s.min_executable_edge,
            "MIN_CONFIDENCE": s.min_confidence,
        }

    def _decision(
        self,
        status: RiskStatus,
        reasons: list[str],
        size: float | None,
        limits: dict,
        kill_switches: KillSwitchReport,
        started: datetime,
    ) -> RiskDecisionResult:
        now = datetime.now(UTC)
        return RiskDecisionResult(
            status=status,
            reasons=reasons,
            approved_size_usd=size,
            limits_snapshot=limits,
            kill_switches=kill_switches.as_dict(),
            checked_at=now,
            risk_latency_ms=int((now - started).total_seconds() * 1000),
        )
