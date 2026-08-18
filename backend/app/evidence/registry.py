"""Evidence source registry.

Every external source the platform may contact is declared here, once. Nothing
elsewhere hard-codes a host, an endpoint, or a rate limit — connectors read
their configuration from their registry entry, and the HTTP client's SSRF
allow-list is derived from the same place. Adding a source is a registry edit,
and removing one genuinely removes the ability to reach it.

Each entry records what was verified and when. `ENABLED` means the connector
exists *and* its credentials (if any) are present; everything else reports
DISABLED on the data-sources page rather than appearing to work.

Rate limits below are quoted from official documentation, and the configured
poll intervals sit far inside them. The BLS keyless limit of 25 queries per day
is the binding constraint in this system and is the reason the BLS connector
batches every series it needs into a single request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    ComponentHealth,
    MarketCategory,
    MarketSubcategory,
    SourceType,
)
from app.db.models import ExternalSource


@dataclass(frozen=True)
class SourceDefinition:
    """A declared source. Frozen: the registry is configuration, not state."""

    source_key: str
    name: str
    source_type: SourceType
    tier: int
    base_url: str
    access_method: str
    reliability_score: float
    update_frequency_s: int
    categories: tuple[MarketCategory, ...]
    subcategories: tuple[MarketSubcategory, ...]
    parser_name: str
    parser_version: str
    documented_rate_limit: str
    terms_url: str
    usage_notes: str
    requires_api_key: bool = False
    api_key_setting: str | None = None
    daily_request_budget: int | None = None
    implemented: bool = False
    """Whether an ingestion connector actually exists. A definition without a
    connector is a documented intention, and the dashboard says so."""

    verified_on: str = ""
    """Date the endpoint was last confirmed to respond with the expected shape."""

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def serves(self, category: MarketCategory, subcategory: MarketSubcategory | None) -> bool:
        if subcategory is not None and subcategory in self.subcategories:
            return True
        return category in self.categories


# ---------------------------------------------------------------------------
# TIER 1 — primary / authoritative
# ---------------------------------------------------------------------------

_MACRO = (MarketCategory.MACROECONOMICS, MarketCategory.FEDERAL_RESERVE)

SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        source_key="treasury_yield_curve",
        name="U.S. Treasury daily yield curve",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        tier=1,
        base_url="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml",
        access_method="XML_FEED",
        reliability_score=0.98,
        update_frequency_s=6 * 3600,
        categories=_MACRO,
        subcategories=(
            MarketSubcategory.TREASURY_YIELDS,
            MarketSubcategory.FED_RATES,
            MarketSubcategory.RECESSION,
        ),
        parser_name="treasury_yield_curve",
        parser_version="v1.0.0",
        documented_rate_limit="none published; polled every 6h",
        terms_url="https://home.treasury.gov/utility/terms-of-use",
        usage_notes=(
            "Public domain. Constant-maturity yields, published each business day. "
            "The short end is the market's own read on near-term policy, which makes "
            "it the single most useful free input for Fed-rate markets."
        ),
        implemented=True,
        verified_on="2026-08-18",
    ),
    SourceDefinition(
        source_key="treasury_fiscal_data",
        name="U.S. Treasury Fiscal Data",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        tier=1,
        base_url="https://api.fiscaldata.treasury.gov/services/api/fiscal_service",
        access_method="REST_JSON",
        reliability_score=0.98,
        update_frequency_s=12 * 3600,
        categories=_MACRO,
        subcategories=(MarketSubcategory.TREASURY_YIELDS,),
        parser_name="treasury_fiscal_data",
        parser_version="v1.0.0",
        documented_rate_limit="none published; polled every 12h",
        terms_url="https://fiscaldata.treasury.gov/api-documentation/",
        usage_notes="Public domain, no key required. Average interest rates on public debt.",
        implemented=True,
        verified_on="2026-08-18",
    ),
    SourceDefinition(
        source_key="bls",
        name="U.S. Bureau of Labor Statistics",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        tier=1,
        base_url="https://api.bls.gov/publicAPI/v2/timeseries/data/",
        access_method="REST_JSON",
        reliability_score=0.98,
        update_frequency_s=6 * 3600,
        categories=_MACRO,
        subcategories=(
            MarketSubcategory.INFLATION,
            MarketSubcategory.EMPLOYMENT,
            MarketSubcategory.FED_RATES,
            MarketSubcategory.RECESSION,
        ),
        parser_name="bls",
        parser_version="v1.0.0",
        documented_rate_limit=(
            "unregistered: 25 queries/day, 25 series/query, 10 years/query; "
            "50 requests/10s shared"
        ),
        terms_url="https://www.bls.gov/developers/api_faqs.htm",
        usage_notes=(
            "CPI, unemployment and payrolls. The 25-queries-per-day keyless limit is "
            "the binding constraint in this platform: the connector batches every "
            "series into one POST and runs 4x/day, so it uses 4 of 25. Registering a "
            "free key raises the limit to 500/day and is the single cheapest upgrade."
        ),
        daily_request_budget=25,
        implemented=True,
        verified_on="2026-08-18",
    ),
    SourceDefinition(
        source_key="fomc_calendar",
        name="Federal Reserve FOMC calendar",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        tier=1,
        base_url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        access_method="HTML_STRUCTURED",
        reliability_score=0.99,
        update_frequency_s=24 * 3600,
        categories=(MarketCategory.FEDERAL_RESERVE, MarketCategory.MACROECONOMICS),
        subcategories=(MarketSubcategory.FED_RATES, MarketSubcategory.FED_PERSONNEL),
        parser_name="fomc_calendar",
        parser_version="v1.0.0",
        documented_rate_limit="none published; polled daily",
        terms_url="https://www.federalreserve.gov/aboutthefed/legal-disclaimer.htm",
        usage_notes=(
            "Public domain. Meeting dates only — the parser extracts scheduled dates "
            "and does not attempt to interpret statement text. Polled once a day "
            "because a meeting calendar does not change more often than that."
        ),
        implemented=True,
        verified_on="2026-08-18",
    ),
    SourceDefinition(
        source_key="sec_edgar",
        name="SEC EDGAR",
        source_type=SourceType.OFFICIAL_COMPANY,
        tier=1,
        base_url="https://data.sec.gov",
        access_method="REST_JSON",
        reliability_score=0.97,
        update_frequency_s=6 * 3600,
        categories=(MarketCategory.BUSINESS,),
        subcategories=(
            MarketSubcategory.CORPORATE_EARNINGS,
            MarketSubcategory.CORPORATE_EVENT,
        ),
        parser_name="sec_edgar",
        parser_version="v1.0.0",
        documented_rate_limit="10 requests/second, declared User-Agent required",
        terms_url="https://www.sec.gov/os/webmaster-faq#developers",
        usage_notes=(
            "Filing metadata only. SEC policy requires a User-Agent identifying the "
            "requester; the connector refuses to run unless SEC_USER_AGENT is set, "
            "rather than sending an anonymous request in violation of that policy. "
            "The connector ingests structured filing metadata and does not attempt "
            "to interpret filing text."
        ),
        implemented=True,
        verified_on="2026-08-18",
    ),
    SourceDefinition(
        source_key="coinbase_exchange",
        name="Coinbase Exchange market data",
        source_type=SourceType.MARKET_DATA,
        tier=1,
        base_url="https://api.exchange.coinbase.com",
        access_method="REST_JSON",
        reliability_score=0.93,
        update_frequency_s=300,
        categories=(MarketCategory.CRYPTO,),
        subcategories=(MarketSubcategory.CRYPTO_PRICE,),
        parser_name="coinbase_exchange",
        parser_version="v1.0.0",
        documented_rate_limit="public endpoints ~10 req/s per IP",
        terms_url="https://docs.cdp.coinbase.com/exchange/docs/welcome",
        usage_notes=(
            "Spot quotes and daily candles for crypto threshold markets. A reputable "
            "public exchange, but an exchange price is not an official statistic: it "
            "is Tier 1 for a market that resolves on price and evidence-only for "
            "anything else."
        ),
        implemented=True,
        verified_on="2026-08-18",
    ),
    SourceDefinition(
        source_key="kraken",
        name="Kraken market data",
        source_type=SourceType.MARKET_DATA,
        tier=1,
        base_url="https://api.kraken.com",
        access_method="REST_JSON",
        reliability_score=0.90,
        update_frequency_s=300,
        categories=(MarketCategory.CRYPTO,),
        subcategories=(MarketSubcategory.CRYPTO_PRICE,),
        parser_name="kraken",
        parser_version="v1.0.0",
        documented_rate_limit="public endpoints ~1 req/s sustained",
        terms_url="https://docs.kraken.com/api/",
        usage_notes=(
            "Independent cross-check on Coinbase. Two venues disagreeing materially "
            "about a spot price is a data-quality signal worth recording, which is "
            "why a second exchange is worth the request budget."
        ),
        implemented=True,
        verified_on="2026-08-18",
    ),
    # -- declared, connector not implemented ------------------------------
    SourceDefinition(
        source_key="fred",
        name="FRED (Federal Reserve Bank of St. Louis)",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        tier=1,
        base_url="https://api.stlouisfed.org/fred",
        access_method="REST_JSON",
        reliability_score=0.98,
        update_frequency_s=6 * 3600,
        categories=_MACRO,
        subcategories=(
            MarketSubcategory.INFLATION,
            MarketSubcategory.EMPLOYMENT,
            MarketSubcategory.FED_RATES,
            MarketSubcategory.GDP_GROWTH,
            MarketSubcategory.RECESSION,
        ),
        parser_name="fred",
        parser_version="v0",
        documented_rate_limit="120 requests/minute with a key",
        terms_url="https://fred.stlouisfed.org/docs/api/terms_of_use.html",
        usage_notes=(
            "Requires a free API key; verified 2026-08-18 to return HTTP 400 without "
            "one. Not implemented because the same underlying series are available "
            "keyless from BLS and Treasury. Worth adding if a key is configured — "
            "FRED's coverage is far broader."
        ),
        requires_api_key=True,
        api_key_setting="fred_api_key",
        implemented=False,
        verified_on="2026-08-18",
    ),
    SourceDefinition(
        source_key="bea",
        name="U.S. Bureau of Economic Analysis",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        tier=1,
        base_url="https://apps.bea.gov/api",
        access_method="REST_JSON",
        reliability_score=0.97,
        update_frequency_s=24 * 3600,
        categories=_MACRO,
        subcategories=(MarketSubcategory.GDP_GROWTH,),
        parser_name="bea",
        parser_version="v0",
        documented_rate_limit="100 requests/minute with a key",
        terms_url="https://apps.bea.gov/API/signup/",
        usage_notes="Requires a free API key. Not implemented; GDP markets are rare on the venue.",
        requires_api_key=True,
        api_key_setting="bea_api_key",
        implemented=False,
        verified_on="2026-08-18",
    ),
    SourceDefinition(
        source_key="fec",
        name="Federal Election Commission",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        tier=1,
        base_url="https://api.open.fec.gov/v1",
        access_method="REST_JSON",
        reliability_score=0.95,
        update_frequency_s=12 * 3600,
        categories=(MarketCategory.ELECTIONS, MarketCategory.POLITICS),
        subcategories=(
            MarketSubcategory.US_PRESIDENTIAL,
            MarketSubcategory.US_CONGRESSIONAL,
            MarketSubcategory.US_PRIMARY,
        ),
        parser_name="fec",
        parser_version="v1.0.0",
        documented_rate_limit="DEMO_KEY: 30 req/hour, 50/day. Registered: 1000 req/hour",
        terms_url="https://api.open.fec.gov/developers/",
        usage_notes=(
            "Candidate and committee registry data — who is actually running, and "
            "filings. Verified reachable with DEMO_KEY, but that key's 30/hour limit "
            "is too tight for production, and FEC data describes campaign finance "
            "rather than election outcomes. Implemented as a registry lookup only; "
            "it must not be mistaken for a polling source."
        ),
        requires_api_key=True,
        api_key_setting="fec_api_key",
        implemented=True,
        verified_on="2026-08-18",
    ),
)

BY_KEY: dict[str, SourceDefinition] = {s.source_key: s for s in SOURCES}


def allowed_evidence_hosts() -> frozenset[str]:
    """SSRF allow-list contribution from the registry.

    Only sources with an implemented connector are included. A declared-but-
    unimplemented source cannot be reached even by accident, which is the point
    of deriving the allow-list from the registry rather than maintaining a
    parallel list that can drift.
    """
    return frozenset(s.host for s in SOURCES if s.implemented and s.host)


def definitions_for(
    category: MarketCategory, subcategory: MarketSubcategory | None
) -> list[SourceDefinition]:
    """Sources relevant to a market, best-tier first.

    This is what stops the platform querying every feed for every market: a Fed
    market asks Treasury, BLS and the FOMC calendar, and nothing else.
    """
    matches = [s for s in SOURCES if s.implemented and s.serves(category, subcategory)]
    return sorted(matches, key=lambda s: (s.tier, -s.reliability_score))


def is_enabled(definition: SourceDefinition, settings) -> tuple[bool, str]:
    """Whether a source can actually run right now, and why not if it cannot."""
    if not definition.implemented:
        return False, "no connector implemented"
    if definition.requires_api_key:
        if not definition.api_key_setting:
            return False, "requires a key but none is configured"
        secret = getattr(settings, definition.api_key_setting, None)
        value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
        if not value:
            return False, f"{definition.api_key_setting.upper()} is not set"
    if definition.source_key == "sec_edgar" and not settings.sec_user_agent:
        # SEC policy requires a declared, contactable User-Agent. Sending an
        # anonymous request would violate it, so the connector stays off.
        return False, "SEC_USER_AGENT is not set; SEC policy requires a declared User-Agent"
    return True, "enabled"


# ---------------------------------------------------------------------------
# Database synchronisation and health
# ---------------------------------------------------------------------------


def sync_registry(session: Session, settings) -> dict:
    """Reconcile declared sources with their database rows.

    Idempotent. Operational counters (success/error counts, last-success time)
    are preserved; everything declarative is overwritten from the definition,
    because the code is the source of truth for configuration.
    """
    created = updated = 0

    for definition in SOURCES:
        enabled, reason = is_enabled(definition, settings)

        row = session.execute(
            select(ExternalSource).where(ExternalSource.source_key == definition.source_key)
        ).scalar_one_or_none()
        if row is None:
            # Fall back to name for rows seeded before source_key existed.
            row = session.execute(
                select(ExternalSource).where(ExternalSource.name == definition.name)
            ).scalar_one_or_none()

        values = {
            "source_key": definition.source_key,
            "name": definition.name,
            "source_type": definition.source_type.value,
            "source_tier": definition.tier,
            "base_url": definition.base_url,
            "access_method": definition.access_method,
            "reliability_score": definition.reliability_score,
            "update_frequency_s": definition.update_frequency_s,
            "daily_request_budget": definition.daily_request_budget,
            "parser_name": definition.parser_name,
            "parser_version": definition.parser_version,
            "terms_url": definition.terms_url,
            "requires_api_key": definition.requires_api_key,
            "enabled": enabled,
            "categories": {
                "categories": [c.value for c in definition.categories],
                "subcategories": [s.value for s in definition.subcategories],
            },
            "usage_notes": (
                definition.usage_notes if enabled else f"{definition.usage_notes} [{reason}]"
            ),
        }

        if row is None:
            session.add(
                ExternalSource(
                    **values,
                    health=(ComponentHealth.UNKNOWN if enabled else ComponentHealth.DISABLED).value,
                )
            )
            created += 1
        else:
            for key, value in values.items():
                setattr(row, key, value)
            if not enabled:
                row.health = ComponentHealth.DISABLED.value
            elif row.health == ComponentHealth.DISABLED.value:
                row.health = ComponentHealth.UNKNOWN.value
            updated += 1

    return {"created": created, "updated": updated, "declared": len(SOURCES)}


def get_source_row(session: Session, source_key: str) -> ExternalSource | None:
    return session.execute(
        select(ExternalSource).where(ExternalSource.source_key == source_key)
    ).scalar_one_or_none()


def consume_budget(session: Session, source_key: str, requests: int = 1) -> bool:
    """Reserve `requests` against a source's documented daily cap.

    Returns False when the budget is exhausted. BLS keyless allows 25 queries a
    day; blowing through that would get the platform blocked, and a connector
    that silently kept trying would be worse than one that stops.
    """
    row = get_source_row(session, source_key)
    if row is None:
        return False
    if row.daily_request_budget is None:
        return True

    now = datetime.now(UTC)
    if row.budget_reset_at is None or row.budget_reset_at <= now:
        row.requests_today = 0
        row.budget_reset_at = now + timedelta(days=1)

    if row.requests_today + requests > row.daily_request_budget:
        return False

    row.requests_today += requests
    return True


def record_source_result(
    session: Session,
    source_key: str,
    *,
    success: bool,
    latency_ms: int | None = None,
    error_code: str | None = None,
    health: ComponentHealth | None = None,
) -> None:
    """Update per-source health. Called on every attempt, success or failure."""
    row = get_source_row(session, source_key)
    if row is None:
        return

    now = datetime.now(UTC)
    if latency_ms is not None:
        row.last_latency_ms = latency_ms

    if success:
        row.success_count += 1
        row.last_success_at = now
        row.health = (health or ComponentHealth.HEALTHY).value
    else:
        row.error_count += 1
        row.last_error_at = now
        row.last_error_code = error_code
        row.health = (health or ComponentHealth.FAILED).value


def evaluate_source_health(row: ExternalSource, *, now: datetime | None = None) -> ComponentHealth:
    """Derive health from observed behaviour rather than from a stored flag.

    A source that has not reported within several of its own update periods is
    STALE even if its last call succeeded, because a feed that stopped is not a
    healthy feed.
    """
    now = now or datetime.now(UTC)

    if not row.enabled:
        return ComponentHealth.DISABLED
    if row.last_success_at is None:
        return ComponentHealth.UNKNOWN

    last_success = row.last_success_at
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=UTC)

    age = (now - last_success).total_seconds()
    period = row.update_frequency_s or 3600
    if age > period * 3:
        return ComponentHealth.STALE

    total = row.success_count + row.error_count
    if total >= 10 and row.error_count / total > 0.25:
        return ComponentHealth.DEGRADED

    return ComponentHealth.HEALTHY
