"""Execution authorization.

This is the only component permitted to mint an execution token, and an adapter
will not act on a token it did not receive from here. The token is deliberately
awkward to forge: it is a frozen dataclass carrying an HMAC over its own
contents, keyed by a per-process secret that exists only in memory.

That is not a cryptographic defence against a determined attacker who already
has code execution — nothing at this layer could be. It is a defence against
the realistic failure mode: a future refactor that constructs an order object
somewhere else and passes it to an adapter, bypassing every check above.

Rules enforced here:

* A LIVE token cannot be minted outside PHASE_3, ever.
* A LIVE token cannot be minted unless `live_trading_enabled` is true *and* the
  phase-3 gate plus operator authorisation are recorded in the database.
* No token of any venue is minted for a signal the risk engine did not approve.
* No token is minted while any kill switch is tripped.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import ExecutionVenue, RiskStatus
from app.db.models import SystemConfig
from app.engines.killswitch import KillSwitchReport
from app.engines.risk import RiskDecisionResult

# Per-process secret. Never persisted, never logged, never sent anywhere.
_PROCESS_SECRET = secrets.token_bytes(32)

TOKEN_TTL_SECONDS = 30

LIVE_AUTHORIZATION_KEY = "live_execution_authorization"
PHASE3_GATE_KEY = "phase_gate_3_passed"

REQUIRED_ACKS = (
    "phase1-complete",
    "phase2-complete",
    "sample-size-reviewed",
    "calibration-reviewed",
    "brier-reviewed",
    "drawdown-reviewed",
    "expectancy-reviewed",
    "liquidity-assumptions-reviewed",
    "slippage-assumptions-reviewed",
    "security-review-complete",
    "api-permissions-reviewed",
    "risk-limits-configured",
    "kill-switches-tested",
    "live-execution-authorised",
)


class AuthorizationDenied(Exception):
    """Raised whenever a token is refused. Always carries the reason."""


@dataclass(frozen=True)
class ExecutionToken:
    venue: ExecutionVenue
    signal_id: int
    market_id: int
    token_id: str
    size_usd: float
    issued_at: datetime
    expires_at: datetime
    signature: str

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at

    def payload(self) -> bytes:
        return "|".join(
            [
                self.venue.value,
                str(self.signal_id),
                str(self.market_id),
                self.token_id,
                f"{self.size_usd:.8f}",
                self.issued_at.isoformat(),
                self.expires_at.isoformat(),
            ]
        ).encode()


def _sign(payload: bytes) -> str:
    return hmac.new(_PROCESS_SECRET, payload, hashlib.sha256).hexdigest()


def verify_token(token: ExecutionToken, *, expected_venue: ExecutionVenue) -> None:
    """Adapters call this before doing anything. Raises on any mismatch."""
    if token.venue is not expected_venue:
        raise AuthorizationDenied(
            f"token is for venue {token.venue.value}, adapter is {expected_venue.value}"
        )
    if token.is_expired():
        raise AuthorizationDenied("execution token has expired")
    if not hmac.compare_digest(token.signature, _sign(token.payload())):
        raise AuthorizationDenied("execution token signature is invalid")


class ExecutionAuthorizationService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def authorize(
        self,
        *,
        venue: ExecutionVenue,
        signal_id: int,
        market_id: int,
        token_id: str,
        risk_decision: RiskDecisionResult,
        kill_switches: KillSwitchReport,
        session: Session | None = None,
    ) -> ExecutionToken:
        if kill_switches.any_tripped:
            raise AuthorizationDenied(
                "kill switch tripped: " + "; ".join(kill_switches.blocking_reasons())
            )

        if risk_decision.status is not RiskStatus.APPROVED:
            raise AuthorizationDenied(
                f"risk decision is {risk_decision.status.value}, not APPROVED"
            )

        size = risk_decision.approved_size_usd
        if size is None or size <= 0:
            raise AuthorizationDenied("risk decision carries no positive approved size")

        if venue is ExecutionVenue.LIVE:
            self._assert_live_permitted(session)
        elif venue is ExecutionVenue.PAPER:
            if not self.settings.paper_trading_active:
                raise AuthorizationDenied(
                    f"paper execution requires PHASE_2 or later; current phase is "
                    f"{self.settings.current_phase}"
                )

        issued = datetime.now(UTC)
        expires = issued + timedelta(seconds=TOKEN_TTL_SECONDS)
        unsigned = ExecutionToken(
            venue=venue,
            signal_id=signal_id,
            market_id=market_id,
            token_id=token_id,
            size_usd=size,
            issued_at=issued,
            expires_at=expires,
            signature="",
        )
        return ExecutionToken(
            venue=venue,
            signal_id=signal_id,
            market_id=market_id,
            token_id=token_id,
            size_usd=size,
            issued_at=issued,
            expires_at=expires,
            signature=_sign(unsigned.payload()),
        )

    # ------------------------------------------------------------------
    def _assert_live_permitted(self, session: Session | None) -> None:
        """Five independent conditions, all required. See docs/PHASE_GATES.md."""
        if not self.settings.live_trading_enabled:
            raise AuthorizationDenied("LIVE_TRADING_ENABLED is false")

        if self.settings.current_phase != "PHASE_3":
            raise AuthorizationDenied(
                f"live execution requires PHASE_3; current phase is {self.settings.current_phase}"
            )

        if session is None:
            raise AuthorizationDenied(
                "live execution requires a database session to verify recorded phase gates"
            )

        gate = session.execute(
            select(SystemConfig).where(SystemConfig.key == PHASE3_GATE_KEY)
        ).scalar_one_or_none()
        if gate is None or not gate.value.get("passed"):
            raise AuthorizationDenied("phase-3 gate has not been recorded as passed")

        auth = session.execute(
            select(SystemConfig).where(SystemConfig.key == LIVE_AUTHORIZATION_KEY)
        ).scalar_one_or_none()
        if auth is None:
            raise AuthorizationDenied("no operator authorisation for live execution is recorded")

        acks = set(auth.value.get("acknowledgements", []))
        missing = [ack for ack in REQUIRED_ACKS if ack not in acks]
        if missing:
            raise AuthorizationDenied(
                f"missing operator acknowledgements: {', '.join(missing)}"
            )

        expires_raw = auth.value.get("expires_at")
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(expires_raw)
            except ValueError as exc:
                raise AuthorizationDenied("operator authorisation has an unparseable expiry") from exc
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if datetime.now(UTC) >= expires_at:
                raise AuthorizationDenied("operator authorisation for live execution has expired")

        # Every hard limit must be a concrete positive value — no defaults.
        for name in (
            "max_position_size_percent",
            "max_market_exposure_percent",
            "max_portfolio_exposure_percent",
            "max_daily_loss_percent",
            "max_drawdown_percent",
            "max_correlated_exposure_percent",
            "min_liquidity",
            "max_spread",
            "max_allowed_slippage",
        ):
            value = getattr(self.settings, name, None)
            if value is None or value <= 0:
                raise AuthorizationDenied(f"hard risk limit {name.upper()} is not configured")
