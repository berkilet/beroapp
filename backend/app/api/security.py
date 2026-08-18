"""API authentication, authorisation, rate limiting and hardening middleware.

The API is read-mostly by design. The only writable surface is the kill
switches, and they can only ever move toward *safer* — there is no route in
this application that places an order, sizes a position, changes a risk limit,
or enables live trading.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from enum import Enum

from fastapi import Header, HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, new_correlation_id, set_correlation_id

log = get_logger("api.security")

API_KEY_HEADER = "X-API-Key"


class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"


def _constant_time_match(candidate: str, expected: str) -> bool:
    """Always compare, even when expected is empty, so timing does not reveal
    whether a key is configured."""
    return bool(expected) and secrets.compare_digest(candidate, expected)


async def require_viewer(
    request: Request,
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> Role:
    settings = get_settings()

    if settings.allow_insecure_local and not settings.api_key.get_secret_value():
        # Explicitly opted into by an operator for loopback development. The
        # config validator already refuses this combination on a non-loopback bind.
        request.state.role = Role.OPERATOR
        return Role.OPERATOR

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if _constant_time_match(x_api_key, settings.operator_api_key.get_secret_value()):
        request.state.role = Role.OPERATOR
        return Role.OPERATOR
    if _constant_time_match(x_api_key, settings.api_key.get_secret_value()):
        request.state.role = Role.VIEWER
        return Role.VIEWER

    log.warning(
        "rejected API key",
        extra={"event": "auth_failed", "detail": {"path": request.url.path}},
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


async def require_operator(
    request: Request,
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> Role:
    role = await require_viewer(request, x_api_key)
    if role is not Role.OPERATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this action requires the operator role",
        )
    return role


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window limiter keyed by API key, falling back to client host.

    Keyed by credential rather than IP alone so that one misbehaving client
    cannot be hidden behind a shared address, and so a leaked key is
    self-limiting.
    """

    def __init__(self, app, settings: Settings | None = None) -> None:
        super().__init__(app)
        self.settings = settings or get_settings()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in ("/health", "/readiness"):
            return await call_next(request)

        identity = request.headers.get(API_KEY_HEADER) or (
            request.client.host if request.client else "unknown"
        )
        is_operator_route = request.url.path.startswith("/api/system/")
        limit = (
            self.settings.operator_rate_limit_per_minute
            if is_operator_route
            else self.settings.api_rate_limit_per_minute
        )
        bucket = f"{'op' if is_operator_route else 'api'}:{identity}"

        now = time.monotonic()
        window = self._hits[bucket]
        while window and now - window[0] > 60.0:
            window.popleft()

        if len(window) >= limit:
            retry_after = max(1, int(60 - (now - window[0])))
            log.warning(
                "rate limit exceeded",
                extra={"event": "rate_limited", "detail": {"path": request.url.path}},
            )
            return Response(
                content='{"error":{"code":"rate_limited","message":"too many requests"}}',
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )

        window.append(now)
        return await call_next(request)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before anything tries to parse them."""

    def __init__(self, app, settings: Settings | None = None) -> None:
        super().__init__(app)
        self.max_bytes = (settings or get_settings()).max_request_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    return Response(
                        content='{"error":{"code":"payload_too_large","message":"request body too large"}}',
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        media_type="application/json",
                    )
            except ValueError:
                return Response(
                    content='{"error":{"code":"bad_request","message":"invalid content-length"}}',
                    status_code=status.HTTP_400_BAD_REQUEST,
                    media_type="application/json",
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id and a conservative set of response headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("X-Correlation-ID")
        # Never echo an unbounded caller-supplied value back into logs.
        correlation_id = (
            incoming[:64] if incoming and incoming.isalnum() else new_correlation_id()
        )
        set_correlation_id(correlation_id)
        request.state.correlation_id = correlation_id

        response = await call_next(request)

        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # This API serves JSON only; nothing should ever be executed from it.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
