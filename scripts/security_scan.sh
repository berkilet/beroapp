#!/usr/bin/env bash
#
# Security checks. Run before any release and before any phase-gate evaluation.
# Exits non-zero if any check fails.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
VENV="$BACKEND/.venv/bin"
failures=0

section() { printf '\n=== %s\n' "$1"; }
check() {
    if "$@"; then
        printf '  PASS\n'
    else
        printf '  FAIL\n'
        failures=$((failures + 1))
    fi
}

section "Security test suite"
check "$VENV/pytest" "$BACKEND/tests/security" -q

section "Static analysis (bandit)"
check "$VENV/bandit" -q -r "$BACKEND/app" -ll

section "Dependency vulnerabilities (pip-audit)"
check "$VENV/pip-audit" --requirement "$BACKEND/requirements.txt" --progress-spinner off

section "Lint (ruff)"
check "$VENV/ruff" check "$BACKEND/app" "$BACKEND/tests"

section "Secret scan (working tree)"
if git -C "$ROOT" grep -nIE \
    '(sk-[A-Za-z0-9_-]{20,}|0x[0-9a-fA-F]{64}|-----BEGIN [A-Z ]*PRIVATE KEY-----)' \
    -- ':!*test*' ':!docs/*' > /tmp/secret_hits 2>/dev/null; then
    echo "  FAIL: possible secrets found"
    cat /tmp/secret_hits
    failures=$((failures + 1))
else
    echo "  PASS"
fi

section "Secret scan (git history)"
if git -C "$ROOT" log --all -p 2>/dev/null | grep -qE '^\+.*(sk-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'; then
    echo "  FAIL: a secret appears in git history"
    failures=$((failures + 1))
else
    echo "  PASS"
fi

section "Committed .env"
if git -C "$ROOT" ls-files --error-unmatch backend/.env >/dev/null 2>&1; then
    echo "  FAIL: backend/.env is tracked by git"
    failures=$((failures + 1))
else
    echo "  PASS"
fi

section "Live trading disabled by default"
if grep -q 'live_trading_enabled: bool = False' "$BACKEND/app/core/config.py"; then
    echo "  PASS"
else
    echo "  FAIL: LIVE_TRADING_ENABLED no longer defaults to false"
    failures=$((failures + 1))
fi

printf '\n=====================================\n'
if [ "$failures" -eq 0 ]; then
    echo "All security checks passed."
    echo "This is evidence, not proof. See docs/SECURITY.md 'Known limitations'."
    exit 0
fi
echo "$failures check(s) FAILED."
exit 1
