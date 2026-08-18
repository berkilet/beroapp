"""Secret handling, and source-level assertions about dangerous constructs.

The source-scanning tests are intentionally blunt. A grep that occasionally
needs a deliberate exception is far better than a property that silently stops
holding.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import re

import pytest

from app.core.config import Settings
from app.core.logging import configure_logging, redact

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PY_FILES = sorted(APP_ROOT.rglob("*.py"))

FAKE_SECRETS = [
    "sk-ant-api03-THIS_IS_A_FAKE_TEST_KEY_0123456789",
    "0x" + "ab" * 32,
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "postgresql+psycopg://user:sup3rs3cr3t@127.0.0.1:5432/db",
]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("secret", FAKE_SECRETS)
def test_redact_removes_secret_shapes(secret: str) -> None:
    scrubbed = redact(f"connecting with {secret} now")
    assert "sup3rs3cr3t" not in scrubbed
    assert "sk-ant-api03-THIS_IS_A_FAKE_TEST_KEY_0123456789" not in scrubbed
    assert "ab" * 32 not in scrubbed
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        'api_key="hunter2secretvalue"',
        "password=hunter2secretvalue",
        "'authorization': 'hunter2secretvalue'",
        "private_key: hunter2secretvalue",
        "TOKEN=hunter2secretvalue",
    ],
)
def test_redact_removes_keyed_values(text: str) -> None:
    assert "hunter2secretvalue" not in redact(text)


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
def test_no_secret_survives_any_log_level(level: str, capsys: pytest.CaptureFixture) -> None:
    """The formatter runs over the fully rendered record, so it catches secrets
    arriving through the message, the args, or the extra dict alike."""
    configure_logging("DEBUG")
    logger = logging.getLogger("test.secrets")
    secret = "sk-ant-api03-THIS_IS_A_FAKE_TEST_KEY_0123456789"

    getattr(logger, level)(
        "connecting with %s",
        secret,
        extra={"event": "test", "detail": {"api_key": secret}},
    )
    output = capsys.readouterr().out
    assert secret not in output
    assert "REDACTED" in output


def test_exception_traceback_is_redacted(capsys: pytest.CaptureFixture) -> None:
    """A traceback is one of the easiest ways for a credential to escape."""
    configure_logging("DEBUG")
    logger = logging.getLogger("test.secrets")
    secret = "0x" + "cd" * 32
    try:
        raise ValueError(f"failed to authenticate with {secret}")
    except ValueError:
        logger.exception("boom", extra={"event": "test"})
    assert secret not in capsys.readouterr().out


def test_secret_str_does_not_leak_via_repr() -> None:
    s = Settings(allow_insecure_local=True, api_key="hunter2secretvalue")
    assert "hunter2secretvalue" not in repr(s)
    assert "hunter2secretvalue" not in str(s)
    assert "hunter2secretvalue" not in repr(s.api_key)
    # And is still retrievable when explicitly requested.
    assert s.api_key.get_secret_value() == "hunter2secretvalue"


def test_env_example_contains_no_real_values() -> None:
    text = (REPO_ROOT / "backend" / ".env.example").read_text()
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in {"API_KEY", "OPERATOR_API_KEY", "LLM_API_KEY", "FRED_API_KEY", "SEC_USER_AGENT"}:
            assert value.strip() == "", f"{key} has a value in .env.example"
    assert "CHANGE_ME" in text  # the database URL placeholder


def test_env_file_is_gitignored() -> None:
    ignore = (REPO_ROOT / ".gitignore").read_text()
    assert ".env" in ignore


SECRET_NAME = re.compile(
    r"(private_key|api_key|apikey|secret|password|passwd|token_secret|mnemonic|passphrase|credential)",
    re.I,
)

# Column names that contain a secret-ish word but cannot hold a secret value.
SECRET_NAME_EXEMPTIONS = {
    # A boolean flag recording whether a source needs a key. It stores True or
    # False, never the key.
    "requires_api_key",
}


def test_no_column_can_store_a_credential() -> None:
    """No table may have a place to put a credential.

    Parsed rather than grepped, so that a boolean flag whose *name* mentions a
    key is distinguished from a string column that could actually hold one.
    """
    tree = ast.parse((APP_ROOT / "db" / "models.py").read_text())
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if not SECRET_NAME.search(name) or name in SECRET_NAME_EXEMPTIONS:
            continue

        # Reject anything that is not plainly a boolean flag.
        annotation = ast.unparse(node.annotation)
        if "bool" not in annotation:
            offenders.append(f"{name}: {annotation}")

    assert not offenders, (
        f"columns that could store a credential: {offenders}. "
        "No secret is ever persisted; secrets live only in the environment."
    )


# ---------------------------------------------------------------------------
# Dangerous constructs
# ---------------------------------------------------------------------------
def test_no_shell_or_eval_in_application_code() -> None:
    """The application never invokes a shell and never evaluates a string."""
    offenders: list[str] = []
    for path in PY_FILES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"eval", "exec", "compile"}:
                    offenders.append(f"{path.name}:{node.lineno} {node.func.id}()")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"subprocess", "pickle", "marshal", "shelve"}:
                        offenders.append(f"{path.name}:{node.lineno} imports {alias.name}")
            if isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "pickle", "marshal"}:
                offenders.append(f"{path.name}:{node.lineno} imports from {node.module}")
    assert not offenders, f"dangerous constructs found: {offenders}"


# The only place SQL text is formatted rather than parameter-bound:
#   db/session.py — SET statement_timeout, which PostgreSQL will not accept as a
#                   bind parameter; the value is int()-coerced first.
SQL_INTERPOLATION_EXEMPTIONS = {"db/session.py"}


def test_no_raw_sql_string_interpolation() -> None:
    """SQL is built by SQLAlchemy with bound parameters, never by formatting."""
    # Word-boundaried so that `findtext(f"d:{field}")` — an XPath expression in
    # the Treasury XML parser — is not mistaken for SQLAlchemy's `text()`.
    pattern = re.compile(r"""\b(execute|text)\s*\(\s*f["']""", re.I)
    offenders: list[str] = []
    for path in PY_FILES:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(APP_ROOT).as_posix()}:{i}: {line.strip()}")

    unexpected = [o for o in offenders if o.split(":")[0] not in SQL_INTERPOLATION_EXEMPTIONS]
    assert not unexpected, f"raw SQL interpolation: {unexpected}"


def test_exempted_sql_interpolation_cannot_take_request_input() -> None:
    """Guard the exemptions themselves rather than trusting the comment.

    Both exempted sites must coerce or constrain what they interpolate, so that
    adding a request-derived value there would be visible.
    """
    session_src = (APP_ROOT / "db" / "session.py").read_text()
    assert "SET statement_timeout = {int(" in session_src, (
        "the statement_timeout interpolation must int()-coerce its value"
    )

    # /metrics counts rows through mapped table objects; there must be no
    # dynamic SQL there at all.
    main_src = (APP_ROOT / "api" / "main.py").read_text()
    assert "FROM {" not in main_src, "/metrics reintroduced dynamic SQL"


def test_no_verify_false_anywhere() -> None:
    """TLS verification is never disabled."""
    for path in PY_FILES:
        text = path.read_text()
        assert "verify=False" not in text, f"{path} disables TLS verification"
        assert "VERIFY_NONE" not in text


def test_no_hardcoded_market_ids() -> None:
    """The spec forbids hard-coded markets; discovery must be dynamic.

    Detects long hex condition ids and the very long decimal CLOB token ids.
    """
    condition_id = re.compile(r"0x[0-9a-f]{60,}")
    token_id = re.compile(r"\b\d{60,}\b")
    for path in PY_FILES:
        text = path.read_text()
        assert not condition_id.search(text), f"{path} contains a hard-coded condition id"
        assert not token_id.search(text), f"{path} contains a hard-coded token id"


def test_no_dangerous_html_in_frontend() -> None:
    frontend = REPO_ROOT / "frontend"
    if not frontend.exists():
        pytest.skip("frontend not present")
    # Matches actual JSX usage (the prop being assigned), not the word appearing
    # in a comment explaining that it is not used.
    usage = re.compile(r"dangerouslySetInnerHTML\s*=")
    for path in list(frontend.glob("app/**/*.tsx")) + list(frontend.glob("components/**/*.tsx")):
        assert not usage.search(path.read_text()), f"{path} uses dangerouslySetInnerHTML"


def test_no_public_env_var_holds_a_credential() -> None:
    """NEXT_PUBLIC_ variables are inlined into the browser bundle."""
    frontend = REPO_ROOT / "frontend"
    if not frontend.exists():
        pytest.skip("frontend not present")
    pattern = re.compile(r"NEXT_PUBLIC_\w*(KEY|SECRET|TOKEN|PASSWORD)\w*", re.I)
    for path in list(frontend.glob("**/*.ts")) + list(frontend.glob("**/*.tsx")) + list(frontend.glob("*.example")):
        if "node_modules" in str(path) or ".next" in str(path):
            continue
        assert not pattern.search(path.read_text()), f"{path} exposes a credential to the browser"
