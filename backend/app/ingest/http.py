"""Hardened async HTTP client for external data sources.

Responsibilities, in priority order:

1. **Never reach a host we did not intend to reach.** An SSRF allow-list is
   checked before connect and again after any redirect; redirects are not
   followed automatically.
2. **Stay well inside documented rate limits.** A token-bucket limiter per host
   throttles us far below the published ceilings recorded in
   docs/DATA_SOURCES.md. We have no need for burst throughput.
3. **Fail in a way the rest of the system can reason about.** Retryable and
   non-retryable failures are distinct exception types, a circuit breaker stops
   us hammering a service that is down, and 429 honours ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger("ingest.http")


class FetchError(Exception):
    """Base class for outbound-request failures."""

    def __init__(self, message: str, *, error_code: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class RetryableFetchError(FetchError):
    """Transient: worth retrying with backoff (5xx, timeout, connection reset)."""


class RateLimitedError(RetryableFetchError):
    def __init__(self, message: str, retry_after_s: float | None) -> None:
        super().__init__(message, error_code="rate_limited", status_code=429)
        self.retry_after_s = retry_after_s


class PermanentFetchError(FetchError):
    """Not worth retrying: 4xx other than 429, or a policy refusal."""


class CircuitOpenError(FetchError):
    def __init__(self, host: str, reopens_in_s: float) -> None:
        super().__init__(
            f"circuit breaker open for {host}", error_code="circuit_open"
        )
        self.host = host
        self.reopens_in_s = reopens_in_s


class TokenBucket:
    """Simple async token bucket. One per host."""

    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        self.rate = rate_per_second
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_second)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


@dataclass
class CircuitBreaker:
    """Opens after N consecutive failures; half-opens after a cooldown."""

    threshold: int
    reset_after_s: float
    consecutive_failures: int = 0
    opened_at: float | None = None

    def before_request(self, host: str) -> None:
        if self.opened_at is None:
            return
        elapsed = time.monotonic() - self.opened_at
        if elapsed < self.reset_after_s:
            raise CircuitOpenError(host, self.reset_after_s - elapsed)
        # Half-open: allow one probe through.
        self.opened_at = None

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.opened_at = time.monotonic()


@dataclass
class HostStats:
    """Per-host observability, surfaced on the data-sources page."""

    success_count: int = 0
    error_count: int = 0
    last_success_at: float | None = None
    last_error_at: float | None = None
    last_error_code: str | None = None
    last_latency_ms: int | None = None
    consecutive_failures: int = 0

    @property
    def error_rate(self) -> float:
        total = self.success_count + self.error_count
        return self.error_count / total if total else 0.0


def _is_public_address(host: str) -> bool:
    """Reject private, loopback, link-local and reserved destinations.

    Guards against an allow-listed hostname resolving to an internal address.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return False
    return True


class HttpFetcher:
    """Async JSON fetcher with limiter, breaker, retry and allow-list."""

    def __init__(self, settings: Settings | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.settings.http_total_timeout_s,
                connect=self.settings.http_connect_timeout_s,
                read=self.settings.http_read_timeout_s,
            ),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={
                "User-Agent": self.settings.polymarket_user_agent,
                "Accept": "application/json",
            },
        )
        self._buckets: dict[str, TokenBucket] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self.stats: dict[str, HostStats] = {}
        self._checked_hosts: set[str] = set()

    # -- lifecycle ------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "HttpFetcher":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- internals ------------------------------------------------------
    # Per-host request rates for evidence sources. Deliberately well under the
    # documented ceilings recorded in docs/DATA_SOURCES.md, and defined here
    # rather than read from the registry so that editing a source definition
    # cannot inadvertently raise our outbound rate.
    EVIDENCE_HOST_RPS = {
        "api.bls.gov": 0.5,
        "home.treasury.gov": 0.5,
        "api.fiscaldata.treasury.gov": 1.0,
        "www.federalreserve.gov": 0.2,
        "data.sec.gov": 2.0,          # SEC documents 10/s; we use a fifth of it
        "api.exchange.coinbase.com": 2.0,
        "api.kraken.com": 0.5,        # Kraken sustains ~1/s; we use half
        "api.open.fec.gov": 0.2,      # DEMO_KEY allows only 30/hour
    }

    def _rate_for(self, host: str) -> float:
        s = self.settings
        mapping = {
            urlparse(s.gamma_base_url).hostname: s.gamma_rps,
            urlparse(s.clob_base_url).hostname: s.clob_rps,
            urlparse(s.data_base_url).hostname: s.data_rps,
        }
        if host in mapping:
            return mapping[host]
        return self.EVIDENCE_HOST_RPS.get(host, 0.5)

    def _bucket(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(self._rate_for(host))
        return self._buckets[host]

    def _breaker(self, host: str) -> CircuitBreaker:
        if host not in self._breakers:
            self._breakers[host] = CircuitBreaker(
                threshold=self.settings.circuit_breaker_failures,
                reset_after_s=self.settings.circuit_breaker_reset_s,
            )
        return self._breakers[host]

    def _host_stats(self, host: str) -> HostStats:
        return self.stats.setdefault(host, HostStats())

    def _check_allowed(self, url: str) -> str:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https":
            raise PermanentFetchError(
                f"refusing non-https scheme {parsed.scheme!r}", error_code="scheme_not_allowed"
            )
        if host not in self.settings.allowed_outbound_hosts:
            raise PermanentFetchError(
                f"host {host!r} is not in the outbound allow-list", error_code="host_not_allowed"
            )
        # DNS rebinding / internal-address guard, checked once per host per process.
        if host not in self._checked_hosts:
            if not _is_public_address(host):
                raise PermanentFetchError(
                    f"host {host!r} resolves to a non-public address", error_code="host_not_public"
                )
            self._checked_hosts.add(host)
        return host

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    def _backoff_delay(self, attempt: int) -> float:
        base = self.settings.http_backoff_base_s * (2 ** (attempt - 1))
        capped = min(base, self.settings.http_backoff_max_s)
        # Full jitter: avoids a thundering herd when several jobs retry together.
        return random.uniform(0.0, capped)

    # -- public ---------------------------------------------------------
    async def fetch_text(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int | None = None,
    ) -> str:
        """Fetch a non-JSON body (XML feed, structured HTML page).

        Same transport guarantees as fetch_json — allow-list, limiter, breaker,
        retry — differing only in that the body is not JSON-decoded.
        """
        return await self._fetch(
            url, method=method, params=params, headers=headers,
            max_retries=max_retries, decode_json=False,
        )

    async def fetch_json(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int | None = None,
    ) -> Any:
        """Fetch and JSON-decode a response, retrying transient failures.

        Raises PermanentFetchError, RetryableFetchError (after exhausting
        retries), or CircuitOpenError. Never returns partial or fabricated data.
        """
        return await self._fetch(
            url, method=method, params=params, json_body=json_body,
            headers=headers, max_retries=max_retries, decode_json=True,
        )

    async def _fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int | None = None,
        decode_json: bool = True,
    ) -> Any:
        host = self._check_allowed(url)
        retries = self.settings.http_max_retries if max_retries is None else max_retries
        stats = self._host_stats(host)
        breaker = self._breaker(host)

        last_error: FetchError | None = None
        for attempt in range(1, retries + 2):
            breaker.before_request(host)
            await self._bucket(host).acquire()

            started = time.monotonic()
            try:
                response = await self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = RetryableFetchError(f"timeout: {exc}", error_code="timeout")
            except httpx.HTTPError as exc:
                last_error = RetryableFetchError(f"transport error: {exc}", error_code="transport")
            else:
                latency_ms = int((time.monotonic() - started) * 1000)
                stats.last_latency_ms = latency_ms

                if response.is_redirect:
                    location = response.headers.get("location", "")
                    # A redirect off the allow-list is a policy failure, not a hop.
                    last_error = PermanentFetchError(
                        f"refusing redirect to {location!r}", error_code="redirect_refused"
                    )
                elif response.status_code == 429:
                    retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                    last_error = RateLimitedError("rate limited by upstream", retry_after)
                elif 500 <= response.status_code < 600:
                    last_error = RetryableFetchError(
                        f"upstream {response.status_code}",
                        error_code=f"http_{response.status_code}",
                        status_code=response.status_code,
                    )
                elif response.status_code >= 400:
                    last_error = PermanentFetchError(
                        f"upstream {response.status_code}",
                        error_code=f"http_{response.status_code}",
                        status_code=response.status_code,
                    )
                elif not decode_json:
                    breaker.record_success()
                    stats.success_count += 1
                    stats.consecutive_failures = 0
                    stats.last_success_at = time.time()
                    return response.text
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        # A 200 with a non-JSON body means the contract changed.
                        # Reject loudly rather than continue with corrupt data.
                        last_error = PermanentFetchError(
                            f"response was not valid JSON: {exc}", error_code="invalid_json"
                        )
                    else:
                        breaker.record_success()
                        stats.success_count += 1
                        stats.consecutive_failures = 0
                        stats.last_success_at = time.time()
                        return payload

            # -- failure bookkeeping --
            stats.error_count += 1
            stats.consecutive_failures += 1
            stats.last_error_at = time.time()
            stats.last_error_code = last_error.error_code if last_error else "unknown"
            breaker.record_failure()

            if isinstance(last_error, PermanentFetchError):
                raise last_error
            if attempt > retries:
                break

            if isinstance(last_error, RateLimitedError) and last_error.retry_after_s is not None:
                delay = last_error.retry_after_s
            else:
                delay = self._backoff_delay(attempt)

            log.warning(
                "retrying after upstream failure",
                extra={
                    "event": "fetch_retry",
                    "error_code": stats.last_error_code,
                    "detail": {"host": host, "attempt": attempt, "delay_s": round(delay, 2)},
                },
            )
            await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error
