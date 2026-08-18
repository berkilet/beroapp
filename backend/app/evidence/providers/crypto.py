"""Crypto market-data connectors.

Two independent exchanges. That is deliberate: crypto threshold markets ("will
BTC be above $100k") resolve on a price, and a single venue's quote is a single
point of failure both technically and economically. Two venues disagreeing
materially is itself a data-quality signal worth recording, and the conflict
engine records it.

**Important scoping note.** An exchange price is evidence about a crypto price
market; it is *not* an independent forecast of one. Knowing BTC is at $64,000
tells you a great deal about "will BTC exceed $64,000 tomorrow" and very little
about "will BTC exceed $150,000 next year". The feature layer is what turns spot
and realised volatility into a probability, and it is careful to say when the
horizon makes that estimate worthless.

Verified 2026-08-18: Coinbase Exchange and Kraken public endpoints both respond
without a key.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

from app.core.enums import (
    ComponentHealth,
    EvidenceType,
    MarketCategory,
    MarketSubcategory,
    SourceType,
    VerificationStatus,
)
from app.evidence.base import EvidenceError, EvidenceItem, EvidenceProvider
from app.ingest.http import FetchError

# Assets worth the request budget: the ones Polymarket actually lists markets on.
TRACKED_ASSETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("BTC", "BTC-USD", ("btc", "bitcoin")),
    ("ETH", "ETH-USD", ("eth", "ethereum", "ether")),
    ("SOL", "SOL-USD", ("sol", "solana")),
    ("XRP", "XRP-USD", ("xrp", "ripple")),
    ("DOGE", "DOGE-USD", ("doge", "dogecoin")),
)

# Kraken uses its own pair naming and returns keys that differ again from the
# request. Mapped explicitly rather than guessed.
KRAKEN_PAIRS: dict[str, str] = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
    "DOGE": "XDGUSD",
}

_CRYPTO_TAGS = ("crypto", "cryptocurrency", "price")


class CoinbaseExchangeProvider(EvidenceProvider):
    """Spot quotes plus daily candles, from which realised volatility is derived."""

    @property
    def request_cost(self) -> int:
        return len(TRACKED_ASSETS) * 2

    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        now = now or datetime.now(UTC)
        started = datetime.now(UTC)
        items: list[EvidenceItem] = []
        failures: list[str] = []

        for asset, product, tags in TRACKED_ASSETS:
            try:
                items.extend(await self._collect_asset(asset, product, tags, now))
            except FetchError as exc:
                # One asset failing must not lose the others. A market on ETH
                # should not go dark because the SOL product was delisted.
                failures.append(f"{asset}:{exc.error_code}")

        if not items:
            self._record_health(
                ComponentHealth.FAILED,
                f"no assets collected ({'; '.join(failures) or 'no data'})",
                error_code="all_assets_failed",
            )
            raise EvidenceError(
                "Coinbase returned no usable data for any tracked asset",
                source_key=self.source_key, error_code="all_assets_failed",
            )

        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._record_health(
            ComponentHealth.DEGRADED if failures else ComponentHealth.HEALTHY,
            f"{len(items)} observations"
            + (f"; failed: {', '.join(failures)}" if failures else ""),
            items=len(items), latency_ms=latency,
        )
        return items

    async def _collect_asset(
        self, asset: str, product: str, tags: tuple[str, ...], now: datetime
    ) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []

        ticker = await self.fetcher.fetch_json(
            f"{self.definition.base_url}/products/{product}/ticker", headers=self._headers()
        )
        price = _to_float(ticker.get("price")) if isinstance(ticker, dict) else None
        if price is not None and price > 0:
            items.append(
                self._item(
                    series_key=f"CRYPTO_SPOT_{asset}_USD",
                    title=f"{asset}/USD spot",
                    value=price,
                    unit="USD",
                    observation_date=_parse_iso(ticker.get("time")) or now,
                    now=now,
                    tags=tags,
                    payload={
                        "bid": _to_float(ticker.get("bid")),
                        "ask": _to_float(ticker.get("ask")),
                        "volume_24h": _to_float(ticker.get("volume")),
                        "product_id": product,
                    },
                )
            )

        # Daily candles: [time, low, high, open, close, volume]
        candles = await self.fetcher.fetch_json(
            f"{self.definition.base_url}/products/{product}/candles",
            params={"granularity": 86_400},
            headers=self._headers(),
        )
        closes = _extract_closes(candles)
        if len(closes) >= 30:
            volatility = _annualised_volatility(closes[:90])
            if volatility is not None:
                items.append(
                    self._item(
                        series_key=f"CRYPTO_VOL_{asset}_USD",
                        title=f"{asset}/USD realised volatility (annualised)",
                        value=volatility,
                        unit="fraction",
                        observation_date=now,
                        now=now,
                        tags=tags,
                        payload={"window_days": min(90, len(closes)), "source": "daily closes"},
                    )
                )

        return items

    def _item(
        self, *, series_key: str, title: str, value: float, unit: str,
        observation_date: datetime, now: datetime, tags: tuple[str, ...], payload: dict,
    ) -> EvidenceItem:
        return EvidenceItem(
            source_key=self.source_key,
            source_type=SourceType.MARKET_DATA,
            source_tier=1,
            evidence_type=EvidenceType.MARKET_QUOTE,
            series_key=series_key,
            title=title,
            numeric_value=value,
            unit=unit,
            observation_date=observation_date,
            known_at=now,
            reference_url="https://exchange.coinbase.com/",
            verification_status=VerificationStatus.CONFIRMED_FACT,
            reliability_score=self.definition.reliability_score,
            parser_version=self.definition.parser_version,
            payload=payload,
            subject_tags=tuple(tags) + _CRYPTO_TAGS,
            categories=(MarketCategory.CRYPTO,),
            subcategories=(MarketSubcategory.CRYPTO_PRICE,),
        )


class KrakenProvider(EvidenceProvider):
    """Independent spot cross-check."""

    @property
    def request_cost(self) -> int:
        return 1

    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        now = now or datetime.now(UTC)
        started = datetime.now(UTC)

        pairs = ",".join(KRAKEN_PAIRS[asset] for asset, _, _ in TRACKED_ASSETS if asset in KRAKEN_PAIRS)
        try:
            payload = await self.fetcher.fetch_json(
                f"{self.definition.base_url}/0/public/Ticker",
                params={"pair": pairs},
                headers=self._headers(),
            )
        except FetchError as exc:
            self._record_health(ComponentHealth.FAILED, str(exc)[:200], error_code=exc.error_code)
            raise EvidenceError(
                f"Kraken fetch failed: {exc}", source_key=self.source_key, error_code=exc.error_code
            ) from exc

        if not isinstance(payload, dict):
            raise EvidenceError(
                "Kraken returned a non-object payload",
                source_key=self.source_key, error_code="schema",
            )
        if payload.get("error"):
            # Kraken reports errors in-band with HTTP 200.
            message = "; ".join(str(e) for e in payload["error"])[:200]
            self._record_health(ComponentHealth.FAILED, message, error_code="kraken_error")
            raise EvidenceError(
                f"Kraken error: {message}", source_key=self.source_key, error_code="kraken_error"
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise EvidenceError(
                "Kraken payload missing result",
                source_key=self.source_key, error_code="schema",
            )

        items: list[EvidenceItem] = []
        for asset, _, tags in TRACKED_ASSETS:
            requested = KRAKEN_PAIRS.get(asset)
            if requested is None:
                continue
            entry = _find_kraken_entry(result, requested, asset)
            if entry is None:
                continue
            # "c" is [last trade price, lot volume].
            last = entry.get("c")
            price = _to_float(last[0]) if isinstance(last, list) and last else None
            if price is None or price <= 0:
                continue

            items.append(
                EvidenceItem(
                    source_key=self.source_key,
                    source_type=SourceType.MARKET_DATA,
                    source_tier=1,
                    evidence_type=EvidenceType.MARKET_QUOTE,
                    series_key=f"CRYPTO_SPOT_{asset}_USD",
                    title=f"{asset}/USD last trade (Kraken)",
                    numeric_value=price,
                    unit="USD",
                    observation_date=now,
                    known_at=now,
                    reference_url="https://www.kraken.com/",
                    verification_status=VerificationStatus.CONFIRMED_FACT,
                    reliability_score=self.definition.reliability_score,
                    parser_version=self.definition.parser_version,
                    payload={"pair": requested},
                    subject_tags=tuple(tags) + _CRYPTO_TAGS,
                    categories=(MarketCategory.CRYPTO,),
                    subcategories=(MarketSubcategory.CRYPTO_PRICE,),
                )
            )

        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._record_health(
            ComponentHealth.HEALTHY if items else ComponentHealth.DEGRADED,
            f"{len(items)} spot quotes",
            items=len(items), latency_ms=latency,
        )
        return items


# ---------------------------------------------------------------------------
def _find_kraken_entry(result: dict, requested: str, asset: str) -> dict | None:
    """Kraken returns normalised pair names that differ from the request.

    XBTUSD comes back as XXBTZUSD. Match exactly first, then by a normalised
    suffix, rather than assuming a transformation rule.
    """
    if requested in result:
        entry = result[requested]
        return entry if isinstance(entry, dict) else None

    for key, entry in result.items():
        if not isinstance(entry, dict):
            continue
        normalised = key.replace("X", "").replace("Z", "")
        if normalised == requested.replace("X", "").replace("Z", ""):
            return entry
    return None


def _extract_closes(candles: object) -> list[float]:
    """Closes from Coinbase candles, newest first.

    Candle rows are [time, low, high, open, close, volume]; index 4 is close.
    """
    if not isinstance(candles, list):
        return []
    closes: list[float] = []
    for row in candles:
        if isinstance(row, list) and len(row) >= 5:
            value = _to_float(row[4])
            if value is not None and value > 0:
                closes.append(value)
    return closes


def _annualised_volatility(closes: list[float]) -> float | None:
    """Annualised standard deviation of daily log returns.

    Crypto trades continuously, so the annualisation factor is sqrt(365) rather
    than the sqrt(252) used for equities.
    """
    if len(closes) < 30:
        return None
    ordered = list(reversed(closes))  # oldest first
    returns = [
        math.log(ordered[i] / ordered[i - 1])
        for i in range(1, len(ordered))
        if ordered[i - 1] > 0 and ordered[i] > 0
    ]
    if len(returns) < 20:
        return None
    daily = statistics.stdev(returns)
    value = daily * math.sqrt(365)
    return value if math.isfinite(value) else None


def _to_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
