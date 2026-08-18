"""Seed the source registry and register the baseline model version.

Idempotent: safe to run on every deploy.

Registers a source row for each evidence source the platform *may* use. A row
here is a registry entry, not a working connector — `enabled` reflects whether
ingestion code exists and its credentials are present, and the data-sources page
shows DISABLED for the rest. This is deliberate: the registry documents intent,
and the flag documents reality.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import get_settings
from app.core.enums import ComponentHealth, SourceType
from app.db.models import ExternalSource, ModelVersion
from app.db.session import session_scope
from app.engines.probability import BASELINE_FEATURE_SET

SOURCES = [
    # -- Tier 1: primary / authoritative -----------------------------------
    dict(
        name="U.S. Treasury Fiscal Data",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        source_tier=1,
        base_url="https://api.fiscaldata.treasury.gov/services/api/fiscal_service",
        reliability_score=0.95,
        requires_api_key=False,
        usage_notes="Public domain, no key required. Connector not yet implemented.",
    ),
    dict(
        name="FRED (Federal Reserve Bank of St. Louis)",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        source_tier=1,
        base_url="https://api.stlouisfed.org/fred",
        reliability_score=0.95,
        requires_api_key=True,
        usage_notes="Free key required. Series-level terms vary by originating agency.",
    ),
    dict(
        name="SEC EDGAR",
        source_type=SourceType.OFFICIAL_COMPANY,
        source_tier=1,
        base_url="https://data.sec.gov",
        reliability_score=0.95,
        requires_api_key=False,
        usage_notes="SEC requires a descriptive User-Agent with contact details; max 10 req/s.",
    ),
    dict(
        name="Bureau of Labor Statistics",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        source_tier=1,
        base_url="https://api.bls.gov/publicAPI/v2",
        reliability_score=0.95,
        requires_api_key=True,
        usage_notes="Key optional; raises the daily quota.",
    ),
    dict(
        name="Bureau of Economic Analysis",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        source_tier=1,
        base_url="https://apps.bea.gov/api",
        reliability_score=0.95,
        requires_api_key=True,
        usage_notes="Free key required.",
    ),
    dict(
        name="Federal Reserve / FOMC calendar",
        source_type=SourceType.OFFICIAL_GOVERNMENT,
        source_tier=1,
        base_url="https://www.federalreserve.gov",
        reliability_score=0.98,
        requires_api_key=False,
        usage_notes="Public domain. Meeting dates and statements.",
    ),
    # -- Market data (implemented) -----------------------------------------
    dict(
        name="Polymarket Gamma API",
        source_type=SourceType.MARKET_DATA,
        source_tier=1,
        base_url="https://gamma-api.polymarket.com",
        reliability_score=1.0,
        requires_api_key=False,
        enabled=True,
        usage_notes="Market and event discovery. Documented limit 300 req/10s on /markets.",
    ),
    dict(
        name="Polymarket CLOB API",
        source_type=SourceType.MARKET_DATA,
        source_tier=1,
        base_url="https://clob.polymarket.com",
        reliability_score=1.0,
        requires_api_key=False,
        enabled=True,
        usage_notes="Order books and executable prices. Batch POST /books, 500 req/10s.",
    ),
    dict(
        name="Polymarket Data API",
        source_type=SourceType.MARKET_DATA,
        source_tier=1,
        base_url="https://data-api.polymarket.com",
        reliability_score=1.0,
        requires_api_key=False,
        enabled=True,
        usage_notes="Open interest and public trade prints.",
    ),
    # -- Tier 2-4: registered, deliberately not implemented -----------------
    dict(
        name="Reuters",
        source_type=SourceType.NEWS,
        source_tier=2,
        base_url="https://www.reuters.com",
        reliability_score=0.80,
        requires_api_key=True,
        usage_notes=(
            "Not implemented. Automated redistribution requires a paid licence; "
            "excluded under the zero-cost constraint. Items would be classified "
            "REPORTED_INFORMATION, never CONFIRMED_FACT."
        ),
    ),
    dict(
        name="Associated Press",
        source_type=SourceType.NEWS,
        source_tier=2,
        base_url="https://apnews.com",
        reliability_score=0.80,
        requires_api_key=True,
        usage_notes="Not implemented. Licensing required for automated use.",
    ),
    dict(
        name="Established polling aggregators",
        source_type=SourceType.POLLING,
        source_tier=3,
        base_url=None,
        reliability_score=0.60,
        requires_api_key=False,
        usage_notes="Not implemented. Terms vary by publisher and must be checked individually.",
    ),
    dict(
        name="Social media",
        source_type=SourceType.SOCIAL_MEDIA,
        source_tier=4,
        base_url=None,
        reliability_score=0.20,
        requires_api_key=True,
        usage_notes=(
            "Not implemented and not planned for Phase 1. If added, every item would be "
            "stored UNVERIFIED and could never raise a claim to CONFIRMED_FACT."
        ),
    ),
]


def seed_sources(session) -> tuple[int, int]:
    settings = get_settings()
    created = updated = 0

    for spec in SOURCES:
        spec = dict(spec)
        name = spec.pop("name")
        enabled = spec.pop("enabled", False)

        # A source requiring a key we do not hold is reported DISABLED rather
        # than being quietly listed as available.
        if spec.get("requires_api_key") and enabled:
            if name.startswith("FRED") and not settings.fred_api_key.get_secret_value():
                enabled = False

        row = session.execute(
            select(ExternalSource).where(ExternalSource.name == name)
        ).scalar_one_or_none()

        values = {
            **spec,
            "source_type": spec["source_type"].value,
            "enabled": enabled,
            "health": (ComponentHealth.UNKNOWN if enabled else ComponentHealth.DISABLED).value,
        }

        if row is None:
            session.add(ExternalSource(name=name, **values))
            created += 1
        else:
            for key, value in values.items():
                # Preserve accumulated operational counters.
                if key not in ("health",) or row.health == ComponentHealth.DISABLED.value:
                    setattr(row, key, value)
            updated += 1

    return created, updated


def register_baseline_model(session) -> bool:
    """Register the baseline so the model registry is never empty.

    Its performance_summary is explicitly null until measured — the spec forbids
    claiming performance that has not been observed.
    """
    settings = get_settings()
    existing = session.execute(
        select(ModelVersion).where(
            ModelVersion.model_id == "baseline",
            ModelVersion.version == settings.baseline_model_version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False

    session.add(
        ModelVersion(
            model_id="baseline",
            version=settings.baseline_model_version,
            category=None,
            algorithm="log-odds adjustment against the market prior",
            feature_set=BASELINE_FEATURE_SET,
            hyperparameters={
                "negrisk_max_adjustment": 0.60,
                "imbalance_max_adjustment": 0.15,
                "longshot_max_adjustment": 0.25,
                "max_trusted_negrisk_error": 0.20,
                "negrisk_min_group_coverage": 0.98,
            },
            training_period_start=None,
            training_period_end=None,
            performance_summary=None,
            is_active=True,
            created_at=datetime.now(UTC),
        )
    )
    return True


def main() -> int:
    with session_scope() as session:
        created, updated = seed_sources(session)
        registered = register_baseline_model(session)

    print(f"sources: {created} created, {updated} updated")
    print(f"baseline model: {'registered' if registered else 'already present'}")
    print(
        "\nA registry row is not a working connector. The data-sources page shows "
        "DISABLED for every source whose ingestion code or credentials are absent."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
