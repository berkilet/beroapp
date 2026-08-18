"""Kill switches.

Five independent switches. All fail **closed**: the safe state on any error, on
any unknown condition, and at process start is *tripped*. A switch clears only
when something has positively verified the condition it guards, and any switch
that cannot be evaluated stays tripped rather than being assumed healthy.

The automatic switches (DATA, MODEL, CONNECTIVITY, RISK) are re-evaluated every
worker cycle. GLOBAL is operator-controlled and is never cleared by code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import KillSwitch
from app.db.models import SystemConfig

_GLOBAL_KEY = "global_kill_switch"


@dataclass
class SwitchState:
    switch: KillSwitch
    tripped: bool
    reason: str
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict:
        return {
            "switch": self.switch.value,
            "tripped": self.tripped,
            "reason": self.reason,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


@dataclass
class KillSwitchReport:
    states: dict[KillSwitch, SwitchState]

    @property
    def any_tripped(self) -> bool:
        return any(state.tripped for state in self.states.values())

    @property
    def tripped_switches(self) -> list[KillSwitch]:
        return [switch for switch, state in self.states.items() if state.tripped]

    def blocking_reasons(self) -> list[str]:
        return [
            f"{switch.value}: {self.states[switch].reason}"
            for switch in self.tripped_switches
        ]

    def as_dict(self) -> dict:
        return {switch.value: state.as_dict() for switch, state in self.states.items()}


@dataclass
class RiskState:
    """Portfolio state the RISK switch needs. Unknown fields mean the switch
    stays tripped — an unknown risk state is not a safe one."""

    equity_usd: float | None = None
    peak_equity_usd: float | None = None
    daily_pnl_usd: float | None = None
    day_start_equity_usd: float | None = None


class KillSwitchEvaluator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    def evaluate(
        self,
        *,
        session: Session | None = None,
        last_data_at: datetime | None = None,
        clock_skew_s: float | None = None,
        consecutive_api_failures: int = 0,
        model_versions_registered: bool | None = None,
        calibration_drift: float | None = None,
        risk_state: RiskState | None = None,
        now: datetime | None = None,
    ) -> KillSwitchReport:
        now = now or datetime.now(UTC)
        states = {
            KillSwitch.GLOBAL: self._global(session),
            KillSwitch.DATA: self._data(last_data_at, now),
            KillSwitch.MODEL: self._model(model_versions_registered, calibration_drift),
            KillSwitch.RISK: self._risk(risk_state),
            KillSwitch.CONNECTIVITY: self._connectivity(clock_skew_s, consecutive_api_failures),
        }
        return KillSwitchReport(states=states)

    # ------------------------------------------------------------------
    def _global(self, session: Session | None) -> SwitchState:
        """Operator-controlled. Config default is tripped; an explicit stored
        value of False is the only thing that clears it."""
        if self.settings.global_kill_switch is False and session is None:
            return SwitchState(KillSwitch.GLOBAL, False, "cleared by configuration")

        if session is None:
            return SwitchState(
                KillSwitch.GLOBAL, True, "no session available to read operator state"
            )

        row = session.execute(
            select(SystemConfig).where(SystemConfig.key == _GLOBAL_KEY)
        ).scalar_one_or_none()

        if row is None:
            # Never explicitly cleared -> tripped. Fail-closed by default.
            return SwitchState(
                KillSwitch.GLOBAL, True, "no operator clearance recorded"
            )
        tripped = bool(row.value.get("tripped", True))
        return SwitchState(
            KillSwitch.GLOBAL,
            tripped,
            row.value.get("reason", "operator setting") if tripped else "cleared by operator",
        )

    def _data(self, last_data_at: datetime | None, now: datetime) -> SwitchState:
        if last_data_at is None:
            return SwitchState(KillSwitch.DATA, True, "no market data has been observed")
        if last_data_at.tzinfo is None:
            last_data_at = last_data_at.replace(tzinfo=UTC)
        age = (now - last_data_at).total_seconds()
        if age > self.settings.data_staleness_s:
            return SwitchState(
                KillSwitch.DATA,
                True,
                f"market data is {age:.0f}s old, beyond the {self.settings.data_staleness_s}s limit",
            )
        if age < -self.settings.max_clock_skew_s:
            return SwitchState(
                KillSwitch.DATA, True, f"market data timestamp is {-age:.0f}s in the future"
            )
        return SwitchState(KillSwitch.DATA, False, f"market data is {age:.0f}s old")

    def _model(
        self, versions_registered: bool | None, calibration_drift: float | None
    ) -> SwitchState:
        if versions_registered is None:
            return SwitchState(KillSwitch.MODEL, True, "model registry state unknown")
        if not versions_registered:
            return SwitchState(
                KillSwitch.MODEL, True, "no active model version is registered"
            )
        if calibration_drift is not None and calibration_drift > 0.10:
            return SwitchState(
                KillSwitch.MODEL,
                True,
                f"calibration drift {calibration_drift:.3f} exceeds the 0.10 tolerance",
            )
        return SwitchState(KillSwitch.MODEL, False, "active model registered and within calibration tolerance")

    def _risk(self, state: RiskState | None) -> SwitchState:
        if state is None or state.equity_usd is None:
            return SwitchState(KillSwitch.RISK, True, "portfolio risk state unknown")

        if state.peak_equity_usd and state.peak_equity_usd > 0:
            drawdown_pct = (state.peak_equity_usd - state.equity_usd) / state.peak_equity_usd * 100
            if drawdown_pct >= self.settings.max_drawdown_percent:
                return SwitchState(
                    KillSwitch.RISK,
                    True,
                    f"drawdown {drawdown_pct:.2f}% at or beyond the "
                    f"{self.settings.max_drawdown_percent}% limit",
                )

        if state.daily_pnl_usd is not None and state.day_start_equity_usd:
            daily_loss_pct = -state.daily_pnl_usd / state.day_start_equity_usd * 100
            if daily_loss_pct >= self.settings.max_daily_loss_percent:
                return SwitchState(
                    KillSwitch.RISK,
                    True,
                    f"daily loss {daily_loss_pct:.2f}% at or beyond the "
                    f"{self.settings.max_daily_loss_percent}% limit",
                )

        return SwitchState(KillSwitch.RISK, False, "within drawdown and daily-loss limits")

    def _connectivity(
        self, clock_skew_s: float | None, consecutive_failures: int
    ) -> SwitchState:
        if clock_skew_s is None:
            return SwitchState(
                KillSwitch.CONNECTIVITY, True, "venue clock skew has not been measured"
            )
        if abs(clock_skew_s) > self.settings.max_clock_skew_s:
            return SwitchState(
                KillSwitch.CONNECTIVITY,
                True,
                f"clock skew {clock_skew_s:.1f}s exceeds the "
                f"{self.settings.max_clock_skew_s}s tolerance",
            )
        if consecutive_failures >= self.settings.circuit_breaker_failures:
            return SwitchState(
                KillSwitch.CONNECTIVITY,
                True,
                f"{consecutive_failures} consecutive upstream failures",
            )
        return SwitchState(
            KillSwitch.CONNECTIVITY,
            False,
            f"clock skew {clock_skew_s:.1f}s, {consecutive_failures} recent failures",
        )


def set_global_kill_switch(
    session: Session, *, tripped: bool, actor: str, reason: str
) -> None:
    """Operator control for the global switch. Callers must also write an
    AuditLog row; see api/routes/system.py."""
    row = session.execute(
        select(SystemConfig).where(SystemConfig.key == _GLOBAL_KEY)
    ).scalar_one_or_none()
    payload = {"tripped": tripped, "reason": reason}
    if row is None:
        session.add(
            SystemConfig(key=_GLOBAL_KEY, value=payload, updated_by=actor, updated_at=datetime.now(UTC))
        )
    else:
        row.value = payload
        row.updated_by = actor
        row.updated_at = datetime.now(UTC)
