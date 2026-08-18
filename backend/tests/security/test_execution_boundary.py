"""The money-loss boundary.

These are the most important tests in the repository. They exist because the
realistic failure mode is not an attacker — it is a future refactor that quietly
removes a guard. Several of them work by static analysis of the source tree
rather than by calling code, precisely so that a property cannot regress by
someone adding an import.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.core.config import Settings
from app.core.enums import ExecutionVenue, RiskStatus
from app.engines.authorization import (
    AuthorizationDenied,
    ExecutionAuthorizationService,
    ExecutionToken,
    verify_token,
)
from app.engines.killswitch import KillSwitchReport, SwitchState
from app.core.enums import KillSwitch
from app.engines.risk import RiskDecisionResult

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app"


# ---------------------------------------------------------------------------
# Static structure: who is allowed to import the execution package
# ---------------------------------------------------------------------------
def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize(
    "relative_path",
    [
        "engines/probability.py",
        "engines/edge.py",
        "engines/classification.py",
        "engines/modelability.py",
        "engines/calibration.py",
        "engines/liquidity.py",
        "api/routes.py",
        "api/main.py",
        "api/security.py",
        "api/health.py",
    ],
)
def test_module_cannot_reach_execution(relative_path: str) -> None:
    """The probability engine, the edge engine and the whole API layer must not
    be able to import the execution package. If one of them ever can, the
    execution boundary has a hole in it whether or not anyone is using it yet."""
    path = APP_ROOT / relative_path
    offenders = {m for m in _imported_modules(path) if m.startswith("app.execution")}
    assert not offenders, (
        f"{relative_path} imports {offenders}. Nothing outside the worker's "
        "execution step may reach the execution package."
    )


def test_llm_package_cannot_reach_execution_or_risk() -> None:
    """If an LLM layer exists, it must not touch execution or risk limits."""
    llm_dir = APP_ROOT / "engines" / "llm"
    if not llm_dir.exists():
        pytest.skip("no LLM layer is implemented")
    for path in llm_dir.rglob("*.py"):
        imported = _imported_modules(path)
        forbidden = {
            m for m in imported if m.startswith(("app.execution", "app.engines.risk", "app.engines.authorization"))
        }
        assert not forbidden, f"{path} imports {forbidden}"


def test_no_api_route_creates_an_order() -> None:
    """The HTTP surface must contain no order-placing endpoint.

    Checked by reading the route module's source: if someone adds a POST route
    whose name suggests execution, this fails loudly.
    """
    source = (APP_ROOT / "api" / "routes.py").read_text().lower()
    for forbidden in ("def place_order", "def submit_order", "def create_order", "def execute"):
        assert forbidden not in source, f"api/routes.py defines {forbidden}"


# ---------------------------------------------------------------------------
# Configuration guards
# ---------------------------------------------------------------------------
def test_live_trading_defaults_to_false() -> None:
    assert Settings(allow_insecure_local=True, api_key="").live_trading_enabled is False


def test_all_kill_switches_default_to_tripped() -> None:
    """Fail-closed means the unknown state is the stopped state."""
    s = Settings(allow_insecure_local=True, api_key="")
    assert s.global_kill_switch is True
    assert s.data_kill_switch is True
    assert s.model_kill_switch is True
    assert s.risk_kill_switch is True
    assert s.connectivity_kill_switch is True


def test_live_trading_refused_outside_phase_3() -> None:
    with pytest.raises(ValueError, match="PHASE_3"):
        Settings(allow_insecure_local=True, api_key="", live_trading_enabled=True, current_phase="PHASE_1")


def test_settings_are_frozen() -> None:
    """A limit that can be mutated at runtime is not a limit."""
    s = Settings(allow_insecure_local=True, api_key="")
    with pytest.raises(Exception):
        s.max_position_size_percent = 100.0  # type: ignore[misc]
    with pytest.raises(Exception):
        s.live_trading_enabled = True  # type: ignore[misc]


def test_debug_refused_on_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="debug"):
        Settings(api_key="x", debug=True, bind_host="0.0.0.0")


def test_missing_api_key_refused_unless_explicitly_insecure() -> None:
    with pytest.raises(ValueError, match="API_KEY"):
        Settings(api_key="", allow_insecure_local=False)


# ---------------------------------------------------------------------------
# Authorization service
# ---------------------------------------------------------------------------
def _clear_switches() -> KillSwitchReport:
    return KillSwitchReport(
        states={s: SwitchState(s, False, "clear") for s in KillSwitch}
    )


def _tripped_switches() -> KillSwitchReport:
    states = {s: SwitchState(s, False, "clear") for s in KillSwitch}
    states[KillSwitch.DATA] = SwitchState(KillSwitch.DATA, True, "data is stale")
    return KillSwitchReport(states=states)


def _approved(size: float = 100.0) -> RiskDecisionResult:
    from datetime import UTC, datetime

    return RiskDecisionResult(
        status=RiskStatus.APPROVED,
        reasons=["ok"],
        approved_size_usd=size,
        limits_snapshot={},
        kill_switches={},
        checked_at=datetime.now(UTC),
        risk_latency_ms=1,
    )


def _rejected() -> RiskDecisionResult:
    from datetime import UTC, datetime

    return RiskDecisionResult(
        status=RiskStatus.REJECTED,
        reasons=["nope"],
        approved_size_usd=None,
        limits_snapshot={},
        kill_switches={},
        checked_at=datetime.now(UTC),
        risk_latency_ms=1,
    )


def test_live_token_refused_in_phase_1(settings: Settings) -> None:
    service = ExecutionAuthorizationService(settings)
    with pytest.raises(AuthorizationDenied, match="LIVE_TRADING_ENABLED"):
        service.authorize(
            venue=ExecutionVenue.LIVE,
            signal_id=1, market_id=1, token_id="t",
            risk_decision=_approved(),
            kill_switches=_clear_switches(),
        )


def test_paper_token_refused_in_phase_1(settings: Settings) -> None:
    """Phase 1 does not simulate execution either."""
    service = ExecutionAuthorizationService(settings)
    with pytest.raises(AuthorizationDenied, match="PHASE_2"):
        service.authorize(
            venue=ExecutionVenue.PAPER,
            signal_id=1, market_id=1, token_id="t",
            risk_decision=_approved(),
            kill_switches=_clear_switches(),
        )


def test_no_token_when_a_kill_switch_is_tripped() -> None:
    s = Settings(allow_insecure_local=True, api_key="", current_phase="PHASE_2")
    service = ExecutionAuthorizationService(s)
    with pytest.raises(AuthorizationDenied, match="kill switch"):
        service.authorize(
            venue=ExecutionVenue.PAPER,
            signal_id=1, market_id=1, token_id="t",
            risk_decision=_approved(),
            kill_switches=_tripped_switches(),
        )


def test_no_token_without_risk_approval() -> None:
    s = Settings(allow_insecure_local=True, api_key="", current_phase="PHASE_2")
    service = ExecutionAuthorizationService(s)
    with pytest.raises(AuthorizationDenied, match="REJECTED"):
        service.authorize(
            venue=ExecutionVenue.PAPER,
            signal_id=1, market_id=1, token_id="t",
            risk_decision=_rejected(),
            kill_switches=_clear_switches(),
        )


def test_paper_token_issued_in_phase_2_and_verifies() -> None:
    s = Settings(allow_insecure_local=True, api_key="", current_phase="PHASE_2")
    token = ExecutionAuthorizationService(s).authorize(
        venue=ExecutionVenue.PAPER,
        signal_id=7, market_id=3, token_id="tok",
        risk_decision=_approved(250.0),
        kill_switches=_clear_switches(),
    )
    assert token.size_usd == 250.0
    verify_token(token, expected_venue=ExecutionVenue.PAPER)


def test_forged_token_is_rejected() -> None:
    """A token constructed anywhere other than the authorization service must
    not be accepted, even if every field looks right."""
    from datetime import UTC, datetime, timedelta

    forged = ExecutionToken(
        venue=ExecutionVenue.PAPER,
        signal_id=1, market_id=1, token_id="t",
        size_usd=1_000_000.0,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        signature="0" * 64,
    )
    with pytest.raises(AuthorizationDenied, match="signature"):
        verify_token(forged, expected_venue=ExecutionVenue.PAPER)


def test_tampered_token_is_rejected() -> None:
    """Changing the size after signing must invalidate the token."""
    import dataclasses

    s = Settings(allow_insecure_local=True, api_key="", current_phase="PHASE_2")
    token = ExecutionAuthorizationService(s).authorize(
        venue=ExecutionVenue.PAPER,
        signal_id=1, market_id=1, token_id="t",
        risk_decision=_approved(100.0),
        kill_switches=_clear_switches(),
    )
    tampered = dataclasses.replace(token, size_usd=999_999.0)
    with pytest.raises(AuthorizationDenied, match="signature"):
        verify_token(tampered, expected_venue=ExecutionVenue.PAPER)


def test_paper_token_cannot_be_used_by_the_live_adapter() -> None:
    s = Settings(allow_insecure_local=True, api_key="", current_phase="PHASE_2")
    token = ExecutionAuthorizationService(s).authorize(
        venue=ExecutionVenue.PAPER,
        signal_id=1, market_id=1, token_id="t",
        risk_decision=_approved(),
        kill_switches=_clear_switches(),
    )
    with pytest.raises(AuthorizationDenied, match="venue"):
        verify_token(token, expected_venue=ExecutionVenue.LIVE)


def test_expired_token_is_rejected() -> None:
    import dataclasses
    from datetime import UTC, datetime, timedelta

    s = Settings(allow_insecure_local=True, api_key="", current_phase="PHASE_2")
    token = ExecutionAuthorizationService(s).authorize(
        venue=ExecutionVenue.PAPER,
        signal_id=1, market_id=1, token_id="t",
        risk_decision=_approved(),
        kill_switches=_clear_switches(),
    )
    stale = dataclasses.replace(token, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(AuthorizationDenied, match="expired"):
        verify_token(stale, expected_venue=ExecutionVenue.LIVE if False else ExecutionVenue.PAPER)


# ---------------------------------------------------------------------------
# Live adapter
# ---------------------------------------------------------------------------
def test_live_adapter_refuses_to_construct(settings: Settings) -> None:
    from app.execution.live import LiveExecutionAdapter, LiveTradingDisabled

    with pytest.raises(LiveTradingDisabled, match="LIVE_TRADING_ENABLED is false"):
        LiveExecutionAdapter(settings)


def test_live_adapter_refuses_even_in_phase_3_without_recorded_gates() -> None:
    """Setting the flag is necessary and nowhere near sufficient."""
    from app.execution.live import LiveExecutionAdapter, LiveTradingDisabled

    s = Settings(
        allow_insecure_local=True, api_key="",
        live_trading_enabled=True, current_phase="PHASE_3",
    )
    with pytest.raises(LiveTradingDisabled):
        LiveExecutionAdapter(s, session=None)


def test_live_module_contains_no_venue_client_or_credentials() -> None:
    """The live module must stay inert: no HTTP, no signing, no keys."""
    source = (APP_ROOT / "execution" / "live.py").read_text().lower()
    for forbidden in ("httpx", "requests", "private_key", "web3", "sign_order", "eth_account"):
        assert forbidden not in source, f"execution/live.py references {forbidden}"
