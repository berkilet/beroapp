"""U.S. Treasury connectors.

Two sources, both public domain and keyless:

* **Daily yield curve** — constant-maturity Treasury yields, published each
  business day as an OData/Atom XML feed. The short end of this curve is the
  market's own expectation of near-term policy, which makes it the most useful
  free input available for Fed-rate markets. Verified 2026-08-18.
* **Fiscal Data** — average interest rates on public debt, REST JSON.

XML is parsed with ``defusedxml``, which hardens the standard parser against
XXE, billion-laughs and quadratic-blowup attacks. The Treasury feed is a trusted
government source, but it arrives over the network and is therefore untrusted
input like any other; the feed is additionally size-capped before parsing.
"""

from __future__ import annotations

import re
import defusedxml.ElementTree as ET
from datetime import UTC, datetime

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

# OData namespaces used by the Treasury feed.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
}

# Feed field -> (series key, human label, tenor in months). Only the tenors that
# actually inform the markets we model; the feed carries several more.
_TENORS: tuple[tuple[str, str, str, float], ...] = (
    ("BC_1MONTH", "UST_YIELD_1M", "1-month Treasury yield", 1),
    ("BC_3MONTH", "UST_YIELD_3M", "3-month Treasury yield", 3),
    ("BC_6MONTH", "UST_YIELD_6M", "6-month Treasury yield", 6),
    ("BC_1YEAR", "UST_YIELD_1Y", "1-year Treasury yield", 12),
    ("BC_2YEAR", "UST_YIELD_2Y", "2-year Treasury yield", 24),
    ("BC_5YEAR", "UST_YIELD_5Y", "5-year Treasury yield", 60),
    ("BC_10YEAR", "UST_YIELD_10Y", "10-year Treasury yield", 120),
    ("BC_30YEAR", "UST_YIELD_30Y", "30-year Treasury yield", 360),
)

MAX_FEED_BYTES = 8 * 1024 * 1024


class TreasuryYieldCurveProvider(EvidenceProvider):
    """Daily constant-maturity Treasury yields."""

    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        now = now or datetime.now(UTC)
        started = datetime.now(UTC)

        try:
            body = await self.fetcher.fetch_text(
                self.definition.base_url,
                params={
                    "data": "daily_treasury_yield_curve",
                    "field_tdr_date_value": str(now.year),
                },
                headers={"User-Agent": self.settings.evidence_user_agent, "Accept": "application/xml"},
            )
        except FetchError as exc:
            self._record_health(ComponentHealth.FAILED, str(exc)[:200], error_code=exc.error_code)
            raise EvidenceError(
                f"treasury yield curve fetch failed: {exc}",
                source_key=self.source_key, error_code=exc.error_code,
            ) from exc

        if len(body) > MAX_FEED_BYTES:
            self._record_health(ComponentHealth.FAILED, "feed exceeded size cap", error_code="oversized")
            raise EvidenceError(
                f"treasury feed exceeded {MAX_FEED_BYTES} bytes",
                source_key=self.source_key, error_code="oversized",
            )

        try:
            root = ET.fromstring(body)
        except Exception as exc:  # defusedxml raises several parse/entity errors
            self._record_health(ComponentHealth.FAILED, f"XML parse failed: {exc}", error_code="parse_error")
            raise EvidenceError(
                f"treasury feed is not parseable XML: {exc}",
                source_key=self.source_key, error_code="parse_error",
            ) from exc

        items: list[EvidenceItem] = []
        latest_date: datetime | None = None

        for entry in root.findall("atom:entry", _NS):
            props = entry.find("atom:content/m:properties", _NS)
            if props is None:
                continue

            observation_date = _parse_odata_datetime(props.findtext("d:NEW_DATE", namespaces=_NS))
            if observation_date is None:
                continue
            if latest_date is None or observation_date > latest_date:
                latest_date = observation_date

            for field, series_key, label, tenor_months in _TENORS:
                raw = props.findtext(f"d:{field}", namespaces=_NS)
                value = _parse_float(raw)
                if value is None:
                    continue

                items.append(
                    EvidenceItem(
                        source_key=self.source_key,
                        source_type=SourceType.OFFICIAL_GOVERNMENT,
                        source_tier=1,
                        evidence_type=EvidenceType.TIME_SERIES_OBSERVATION,
                        series_key=series_key,
                        title=f"{label}, {observation_date:%Y-%m-%d}",
                        numeric_value=value,
                        unit="percent",
                        observation_date=observation_date,
                        # The curve for a given business day is published that
                        # evening. We do not know the exact release minute, so
                        # known_at is our observation time — never the
                        # observation date, which would be look-ahead.
                        published_at=None,
                        known_at=now,
                        reference_url="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve",
                        verification_status=VerificationStatus.CONFIRMED_FACT,
                        reliability_score=self.definition.reliability_score,
                        parser_version=self.definition.parser_version,
                        payload={"tenor_months": tenor_months, "field": field},
                        subject_tags=("treasury", "yield", "rates", "interest rate", "bond"),
                        categories=(MarketCategory.MACROECONOMICS, MarketCategory.FEDERAL_RESERVE),
                        subcategories=(
                            MarketSubcategory.TREASURY_YIELDS,
                            MarketSubcategory.FED_RATES,
                        ),
                    )
                )

        if not items:
            self._record_health(
                ComponentHealth.DEGRADED, "feed parsed but contained no usable yields"
            )
            return []

        # Keep only the most recent business day. History is already stored from
        # previous cycles, and re-emitting the whole year every time would churn
        # thousands of rows through the deduplicator for no benefit.
        items = [i for i in items if i.observation_date == latest_date]

        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._record_health(
            ComponentHealth.HEALTHY,
            f"{len(items)} yields for {latest_date:%Y-%m-%d}",
            items=len(items), latency_ms=latency,
        )
        return items


class TreasuryFiscalDataProvider(EvidenceProvider):
    """Average interest rates on public debt (Fiscal Data REST API)."""

    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        now = now or datetime.now(UTC)
        started = datetime.now(UTC)
        url = f"{self.definition.base_url}/v2/accounting/od/avg_interest_rates"

        try:
            payload = await self.fetcher.fetch_json(
                url,
                params={"page[size]": 20, "sort": "-record_date"},
                headers=self._headers(),
            )
        except FetchError as exc:
            self._record_health(ComponentHealth.FAILED, str(exc)[:200], error_code=exc.error_code)
            raise EvidenceError(
                f"treasury fiscal data fetch failed: {exc}",
                source_key=self.source_key, error_code=exc.error_code,
            ) from exc

        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            self._record_health(ComponentHealth.FAILED, "unexpected payload shape", error_code="schema")
            raise EvidenceError(
                "fiscal data returned an unexpected shape",
                source_key=self.source_key, error_code="schema",
            )

        items: list[EvidenceItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            observation_date = _parse_date(row.get("record_date"))
            value = _parse_float(row.get("avg_interest_rate_amt"))
            security = row.get("security_desc")
            if observation_date is None or value is None or not security:
                continue

            slug = re.sub(r"[^A-Z0-9]+", "_", str(security).upper()).strip("_")
            items.append(
                EvidenceItem(
                    source_key=self.source_key,
                    source_type=SourceType.OFFICIAL_GOVERNMENT,
                    source_tier=1,
                    evidence_type=EvidenceType.TIME_SERIES_OBSERVATION,
                    series_key=f"UST_AVG_RATE_{slug}",
                    title=f"Average interest rate, {security}, {observation_date:%Y-%m}",
                    numeric_value=value,
                    unit="percent",
                    observation_date=observation_date,
                    known_at=now,
                    reference_url="https://fiscaldata.treasury.gov/datasets/average-interest-rates-treasury-securities/",
                    verification_status=VerificationStatus.CONFIRMED_FACT,
                    reliability_score=self.definition.reliability_score,
                    parser_version=self.definition.parser_version,
                    payload={"security_type": row.get("security_type_desc")},
                    subject_tags=("treasury", "debt", "interest rate"),
                    categories=(MarketCategory.MACROECONOMICS,),
                    subcategories=(MarketSubcategory.TREASURY_YIELDS,),
                )
            )

        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._record_health(
            ComponentHealth.HEALTHY if items else ComponentHealth.DEGRADED,
            f"{len(items)} average-rate observations",
            items=len(items), latency_ms=latency,
        )
        return items


# ---------------------------------------------------------------------------
def _parse_float(raw: object) -> float | None:
    """Parse a number, returning None rather than a substituted default."""
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _parse_odata_datetime(raw: str | None) -> datetime | None:
    """OData emits ``2026-08-17T00:00:00`` with no zone; Treasury dates are ET
    business days, which we normalise to UTC midnight for comparison only."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_date(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).strip()).replace(tzinfo=UTC)
    except ValueError:
        return None
