"""Structured JSON logging with mandatory secret redaction.

Two properties matter here:

1. Every record is a single JSON object carrying timestamp, level, component,
   event, correlation_id, and where applicable market_id and error_code.
2. Nothing that looks like a credential survives the formatter. Redaction runs
   over the fully-rendered record, not over the arguments, because the ways a
   secret reaches a log line are more numerous than the ways one intends it to.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

REDACTED = "***REDACTED***"

# Key names whose *values* must never appear, whatever the surrounding syntax.
_SECRET_KEY_NAMES = (
    "api_key",
    "apikey",
    "api-key",
    "private_key",
    "privatekey",
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "auth",
    "session",
    "credential",
    "mnemonic",
    "seed_phrase",
    "passphrase",
)

_KEY_VALUE_PATTERNS = [
    # "api_key": "value"  /  api_key=value  /  api_key: value
    re.compile(
        rf'(?i)(["\']?(?:{"|".join(_SECRET_KEY_NAMES)})["\']?\s*[:=]\s*)'
        r'(["\']?)([^\s,;"\'}\)]+)(\2)'
    ),
]

_VALUE_PATTERNS = [
    # Ethereum-style private keys / long hex blobs.
    re.compile(r"(?i)\b0x[0-9a-f]{40,}\b"),
    # Bearer tokens.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),
    # Anthropic / OpenAI style keys.
    re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}\b"),
    # Postgres URLs with inline credentials.
    re.compile(r"(?i)\b(postgres(?:ql)?(?:\+\w+)?://)[^:/@\s]+:[^@\s]+@"),
]


def redact(text: str) -> str:
    """Remove anything that looks like a credential from a rendered string."""
    if not text:
        return text
    out = text
    for pattern in _KEY_VALUE_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(4)}", out)
    for pattern in _VALUE_PATTERNS:
        if pattern.pattern.startswith("(?i)\\b(postgres"):
            out = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}@", out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", record.getMessage()),
            "correlation_id": getattr(record, "correlation_id", None) or _correlation_id.get(),
        }
        for optional in ("market_id", "error_code", "model_version", "detail"):
            value = getattr(record, optional, None)
            if value is not None:
                payload[optional] = value

        if record.getMessage() != payload["event"]:
            payload["message"] = record.getMessage()

        if record.exc_info:
            # The traceback goes to the log, never to an HTTP client.
            payload["exception"] = self.formatException(record.exc_info)

        rendered = json.dumps(payload, default=str, ensure_ascii=False)
        return redact(rendered)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx logs full request URLs at INFO, which can carry query credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex
    _correlation_id.set(cid)
    return cid


def set_correlation_id(cid: str | None) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def get_logger(component: str) -> logging.LoggerAdapter:
    """Return a logger that always stamps its component name."""
    return logging.LoggerAdapter(logging.getLogger(component), {"component": component})
