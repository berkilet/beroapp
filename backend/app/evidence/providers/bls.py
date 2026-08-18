"""U.S. Bureau of Labor Statistics connector.

The binding constraint in this platform. BLS documents 25 queries per day for
unregistered use (500 with a free key), so this connector is built around
spending as few queries as possible:

* every series is fetched in **one** POST, so a cycle costs 1 query, not N;
* the cycle runs 4x/day by default, using 4 of the 25;
* the daily budget is tracked in the database and the connector refuses to run
  when it is exhausted, rather than hammering an endpoint that will start
  rejecting us.

Series selection is deliberate and small. Pulling every BLS series would be both
wasteful and useless — the model needs the handful that actually inform the
markets we trade.

Verified 2026-08-18: keyless GET and POST both return REQUEST_SUCCEEDED.
"""

from __future__ import annotations

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

# BLS series id -> (our series key, label, unit, subject tags, subcategories).
# Keyless allows 25 series per query, so this list has room to grow.
SERIES: dict[str, tuple[str, str, str, tuple[str, ...], tuple[MarketSubcategory, ...]]] = {
    "CUUR0000SA0": (
        "CPI_URBAN_ALL",
        "CPI-U, all items (index 1982-84=100)",
        "index",
        ("cpi", "inflation", "consumer price"),
        (MarketSubcategory.INFLATION,),
    ),
    "CUUR0000SA0L1E": (
        "CPI_CORE",
        "CPI-U, all items less food and energy",
        "index",
        ("core cpi", "inflation", "consumer price"),
        (MarketSubcategory.INFLATION,),
    ),
    "LNS14000000": (
        "UNEMPLOYMENT_RATE",
        "Unemployment rate, seasonally adjusted",
        "percent",
        ("unemployment", "jobless", "labor"),
        (MarketSubcategory.EMPLOYMENT, MarketSubcategory.RECESSION),
    ),
    "CES0000000001": (
        "NONFARM_PAYROLLS",
        "Total nonfarm employment, seasonally adjusted",
        "thousands",
        ("payrolls", "jobs report", "employment", "nonfarm"),
        (MarketSubcategory.EMPLOYMENT,),
    ),
}

_MONTH_PERIODS = {f"M{i:02d}": i for i in range(1, 13)}


class BLSProvider(EvidenceProvider):
    """Batched BLS time-series ingestion."""

    @property
    def request_cost(self) -> int:
        # One POST covers every series. This is the whole point of the design.
        return 1

    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        now = now or datetime.now(UTC)
        started = datetime.now(UTC)

        body: dict = {
            "seriesid": list(SERIES),
            "startyear": str(now.year - 2),
            "endyear": str(now.year),
        }
        # A registered key raises the daily limit from 25 to 500 and is free.
        key = self.settings.bls_api_key.get_secret_value()
        if key:
            body["registrationkey"] = key

        try:
            payload = await self.fetcher.fetch_json(
                self.definition.base_url,
                method="POST",
                json_body=body,
                headers={**self._headers(), "Content-Type": "application/json"},
            )
        except FetchError as exc:
            self._record_health(ComponentHealth.FAILED, str(exc)[:200], error_code=exc.error_code)
            raise EvidenceError(
                f"BLS fetch failed: {exc}", source_key=self.source_key, error_code=exc.error_code
            ) from exc

        if not isinstance(payload, dict):
            raise EvidenceError(
                "BLS returned a non-object payload",
                source_key=self.source_key, error_code="schema",
            )

        status = payload.get("status")
        if status != "REQUEST_SUCCEEDED":
            # BLS reports quota exhaustion and bad requests in-band with a 200,
            # so the HTTP layer cannot catch it. Treat it as a real failure.
            message = "; ".join(str(m) for m in payload.get("message", []))[:300]
            self._record_health(
                ComponentHealth.FAILED, f"{status}: {message}", error_code="bls_request_failed"
            )
            raise EvidenceError(
                f"BLS request not successful ({status}): {message}",
                source_key=self.source_key, error_code="bls_request_failed",
            )

        series_list = payload.get("Results", {}).get("series")
        if not isinstance(series_list, list):
            raise EvidenceError(
                "BLS payload missing Results.series",
                source_key=self.source_key, error_code="schema",
            )

        items: list[EvidenceItem] = []
        for series in series_list:
            if not isinstance(series, dict):
                continue
            mapping = SERIES.get(str(series.get("seriesID")))
            if mapping is None:
                continue
            series_key, label, unit, tags, subcategories = mapping

            for observation in series.get("data", []) or []:
                item = self._to_item(
                    observation, series_key, label, unit, tags, subcategories, now
                )
                if item is not None:
                    items.append(item)

        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._record_health(
            ComponentHealth.HEALTHY if items else ComponentHealth.DEGRADED,
            f"{len(items)} observations across {len(series_list)} series",
            items=len(items), latency_ms=latency,
        )
        return items

    # ------------------------------------------------------------------
    def _to_item(
        self,
        observation: object,
        series_key: str,
        label: str,
        unit: str,
        tags: tuple[str, ...],
        subcategories: tuple[MarketSubcategory, ...],
        now: datetime,
    ) -> EvidenceItem | None:
        if not isinstance(observation, dict):
            return None

        period = str(observation.get("period", ""))
        month = _MONTH_PERIODS.get(period)
        if month is None:
            # M13 is an annual average and quarterly periods are not monthly
            # observations; skipping them is correct, not a parse failure.
            return None

        try:
            year = int(observation.get("year"))
            value = float(observation.get("value"))
        except (TypeError, ValueError):
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None

        observation_date = datetime(year, month, 1, tzinfo=UTC)

        # BLS publishes a month's figure partway through the following month.
        # We do not get the exact release timestamp in this payload, so
        # published_at stays None and known_at is our observation time. Assuming
        # the observation date as the publication date would let a backtest use
        # July CPI during July.
        return EvidenceItem(
            source_key=self.source_key,
            source_type=SourceType.OFFICIAL_GOVERNMENT,
            source_tier=1,
            evidence_type=EvidenceType.TIME_SERIES_OBSERVATION,
            series_key=series_key,
            title=f"{label}, {observation_date:%Y-%m}",
            numeric_value=value,
            unit=unit,
            observation_date=observation_date,
            known_at=now,
            reference_url="https://www.bls.gov/data/",
            verification_status=VerificationStatus.CONFIRMED_FACT,
            reliability_score=self.definition.reliability_score,
            parser_version=self.definition.parser_version,
            payload={
                "period": period,
                "period_name": observation.get("periodName"),
                "is_latest": observation.get("latest") == "true",
                "footnotes": [
                    f.get("text") for f in observation.get("footnotes", []) if isinstance(f, dict) and f.get("text")
                ],
            },
            subject_tags=tags,
            categories=(MarketCategory.MACROECONOMICS, MarketCategory.FEDERAL_RESERVE),
            subcategories=subcategories,
        )
