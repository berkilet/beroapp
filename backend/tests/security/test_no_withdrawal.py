"""Absence of wallet-control functionality.

The spec is absolute: no withdrawals, no transfers, no wallet export, no
private-key display, no unrestricted wallet control. This test enforces that by
scanning the entire tree, so the capability cannot appear later without the
build failing.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BACKEND_APP = REPO_ROOT / "backend" / "app"
FRONTEND = REPO_ROOT / "frontend"

# Patterns that would indicate wallet-control capability. Word-boundaried so
# that ordinary prose ("the venue's withdrawal history endpoint is documented
# but not used") does not trip them — only identifiers do.
FORBIDDEN_IDENTIFIERS = [
    r"\bdef\s+withdraw",
    r"\bdef\s+transfer_funds",
    r"\bdef\s+export_wallet",
    r"\bdef\s+export_private_key",
    r"\bdef\s+reveal_private_key",
    r"\bdef\s+send_transaction",
    r"\bdef\s+sign_transaction",
    r"\bwithdraw\s*\(",
    r"\bprivate_key\s*=",
    r"\beth_account\b",
    r"\bfrom\s+web3\b",
    r"\bimport\s+web3\b",
]

FORBIDDEN_ROUTES = [
    r'["\']/withdraw',
    r'["\']/transfer',
    r'["\']/wallet',
    r'["\']/api/withdraw',
]


def _source_files() -> list[pathlib.Path]:
    files = list(BACKEND_APP.rglob("*.py"))
    if FRONTEND.exists():
        for pattern in ("app/**/*.tsx", "app/**/*.ts", "components/**/*.tsx", "lib/**/*.ts"):
            files.extend(
                p for p in FRONTEND.glob(pattern)
                if "node_modules" not in str(p) and ".next" not in str(p)
            )
    return files


def test_no_withdrawal_or_wallet_control_capability() -> None:
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text()
        for pattern in FORBIDDEN_IDENTIFIERS + FORBIDDEN_ROUTES:
            for match in re.finditer(pattern, text, re.I):
                line_no = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)!r}")

    assert not offenders, (
        "wallet-control capability detected. This repository must never implement "
        f"withdrawals, transfers, key export or transaction signing: {offenders}"
    )


def test_no_wallet_signing_dependency_is_declared() -> None:
    """Not even as an unused dependency."""
    requirements = (REPO_ROOT / "backend" / "requirements.txt").read_text().lower()
    for package in ("web3", "eth-account", "eth_account", "py-clob-client", "eth-keys"):
        assert package not in requirements, f"{package} is declared as a dependency"


def test_data_api_user_endpoints_are_not_called() -> None:
    """We hold no account, so nothing may query account-scoped endpoints."""
    client_source = (BACKEND_APP / "ingest" / "polymarket.py").read_text()
    for endpoint in ("/positions", "/closed-positions", "/value", "/activity", "/balance-allowance"):
        assert f'data_base_url}}{endpoint}' not in client_source
        assert f'clob_base_url}}{endpoint}' not in client_source


def test_no_order_placement_endpoint_is_called() -> None:
    """The CLOB order endpoints must not appear in the client at all."""
    client_source = (BACKEND_APP / "ingest" / "polymarket.py").read_text()
    for endpoint in ("/order", "/orders", "/cancel-all", "/auth/api-key", "/auth/derive-api-key"):
        assert f'"{endpoint}"' not in client_source
        assert f"{{self.settings.clob_base_url}}{endpoint}" not in client_source
