"""Live execution adapter — DELIBERATELY NOT IMPLEMENTED.

This module exists so that the shape of the execution boundary is visible and
testable, and so that a future maintainer adding live trading has to walk past
every guard rather than discovering the absence of one.

It contains **no venue client, no signing code, no credential handling, and no
network call**. Constructing the adapter raises unless every phase gate and
operator authorisation is recorded, and even then `submit` raises
`NotImplementedError`, because the order-placement path has not been written,
reviewed, or tested.

Explicitly absent, and to remain absent:
  * withdrawals
  * transfers
  * wallet export
  * private-key display or storage
  * any general wallet control

See docs/SECURITY.md and docs/PHASE_GATES.md.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.enums import ExecutionVenue
from app.engines.authorization import (
    AuthorizationDenied,
    ExecutionToken,
    verify_token,
)


class LiveTradingDisabled(RuntimeError):
    """Raised when anything attempts to construct or use the live adapter."""


class LiveExecutionAdapter:
    """Refuses to exist unless the system is fully authorised for live trading.

    The refusal happens in ``__init__`` rather than in ``submit`` on purpose: a
    partially-configured system should not get a partially-working adapter it
    might later hand a token to.
    """

    def __init__(self, settings: Settings | None = None, session: object | None = None) -> None:
        self.settings = settings or get_settings()

        if not self.settings.live_trading_enabled:
            raise LiveTradingDisabled(
                "LIVE_TRADING_ENABLED is false. The live execution adapter cannot be "
                "constructed. This is the default and intended state."
            )
        if self.settings.current_phase != "PHASE_3":
            raise LiveTradingDisabled(
                f"live execution requires PHASE_3; current phase is "
                f"{self.settings.current_phase}"
            )

        # Reuse the authorization service's checks rather than duplicating them,
        # so there is exactly one definition of "permitted to trade live".
        from app.engines.authorization import ExecutionAuthorizationService

        try:
            ExecutionAuthorizationService(self.settings)._assert_live_permitted(session)  # noqa: SLF001
        except AuthorizationDenied as exc:
            raise LiveTradingDisabled(f"live execution is not authorised: {exc}") from exc

    def submit(self, token: ExecutionToken) -> None:
        verify_token(token, expected_venue=ExecutionVenue.LIVE)
        raise NotImplementedError(
            "Live order placement is not implemented. Phase 3 is an architectural "
            "capability in this repository, not a working path: no venue client, "
            "signing code, or credential handling exists. Implementing it requires "
            "completing the checklist in docs/PHASE_GATES.md first."
        )
