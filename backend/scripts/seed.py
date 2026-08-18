"""Seed the source registry and register the baseline model version.

Idempotent: safe to run on every deploy.

The source list itself lives in `app.evidence.registry`, which is the single
declaration of every source the platform may use — implemented or not. This
script only pushes that declaration into the database. It used to carry its own
parallel list, which produced two rows for the same institution under slightly
different names: one enabled and collecting, one disabled and empty. A registry
row is still not a working connector; `enabled` reflects whether ingestion code
exists and its credentials are present, and the data-sources page shows DISABLED
for the rest.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import ModelVersion
from app.db.session import session_scope
from app.engines.probability import BASELINE_FEATURE_SET
from app.evidence.registry import sync_registry


def seed_sources(session) -> tuple[int, int]:
    """Reconcile the declared registry with its database rows."""
    result = sync_registry(session, get_settings())
    return result["created"], result["updated"]


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
