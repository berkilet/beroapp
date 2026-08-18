"""SEC EDGAR connector.

Structured filing metadata for company-event markets. Filing *metadata* only —
form type, filing date, accession number, period. The connector deliberately
does not download or interpret filing documents: automatically reading a 10-K
and deciding what it implies is exactly the kind of unearned interpretation this
platform refuses to do.

**Policy compliance.** SEC requires every automated requester to send a
User-Agent identifying them with contact details, and documents a 10 req/s
ceiling. This connector refuses to run unless ``SEC_USER_AGENT`` is configured,
rather than sending an anonymous request in breach of that policy, and the HTTP
layer rate-limits data.sec.gov to 2 req/s — a fifth of the documented allowance.

Verified 2026-08-18: ``/submissions/CIK##########.json`` and the XBRL company
concept endpoint both respond.
"""

from __future__ import annotations

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

# Companies Polymarket actually runs event markets on. Keyed by ticker so the
# matcher can find them from a market question.
TRACKED_COMPANIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("AAPL", "0000320193", "Apple Inc.", ("apple", "aapl")),
    ("MSFT", "0000789019", "Microsoft Corporation", ("microsoft", "msft")),
    ("NVDA", "0001045810", "NVIDIA Corporation", ("nvidia", "nvda")),
    ("TSLA", "0001318605", "Tesla, Inc.", ("tesla", "tsla")),
    ("AMZN", "0001018724", "Amazon.com, Inc.", ("amazon", "amzn")),
    ("GOOGL", "0001652044", "Alphabet Inc.", ("alphabet", "google", "googl")),
    ("META", "0001326801", "Meta Platforms, Inc.", ("meta", "facebook")),
    ("COIN", "0001679788", "Coinbase Global, Inc.", ("coinbase", "coin")),
    ("MSTR", "0001050446", "MicroStrategy Incorporated", ("microstrategy", "mstr", "strategy")),
)

# Forms that signal an event a prediction market might be written about.
MATERIAL_FORMS = {"8-K", "10-Q", "10-K", "S-1", "S-4", "DEF 14A", "SC 13D", "425"}

RECENT_FILING_DAYS = 120


class SECEdgarProvider(EvidenceProvider):
    """Recent material filing metadata for tracked companies."""

    @property
    def request_cost(self) -> int:
        return len(TRACKED_COMPANIES)

    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        now = now or datetime.now(UTC)
        started = datetime.now(UTC)

        if not self.settings.sec_user_agent:
            # Refusing is the correct behaviour: SEC policy requires a declared
            # identity, and an anonymous request would breach it.
            self._record_health(
                ComponentHealth.DISABLED,
                "SEC_USER_AGENT is not set; SEC policy requires a declared User-Agent",
                error_code="missing_user_agent",
            )
            raise EvidenceError(
                "SEC connector refuses to run without SEC_USER_AGENT",
                source_key=self.source_key, error_code="missing_user_agent",
            )

        items: list[EvidenceItem] = []
        failures: list[str] = []
        cutoff = now - timedelta(days=RECENT_FILING_DAYS)

        for ticker, cik, name, tags in TRACKED_COMPANIES:
            try:
                items.extend(await self._collect_company(ticker, cik, name, tags, cutoff, now))
            except FetchError as exc:
                failures.append(f"{ticker}:{exc.error_code}")

        if failures and not items:
            self._record_health(
                ComponentHealth.FAILED, f"all companies failed: {', '.join(failures)}",
                error_code="all_failed",
            )
            raise EvidenceError(
                "SEC returned no usable filings for any tracked company",
                source_key=self.source_key, error_code="all_failed",
            )

        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._record_health(
            ComponentHealth.DEGRADED if failures else ComponentHealth.HEALTHY,
            f"{len(items)} filings" + (f"; failed: {', '.join(failures)}" if failures else ""),
            items=len(items), latency_ms=latency,
        )
        return items

    async def _collect_company(
        self,
        ticker: str,
        cik: str,
        name: str,
        tags: tuple[str, ...],
        cutoff: datetime,
        now: datetime,
    ) -> list[EvidenceItem]:
        payload = await self.fetcher.fetch_json(
            f"{self.definition.base_url}/submissions/CIK{cik}.json",
            headers={
                "User-Agent": self.settings.sec_user_agent,
                "Accept": "application/json",
            },
        )
        if not isinstance(payload, dict):
            return []

        recent = payload.get("filings", {}).get("recent")
        if not isinstance(recent, dict):
            return []

        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accessions = recent.get("accessionNumber") or []
        periods = recent.get("reportDate") or []
        docs = recent.get("primaryDocument") or []

        items: list[EvidenceItem] = []
        for index, form in enumerate(forms):
            if form not in MATERIAL_FORMS:
                continue
            filed = _parse_date(dates[index] if index < len(dates) else None)
            if filed is None or filed < cutoff:
                continue

            accession = accessions[index] if index < len(accessions) else ""
            period = _parse_date(periods[index] if index < len(periods) else None)
            document = docs[index] if index < len(docs) else ""
            plain_accession = str(accession).replace("-", "")

            items.append(
                EvidenceItem(
                    source_key=self.source_key,
                    source_type=SourceType.OFFICIAL_COMPANY,
                    source_tier=1,
                    evidence_type=EvidenceType.FILING,
                    series_key=f"SEC_FILING_{ticker}_{form.replace(' ', '_')}",
                    title=f"{name} filed {form} on {filed:%Y-%m-%d}",
                    numeric_value=None,
                    unit=None,
                    observation_date=period or filed,
                    # Filing date is a real publication date, unlike the
                    # inferred timestamps elsewhere, so it is recorded as one.
                    published_at=filed,
                    known_at=max(now, filed),
                    reference_url=(
                        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                        f"{plain_accession}/{document}"
                        if plain_accession and document
                        else "https://www.sec.gov/cgi-bin/browse-edgar"
                    ),
                    verification_status=VerificationStatus.CONFIRMED_FACT,
                    reliability_score=self.definition.reliability_score,
                    parser_version=self.definition.parser_version,
                    payload={
                        "ticker": ticker,
                        "cik": cik,
                        "form": form,
                        "accession_number": accession,
                        "filing_date": filed.isoformat(),
                        "period_of_report": period.isoformat() if period else None,
                    },
                    subject_tags=tuple(tags) + (ticker.lower(), "sec", "filing", "earnings"),
                    categories=(MarketCategory.BUSINESS,),
                    subcategories=(
                        MarketSubcategory.CORPORATE_EARNINGS
                        if form in {"10-Q", "10-K"}
                        else MarketSubcategory.CORPORATE_EVENT,
                    ),
                )
            )

        return items


def _parse_date(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).strip()).replace(tzinfo=UTC)
    except ValueError:
        return None
